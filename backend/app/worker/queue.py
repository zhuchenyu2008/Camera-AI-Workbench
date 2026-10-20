from __future__ import annotations

import random
import secrets
import time

from sqlalchemy import case, or_, select, update

from app.errors import AppError
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.schemas import Progress
from app.timeutils import iso

TERMINAL = {"succeeded", "partial", "failed", "expired", "cancelled"}
IMAGE_KINDS = {
    "sparse_analysis",
    "dense_analysis",
    "dense_backfill",
    "manual_visual",
    "temporal_local_observation",
    "temporal_reobserve",
    "temporal_review",
    "temporal_finalize",
}
AI_KINDS = IMAGE_KINDS | {
    "provider_test",
    "summarize_hour",
    "summarize_day",
    "manual_records",
    "entity_discovery",
}


def enqueue(
    conn,
    now: int,
    kind: str,
    payload: dict,
    dedup_key: str,
    *,
    deadline: int | None = None,
    delay: int = 0,
    dependencies: list[str] | None = None,
    total: int | None = None,
    unit: str = "step",
) -> dict:
    existing = conn.execute(select(s.jobs).where(s.jobs.c.dedup_key == dedup_key)).mappings().first()
    if existing:
        return dict(existing)
    priority = (
        40
        if kind == "entity_discovery"
        else 0
        if kind in {"cleanup_assets", "apply_settings", "project_events", "exchange_rate_refresh"}
        else 10
        if kind in IMAGE_KINDS
        else 20
        if kind != "summarize_day"
        else 30
    )
    row = dict(
        id=new_id("job"),
        created_at_ms=now,
        updated_at_ms=now,
        kind=kind,
        dedup_key=dedup_key,
        payload_json=payload,
        state="queued",
        queue_reason="dependency" if dependencies else "scheduled",
        phase="queued",
        progress_json=Progress(total_units=total, unit=unit).model_dump(),
        priority=priority,
        not_before_ms=now + delay,
        deadline_at_ms=deadline,
        lease_owner=None,
        lease_token=None,
        lease_until_ms=None,
        cancel_requested=False,
        attempt_count=0,
        started_at_ms=None,
        finished_at_ms=None,
        result_json={},
        error_code=None,
        error_json=None,
    )
    conn.execute(s.jobs.insert().values(**row))
    for parent in dependencies or []:
        one(conn, s.jobs, parent)
        # 新任务的 ID 尚未被任何旧任务引用，因此这里不可能形成环。
        conn.execute(
            s.job_dependencies.insert().values(job_id=row["id"], depends_on_id=parent, accept_partial=True)
        )
    return row


def safe_error_details(details: dict) -> dict:
    """Persist only known, bounded provider/worker diagnostics, never arbitrary exception data."""
    safe = {}
    status = details.get("provider_http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        safe["provider_http_status"] = status
    for key, limit in (
        ("provider_request_id", 160),
        ("provider_error_code", 100),
        ("provider_error_type", 100),
        ("provider_error_message", 240),
        ("provider_transport_error", 100),
        ("worker_error_type", 100),
    ):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            safe[key] = value.strip()[:limit]
    return safe


def job_dto(row: dict) -> dict:
    return dict(
        id=row["id"],
        kind=row["kind"],
        job_state=row["state"],
        queue_reason=row["queue_reason"] if row["state"] == "queued" else None,
        phase=row["phase"],
        created_at=iso(row["created_at_ms"]),
        started_at=iso(row["started_at_ms"]),
        finished_at=iso(row["finished_at_ms"]),
        progress=row["progress_json"],
        attempts=row["attempt_count"],
        cancel_requested=row["cancel_requested"],
        deadline_at=iso(row["deadline_at_ms"]),
        error=row["error_json"],
        result_links=row["result_json"].get("links", []),
    )


class Queue:
    def __init__(self, workbench):
        self.w = workbench

    def _sync_temporal_terminal(self, conn, job: dict, state: str, now: int, error_code: str | None = None):
        """Keep temporal run rows aligned when a job ends before its executor runs."""
        kind = job["kind"]
        payload = job.get("payload_json") or {}
        error = {"code": error_code} if error_code else None
        if kind in {"temporal_local_observation", "temporal_reobserve"}:
            temporal_state = state if state in {"queued", "running", "succeeded", "partial", "failed", "expired", "cancelled"} else "failed"
            conn.execute(
                update(s.local_observation_runs)
                .where(s.local_observation_runs.c.job_id == job["id"])
                .values(
                    status=temporal_state,
                    error_json=error,
                    updated_at_ms=now,
                    finished_at_ms=now if temporal_state in TERMINAL else None,
                )
            )
            if temporal_state in {"expired", "cancelled", "failed", "partial"}:
                conn.execute(
                    update(s.temporal_slices)
                    .where(s.temporal_slices.c.id == payload.get("slice_id"))
                    .values(
                        state=("expired" if temporal_state == "expired" else "cancelled" if temporal_state == "cancelled" else "partial"),
                        updated_at_ms=now,
                    )
                )
        elif kind in {"temporal_review", "temporal_finalize"}:
            temporal_state = state if state in {"queued", "running", "succeeded", "partial", "failed", "expired", "cancelled"} else "failed"
            conn.execute(
                update(s.temporal_review_runs)
                .where(s.temporal_review_runs.c.job_id == job["id"])
                .values(
                    status=temporal_state,
                    error_json=error,
                    updated_at_ms=now,
                    finished_at_ms=now if temporal_state in TERMINAL else None,
                )
            )

    def claim(self, owner: str, *, excluded_cameras: set[str] | None = None, allow_ai=True) -> dict | None:
        now = self.w.now()
        with self.w.db.write() as conn:
            rows = (
                conn.execute(
                    select(s.jobs)
                    .where(s.jobs.c.state == "queued", s.jobs.c.not_before_ms <= now)
                    .order_by(
                        s.jobs.c.priority,
                        case((s.jobs.c.deadline_at_ms.is_(None), 1), else_=0),
                        s.jobs.c.deadline_at_ms,
                        s.jobs.c.created_at_ms,
                    )
                    .limit(200)
                )
                .mappings()
                .all()
            )
            # 文字请求等待超过 10 分钟且图像期限安全时，给予一次公平调度机会。
            if not any(r["deadline_at_ms"] and r["deadline_at_ms"] < now + 300000 for r in rows):
                rows = sorted(
                    rows,
                    key=lambda r: (
                        0
                        if r["priority"] == 0
                        else 1
                        if r["kind"] in {"summarize_hour", "summarize_day", "manual_records"}
                        and r["created_at_ms"] < now - 600000
                        else 2
                    ),
                )
            for candidate in rows:
                row = dict(candidate)
                if row["cancel_requested"]:
                    conn.execute(
                        update(s.jobs)
                        .where(s.jobs.c.id == row["id"])
                        .values(state="cancelled", finished_at_ms=now, phase="cancelled")
                    )
                    self._sync_temporal_terminal(conn, row, "cancelled", now, "JOB_CANCELLED")
                    continue
                if row["deadline_at_ms"] is not None and row["deadline_at_ms"] <= now:
                    conn.execute(
                        update(s.jobs)
                        .where(s.jobs.c.id == row["id"])
                        .values(
                            state="expired",
                            finished_at_ms=now,
                            phase="expired",
                            error_code="EVIDENCE_EXPIRED",
                        )
                    )
                    self._sync_temporal_terminal(conn, row, "expired", now, "EVIDENCE_EXPIRED")
                    continue
                if row["kind"] == "minute_decision":
                    progress = row["progress_json"]
                    if progress["total_units"] is not None:
                        progress["completed_units"] = progress["total_units"]
                    conn.execute(
                        update(s.jobs)
                        .where(s.jobs.c.id == row["id"])
                        .values(
                            state="succeeded",
                            phase="succeeded",
                            queue_reason=None,
                            finished_at_ms=now,
                            updated_at_ms=now,
                            result_json={"status": "skipped", "reason": "分钟决策任务已移除，采用确定性统计"},
                            progress_json=progress,
                            error_code=None,
                            error_json=None,
                        )
                    )
                    continue
                if row["kind"] in AI_KINDS and not allow_ai:
                    continue
                if (
                    row["kind"] == "entity_discovery"
                    and conn.execute(
                        select(s.jobs.c.id).where(
                            s.jobs.c.kind == "entity_discovery", s.jobs.c.state == "running"
                        )
                    ).first()
                ):
                    continue
                if row["kind"] in IMAGE_KINDS and row["payload_json"].get("camera_id") in (
                    excluded_cameras or set()
                ):
                    continue
                provider_id = row["payload_json"].get("provider_id")
                if provider_id:
                    provider = (
                        conn.execute(select(s.providers).where(s.providers.c.id == provider_id))
                        .mappings()
                        .first()
                    )
                    if provider and provider["paused"] and row["kind"] != "provider_test":
                        conn.execute(
                            update(s.jobs)
                            .where(s.jobs.c.id == row["id"])
                            .values(queue_reason="provider_paused")
                        )
                        continue
                deps = conn.execute(
                    select(s.jobs.c.state, s.job_dependencies.c.accept_partial)
                    .join(s.job_dependencies, s.jobs.c.id == s.job_dependencies.c.depends_on_id)
                    .where(s.job_dependencies.c.job_id == row["id"])
                ).all()
                if any(state not in TERMINAL for state, _ in deps):
                    continue
                if row["kind"] not in {"reconcile_minutes", "manual_visual"} and any(
                    state not in {"succeeded", "partial"} or (state == "partial" and not partial)
                    for state, partial in deps
                ):
                    conn.execute(
                        update(s.jobs)
                        .where(s.jobs.c.id == row["id"])
                        .values(
                            state="failed",
                            phase="dependency_failed",
                            finished_at_ms=now,
                            error_code="DEPENDENCY_FAILED",
                        )
                    )
                    continue
                values = dict(
                    state="running",
                    queue_reason=None,
                    phase="executing",
                    lease_owner=owner,
                    lease_token=secrets.token_urlsafe(24),
                    lease_until_ms=now + 180000,
                    started_at_ms=row["started_at_ms"] or now,
                    updated_at_ms=now,
                )
                conn.execute(
                    update(s.jobs)
                    .where(s.jobs.c.id == row["id"], s.jobs.c.state == "queued")
                    .values(**values)
                )
                if row["kind"] in {"temporal_local_observation", "temporal_reobserve"}:
                    conn.execute(
                        update(s.local_observation_runs)
                        .where(s.local_observation_runs.c.job_id == row["id"])
                        .values(status="running", updated_at_ms=now)
                    )
                    conn.execute(
                        update(s.temporal_slices)
                        .where(
                            s.temporal_slices.c.id == row["payload_json"].get("slice_id")
                        )
                        .values(state="observing", updated_at_ms=now)
                    )
                elif row["kind"] in {"temporal_review", "temporal_finalize"}:
                    conn.execute(
                        update(s.temporal_review_runs)
                        .where(s.temporal_review_runs.c.job_id == row["id"])
                        .values(status="running", updated_at_ms=now)
                    )
                return row | values
        return None

    def guard(self, conn, job: dict):
        row = one(conn, s.jobs, job["id"])
        if (
            row["state"] != "running"
            or row["lease_token"] != job["lease_token"]
            or (row["lease_until_ms"] or 0) <= self.w.now()
        ):
            raise AppError(409, "LEASE_LOST", "任务租约已失效")
        if row["cancel_requested"]:
            raise AppError(409, "JOB_CANCELLED", "任务已取消")
        return row

    def renew(self, job: dict) -> bool:
        with self.w.db.write() as conn:
            return (
                conn.execute(
                    update(s.jobs)
                    .where(
                        s.jobs.c.id == job["id"],
                        s.jobs.c.state == "running",
                        s.jobs.c.lease_token == job["lease_token"],
                        s.jobs.c.cancel_requested.is_(False),
                    )
                    .values(lease_until_ms=self.w.now() + 180000, updated_at_ms=self.w.now())
                ).rowcount
                == 1
            )

    def finish(self, job: dict, state="succeeded", result=None, commit=None):
        for delay in (0, 0.05, 0.2, 0.5):
            if delay:
                time.sleep(delay)
            try:
                return self._finish_once(job, state, result, commit)
            except AppError as error:
                if error.code != "DB_BUSY":
                    raise
        raise AppError(503, "DB_BUSY", "提交结果时数据库持续繁忙，请检查存储")

    def _finish_once(self, job: dict, state="succeeded", result=None, commit=None):
        with self.w.db.write() as conn:
            self.guard(conn, job)
            if commit:
                committed = commit(conn)
                if isinstance(committed, tuple):
                    result, state = committed
            progress = one(conn, s.jobs, job["id"])["progress_json"]
            if progress["total_units"] is not None and state == "succeeded":
                progress["completed_units"] = progress["total_units"]
            conn.execute(
                update(s.jobs)
                .where(s.jobs.c.id == job["id"], s.jobs.c.lease_token == job["lease_token"])
                .values(
                    state=state,
                    phase=state,
                    finished_at_ms=self.w.now(),
                    updated_at_ms=self.w.now(),
                    lease_until_ms=None,
                    result_json=result or {},
                    progress_json=progress,
                    error_code=None,
                    error_json=None,
                )
            )

    def recover(self):
        now = self.w.now()
        with self.w.db.write() as conn:
            lost = (
                conn.execute(
                    select(s.jobs).where(
                        s.jobs.c.state == "running",
                        or_(s.jobs.c.lease_until_ms <= now, s.jobs.c.lease_until_ms.is_(None)),
                    )
                )
                .mappings()
                .all()
            )
            for row in lost:
                state = (
                    "cancelled"
                    if row["cancel_requested"]
                    else "expired"
                    if row["deadline_at_ms"] and row["deadline_at_ms"] <= now
                    else "queued"
                )
                conn.execute(
                    update(s.jobs)
                    .where(s.jobs.c.id == row["id"])
                    .values(
                        state=state,
                        queue_reason="retry_wait" if state == "queued" else None,
                        phase="recovering",
                        lease_token=None,
                        lease_owner=None,
                        lease_until_ms=None,
                        not_before_ms=now + 30000,
                        finished_at_ms=now if state != "queued" else None,
                    )
                )
                self._sync_temporal_terminal(conn, row, "queued" if state == "queued" else state, now, "WORKER_INTERRUPTED" if state != "queued" else None)
                conn.execute(
                    update(s.job_attempts)
                    .where(s.job_attempts.c.job_id == row["id"], s.job_attempts.c.state == "running")
                    .values(state="uncertain", error_code="WORKER_INTERRUPTED", finished_at_ms=now)
                )

    def fail(self, job: dict, error: AppError):
        now = self.w.now()
        with self.w.db.write() as conn:
            current = one(conn, s.jobs, job["id"])
            if current["lease_token"] != job["lease_token"] or current["state"] != "running":
                return
            if current["cancel_requested"] or error.code == "JOB_CANCELLED":
                state, reason, wait = "cancelled", None, 0
            elif error.code == "PROVIDER_AUTH_FAILED":
                state, reason, wait = (
                    ("failed", None, 0)
                    if job["kind"] == "provider_test"
                    else ("queued", "provider_paused", 30000)
                )
                provider_id = job["payload_json"].get("provider_id")
                conn.execute(
                    update(s.providers)
                    .where(s.providers.c.id == provider_id)
                    .values(paused=True, last_test_error=error.code)
                )
            elif job["kind"] == "exchange_rate_refresh":
                delays = [60000, 300000, 900000]
                failures = current["failure_count"] + 1
                retry = error.retryable and failures <= len(delays)
                wait = delays[failures - 1] if retry else 0
                state = "queued" if retry else "failed"
                reason = "retry_wait" if retry else None
            else:
                settings = self.w.settings_config(conn)
                attempts = current["failure_count"] + 1
                invalid_output = error.code in {"INVALID_MODEL_OUTPUT", "INVALID_MODEL_REFERENCES"}
                retry_budget = min(1, settings.ai_retry_count) if invalid_output else settings.ai_retry_count
                retry = (error.retryable or invalid_output) and attempts <= retry_budget
                wait = max(
                    int([30000, 120000, 300000][min(max(attempts - 1, 0), 2)] * random.uniform(1, 1.1)),
                    error.details.get("retry_after_ms", 0),
                )
                if current["deadline_at_ms"] and now + wait >= current["deadline_at_ms"]:
                    retry = False
                state = (
                    "queued"
                    if retry
                    else "expired"
                    if error.code.startswith("EVIDENCE_") and error.status == 410
                    else "failed"
                )
                reason = "retry_wait" if retry else None
            conn.execute(
                update(s.jobs)
                .where(s.jobs.c.id == job["id"])
                .values(
                    state=state,
                    queue_reason=reason,
                    not_before_ms=now + wait,
                    phase="waiting" if state == "queued" else state,
                    error_code=error.code,
                    error_json={
                        "code": error.code,
                        "message": error.message,
                        "retryable": state == "queued",
                        "next_retry_at": iso(now + wait) if state == "queued" else None,
                        "safe_details": safe_error_details(error.details),
                    },
                    lease_until_ms=None,
                    finished_at_ms=now if state in TERMINAL else None,
                    updated_at_ms=now,
                    failure_count=current["failure_count"] + 1,
                )
            )
            if job["kind"] in {"temporal_local_observation", "temporal_reobserve"}:
                temporal_state = (
                    "expired"
                    if state == "expired"
                    else "cancelled"
                    if state == "cancelled"
                    else "failed"
                    if state == "failed"
                    else "queued"
                )
                conn.execute(
                    update(s.local_observation_runs)
                    .where(s.local_observation_runs.c.job_id == job["id"])
                    .values(
                        status=temporal_state,
                        error_json={"code": error.code},
                        updated_at_ms=now,
                        finished_at_ms=now if temporal_state != "queued" else None,
                    )
                )
                if temporal_state in {"expired", "cancelled", "failed"}:
                    conn.execute(
                        update(s.temporal_slices)
                        .where(s.temporal_slices.c.id == job["payload_json"].get("slice_id"))
                        .values(
                            state="expired"
                            if temporal_state == "expired"
                            else "cancelled"
                            if temporal_state == "cancelled"
                            else "partial",
                            updated_at_ms=now,
                        )
                    )
            elif job["kind"] in {"temporal_review", "temporal_finalize"}:
                temporal_state = (
                    "expired" if state == "expired" else "cancelled" if state == "cancelled" else state
                )
                conn.execute(
                    update(s.temporal_review_runs)
                    .where(s.temporal_review_runs.c.job_id == job["id"])
                    .values(
                        status=temporal_state,
                        error_json={"code": error.code},
                        updated_at_ms=now,
                        finished_at_ms=now if temporal_state in TERMINAL else None,
                    )
                )

    def cancel(self, conn, job: dict) -> dict:
        if job["state"] in TERMINAL:
            return job
        changes = {"cancel_requested": True, "updated_at_ms": self.w.now()}
        if job["state"] == "queued":
            changes |= {"state": "cancelled", "phase": "cancelled", "finished_at_ms": self.w.now()}
        conn.execute(update(s.jobs).where(s.jobs.c.id == job["id"]).values(**changes))
        if changes.get("state") == "cancelled":
            self._sync_temporal_terminal(conn, job, "cancelled", self.w.now(), "JOB_CANCELLED")
        if job["kind"] in {"manual_visual", "reconcile_minutes"}:
            for baseline in job["payload_json"].get("baselines", []):
                minute = one(conn, s.minute_records, baseline["id"])
                if not minute["current_revision"]:
                    conn.execute(
                        update(s.minute_records)
                        .where(s.minute_records.c.id == minute["id"])
                        .values(analysis_state="failed")
                    )
                    self.w.ledger.advance(conn)
                    self.w.ledger.invalidate(conn, minute["start_at_ms"], minute["start_at_ms"] + 60000)
        for child_id in job["payload_json"].get("children", []):
            child = one(conn, s.jobs, child_id)
            if child["state"] not in TERMINAL:
                self.cancel(conn, child)
        return job | changes
