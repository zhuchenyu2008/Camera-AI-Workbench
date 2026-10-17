from __future__ import annotations

from sqlalchemy import select

from app.activities.ledger import segment_dto
from app.errors import AppError
from app.repositories import schema_v1 as s
from app.repositories.db import one
from app.timeutils import iso


class Records:
    def __init__(self, workbench):
        self.w = workbench

    def minute(self, conn, minute_id, revision=None, compact=False):
        minute = one(conn, s.minute_records, minute_id)
        version = revision or minute["current_revision"]
        row = (
            conn.execute(
                select(s.minute_revisions).where(
                    s.minute_revisions.c.minute_id == minute_id, s.minute_revisions.c.revision == version
                )
            )
            .mappings()
            .first()
        )
        if row is None and revision is not None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "分钟版本不存在")
        result = row["result_json"] if row else {}
        spans = [
            dict(r)
            for r in conn.execute(
                select(s.activity_spans)
                .where(
                    s.activity_spans.c.minute_id == minute_id, s.activity_spans.c.minute_revision == version
                )
                .order_by(s.activity_spans.c.start_at_ms, s.activity_spans.c.id)
            ).mappings()
        ]
        evidence_ids = result.get("evidence_ids", [])
        samples = []
        for evidence_id in evidence_ids[:3]:
            try:
                samples.append(self.w.assets.metadata(conn, evidence_id))
            except AppError:
                pass
        scope = {"camera_ids": [minute["camera_id"]], "subject_id": None}
        calculated = self.w.ledger.calculate(
            conn, minute["start_at_ms"], minute["start_at_ms"] + 60000, scope
        )
        capture = (
            conn.execute(select(s.minute_capture).where(s.minute_capture.c.minute_id == minute_id))
            .mappings()
            .one()
        )
        versions = (
            list(
                conn.execute(
                    select(s.minute_revisions)
                    .where(s.minute_revisions.c.minute_id == minute_id)
                    .order_by(s.minute_revisions.c.revision.desc())
                    .limit(20)
                ).mappings()
            )
            if not compact
            else []
        )
        pending = None
        for job in conn.execute(
            select(s.jobs).where(
                s.jobs.c.kind.in_(["manual_visual", "reconcile_minutes"]),
                s.jobs.c.state.in_(["queued", "running"]),
            )
        ).mappings():
            if any(b["id"] == minute_id for b in job["payload_json"].get("baselines", [])):
                pending = job["id"]
                break
        # 当前图过期只改变证据可用性，不改变已完成分析状态。
        return dict(
            id=minute_id,
            camera_id=minute["camera_id"],
            start_at=iso(minute["start_at_ms"]),
            end_at=iso(minute["start_at_ms"] + 60000),
            revision=version,
            analysis_state=result.get("analysis_state", minute["analysis_state"]),
            analysis_mode=result.get("analysis_mode"),
            evidence_state=self.w.ledger.evidence_state(conn, evidence_ids),
            coverage=calculated["coverage"],
            description=result.get("description", ""),
            segments=[segment_dto(r) for r in spans[:3] if compact]
            if compact
            else [segment_dto(r) for r in spans],
            evidence_samples=samples,
            reference_set_id=row["reference_set_id"] if row else None,
            model_version=result.get("model_version"),
            has_manual_revision=result.get("has_manual_revision", False),
            pending_job_id=pending,
            sampling=result.get(
                "sampling",
                {
                    "target_frame_count": capture["planned_bitmap"].bit_count(),
                    "captured_frame_count": capture["captured_bitmap"].bit_count(),
                    "submitted_frame_count": 0,
                    "successful_frame_count": 0,
                },
            ),
            versions=[
                {
                    "revision": r["revision"],
                    "created_at": iso(r["created_at_ms"]),
                    "origin": r["source_kind"],
                    "is_current": r["revision"] == minute["current_revision"],
                    "has_manual_revision": r["result_json"].get("has_manual_revision", False),
                }
                for r in versions
            ],
        )

    def evidence_slots(self, conn, minute_id):
        minute = one(conn, s.minute_records, minute_id)
        capture = (
            conn.execute(select(s.minute_capture).where(s.minute_capture.c.minute_id == minute_id))
            .mappings()
            .one()
        )
        frames = {
            r["slot_at_ms"]: dict(r)
            for r in conn.execute(
                select(s.frames).where(
                    s.frames.c.camera_id == minute["camera_id"],
                    s.frames.c.slot_at_ms >= minute["start_at_ms"],
                    s.frames.c.slot_at_ms < minute["start_at_ms"] + 60000,
                    s.frames.c.is_test.is_(False),
                )
            ).mappings()
        }
        result = []
        for i in range(60):
            slot = minute["start_at_ms"] + i * 1000
            frame = frames.get(slot)
            evidence = self.w.assets.metadata(conn, frame["id"]) if frame else None
            bit = 1 << i
            state, reason = "not_captured", capture["gap_reasons_json"].get(str(i), "no_frame")
            if not capture["planned_bitmap"] & bit:
                state, reason = "outside_schedule", "outside_schedule"
            elif evidence:
                state, reason = evidence["state"], evidence["reason"]
            elif capture["evicted_bitmap"] & bit:
                state, reason = "evicted", capture["gap_reasons_json"].get(str(i), "quota")
            elif capture["missing_bitmap"] & bit:
                state, reason = "missing", "missing_file"
            elif capture["captured_bitmap"] & bit:
                state, reason = "expired", "ttl"
            result.append(
                dict(
                    slot_at=iso(slot),
                    slot_state=state,
                    captured_at=evidence["captured_at"] if evidence else None,
                    evidence_id=frame["id"] if frame else None,
                    evidence=evidence,
                    reason=reason,
                )
            )
        return result
