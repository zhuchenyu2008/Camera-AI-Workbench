from __future__ import annotations

"""小时/日报摘要服务与 stale 管理。

摘要只读取第二层 ActivitySegment/用户修订，不从 L1 MinuteObservation 直接生成总结。真实 ORM
由 SummaryRepository 适配；本服务负责源版本指纹、stale 失效、L3 Job 幂等入队和输出引用校验。
"""

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from sqlalchemy.orm import Session

from app.queue import JobRecord, TaskQueue, build_idempotency_key, utc_now_ms
from app.scheduler import SessionRunner


class SummaryError(RuntimeError):
    pass


class InvalidSummaryOutput(SummaryError):
    pass


@dataclass(frozen=True)
class SummarySnapshot:
    """L2 来源的固定快照。segment_revisions 是 (segment_id, revision) 排序列表。"""

    segment_ids: tuple[str, ...]
    segment_revisions: tuple[tuple[str, int], ...]
    statistics: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    complete: bool = True

    @property
    def fingerprint(self) -> str:
        data = {
            "segment_revisions": sorted((str(key), int(value)) for key, value in self.segment_revisions),
            "segment_ids": sorted(set(self.segment_ids)),
            "statistics": self.statistics,
            "complete": self.complete,
        }
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SummaryRecord:
    id: str
    camera_ids: tuple[str, ...]
    period_kind: str
    period_start_ms: int
    period_end_ms: int
    timezone: str
    source_fingerprint: str | None = None
    source_segment_ids: tuple[str, ...] = ()
    state: str = "stale"
    stale: bool = True
    version: int = 1
    body: str | None = None
    statistics: Mapping[str, Any] = field(default_factory=dict)
    ai_job_id: str | None = None


@dataclass(frozen=True)
class SummaryEnqueueResult:
    summary: SummaryRecord
    job: JobRecord | None
    unchanged: bool = False


class SummaryRepository(Protocol):
    """SQLAlchemy repository contract。方法必须在调用方 Session 中完成，不自行提交。"""

    def snapshot_l2_segments(
        self,
        session: Session,
        camera_ids: Sequence[str],
        start_ms: int,
        end_ms: int,
    ) -> SummarySnapshot: ...

    def get_or_create_summary(
        self,
        session: Session,
        camera_ids: Sequence[str],
        period_kind: str,
        start_ms: int,
        end_ms: int,
        timezone_name: str,
        source_fingerprint: str,
        source_segment_ids: Sequence[str],
        now_ms: int,
    ) -> SummaryRecord: ...

    def get_summary(self, session: Session, summary_id: str) -> SummaryRecord | None: ...

    def mark_range_stale(
        self,
        session: Session,
        camera_id: str,
        start_ms: int,
        end_ms: int,
        *,
        reason: str,
        now_ms: int,
    ) -> int: ...

    def mark_summary_queued(
        self, session: Session, summary_id: str, job_id: str, *, now_ms: int
    ) -> None: ...

    def save_summary_result(
        self,
        session: Session,
        summary: SummaryRecord,
        *,
        source_fingerprint: str,
        source_segment_ids: Sequence[str],
        body: str,
        statistics: Mapping[str, Any],
        incomplete: bool,
        now_ms: int,
    ) -> SummaryRecord: ...


class SummaryService:
    def __init__(
        self,
        repository: SummaryRepository,
        queue: TaskQueue,
        *,
        session_factory: Callable[[], Session] | None = None,
        clock: Callable[[], int] = utc_now_ms,
        prompt_version: str = "v1",
        schema_version: str = "v1",
    ) -> None:
        self.repository = repository
        self.queue = queue
        self.runner = SessionRunner(session_factory)
        self.clock = clock
        self.prompt_version = prompt_version
        self.schema_version = schema_version

    def enqueue_period(
        self,
        camera_ids: Sequence[str],
        period_kind: str,
        start_ms: int,
        end_ms: int,
        *,
        timezone_name: str = "Asia/Shanghai",
        idempotency_key: str | None = None,
        force: bool = False,
        session: Session | None = None,
    ) -> SummaryEnqueueResult:
        cameras = tuple(sorted(set(str(item) for item in camera_ids)))
        kind = str(period_kind).lower()
        start, end = int(start_ms), int(end_ms)
        if not cameras:
            raise ValueError("camera_ids 不能为空")
        if kind not in {"hour", "day"} or end <= start:
            raise ValueError("摘要周期或时间范围无效")

        def operation(db: Session | None) -> SummaryEnqueueResult:
            snapshot = self.repository.snapshot_l2_segments(db, cameras, start, end)
            fingerprint = snapshot.fingerprint
            summary = self.repository.get_or_create_summary(
                db,
                cameras,
                kind,
                start,
                end,
                timezone_name,
                fingerprint,
                snapshot.segment_ids,
                self.clock(),
            )
            if not force and not summary.stale and summary.source_fingerprint == fingerprint:
                return SummaryEnqueueResult(summary=summary, job=None, unchanged=True)

            scope = ",".join(cameras)
            stable_key = idempotency_key or build_idempotency_key(
                f"l3_{kind}", scope, start, end, fingerprint, self.prompt_version, self.schema_version
            )
            if force and idempotency_key is None:
                # API 应优先传 Idempotency-Key；无 key 的显式再生成代表一次新请求。
                stable_key = f"{stable_key}:manual:{secrets.token_urlsafe(12)}"
            job = self.queue.enqueue(
                layer="l3",
                idempotency_key=stable_key,
                camera_id=scope,
                period_start_ms=start,
                period_end_ms=end,
                payload={
                    "summary_id": summary.id,
                    "period_kind": kind,
                    "timezone": timezone_name,
                    "camera_ids": list(cameras),
                    "segment_ids": list(snapshot.segment_ids),
                    "segment_revisions": [list(item) for item in snapshot.segment_revisions],
                    "statistics": dict(snapshot.statistics),
                    "context": dict(snapshot.context),
                    "source_fingerprint": fingerprint,
                    "incomplete": not snapshot.complete,
                    "source_layer": "l2_only",
                },
                input_version=fingerprint,
                prompt_version=self.prompt_version,
                schema_version=self.schema_version,
                priority=200,
                session=db,
            )
            self.repository.mark_summary_queued(db, summary.id, job.id, now_ms=self.clock())
            return SummaryEnqueueResult(summary=summary, job=job, unchanged=False)

        return self.runner.run(operation, session)

    def regenerate(
        self,
        summary_id: str,
        *,
        idempotency_key: str | None = None,
        session: Session | None = None,
    ) -> SummaryEnqueueResult:
        def lookup(db: Session | None) -> SummaryRecord:
            summary = self.repository.get_summary(db, summary_id)
            if summary is None:
                raise SummaryError("摘要不存在")
            return summary

        summary = self.runner.run(lookup, session)
        return self.enqueue_period(
            summary.camera_ids,
            summary.period_kind,
            summary.period_start_ms,
            summary.period_end_ms,
            timezone_name=summary.timezone,
            idempotency_key=idempotency_key,
            force=True,
            session=session,
        )

    def mark_stale(
        self,
        camera_id: str,
        start_ms: int,
        end_ms: int,
        *,
        reason: str = "source_segment_changed",
        session: Session | None = None,
    ) -> int:
        """活动段编辑、锁定、重算或删除后调用；Repository 将覆盖到的 hour/day 摘要标为 stale。"""
        if int(end_ms) <= int(start_ms):
            raise ValueError("失效范围必须满足 start < end")
        return int(
            self.runner.run(
                lambda db: self.repository.mark_range_stale(
                    db,
                    camera_id,
                    int(start_ms),
                    int(end_ms),
                    reason=reason,
                    now_ms=self.clock(),
                ),
                session,
            )
        )

    def source_is_stale(self, summary_id: str, *, session: Session | None = None) -> bool:
        def operation(db: Session | None) -> bool:
            summary = self.repository.get_summary(db, summary_id)
            if summary is None:
                raise SummaryError("摘要不存在")
            current = self.repository.snapshot_l2_segments(
                db,
                summary.camera_ids,
                summary.period_start_ms,
                summary.period_end_ms,
            )
            return summary.stale or summary.source_fingerprint != current.fingerprint

        return bool(self.runner.run(operation, session))

    def persist_output(
        self,
        job: JobRecord,
        output: Mapping[str, Any],
        *,
        session: Session,
    ) -> SummaryRecord:
        """AnalysisRepository.persist_l3 可调用此方法共享严格引用校验。"""
        summary_id = job.payload.get("summary_id")
        if not isinstance(summary_id, str) or not summary_id:
            raise InvalidSummaryOutput("L3 job 缺少 summary_id")
        summary = self.repository.get_summary(session, summary_id)
        if summary is None:
            raise SummaryError("摘要不存在")
        expected_ids = tuple(str(item) for item in job.payload.get("segment_ids", ()))
        allowed = set(expected_ids)
        returned_ids = output.get("source_segment_ids", output.get("source_ids", expected_ids))
        if not isinstance(returned_ids, (list, tuple)):
            raise InvalidSummaryOutput("source_segment_ids 必须是数组")
        if any(not isinstance(item, str) or item not in allowed for item in returned_ids):
            raise InvalidSummaryOutput("摘要引用了本次任务 L2 快照以外的活动段")
        body = output.get("summary_text", output.get("body", output.get("narrative")))
        if not isinstance(body, str) or not body.strip():
            raise InvalidSummaryOutput("摘要正文不能为空")
        statistics = output.get("statistics", job.payload.get("statistics", {}))
        if not isinstance(statistics, Mapping):
            raise InvalidSummaryOutput("摘要 statistics 必须是 JSON 对象")
        return self.repository.save_summary_result(
            session,
            summary,
            source_fingerprint=str(job.payload["source_fingerprint"]),
            source_segment_ids=expected_ids,
            body=body.strip(),
            statistics=dict(statistics),
            incomplete=bool(job.payload.get("incomplete", False)),
            now_ms=self.clock(),
        )


SummaryManager = SummaryService

__all__ = [
    "InvalidSummaryOutput",
    "SummaryEnqueueResult",
    "SummaryError",
    "SummaryManager",
    "SummaryRecord",
    "SummaryRepository",
    "SummaryService",
    "SummarySnapshot",
]
