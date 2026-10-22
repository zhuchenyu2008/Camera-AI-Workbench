from __future__ import annotations

"""分批、可恢复且保护收藏/任务引用的帧留存清理。

RetentionRepository 是应用 ORM 的适配边界。claim_for_deletion 必须在短事务内再次验证收藏、
pin_until、任务引用，并将 Frame 标为 deleting；同时仅返回无其他可用 Frame 引用的物理文件路径。
文件删除在事务外进行，成功后 finalize_expired 保留证据元数据并标记 expired；失败则持久化安全错误，
下一轮可以重试。队列引用不会无限延长帧寿命，应通过 pin_until_ms 的截止值实现有限保护。
"""

import os
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping, Protocol, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from app.models import AIJob, Frame, FrameBlob

from app.queue import utc_now_ms
from app.scheduler import SessionRunner


HOUR_MS = 60 * 60 * 1000


class RetentionError(RuntimeError):
    pass


class UnsafeAssetPathError(RetentionError):
    pass


@dataclass(frozen=True)
class RetentionPolicy:
    ordinary_days: float = 1 / 24  # 产品基线：普通帧默认 1 小时
    event_days: float = 6 / 24  # 产品基线：事件帧默认 6 小时
    batch_size: int = 100
    max_batches_per_run: int = 10

    def __post_init__(self) -> None:
        if self.ordinary_days < 0 or self.event_days < 0:
            raise ValueError("留存天数不能小于 0")
        if self.batch_size <= 0 or self.max_batches_per_run <= 0:
            raise ValueError("清理批次大小和轮数必须大于 0")

    @property
    def ordinary_retention_ms(self) -> int:
        return int(self.ordinary_days * 24 * HOUR_MS)

    @property
    def event_retention_ms(self) -> int:
        return int(self.event_days * 24 * HOUR_MS)


@dataclass(frozen=True)
class RetentionCandidate:
    id: str
    camera_id: str
    captured_at_ms: int
    is_event: bool = False
    retained_until_ms: int | None = None
    favorite: bool = False
    pin_until_ms: int | None = None
    task_reference_until_ms: int | None = None
    # 只允许 repository 返回已确认没有其他可用 Frame 引用的路径。
    deletable_paths: tuple[str, ...] = ()
    state: str = "available"
    byte_size: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def protected_until_ms(self) -> int | None:
        values = [v for v in (self.pin_until_ms, self.task_reference_until_ms) if v is not None]
        return max(values) if values else None

    def due_at_ms(self, policy: RetentionPolicy) -> int:
        duration = policy.event_retention_ms if self.is_event else policy.ordinary_retention_ms
        policy_due = self.captured_at_ms + duration
        return max(policy_due, self.retained_until_ms or 0)


@dataclass(frozen=True)
class RetentionBatchResult:
    scanned: int = 0
    deleted_frames: int = 0
    skipped_favorites: int = 0
    skipped_task_references: int = 0
    skipped_not_expired: int = 0
    failed: int = 0
    bytes_freed: int = 0
    next_cursor: str | None = None
    has_more: bool = False

    def plus(self, other: "RetentionBatchResult") -> "RetentionBatchResult":
        return RetentionBatchResult(
            scanned=self.scanned + other.scanned,
            deleted_frames=self.deleted_frames + other.deleted_frames,
            skipped_favorites=self.skipped_favorites + other.skipped_favorites,
            skipped_task_references=self.skipped_task_references + other.skipped_task_references,
            skipped_not_expired=self.skipped_not_expired + other.skipped_not_expired,
            failed=self.failed + other.failed,
            bytes_freed=self.bytes_freed + other.bytes_freed,
            next_cursor=other.next_cursor,
            has_more=other.has_more,
        )

    def to_dict(self) -> dict[str, int | str | bool | None]:
        return {
            "scanned": self.scanned,
            "deleted": self.deleted_frames,
            "skipped_favorites": self.skipped_favorites,
            "skipped_task_references": self.skipped_task_references,
            "skipped_not_expired": self.skipped_not_expired,
            "failed": self.failed,
            "bytes_freed": self.bytes_freed,
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
        }


class RetentionRepository(Protocol):
    """清理必须实现的 repository 接口。所有路径由数据库元数据映射，不能来自用户或模型。"""

    def list_candidates(
        self,
        session: Session,
        *,
        camera_id: str | None,
        ordinary_before_ms: int,
        event_before_ms: int,
        now_ms: int,
        after_id: str | None,
        limit: int,
    ) -> Sequence[RetentionCandidate]: ...

    def claim_for_deletion(
        self, session: Session, candidate_id: str, *, now_ms: int, policy: RetentionPolicy
    ) -> RetentionCandidate | None: ...

    def finalize_expired(
        self,
        session: Session,
        candidate: RetentionCandidate,
        *,
        now_ms: int,
        deleted_paths: Sequence[str],
        bytes_freed: int,
    ) -> None: ...

    def mark_delete_failed(
        self, session: Session, candidate: RetentionCandidate, *, now_ms: int, safe_error: str
    ) -> None: ...


class SqlAlchemyRetentionRepository:
    """ORM adapter for the v1 Frame/FrameBlob tables.

    It deliberately keeps the image paths from database metadata only and rechecks
    favorite/pin/task protections inside the claim transaction before a file is
    handed to ``SafeFileDeleter``.
    """

    ACTIVE_JOB_STATES = ("queued", "leased", "running", "retry_wait")
    # Jobs created outside the scheduler may not carry a pin/deadline.  Keep their
    # evidence for a finite grace period, never forever and never only until
    # ``available_at_ms``.

    def _task_pins(self, session: Session, frame_id: str) -> int | None:
        latest: int | None = None
        for job in session.scalars(select(AIJob).where(AIJob.state.in_(self.ACTIVE_JOB_STATES))):
            if frame_id in (job.input_frame_ids_json or []):
                created = int(job.created_at_ms or job.available_at_ms or utc_now_ms())
                grace = 12 * HOUR_MS if str(job.layer).upper() == "L3" else 6 * HOUR_MS
                maximum_deadline = created + grace
                deadline = job.deadline_at_ms or job.lease_expires_at_ms or maximum_deadline
                # Even a repeatedly renewed lease cannot pin evidence forever.
                deadline = min(int(deadline), maximum_deadline)
                latest = max(latest or 0, deadline)
        return latest

    @staticmethod
    def _candidate(frame: Frame, task_until: int | None) -> RetentionCandidate:
        return RetentionCandidate(
            id=frame.id,
            camera_id=frame.camera_id,
            captured_at_ms=frame.captured_at_ms,
            is_event=frame.event_id is not None,
            retained_until_ms=frame.retained_until_ms,
            favorite=bool(frame.favorite),
            pin_until_ms=frame.pin_until_ms,
            task_reference_until_ms=task_until,
            deletable_paths=(),
            state=frame.state,
            byte_size=int(frame.byte_size or 0),
            metadata={"sha256": frame.sha256, "blob_sha256": frame.blob_sha256},
        )

    def list_candidates(self, session: Session, *, camera_id: str | None, ordinary_before_ms: int,
                        event_before_ms: int, now_ms: int, after_id: str | None,
                        limit: int) -> Sequence[RetentionCandidate]:
        filters=[Frame.state=="active",or_(
            and_(Frame.event_id.is_(None),Frame.captured_at_ms<=ordinary_before_ms),
            and_(Frame.event_id.is_not(None),Frame.captured_at_ms<=event_before_ms),
        )]
        if camera_id:filters.append(Frame.camera_id==camera_id)
        if after_id:filters.append(Frame.id>after_id)
        rows=list(session.scalars(select(Frame).where(*filters).order_by(Frame.id).limit(limit)))
        return [self._candidate(row,self._task_pins(session,row.id)) for row in rows]

    def claim_for_deletion(self, session: Session, candidate_id: str, *, now_ms: int,
                           policy: RetentionPolicy) -> RetentionCandidate | None:
        frame=session.get(Frame,candidate_id)
        if frame is None or frame.state!="active" or frame.favorite:
            return None
        task_until=self._task_pins(session,frame.id)
        candidate=self._candidate(frame,task_until)
        if candidate.protected_until_ms is not None and candidate.protected_until_ms>now_ms:
            return None
        if candidate.due_at_ms(policy)>now_ms:
            return None
        references=0
        if frame.blob_sha256:
            references=session.scalar(select(func.count(Frame.id)).where(
                Frame.blob_sha256==frame.blob_sha256,Frame.id!=frame.id,Frame.state=="active")) or 0
        paths=[]
        if references==0:
            paths.extend(path for path in (frame.relative_path,frame.thumbnail_relative_path) if path)
            blob=session.get(FrameBlob,frame.blob_sha256) if frame.blob_sha256 else None
            if blob and blob.relative_path not in paths:paths.append(blob.relative_path)
        frame.state="deleting"
        session.flush()
        return replace(candidate,deletable_paths=tuple(paths),state="deleting")

    def finalize_expired(self, session: Session, candidate: RetentionCandidate, *, now_ms: int,
                        deleted_paths: Sequence[str], bytes_freed: int) -> None:
        frame=session.get(Frame,candidate.id)
        if frame is None:return
        frame.state="expired"
        frame.updated_at_ms=now_ms if hasattr(frame,"updated_at_ms") else frame.ingested_at_ms
        if frame.blob_sha256:
            active=session.scalar(select(func.count(Frame.id)).where(Frame.blob_sha256==frame.blob_sha256,Frame.state=="active")) or 0
            if active==0:
                blob=session.get(FrameBlob,frame.blob_sha256)
                if blob:blob.state="expired"
        session.flush()

    def mark_delete_failed(self, session: Session, candidate: RetentionCandidate, *, now_ms: int,
                           safe_error: str) -> None:
        frame=session.get(Frame,candidate.id)
        if frame is not None:
            frame.state="active"
            signal=dict(frame.local_signal_json or {})
            signal["retention_error"]=safe_error[:200]
            frame.local_signal_json=signal
            session.flush()


class SafeFileDeleter:
    """将 repository 授权的相对路径安全地解析到数据卷内并删除。

    任何 symlink、绝对路径、盘符路径或目录穿越都会拒绝。共享 blob 必须由 repository 在
    claim_for_deletion 时确认引用数为零后才加入 deletable_paths。
    """

    def __init__(self, data_root: str | Path):
        self.root = Path(data_root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("data_root 必须是已存在目录")

    def _resolve(self, relative_path: str) -> Path:
        if not relative_path or "\x00" in relative_path:
            raise UnsafeAssetPathError("空或非法的相对路径")
        posix = PurePosixPath(relative_path)
        windows = PureWindowsPath(relative_path)
        if posix.is_absolute() or windows.is_absolute() or windows.drive:
            raise UnsafeAssetPathError("拒绝绝对路径")
        if any(part in {"..", ""} for part in posix.parts):
            raise UnsafeAssetPathError("拒绝目录穿越路径")
        target = self.root.joinpath(*posix.parts)
        # 对每一层检查 symlink；resolve(strict=False) 处理不存在文件并检测路径逃逸。
        cursor = self.root
        for part in posix.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise UnsafeAssetPathError("拒绝通过符号链接删除文件")
        resolved = target.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise UnsafeAssetPathError("文件路径超出数据目录") from exc
        return target

    def delete(self, paths: Sequence[str]) -> tuple[list[str], int]:
        deleted: list[str] = []
        bytes_freed = 0
        for relative in dict.fromkeys(paths):
            path = self._resolve(relative)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                # 已经删除但元数据尚未提交时，重试视为成功。
                deleted.append(relative)
                continue
            if not path.is_file():
                raise UnsafeAssetPathError("只允许删除常规文件")
            path.unlink()
            deleted.append(relative)
            bytes_freed += size
        return deleted, bytes_freed


class RetentionService:
    """可由 scheduler/worker_main 定期调用的清理服务。"""

    def __init__(
        self,
        repository: RetentionRepository,
        file_deleter: SafeFileDeleter | Callable[[Sequence[str]], tuple[list[str], int]],
        *,
        session_factory: Callable[[], Session] | None = None,
        clock: Callable[[], int] = utc_now_ms,
        policy: RetentionPolicy | Callable[[str | None], RetentionPolicy] | None = None,
    ) -> None:
        self.repository = repository
        self.file_deleter = file_deleter
        self.runner = SessionRunner(session_factory)
        self.clock = clock
        self.policy = policy or RetentionPolicy()

    def _policy(self, camera_id: str | None) -> RetentionPolicy:
        return self.policy(camera_id) if callable(self.policy) else self.policy

    def run_batch(
        self,
        *,
        camera_id: str | None = None,
        now_ms: int | None = None,
        cursor: str | None = None,
        batch_size: int | None = None,
        session: Session | None = None,
    ) -> RetentionBatchResult:
        current = self.clock() if now_ms is None else int(now_ms)
        policy = self._policy(camera_id)
        size = max(1, int(batch_size or policy.batch_size))
        ordinary_before = current - policy.ordinary_retention_ms
        event_before = current - policy.event_retention_ms

        def list_operation(db: Session | None):
            return list(
                self.repository.list_candidates(
                    db,
                    camera_id=camera_id,
                    ordinary_before_ms=ordinary_before,
                    event_before_ms=event_before,
                    now_ms=current,
                    after_id=cursor,
                    limit=size,
                )
            )

        candidates = self.runner.run(list_operation, session)
        scanned = 0
        deleted = skipped_favorite = skipped_refs = skipped_not_due = failed = bytes_freed = 0
        last_id = candidates[-1].id if candidates else cursor
        for candidate in candidates:
            scanned += 1
            if candidate.favorite:
                skipped_favorite += 1
                continue
            protection_until = candidate.protected_until_ms
            if protection_until is not None and protection_until > current:
                skipped_refs += 1
                continue
            if candidate.due_at_ms(policy) > current:
                skipped_not_due += 1
                continue

            claim = self.runner.run(
                lambda db, item=candidate: self.repository.claim_for_deletion(
                    db, item.id, now_ms=current, policy=policy
                ),
                session,
            )
            if claim is None:
                # Claim 可能发现收藏/pin 在列表之后更新。repository 应返回 None；重新读取
                # 状态可更精确计数，不会把该 Frame 当作清理失败。
                skipped_refs += 1
                continue
            try:
                paths, released = self.file_deleter.delete(claim.deletable_paths) if isinstance(
                    self.file_deleter, SafeFileDeleter
                ) else self.file_deleter(claim.deletable_paths)
                self.runner.run(
                    lambda db, item=claim, removed=paths, amount=released: self.repository.finalize_expired(
                        db,
                        item,
                        now_ms=current,
                        deleted_paths=removed,
                        bytes_freed=amount,
                    ),
                    session,
                )
                deleted += 1
                bytes_freed += int(released)
            except Exception as exc:
                failed += 1
                safe_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:180]}"
                try:
                    self.runner.run(
                        lambda db, item=claim, error=safe_error: self.repository.mark_delete_failed(
                            db, item, now_ms=current, safe_error=error
                        ),
                        session,
                    )
                except Exception:
                    # 保留 deleting 状态，由启动恢复/下轮清理继续处理；不隐藏原始失败计数。
                    pass
        return RetentionBatchResult(
            scanned=scanned,
            deleted_frames=deleted,
            skipped_favorites=skipped_favorite,
            skipped_task_references=skipped_refs,
            skipped_not_expired=skipped_not_due,
            failed=failed,
            bytes_freed=bytes_freed,
            next_cursor=last_id,
            has_more=len(candidates) == size,
        )

    def run(
        self,
        *,
        camera_id: str | None = None,
        now_ms: int | None = None,
        max_batches: int | None = None,
        session: Session | None = None,
    ) -> RetentionBatchResult:
        policy = self._policy(camera_id)
        batch_limit = max(1, int(max_batches or policy.max_batches_per_run))
        result = RetentionBatchResult()
        cursor: str | None = None
        for _ in range(batch_limit):
            batch = self.run_batch(
                camera_id=camera_id,
                now_ms=now_ms,
                cursor=cursor,
                batch_size=policy.batch_size,
                session=session,
            )
            result = result.plus(batch)
            if not batch.has_more or not batch.next_cursor or batch.next_cursor == cursor:
                break
            cursor = batch.next_cursor
        return result


RetentionCleaner = RetentionService

__all__ = [
    "RetentionBatchResult",
    "RetentionCandidate",
    "RetentionCleaner",
    "RetentionError",
    "RetentionPolicy",
    "RetentionRepository",
    "SqlAlchemyRetentionRepository",
    "RetentionService",
    "SafeFileDeleter",
    "UnsafeAssetPathError",
]
