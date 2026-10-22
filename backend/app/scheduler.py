from __future__ import annotations

"""基于可注入 SQLAlchemy repository 的分钟/L2/L3 调度器。

Scheduler 不直接依赖 Workbench 或应用 ORM。主 agent 需要提供 SchedulerRepository，所有方法
接收同一个 SQLAlchemy Session；任务创建通过 queue.TaskQueue 完成。短事务内固化输入快照，
AI 网络调用由 Worker 在事务外执行。
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.queue import JobRecord, TaskQueue, build_idempotency_key, utc_now_ms

UTC = timezone.utc
MINUTE_MS = 60_000
FIVE_MINUTES_MS = 5 * MINUTE_MS


class SchedulerError(RuntimeError):
    pass


@dataclass(frozen=True)
class CameraSchedule:
    id: str
    timezone: str = "Asia/Shanghai"
    enabled: bool = True
    l1_enabled: bool = True
    strict_mode: bool = False
    prompt_versions: Mapping[str, str] = field(default_factory=dict)
    schema_versions: Mapping[str, str] = field(default_factory=dict)
    input_version: str | int = 1


@dataclass(frozen=True)
class MinuteSnapshot:
    """Repository 从 Frame/ChangeEvent/Coverage 表得到的分钟不可变输入。"""

    frame_ids: tuple[str, ...] = ()
    signals: Mapping[str, Any] = field(default_factory=dict)
    gaps: tuple[Mapping[str, Any], ...] = ()
    coverage_ratio: float = 0.0
    input_version: str | int = 1
    stable: bool = False
    no_ai_reason: str | None = None
    pin_until_ms: int | None = None


@dataclass(frozen=True)
class MinuteObservationRecord:
    id: str
    camera_id: str
    start_at_ms: int
    end_at_ms: int
    source: str
    state: str
    evidence_frame_ids: tuple[str, ...]
    gaps: tuple[Mapping[str, Any], ...] = ()
    coverage_ratio: float = 0.0
    ai_job_id: str | None = None
    version: str | int = 1
    observation: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class L2Snapshot:
    observation_ids: tuple[str, ...]
    evidence_frame_ids: tuple[str, ...]
    context: Mapping[str, Any] = field(default_factory=dict)
    input_version: str | int = 1
    complete: bool = True


@dataclass(frozen=True)
class L3Snapshot:
    segment_ids: tuple[str, ...]
    segment_revisions: tuple[tuple[str, int], ...]
    context: Mapping[str, Any] = field(default_factory=dict)
    input_version: str | int = 1
    complete: bool = True


class SchedulerRepository(Protocol):
    """SQLAlchemy repository 接口；需要实现者将文档字段映射到实际 ORM model。"""

    def snapshot_minute(
        self, session: Session, camera_id: str, start_ms: int, end_ms: int, *, strict_mode: bool
    ) -> MinuteSnapshot: ...

    def save_local_observation(self, session: Session, observation: MinuteObservationRecord) -> None: ...

    def pin_frames(self, session: Session, frame_ids: Sequence[str], until_ms: int, reason: str) -> None: ...

    def snapshot_l2(
        self, session: Session, camera_id: str, start_ms: int, end_ms: int, *, context_before_ms: int, context_after_ms: int
    ) -> L2Snapshot: ...

    def snapshot_l3(
        self, session: Session, camera_ids: Sequence[str], start_ms: int, end_ms: int
    ) -> L3Snapshot: ...

    def mark_missing_l2_windows(self, session: Session, camera_id: str, start_ms: int, end_ms: int) -> None: ...


class SessionRunner:
    """让纯 SQLAlchemy SessionFactory 也能作为清晰的 transaction boundary 使用。"""

    def __init__(self, session_factory: Callable[[], Session] | None = None):
        self.session_factory = session_factory

    def run(self, operation: Callable[[Session | None], Any], session: Session | None = None) -> Any:
        if session is not None:
            return operation(session)
        if self.session_factory is None:
            return operation(None)
        db = self.session_factory()
        try:
            result = operation(db)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def _dt_ms(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("必须传入带时区的 datetime")
    return int(value.timestamp() * 1000)


def _window_ms(start_ms: int, end_ms: int) -> tuple[int, int]:
    start_ms, end_ms = int(start_ms), int(end_ms)
    if end_ms <= start_ms:
        raise ValueError("窗口必须满足 start < end")
    return start_ms, end_ms


def floor_minute_ms(value_ms: int) -> int:
    return (int(value_ms) // MINUTE_MS) * MINUTE_MS


def floor_local_window_ms(value_ms: int, minutes: int, timezone_name: str) -> int:
    """按 IANA 本地自然时间窗口向下取整，正确处理 DST 的重复/缺失小时。"""
    if minutes <= 0 or 60 % minutes:
        raise ValueError("minutes 必须是 60 的正约数")
    zone = ZoneInfo(timezone_name)
    local = datetime.fromtimestamp(int(value_ms) / 1000, UTC).astimezone(zone)
    local_floor = local.replace(minute=(local.minute // minutes) * minutes, second=0, microsecond=0)
    return _dt_ms(local_floor)


def local_period_bounds_ms(value_ms: int, period: str, timezone_name: str) -> tuple[int, int]:
    """返回当前本地 hour/day 的 UTC epoch 毫秒边界，包含 DST 23/25 小时日。"""
    zone = ZoneInfo(timezone_name)
    local = datetime.fromtimestamp(int(value_ms) / 1000, UTC).astimezone(zone)
    if period == "hour":
        start_local = local.replace(minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(hours=1)
    elif period == "day":
        start_local = datetime.combine(local.date(), time.min, tzinfo=zone)
        end_local = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=zone)
    else:
        raise ValueError("period 仅支持 hour/day")
    return _dt_ms(start_local), _dt_ms(end_local)


def _canonical_fingerprint(data: Any) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class WindowScheduler:
    """窗口调度服务。

    每个 schedule_* 方法既可由 API/定时循环传入 Session，也可用 session_factory 自动开短事务。
    周期任务幂等键由 layer、camera/window、输入版本、prompt/schema 版本构成。
    """

    def __init__(
        self,
        repository: SchedulerRepository,
        queue: TaskQueue,
        *,
        session_factory: Callable[[], Session] | None = None,
        clock: Callable[[], int] = utc_now_ms,
        minute_lateness_ms: int = 15_000,
        aggregation_grace_ms: int = 2 * MINUTE_MS,
        frame_pin_ms: int = 30 * MINUTE_MS,
        l3_wait_ms: int = 5 * MINUTE_MS,
        l2_context_ms: int = 30_000,
        l2_batch_limit: int = 30,
        l2_extra_frame_limit: int = 30,
    ) -> None:
        self.repository = repository
        self.queue = queue
        self.runner = SessionRunner(session_factory)
        self.clock = clock
        self.minute_lateness_ms = max(0, int(minute_lateness_ms))
        self.aggregation_grace_ms = max(0, int(aggregation_grace_ms))
        self.frame_pin_ms = max(0, int(frame_pin_ms))
        self.l3_wait_ms = max(0, int(l3_wait_ms))
        self.l2_context_ms = max(0, int(l2_context_ms))
        self.l2_batch_limit = max(1, int(l2_batch_limit))
        self.l2_extra_frame_limit = max(0, int(l2_extra_frame_limit))

    def schedule_minute(
        self,
        camera: CameraSchedule,
        start_ms: int,
        end_ms: int,
        *,
        session: Session | None = None,
    ) -> JobRecord | MinuteObservationRecord | None:
        start, end = _window_ms(start_ms, end_ms)
        if end - start != MINUTE_MS or start % MINUTE_MS:
            raise ValueError("分钟窗口必须是 UTC 整分钟且长度为 60 秒")
        if not camera.enabled:
            return None

        def operation(db: Session | None):
            snapshot = self.repository.snapshot_minute(
                db, camera.id, start, end, strict_mode=camera.strict_mode
            )
            frame_ids = tuple(dict.fromkeys(snapshot.frame_ids))
            version = snapshot.input_version if snapshot.input_version is not None else camera.input_version
            if snapshot.stable or not camera.l1_enabled or snapshot.no_ai_reason:
                reason = snapshot.no_ai_reason or ("stable_scene" if snapshot.stable else "ai_disabled")
                observation = MinuteObservationRecord(
                    id=f"minute:{camera.id}:{start}:{version}",
                    camera_id=camera.id,
                    start_at_ms=start,
                    end_at_ms=end,
                    source="local_stable",
                    state="succeeded",
                    evidence_frame_ids=frame_ids,
                    gaps=tuple(snapshot.gaps),
                    coverage_ratio=max(0.0, min(1.0, snapshot.coverage_ratio)),
                    version=version,
                    observation={
                        "kind": "LOCAL_STABLE_CONTINUATION",
                        "reason": reason,
                        "signals": dict(snapshot.signals),
                    },
                )
                self.repository.save_local_observation(db, observation)
                return observation

            if not frame_ids:
                # 缺图时必须保留显式 gap/local 状态，不创建空输入的 AI 任务。
                observation = MinuteObservationRecord(
                    id=f"minute:{camera.id}:{start}:{version}",
                    camera_id=camera.id,
                    start_at_ms=start,
                    end_at_ms=end,
                    source="failed",
                    state="needs_review",
                    evidence_frame_ids=(),
                    gaps=tuple(snapshot.gaps) or ({"start_at_ms": start, "end_at_ms": end, "kind": "no_frames"},),
                    coverage_ratio=0.0,
                    version=version,
                    observation={"reason": "NO_EVIDENCE"},
                )
                self.repository.save_local_observation(db, observation)
                return observation

            prompt = camera.prompt_versions.get("l1", "v1")
            schema = camera.schema_versions.get("l1", "v1")
            key = build_idempotency_key("l1", camera.id, start, end, version, prompt, schema)
            pin_until = max(snapshot.pin_until_ms or 0, end + self.frame_pin_ms)
            self.repository.pin_frames(db, frame_ids, pin_until, f"ai-job:{key}")
            job = self.queue.enqueue(
                layer="l1",
                idempotency_key=key,
                camera_id=camera.id,
                period_start_ms=start,
                period_end_ms=end,
                payload={
                    "window": {"start_ms": start, "end_ms": end},
                    "signals": dict(snapshot.signals),
                    "gaps": list(snapshot.gaps),
                    "coverage_ratio": max(0.0, min(1.0, snapshot.coverage_ratio)),
                    "strict_mode": camera.strict_mode,
                    "input_frame_ids": list(frame_ids),
                },
                input_frame_ids=frame_ids,
                input_version=version,
                prompt_version=prompt,
                schema_version=schema,
                priority=400,
                session=db,
            )
            return job

        return self.runner.run(operation, session)

    def schedule_l2(
        self,
        camera: CameraSchedule,
        start_ms: int,
        end_ms: int,
        *,
        now_ms: int | None = None,
        force_incomplete: bool = False,
        session: Session | None = None,
    ) -> JobRecord | None:
        start, end = _window_ms(start_ms, end_ms)
        if end - start != FIVE_MINUTES_MS:
            raise ValueError("L2 窗口长度必须为 5 分钟")
        current = self.clock() if now_ms is None else int(now_ms)
        if end > current - self.aggregation_grace_ms and not force_incomplete:
            return None

        def operation(db: Session | None):
            snapshot = self.repository.snapshot_l2(
                db,
                camera.id,
                start,
                end,
                context_before_ms=self.l2_context_ms,
                context_after_ms=self.l2_context_ms,
            )
            if not snapshot.complete:
                if not force_incomplete and current < end + self.l3_wait_ms + self.aggregation_grace_ms:
                    return None
                self.repository.mark_missing_l2_windows(db, camera.id, start, end)
            frame_ids = tuple(dict.fromkeys(snapshot.evidence_frame_ids))[: self.l2_batch_limit + self.l2_extra_frame_limit]
            prompt = camera.prompt_versions.get("l2", "v1")
            schema = camera.schema_versions.get("l2", "v1")
            fingerprint = snapshot.input_version or _canonical_fingerprint(
                {"observations": snapshot.observation_ids, "frames": frame_ids, "context": snapshot.context}
            )
            key = build_idempotency_key("l2", camera.id, start, end, fingerprint, prompt, schema)
            self.repository.pin_frames(db, frame_ids, end + self.frame_pin_ms, f"ai-job:{key}")
            return self.queue.enqueue(
                layer="l2",
                idempotency_key=key,
                camera_id=camera.id,
                period_start_ms=start,
                period_end_ms=end,
                payload={
                    "observation_ids": list(snapshot.observation_ids),
                    "context": dict(snapshot.context),
                    "context_before_ms": self.l2_context_ms,
                    "context_after_ms": self.l2_context_ms,
                    "output_window": {"start_ms": start, "end_ms": end},
                    "input_complete": snapshot.complete,
                    "input_frame_ids": list(frame_ids),
                    "max_tool_frames": self.l2_extra_frame_limit,
                },
                input_frame_ids=frame_ids,
                input_version=fingerprint,
                prompt_version=prompt,
                schema_version=schema,
                priority=300,
                session=db,
            )

        return self.runner.run(operation, session)

    def schedule_l3(
        self,
        camera_ids: Sequence[str],
        period: str,
        start_ms: int,
        end_ms: int,
        *,
        timezone_name: str = "Asia/Shanghai",
        now_ms: int | None = None,
        force_incomplete: bool = False,
        session: Session | None = None,
    ) -> JobRecord | None:
        start, end = _window_ms(start_ms, end_ms)
        if period not in {"hour", "day"}:
            raise ValueError("period 仅支持 hour/day")
        current = self.clock() if now_ms is None else int(now_ms)
        if end > current - self.l3_wait_ms and not force_incomplete:
            return None
        normalized_cameras = tuple(sorted(set(camera_ids)))
        if not normalized_cameras:
            return None

        def operation(db: Session | None):
            snapshot = self.repository.snapshot_l3(db, normalized_cameras, start, end)
            if not snapshot.complete and not force_incomplete and current < end + self.l3_wait_ms:
                return None
            prompt = "v1"
            schema = "v1"
            fingerprint = snapshot.input_version or _canonical_fingerprint(
                {"segment_revisions": snapshot.segment_revisions, "segments": snapshot.segment_ids}
            )
            scope = ",".join(normalized_cameras)
            key = build_idempotency_key(f"l3_{period}", scope, start, end, fingerprint, prompt, schema)
            return self.queue.enqueue(
                layer="l3",
                idempotency_key=key,
                camera_id=scope,
                period_start_ms=start,
                period_end_ms=end,
                payload={
                    "period": period,
                    "timezone": timezone_name,
                    "camera_ids": list(normalized_cameras),
                    "segment_ids": list(snapshot.segment_ids),
                    "segment_revisions": [list(item) for item in snapshot.segment_revisions],
                    "context": dict(snapshot.context),
                    "incomplete": not snapshot.complete,
                    "source_layer": "l2_only",
                },
                input_version=fingerprint,
                prompt_version=prompt,
                schema_version=schema,
                priority=200,
                session=db,
            )

        return self.runner.run(operation, session)

    def schedule_closed_periods(
        self,
        camera_ids: Sequence[str],
        now_ms: int | None = None,
        *,
        include_daily: bool = True,
        session: Session | None = None,
    ) -> list[JobRecord]:
        """调度当前最近已结束的 5 分钟、小时和本地日窗口；可由应用轮询器每 10–30 秒调用。"""
        current = self.clock() if now_ms is None else int(now_ms)
        output: list[JobRecord] = []
        for camera_id in sorted(set(camera_ids)):
            zone_name = "Asia/Shanghai"
            end = floor_local_window_ms(current - self.aggregation_grace_ms, 5, zone_name)
            if end > 0:
                camera = CameraSchedule(id=camera_id, timezone=zone_name)
                job = self.schedule_l2(camera, end - FIVE_MINUTES_MS, end, now_ms=current, session=session)
                if job:
                    output.append(job)
        # L3 采用各相机统一时区/范围；正式应用可按用户配置时区分组调用。
        if camera_ids:
            start, end = local_period_bounds_ms(current - self.l3_wait_ms, "hour", "Asia/Shanghai")
            if end <= current - self.l3_wait_ms:
                job = self.schedule_l3(camera_ids, "hour", start, end, now_ms=current, session=session)
                if job:
                    output.append(job)
            if include_daily:
                start, end = local_period_bounds_ms(current - self.l3_wait_ms, "day", "Asia/Shanghai")
                if end <= current - self.l3_wait_ms:
                    job = self.schedule_l3(camera_ids, "day", start, end, now_ms=current, session=session)
                    if job:
                        output.append(job)
        return output

    def schedule_lookback(
        self,
        cameras: Sequence[CameraSchedule],
        *,
        now_ms: int | None = None,
        catch_up_minutes: int = 2,
        session: Session | None = None,
    ) -> list[JobRecord | MinuteObservationRecord]:
        """恢复短暂停机期间的已结束分钟窗口；幂等键使重复轮询安全。"""
        current = self.clock() if now_ms is None else int(now_ms)
        closed_minute = floor_minute_ms(current - self.minute_lateness_ms)
        output: list[JobRecord | MinuteObservationRecord] = []
        for camera in cameras:
            if not camera.enabled:
                continue
            for offset in reversed(range(max(1, catch_up_minutes))):
                end = closed_minute - (offset * MINUTE_MS)
                start = end - MINUTE_MS
                if end <= 0:
                    continue
                item = self.schedule_minute(camera, start, end, session=session)
                if item is not None:
                    output.append(item)
        return output


Scheduler = WindowScheduler

__all__ = [
    "CameraSchedule",
    "L2Snapshot",
    "L3Snapshot",
    "MINUTE_MS",
    "MinuteObservationRecord",
    "MinuteSnapshot",
    "Scheduler",
    "SchedulerError",
    "SchedulerRepository",
    "WindowScheduler",
    "floor_local_window_ms",
    "floor_minute_ms",
    "local_period_bounds_ms",
]
