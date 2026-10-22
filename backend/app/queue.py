from __future__ import annotations

"""持久化任务队列。

本模块只依赖 SQLAlchemy 的 Session/Model 约定，不依赖 Workbench、FastAPI 或具体 API 路由。
主 agent 可以把自己的 ORM 模型传给 :class:\`SqlAlchemyJobRepository\`，也可以实现
:class:\`JobRepository\` 协议后复用 :class:\`TaskQueue\`。

所有时间均为 UTC epoch milliseconds。队列的事务边界由 TaskQueue 管理：传入 Session
时不提交事务；配置 session_factory 时每个操作独立提交。网络调用不应在本模块的事务内执行。
"""

import json
import random
import secrets
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from sqlalchemy import JSON, Boolean, Column, Integer, MetaData, String, Table, and_, or_, select, update
from sqlalchemy.exc import IntegrityError


UTC = timezone.utc


class JobState(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {JobState.SUCCEEDED.value, JobState.FAILED.value, JobState.NEEDS_REVIEW.value, JobState.CANCELLED.value}
)
ACTIVE_STATES = frozenset({JobState.LEASED.value, JobState.RUNNING.value})
RETRYABLE_ERROR_CODES = frozenset(
    {
        "TIMEOUT",
        "NETWORK_ERROR",
        "PROVIDER_TIMEOUT",
        "PROVIDER_UNAVAILABLE",
        "RATE_LIMITED",
        "HTTP_429",
        "HTTP_500",
        "HTTP_502",
        "HTTP_503",
        "HTTP_504",
        "TEMPORARY_FAILURE",
        "CONNECTION_RESET",
    }
)
NON_RETRYABLE_ERROR_CODES = frozenset(
    {
        "AUTH_FAILED",
        "PROVIDER_AUTH_FAILED",
        "HTTP_401",
        "HTTP_403",
        "MODEL_NOT_FOUND",
        "UNSUPPORTED_IMAGE",
        "UNSUPPORTED_TOOLS",
        "INVALID_REQUEST",
        "EVIDENCE_EXPIRED",
        "CANCELLED",
        "JOB_CANCELLED",
    }
)

# 数值越大越优先。应用可以覆盖，但建议保留层级顺序。
DEFAULT_PRIORITIES: dict[str, int] = {
    "event_l1": 500,
    "realtime_l1": 400,
    "l1": 400,
    "minute": 400,
    "l2": 300,
    "l3": 200,
    "history": 100,
    "cleanup": 0,
}


class QueueError(RuntimeError):
    """队列操作错误。"""


class IdempotencyConflictError(QueueError):
    """同一个幂等键被用于不同的任务定义。"""


class LeaseLostError(QueueError):
    """任务租约已丢失，禁止旧 Worker 覆盖结果。"""


class InvalidJobError(QueueError):
    """任务字段不满足队列约束。"""


def utc_now_ms() -> int:
    return int(time.time() * 1000)


def to_epoch_ms(value: int | float | datetime | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("datetime 必须带时区")
        return int(value.timestamp() * 1000)
    return int(value)


def _new_id(prefix: str = "job") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def build_idempotency_key(
    layer: str,
    camera_id: str | None,
    period_start_ms: int | None,
    period_end_ms: int | None,
    input_version: str | int = 1,
    prompt_version: str = "v1",
    schema_version: str = "v1",
) -> str:
    """按设计文档生成稳定幂等键。

    显式传入的 idempotency key 仍然优先；该函数用于 Scheduler、API 和 Worker 共享规则。
    """

    parts = (
        str(layer).strip().lower(),
        camera_id or "-",
        "-" if period_start_ms is None else str(int(period_start_ms)),
        "-" if period_end_ms is None else str(int(period_end_ms)),
        str(input_version),
        str(prompt_version),
        str(schema_version),
    )
    if any(not part for part in parts):
        raise ValueError("幂等键组成字段不能为空")
    return ":".join(parts)


def priority_for(
    layer: str,
    *,
    high_precision: bool = False,
    historical: bool = False,
    override: int | None = None,
) -> int:
    if override is not None:
        return int(override)
    normalized = str(layer).strip().lower()
    if high_precision or normalized in {"event_l1", "high_precision_l1"}:
        return DEFAULT_PRIORITIES["event_l1"]
    if historical:
        return DEFAULT_PRIORITIES["history"]
    return DEFAULT_PRIORITIES.get(normalized, DEFAULT_PRIORITIES["history"])


@dataclass(frozen=True)
class RetryPolicy:
    """指数退避策略。

    attempts 从 1 开始表示已经发出的请求次数。第一次失败使用 base_delay_ms，之后翻倍，
    并加入正向随机抖动；Retry-After 会取更大的值。
    """

    max_attempts: int = 3
    base_delay_ms: int = 30_000
    max_delay_ms: int = 15 * 60_000
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        if self.base_delay_ms < 0 or self.max_delay_ms < self.base_delay_ms:
            raise ValueError("退避时间范围无效")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio 必须在 [0, 1] 内")

    def delay_ms(
        self,
        attempt: int,
        *,
        retry_after_ms: int | None = None,
        random_fn: Callable[[], float] | None = None,
    ) -> int:
        attempt = max(1, int(attempt))
        exponential = min(self.max_delay_ms, self.base_delay_ms * (2 ** (attempt - 1)))
        rng = random.random if random_fn is None else random_fn
        jitter = int(exponential * self.jitter_ratio * max(0.0, min(1.0, float(rng()))))
        return max(int(retry_after_ms or 0), min(self.max_delay_ms, exponential + jitter))


@dataclass(frozen=True)
class JobSpec:
    layer: str
    idempotency_key: str
    camera_id: str | None = None
    period_start_ms: int | None = None
    period_end_ms: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    input_frame_ids: Sequence[str] = field(default_factory=tuple)
    priority: int = 0
    input_version: str | int = 1
    prompt_version: str = "v1"
    schema_version: str = "v1"
    max_attempts: int = 3
    available_at_ms: int | None = None
    deadline_at_ms: int | None = None

    def validate(self) -> None:
        if not self.layer or not self.layer.strip():
            raise InvalidJobError("layer 不能为空")
        if not self.idempotency_key or len(self.idempotency_key) > 512:
            raise InvalidJobError("idempotency_key 不能为空且不能超过 512 字符")
        if self.period_start_ms is not None and self.period_end_ms is not None:
            if int(self.period_end_ms) <= int(self.period_start_ms):
                raise InvalidJobError("任务时间区间必须是左闭右开且长度大于 0")
        if self.max_attempts < 1:
            raise InvalidJobError("max_attempts 必须大于 0")
        if self.deadline_at_ms is not None and self.available_at_ms is not None:
            if self.deadline_at_ms < self.available_at_ms:
                raise InvalidJobError("deadline 不能早于 available_at")


@dataclass
class JobRecord:
    id: str
    idempotency_key: str
    layer: str
    state: str = JobState.QUEUED.value
    camera_id: str | None = None
    period_start_ms: int | None = None
    period_end_ms: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    input_frame_ids: list[str] = field(default_factory=list)
    priority: int = 0
    input_version: str | int = 1
    prompt_version: str = "v1"
    schema_version: str = "v1"
    attempts: int = 0
    max_attempts: int = 3
    available_at_ms: int = 0
    deadline_at_ms: int | None = None
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at_ms: int | None = None
    heartbeat_at_ms: int | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    result_ref: Any = None
    error_code: str | None = None
    error_safe_message: str | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | Any) -> "JobRecord":
        def get(*names: str, default: Any = None) -> Any:
            for name in names:
                if isinstance(value, Mapping) and name in value:
                    return value[name]
                if hasattr(value, name):
                    return getattr(value, name)
            return default

        payload = get("payload", "payload_json", default={}) or {}
        frame_ids = get("input_frame_ids", "input_frame_ids_json", default=[]) or []
        return cls(
            id=str(get("id")),
            idempotency_key=str(get("idempotency_key", "dedup_key", default="")),
            layer=str(get("layer", "kind", default="")),
            state=str(get("state", "status", default=JobState.QUEUED.value)),
            camera_id=get("camera_id"),
            period_start_ms=get("period_start_ms", "start_at_ms"),
            period_end_ms=get("period_end_ms", "end_at_ms"),
            payload=dict(payload) if isinstance(payload, Mapping) else {},
            input_frame_ids=list(frame_ids) if isinstance(frame_ids, (list, tuple)) else [],
            priority=int(get("priority", default=0) or 0),
            input_version=get("input_version", default=1),
            prompt_version=str(get("prompt_version", default="v1")),
            schema_version=str(get("schema_version", default="v1")),
            attempts=int(get("attempts", "attempt_count", "failure_count", default=0) or 0),
            max_attempts=int(get("max_attempts", default=3) or 3),
            available_at_ms=int(get("available_at_ms", "not_before_ms", default=0) or 0),
            deadline_at_ms=get("deadline_at_ms", "deadline_at"),
            lease_owner=get("lease_owner"),
            lease_token=get("lease_token"),
            lease_expires_at_ms=get("lease_expires_at_ms", "lease_until_ms", "lease_until"),
            heartbeat_at_ms=get("heartbeat_at_ms"),
            started_at_ms=get("started_at_ms"),
            finished_at_ms=get("finished_at_ms"),
            result_ref=get("result_ref", "result_json"),
            error_code=get("error_code"),
            error_safe_message=get("error_safe_message", "error_message"),
            created_at_ms=int(get("created_at_ms", default=0) or 0),
            updated_at_ms=int(get("updated_at_ms", default=0) or 0),
        )

    def to_mapping(self) -> dict[str, Any]:
        """返回同时适配文档字段名和 repository 常见字段名的规范映射。"""

        return {
            "id": self.id,
            "idempotency_key": self.idempotency_key,
            "layer": self.layer,
            "state": self.state,
            "camera_id": self.camera_id,
            "period_start_ms": self.period_start_ms,
            "period_end_ms": self.period_end_ms,
            "payload_json": dict(self.payload),
            "input_frame_ids_json": list(self.input_frame_ids),
            "priority": self.priority,
            "input_version": self.input_version,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "available_at_ms": self.available_at_ms,
            "deadline_at_ms": self.deadline_at_ms,
            "lease_owner": self.lease_owner,
            "lease_token": self.lease_token,
            "lease_expires_at_ms": self.lease_expires_at_ms,
            "heartbeat_at_ms": self.heartbeat_at_ms,
            "started_at_ms": self.started_at_ms,
            "finished_at_ms": self.finished_at_ms,
            "finished_at": self.finished_at_ms,
            "result_ref": self.result_ref,
            "error_code": self.error_code,
            "error_safe_message": self.error_safe_message,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }


class JobRepository(Protocol):
    """TaskQueue 所需的最小 repository 接口。

    方法中的 session 可以是 SQLAlchemy Session，也可以是 None。实现可在 session 上使用 ORM
    Model；TaskQueue 不会在网络调用期间持有事务。
    """

    def get_by_idempotency(self, session: Any, idempotency_key: str) -> JobRecord | None: ...

    def insert(self, session: Any, job: JobRecord) -> JobRecord: ...

    def get(self, session: Any, job_id: str) -> JobRecord | None: ...

    def claim_next(self, session: Any, now_ms: int, owner: str, lease_ms: int) -> JobRecord | None: ...

    def update(
        self,
        session: Any,
        job_id: str,
        values: Mapping[str, Any],
        expected_lease_token: str | None = None,
    ) -> JobRecord | None: ...

    def list_expired(self, session: Any, now_ms: int, limit: int) -> list[JobRecord]: ...

    def promote_due_retries(self, session: Any, now_ms: int, limit: int) -> int: ...


class InMemoryJobRepository:
    """线程安全的参考 repository，供测试和单进程开发使用。"""

    def __init__(self) -> None:
        import threading

        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._keys: dict[str, str] = {}

    def _copy(self, job: JobRecord | None) -> JobRecord | None:
        return None if job is None else replace(job, payload=dict(job.payload), input_frame_ids=list(job.input_frame_ids))

    def get_by_idempotency(self, session: Any, idempotency_key: str) -> JobRecord | None:
        with self._lock:
            return self._copy(self._jobs.get(self._keys.get(idempotency_key, "")))

    def insert(self, session: Any, job: JobRecord) -> JobRecord:
        with self._lock:
            existing_id = self._keys.get(job.idempotency_key)
            if existing_id:
                return self._copy(self._jobs[existing_id])  # type: ignore[return-value]
            self._jobs[job.id] = self._copy(job)  # type: ignore[assignment]
            self._keys[job.idempotency_key] = job.id
            return self._copy(job)  # type: ignore[return-value]

    def get(self, session: Any, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._copy(self._jobs.get(job_id))

    def claim_next(self, session: Any, now_ms: int, owner: str, lease_ms: int) -> JobRecord | None:
        with self._lock:
            self.promote_due_retries(session, now_ms, limit=1000)
            candidates = [
                job
                for job in self._jobs.values()
                if job.state == JobState.QUEUED.value and job.available_at_ms <= now_ms
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda item: (-item.priority, item.available_at_ms, item.created_at_ms, item.id))
            job = candidates[0]
            job.state = JobState.LEASED.value
            job.lease_owner = owner
            job.lease_token = secrets.token_urlsafe(24)
            job.lease_expires_at_ms = now_ms + max(1, lease_ms)
            job.heartbeat_at_ms = now_ms
            job.started_at_ms = job.started_at_ms or now_ms
            job.attempts += 1
            job.updated_at_ms = now_ms
            return self._copy(job)

    def update(
        self,
        session: Any,
        job_id: str,
        values: Mapping[str, Any],
        expected_lease_token: str | None = None,
    ) -> JobRecord | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if expected_lease_token is not None and job.lease_token != expected_lease_token:
                return None
            for key, value in values.items():
                normalized = {
                    "payload_json": "payload",
                    "input_frame_ids_json": "input_frame_ids",
                    "attempt_count": "attempts",
                    "lease_until_ms": "lease_expires_at_ms",
                    "not_before_ms": "available_at_ms",
                    "error_message": "error_safe_message",
                }.get(key, key)
                if hasattr(job, normalized):
                    setattr(job, normalized, value)
            return self._copy(job)

    def list_expired(self, session: Any, now_ms: int, limit: int) -> list[JobRecord]:
        with self._lock:
            jobs = [
                job
                for job in self._jobs.values()
                if job.state in ACTIVE_STATES
                and (job.lease_expires_at_ms is None or job.lease_expires_at_ms <= now_ms)
            ]
            jobs.sort(key=lambda item: (item.lease_expires_at_ms or 0, item.created_at_ms, item.id))
            return [self._copy(job) for job in jobs[: max(0, limit)]]  # type: ignore[misc]

    def promote_due_retries(self, session: Any, now_ms: int, limit: int) -> int:
        with self._lock:
            count = 0
            for job in sorted(self._jobs.values(), key=lambda item: item.available_at_ms):
                if count >= max(0, limit):
                    break
                if job.state == JobState.RETRY_WAIT.value and job.available_at_ms <= now_ms:
                    job.state = JobState.QUEUED.value
                    job.updated_at_ms = now_ms
                    count += 1
            return count

    def all(self) -> list[JobRecord]:
        with self._lock:
            return [self._copy(item) for item in self._jobs.values()]  # type: ignore[misc]


# SQLAlchemy ORM/Table reference implementation. Existing applications may use their own repository
# and do not need to use this table factory.
def define_job_table(metadata: MetaData, name: str = "ai_job") -> Table:
    return Table(
        name,
        metadata,
        Column("id", String(128), primary_key=True),
        Column("idempotency_key", String(512), nullable=False, unique=True, index=True),
        Column("layer", String(32), nullable=False, index=True),
        Column("state", String(32), nullable=False, index=True),
        Column("camera_id", String(128), nullable=True, index=True),
        Column("period_start_ms", Integer, nullable=True),
        Column("period_end_ms", Integer, nullable=True),
        Column("payload_json", JSON, nullable=False, default=dict),
        Column("input_frame_ids_json", JSON, nullable=False, default=list),
        Column("priority", Integer, nullable=False, default=0, index=True),
        Column("input_version", String(64), nullable=False, default="1"),
        Column("prompt_version", String(128), nullable=False, default="v1"),
        Column("schema_version", String(128), nullable=False, default="v1"),
        Column("attempts", Integer, nullable=False, default=0),
        Column("max_attempts", Integer, nullable=False, default=3),
        Column("available_at_ms", Integer, nullable=False, index=True),
        Column("deadline_at_ms", Integer, nullable=True),
        Column("lease_owner", String(256), nullable=True, index=True),
        Column("lease_token", String(256), nullable=True),
        Column("lease_expires_at_ms", Integer, nullable=True, index=True),
        Column("heartbeat_at_ms", Integer, nullable=True),
        Column("started_at_ms", Integer, nullable=True),
        Column("finished_at_ms", Integer, nullable=True),
        Column("result_ref", JSON, nullable=True),
        Column("error_code", String(128), nullable=True),
        Column("error_safe_message", String(512), nullable=True),
        Column("created_at_ms", Integer, nullable=False, index=True),
        Column("updated_at_ms", Integer, nullable=False),
    )


class SqlAlchemyJobRepository:
    """适配 ORM model 或 Table 的通用 repository。

    ORM model 至少应包含 define_job_table 中的字段；旧项目若使用 payload_json/attempt_count 等
    同义字段，可以在自己的 repository 中适配，避免修改既有数据库模型。
    """

    def __init__(self, model: Any) -> None:
        self.model = model
        self.table = model if isinstance(model, Table) else getattr(model, "__table__", None)
        if self.table is None:
            raise TypeError("model 必须是 SQLAlchemy ORM model 或 Table")
        self.columns = {column.name for column in self.table.columns}
        self.is_table = isinstance(model, Table)

    def _col(self, name: str) -> Any:
        aliases = {
            "payload_json": ("payload_json", "payload"),
            "input_frame_ids_json": ("input_frame_ids_json", "input_frame_ids"),
            "attempts": ("attempts", "attempt_count", "failure_count"),
            "available_at_ms": ("available_at_ms", "not_before_ms"),
            "lease_expires_at_ms": ("lease_expires_at_ms", "lease_until_ms", "lease_until"),
            "error_safe_message": ("error_safe_message", "error_message"),
        }
        for candidate in aliases.get(name, (name,)):
            if candidate in self.columns:
                return self.table.c[candidate] if self.is_table else getattr(self.model, candidate)
        return None

    def _field_name(self, canonical: str) -> str | None:
        aliases = {
            "payload_json": ("payload_json", "payload"),
            "input_frame_ids_json": ("input_frame_ids_json", "input_frame_ids"),
            "attempts": ("attempts", "attempt_count", "failure_count"),
            "available_at_ms": ("available_at_ms", "not_before_ms"),
            "lease_expires_at_ms": ("lease_expires_at_ms", "lease_until_ms", "lease_until"),
            "error_safe_message": ("error_safe_message", "error_message"),
        }
        for candidate in aliases.get(canonical, (canonical,)):
            if candidate in self.columns:
                return candidate
        return None

    def _query(self, session: Any, *criteria: Any, order_by: Sequence[Any] = ()) -> Any:
        statement = select(self.model).where(*criteria)
        if order_by:
            statement = statement.order_by(*order_by)
        return session.execute(statement)

    def _one(self, session: Any, *criteria: Any) -> Any:
        result = self._query(session, *criteria)
        return result.mappings().first() if self.is_table else result.scalars().first()

    def _to_record(self, value: Any) -> JobRecord | None:
        if value is None:
            return None
        record = JobRecord.from_mapping(value)
        if "input_ref_json" in self.columns and "payload_json" not in self.columns and "payload" not in self.columns:
            source = value.get("input_ref_json", {}) if isinstance(value, Mapping) else getattr(value, "input_ref_json", {})
            if isinstance(source, Mapping):
                record.payload = dict(source)
        return record

    def _values(self, mapping: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in mapping.items():
            actual = self._field_name(key) or (key if key in self.columns else None)
            if actual is not None:
                result[actual] = value
        return result

    def get_by_idempotency(self, session: Any, idempotency_key: str) -> JobRecord | None:
        key_col = self._col("idempotency_key")
        if key_col is None:
            raise InvalidJobError("Job model 缺少 idempotency_key")
        return self._to_record(self._one(session, key_col == idempotency_key))

    def get(self, session: Any, job_id: str) -> JobRecord | None:
        id_col = self._col("id")
        return self._to_record(self._one(session, id_col == job_id))

    def insert(self, session: Any, job: JobRecord) -> JobRecord:
        values = self._values(job.to_mapping())
        # This model stores immutable task parameters in input_ref_json. Keep the
        # TaskQueue payload contract intact and map it only at this adapter boundary.
        if "input_ref_json" in self.columns and "payload_json" not in self.columns and "payload" not in self.columns:
            values["input_ref_json"] = dict(job.payload)
        try:
            if self.is_table:
                session.execute(self.model.insert().values(**values))
            else:
                session.add(self.model(**values))
            session.flush()
        except IntegrityError:
            # 竞争性重复入队时由上层再次读取唯一键记录。
            session.rollback()
            existing = self.get_by_idempotency(session, job.idempotency_key)
            if existing is not None:
                return existing
            raise
        return self.get(session, job.id) or job

    def claim_next(self, session: Any, now_ms: int, owner: str, lease_ms: int) -> JobRecord | None:
        self.promote_due_retries(session, now_ms, limit=1000)
        state_col = self._col("state")
        available_col = self._col("available_at_ms")
        priority_col = self._col("priority")
        created_col = self._col("created_at_ms")
        id_col = self._col("id")
        if any(value is None for value in (state_col, available_col, priority_col, id_col)):
            raise InvalidJobError("Job model 缺少领取任务所需字段")
        for _ in range(3):
            statement = (
                select(self.model)
                .where(state_col == JobState.QUEUED.value, available_col <= now_ms)
                .order_by(priority_col.desc(), available_col.asc(), created_col.asc(), id_col.asc())
                .limit(1)
            )
            result = session.execute(statement)
            candidate = result.mappings().first() if self.is_table else result.scalars().first()
            if candidate is None:
                return None
            candidate_id = JobRecord.from_mapping(candidate).id
            token = secrets.token_urlsafe(24)
            values = self._values(
                {
                    "state": JobState.LEASED.value,
                    "lease_owner": owner,
                    "lease_token": token,
                    "lease_expires_at_ms": now_ms + max(1, lease_ms),
                    "heartbeat_at_ms": now_ms,
                    "started_at_ms": now_ms,
                    "attempts": (self._col("attempts") + 1) if self._col("attempts") is not None else 1,
                    "updated_at_ms": now_ms,
                }
            )
            guarded = update(self.model).where(
                id_col == candidate_id,
                state_col == JobState.QUEUED.value,
                available_col <= now_ms,
            )
            result = session.execute(guarded.values(**values))
            if result.rowcount == 1:
                return self.get(session, candidate_id)
        return None

    def update(
        self,
        session: Any,
        job_id: str,
        values: Mapping[str, Any],
        expected_lease_token: str | None = None,
    ) -> JobRecord | None:
        id_col = self._col("id")
        criteria: list[Any] = [id_col == job_id]
        token_col = self._col("lease_token")
        if expected_lease_token is not None and token_col is not None:
            criteria.append(token_col == expected_lease_token)
        statement = update(self.model).where(*criteria).values(**self._values(values))
        result = session.execute(statement)
        if result.rowcount == 0:
            return None
        return self.get(session, job_id)

    def list_expired(self, session: Any, now_ms: int, limit: int) -> list[JobRecord]:
        state_col = self._col("state")
        expires_col = self._col("lease_expires_at_ms")
        created_col = self._col("created_at_ms")
        statement = (
            select(self.model)
            .where(
                state_col.in_(tuple(ACTIVE_STATES)),
                or_(expires_col.is_(None), expires_col <= now_ms),
            )
            .order_by(expires_col.asc(), created_col.asc())
            .limit(max(0, limit))
        )
        result = session.execute(statement)
        rows = result.mappings().all() if self.is_table else result.scalars().all()
        return [JobRecord.from_mapping(row) for row in rows]

    def promote_due_retries(self, session: Any, now_ms: int, limit: int) -> int:
        state_col = self._col("state")
        available_col = self._col("available_at_ms")
        id_col = self._col("id")
        statement = (
            select(id_col)
            .where(state_col == JobState.RETRY_WAIT.value, available_col <= now_ms)
            .order_by(available_col.asc())
            .limit(max(0, limit))
        )
        ids = [row[0] for row in session.execute(statement).all()]
        if not ids:
            return 0
        result = session.execute(
            update(self.model).where(id_col.in_(ids), state_col == JobState.RETRY_WAIT.value).values(
                **self._values({"state": JobState.QUEUED.value, "updated_at_ms": now_ms})
            )
        )
        return int(result.rowcount or 0)


def _safe_message(message: str | None) -> str | None:
    if message is None:
        return None
    text = " ".join(str(message).split())
    return text[:512] or None


def is_retryable_error(error_code: str | None, status_code: int | None = None) -> bool:
    if status_code is not None:
        if int(status_code) == 429 or int(status_code) >= 500:
            return True
        if int(status_code) in {401, 403, 400, 404}:
            return False
    normalized = str(error_code or "").strip().upper()
    if normalized in NON_RETRYABLE_ERROR_CODES:
        return False
    if normalized in RETRYABLE_ERROR_CODES:
        return True
    return any(token in normalized for token in ("TIMEOUT", "TEMPORARY", "UNAVAILABLE", "RATE_LIMIT"))


def _payload_equal(a: JobRecord, spec: JobSpec) -> bool:
    return (
        a.layer == spec.layer
        and a.camera_id == spec.camera_id
        and a.period_start_ms == spec.period_start_ms
        and a.period_end_ms == spec.period_end_ms
        and str(a.input_version) == str(spec.input_version)
        and a.prompt_version == spec.prompt_version
        and a.schema_version == spec.schema_version
        and list(a.input_frame_ids) == list(spec.input_frame_ids)
        and json.dumps(a.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        == json.dumps(dict(spec.payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


class TaskQueue:
    """事务型持久任务队列服务。"""

    def __init__(
        self,
        repository: JobRepository,
        *,
        session_factory: Callable[[], Any] | None = None,
        clock: Callable[[], int] = utc_now_ms,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.session_factory = session_factory
        self.clock = clock
        self.retry_policy = retry_policy or RetryPolicy()

    def _run(self, operation: Callable[[Any], Any], session: Any = None) -> Any:
        if session is not None:
            return operation(session)
        if self.session_factory is None:
            return operation(None)
        current = self.session_factory()
        try:
            result = operation(current)
            current.commit()
            return result
        except Exception:
            current.rollback()
            raise
        finally:
            current.close()

    def enqueue(
        self,
        *,
        layer: str,
        idempotency_key: str,
        camera_id: str | None = None,
        period_start_ms: int | datetime | None = None,
        period_end_ms: int | datetime | None = None,
        payload: Mapping[str, Any] | None = None,
        input_frame_ids: Sequence[str] = (),
        priority: int | None = None,
        input_version: str | int = 1,
        prompt_version: str = "v1",
        schema_version: str = "v1",
        max_attempts: int | None = None,
        available_at_ms: int | datetime | None = None,
        deadline_at_ms: int | datetime | None = None,
        high_precision: bool = False,
        historical: bool = False,
        session: Any = None,
    ) -> JobRecord:
        now = self.clock()
        spec = JobSpec(
            layer=layer,
            idempotency_key=idempotency_key,
            camera_id=camera_id,
            period_start_ms=to_epoch_ms(period_start_ms),
            period_end_ms=to_epoch_ms(period_end_ms),
            payload=dict(payload or {}),
            input_frame_ids=tuple(input_frame_ids),
            priority=priority_for(layer, high_precision=high_precision, historical=historical, override=priority),
            input_version=input_version,
            prompt_version=prompt_version,
            schema_version=schema_version,
            max_attempts=max_attempts or self.retry_policy.max_attempts,
            available_at_ms=to_epoch_ms(available_at_ms),
            deadline_at_ms=to_epoch_ms(deadline_at_ms),
        )
        spec.validate()
        row = JobRecord(
            id=_new_id(),
            idempotency_key=spec.idempotency_key,
            layer=spec.layer,
            camera_id=spec.camera_id,
            period_start_ms=spec.period_start_ms,
            period_end_ms=spec.period_end_ms,
            payload=dict(spec.payload),
            input_frame_ids=list(dict.fromkeys(str(item) for item in spec.input_frame_ids)),
            priority=spec.priority,
            input_version=spec.input_version,
            prompt_version=spec.prompt_version,
            schema_version=spec.schema_version,
            max_attempts=spec.max_attempts,
            available_at_ms=spec.available_at_ms if spec.available_at_ms is not None else now,
            deadline_at_ms=spec.deadline_at_ms,
            created_at_ms=now,
            updated_at_ms=now,
        )

        def operation(db: Any) -> JobRecord:
            existing = self.repository.get_by_idempotency(db, spec.idempotency_key)
            if existing is not None:
                if not _payload_equal(existing, spec):
                    raise IdempotencyConflictError(
                        f"幂等键 {spec.idempotency_key!r} 已绑定到不同任务定义"
                    )
                return existing
            inserted = self.repository.insert(db, row)
            if not isinstance(inserted, JobRecord):
                inserted = JobRecord.from_mapping(inserted)
            # repository 可能因并发唯一键返回另一个已有记录。
            if not _payload_equal(inserted, spec):
                raise IdempotencyConflictError(
                    f"幂等键 {spec.idempotency_key!r} 已绑定到不同任务定义"
                )
            return inserted

        return self._run(operation, session)

    def claim(
        self,
        owner: str,
        *,
        lease_ms: int = 60_000,
        now_ms: int | None = None,
        session: Any = None,
    ) -> JobRecord | None:
        if not owner or not owner.strip():
            raise ValueError("owner 不能为空")
        if lease_ms <= 0:
            raise ValueError("lease_ms 必须大于 0")
        current = self.clock() if now_ms is None else int(now_ms)

        def operation(db: Any) -> JobRecord | None:
            result = self.repository.claim_next(db, current, owner, lease_ms)
            return result if result is None or isinstance(result, JobRecord) else JobRecord.from_mapping(result)

        return self._run(operation, session)

    def mark_running(
        self,
        job: JobRecord | str,
        *,
        lease_token: str | None = None,
        session: Any = None,
    ) -> JobRecord:
        job_id, token = self._job_identity(job, lease_token)

        def operation(db: Any) -> JobRecord:
            current = self.repository.get(db, job_id)
            self._assert_lease(current, token)
            result = self.repository.update(
                db,
                job_id,
                {"state": JobState.RUNNING.value, "updated_at_ms": self.clock()},
                expected_lease_token=token,
            )
            if result is None:
                raise LeaseLostError("任务租约已失效")
            return result if isinstance(result, JobRecord) else JobRecord.from_mapping(result)

        return self._run(operation, session)

    def heartbeat(
        self,
        job: JobRecord | str,
        *,
        lease_token: str | None = None,
        lease_ms: int = 60_000,
        now_ms: int | None = None,
        session: Any = None,
    ) -> JobRecord:
        if lease_ms <= 0:
            raise ValueError("lease_ms 必须大于 0")
        job_id, token = self._job_identity(job, lease_token)
        current_time = self.clock() if now_ms is None else int(now_ms)

        def operation(db: Any) -> JobRecord:
            current = self.repository.get(db, job_id)
            self._assert_lease(current, token, now_ms=current_time)
            result = self.repository.update(
                db,
                job_id,
                {
                    "lease_expires_at_ms": current_time + lease_ms,
                    "heartbeat_at_ms": current_time,
                    "updated_at_ms": current_time,
                },
                expected_lease_token=token,
            )
            if result is None:
                raise LeaseLostError("任务租约已失效")
            return result if isinstance(result, JobRecord) else JobRecord.from_mapping(result)

        return self._run(operation, session)

    def complete(
        self,
        job: JobRecord | str,
        *,
        result_ref: Any = None,
        session: Any = None,
    ) -> JobRecord:
        job_id, token = self._job_identity(job, None)

        def operation(db: Any) -> JobRecord:
            current = self.repository.get(db, job_id)
            if current is None:
                raise QueueError("任务不存在")
            if current.state == JobState.SUCCEEDED.value:
                return current
            self._assert_lease(current, token)
            now = self.clock()
            result = self.repository.update(
                db,
                job_id,
                {
                    "state": JobState.SUCCEEDED.value,
                    "finished_at_ms": now,
                    "updated_at_ms": now,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at_ms": None,
                    "heartbeat_at_ms": None,
                    "result_ref": result_ref,
                    "error_code": None,
                    "error_safe_message": None,
                },
                expected_lease_token=token,
            )
            if result is None:
                raise LeaseLostError("任务租约已失效")
            return result if isinstance(result, JobRecord) else JobRecord.from_mapping(result)

        return self._run(operation, session)

    def fail(
        self,
        job: JobRecord | str,
        *,
        error_code: str,
        safe_message: str | None = None,
        retryable: bool | None = None,
        retry_after_ms: int | None = None,
        status_code: int | None = None,
        needs_review: bool = False,
        session: Any = None,
    ) -> JobRecord:
        job_id, token = self._job_identity(job, None)
        normalized_code = str(error_code or "UNKNOWN_ERROR").strip().upper()[:128]

        def operation(db: Any) -> JobRecord:
            current = self.repository.get(db, job_id)
            if current is None:
                raise QueueError("任务不存在")
            if current.state in TERMINAL_STATES:
                return current
            self._assert_lease(current, token)
            now = self.clock()
            retry = is_retryable_error(normalized_code, status_code) if retryable is None else bool(retryable)
            if normalized_code in NON_RETRYABLE_ERROR_CODES:
                retry = False
            if needs_review:
                retry = False
            error_code_value = normalized_code
            if current.deadline_at_ms is not None and now >= current.deadline_at_ms:
                retry = False
                error_code_value = "DEADLINE_EXCEEDED"
            can_retry = retry and current.attempts < current.max_attempts
            if can_retry:
                delay = self.retry_policy.delay_ms(current.attempts, retry_after_ms=retry_after_ms)
                state = JobState.RETRY_WAIT.value
                available = now + delay
                finished = None
            else:
                delay = 0
                available = current.available_at_ms
                state = JobState.NEEDS_REVIEW.value if needs_review else JobState.FAILED.value
                finished = now
            values = {
                "state": state,
                "available_at_ms": available,
                "finished_at_ms": finished,
                "updated_at_ms": now,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at_ms": None,
                "heartbeat_at_ms": None,
                "error_code": error_code_value,
                "error_safe_message": _safe_message(safe_message),
            }
            result = self.repository.update(db, job_id, values, expected_lease_token=token)
            if result is None:
                raise LeaseLostError("任务租约已失效")
            return result if isinstance(result, JobRecord) else JobRecord.from_mapping(result)

        return self._run(operation, session)

    def recover_expired(
        self,
        *,
        now_ms: int | None = None,
        limit: int = 100,
        session: Any = None,
    ) -> list[JobRecord]:
        current_time = self.clock() if now_ms is None else int(now_ms)
        if limit <= 0:
            return []

        def operation(db: Any) -> list[JobRecord]:
            expired = self.repository.list_expired(db, current_time, limit)
            recovered: list[JobRecord] = []
            for old in expired:
                # 用旧 token 做条件更新，避免旧 Worker 恰好心跳成功时被恢复逻辑覆盖。
                if old.deadline_at_ms is not None and current_time >= old.deadline_at_ms:
                    values = {
                        "state": JobState.FAILED.value,
                        "finished_at_ms": current_time,
                        "error_code": "DEADLINE_EXCEEDED",
                        "error_safe_message": "任务超过截止时间",
                    }
                elif old.attempts < old.max_attempts:
                    values = {
                        "state": JobState.RETRY_WAIT.value,
                        "available_at_ms": current_time
                        + self.retry_policy.delay_ms(old.attempts, random_fn=lambda: 0.0),
                        "error_code": "LEASE_EXPIRED",
                        "error_safe_message": "Worker 租约超时，任务将恢复",
                    }
                else:
                    values = {
                        "state": JobState.FAILED.value,
                        "finished_at_ms": current_time,
                        "error_code": "LEASE_EXPIRED_MAX_ATTEMPTS",
                        "error_safe_message": "Worker 租约超时且已达到最大尝试次数",
                    }
                values.update(
                    {
                        "updated_at_ms": current_time,
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_expires_at_ms": None,
                        "heartbeat_at_ms": None,
                    }
                )
                result = self.repository.update(
                    db, old.id, values, expected_lease_token=old.lease_token
                )
                if result is not None:
                    recovered.append(result if isinstance(result, JobRecord) else JobRecord.from_mapping(result))
            return recovered

        return self._run(operation, session)

    def promote_due_retries(
        self, *, now_ms: int | None = None, limit: int = 100, session: Any = None
    ) -> int:
        current_time = self.clock() if now_ms is None else int(now_ms)
        return int(
            self._run(
                lambda db: self.repository.promote_due_retries(db, current_time, max(0, limit)), session
            )
        )

    def cancel(self, job: JobRecord | str, *, reason: str = "用户取消", session: Any = None) -> JobRecord:
        job_id, token = self._job_identity(job, None)

        def operation(db: Any) -> JobRecord:
            current = self.repository.get(db, job_id)
            if current is None:
                raise QueueError("任务不存在")
            if current.state in TERMINAL_STATES:
                return current
            if current.state in ACTIVE_STATES:
                self._assert_lease(current, token)
            now = self.clock()
            result = self.repository.update(
                db,
                job_id,
                {
                    "state": JobState.CANCELLED.value,
                    "finished_at_ms": now,
                    "updated_at_ms": now,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at_ms": None,
                    "heartbeat_at_ms": None,
                    "error_code": "CANCELLED",
                    "error_safe_message": _safe_message(reason),
                },
                expected_lease_token=token if current.state in ACTIVE_STATES else None,
            )
            if result is None:
                raise LeaseLostError("任务租约已失效")
            return result if isinstance(result, JobRecord) else JobRecord.from_mapping(result)

        return self._run(operation, session)

    def _job_identity(self, job: JobRecord | str, lease_token: str | None) -> tuple[str, str]:
        if isinstance(job, JobRecord):
            return job.id, lease_token or job.lease_token or ""
        if not lease_token:
            raise LeaseLostError("使用 job_id 操作时必须显式传入 lease_token")
        return str(job), lease_token

    @staticmethod
    def _assert_lease(
        job: JobRecord | None,
        token: str,
        *,
        now_ms: int | None = None,
    ) -> None:
        if job is None:
            raise QueueError("任务不存在")
        if job.state not in ACTIVE_STATES:
            raise LeaseLostError(f"任务当前状态为 {job.state}，不能使用该租约")
        if not token or job.lease_token != token:
            raise LeaseLostError("任务租约 token 不匹配")
        if now_ms is not None and job.lease_expires_at_ms is not None and job.lease_expires_at_ms <= now_ms:
            raise LeaseLostError("任务租约已过期")


# 兼容主 agent 可能采用的命名。
PersistentTaskQueue = TaskQueue
Queue = TaskQueue
SqlAlchemyQueueRepository = SqlAlchemyJobRepository

__all__ = [
    "ACTIVE_STATES",
    "DEFAULT_PRIORITIES",
    "TERMINAL_STATES",
    "IdempotencyConflictError",
    "InMemoryJobRepository",
    "InvalidJobError",
    "JobRecord",
    "JobRepository",
    "JobSpec",
    "JobState",
    "LeaseLostError",
    "PersistentTaskQueue",
    "Queue",
    "QueueError",
    "RetryPolicy",
    "SqlAlchemyJobRepository",
    "SqlAlchemyQueueRepository",
    "TaskQueue",
    "build_idempotency_key",
    "define_job_table",
    "is_retryable_error",
    "priority_for",
    "to_epoch_ms",
    "utc_now_ms",
]
