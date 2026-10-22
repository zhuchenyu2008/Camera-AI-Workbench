from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from ..models import AIJob, Base, utc_now_ms

ModelT = TypeVar("ModelT", bound=Base)


class Repository(Generic[ModelT]):
    """Small SQLAlchemy repository base; transaction ownership stays with caller."""

    def __init__(self, model: type[ModelT]) -> None:
        self.model = model

    def get(self, db: Session, identity: Any) -> ModelT | None:
        return db.get(self.model, identity)

    def add(self, db: Session, instance: ModelT, *, flush: bool = True) -> ModelT:
        db.add(instance)
        if flush:
            db.flush()
        return instance

    def delete(self, db: Session, instance: ModelT, *, flush: bool = True) -> None:
        db.delete(instance)
        if flush:
            db.flush()

    def statement(
        self,
        *,
        filters: Iterable[ColumnElement[bool]] = (),
        order_by: Sequence[Any] = (),
        offset: int = 0,
        limit: int | None = None,
    ) -> Select[tuple[ModelT]]:
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("offset/limit 不得为负数")
        query = select(self.model).where(*filters)
        if order_by:
            query = query.order_by(*order_by)
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return query

    def list(
        self,
        db: Session,
        *,
        filters: Iterable[ColumnElement[bool]] = (),
        order_by: Sequence[Any] = (),
        offset: int = 0,
        limit: int = 50,
    ) -> list[ModelT]:
        if limit > 200:
            raise ValueError("单页记录不能超过 200 条")
        return list(db.scalars(self.statement(filters=filters, order_by=order_by, offset=offset, limit=limit)))

    def count(self, db: Session, *, filters: Iterable[ColumnElement[bool]] = ()) -> int:
        query = select(func.count()).select_from(self.model).where(*filters)
        return int(db.scalar(query) or 0)


class AIJobRepository(Repository[AIJob]):
    """Idempotent enqueue and short-transaction lease operations for AI jobs."""

    def __init__(self) -> None:
        super().__init__(AIJob)

    def get_by_idempotency_key(self, db: Session, key: str) -> AIJob | None:
        return db.scalar(select(AIJob).where(AIJob.idempotency_key == key))

    def enqueue_once(self, db: Session, job: AIJob) -> tuple[AIJob, bool]:
        existing = self.get_by_idempotency_key(db, job.idempotency_key)
        if existing is not None:
            return existing, False
        db.add(job)
        # The unique index is the final arbiter in concurrent insert races;
        # caller can rollback and reload on IntegrityError.
        db.flush()
        return job, True

    def claim_next(
        self,
        db: Session,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        now_ms: int | None = None,
    ) -> AIJob | None:
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("worker_id 必填且 lease_seconds 必须为正数")
        now = now_ms if now_ms is not None else utc_now_ms()
        candidate = db.scalar(
            select(AIJob)
            .where(AIJob.state == "queued", AIJob.available_at_ms <= now)
            .order_by(AIJob.priority.desc(), AIJob.created_at_ms.asc(), AIJob.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if candidate is None:
            return None
        result = db.execute(
            update(AIJob)
            .where(AIJob.id == candidate.id, AIJob.state == "queued", AIJob.available_at_ms <= now)
            .values(
                state="leased",
                lease_owner=worker_id,
                lease_expires_at_ms=now + lease_seconds * 1000,
                heartbeat_at_ms=now,
                updated_at_ms=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.expire(candidate)
            return None
        db.refresh(candidate)
        return candidate

    def heartbeat(
        self,
        db: Session,
        *,
        job_id: str,
        worker_id: str,
        lease_seconds: int = 60,
        now_ms: int | None = None,
    ) -> bool:
        now = now_ms if now_ms is not None else utc_now_ms()
        result = db.execute(
            update(AIJob)
            .where(
                AIJob.id == job_id,
                AIJob.lease_owner == worker_id,
                AIJob.state.in_(("leased", "running")),
            )
            .values(
                heartbeat_at_ms=now,
                lease_expires_at_ms=now + lease_seconds * 1000,
                updated_at_ms=now,
            )
        )
        if result.rowcount:
            db.expire_all()
            return True
        return False


__all__ = ["AIJobRepository", "Repository"]
