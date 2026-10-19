from __future__ import annotations

from sqlalchemy import and_, select

from app.activities.ledger import segment_dto
from app.errors import AppError
from app.repositories import schema as s
from app.repositories.db import one
from app.timeutils import iso


class Records:
    def __init__(self, workbench):
        self.w = workbench

    def list_minutes(self, conn, rows):
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        cameras = sorted({r["camera_id"] for r in rows})
        source = self.w.ledger.load_range(
            conn,
            min(r["start_at_ms"] for r in rows),
            max(r["start_at_ms"] for r in rows) + 60000,
            {"camera_ids": cameras, "subject_id": None},
        )
        versions = {
            r["minute_id"]: dict(r)
            for r in conn.execute(
                select(s.minute_revisions)
                .join(
                    s.minute_records,
                    and_(
                        s.minute_records.c.id == s.minute_revisions.c.minute_id,
                        s.minute_records.c.current_revision == s.minute_revisions.c.revision,
                    ),
                )
                .where(s.minute_records.c.id.in_(ids))
            ).mappings()
        }
        captures = {
            r["minute_id"]: r
            for r in conn.execute(
                select(s.minute_capture).where(s.minute_capture.c.minute_id.in_(ids))
            ).mappings()
        }
        metadata = self.w.assets.metadata_many(
            conn, [e for r in versions.values() for e in r["result_json"].get("evidence_ids", [])]
        )
        pending = {}
        for job in conn.execute(
            select(s.jobs.c.id, s.jobs.c.payload_json).where(
                s.jobs.c.kind.in_(["manual_visual", "reconcile_minutes"]),
                s.jobs.c.state.in_(["queued", "running"]),
            )
        ):
            pending.update({b["id"]: job.id for b in job.payload_json.get("baselines", [])})
        result = []
        for minute in rows:
            camera, start = minute["camera_id"], minute["start_at_ms"]
            row = versions.get(minute["id"], {})
            data = row.get("result_json", {})
            spans = [r for r in source["spans"] if r["minute_id"] == minute["id"]]
            local = source | {
                "planned": {camera: source["planned"][camera]},
                "captured": {camera: source["captured"][camera]},
                "spans": spans,
                "minutes": [r for r in source["minutes"] if r["id"] == minute["id"]],
            }
            stats = self.w.ledger.calculate(
                conn, start, start + 60000, {"camera_ids": [camera], "subject_id": None}, data=local
            )
            evidence = data.get("evidence_ids", [])
            samples = []
            for eid in evidence[:3]:
                meta = metadata.get(eid)
                if meta:
                    samples.append(
                        dict(
                            id=eid,
                            camera_id=meta["camera_id"],
                            captured_at=iso(meta["captured_at_ms"]),
                            expires_at=iso(meta["effective_expiry_ms"]),
                            state=meta["state"],
                            width=meta["width"],
                            height=meta["height"],
                            content_url=f"/api/evidence/{eid}" if meta["state"] == "available" else None,
                            reason=meta.get("reason"),
                        )
                    )
            capture = captures[minute["id"]]
            result.append(
                dict(
                    id=minute["id"],
                    camera_id=camera,
                    start_at=iso(start),
                    end_at=iso(start + 60000),
                    revision=minute["current_revision"] or 0,
                    analysis_state=data.get("analysis_state", minute["analysis_state"]),
                    analysis_mode=data.get("analysis_mode"),
                    evidence_state=self.w.ledger.combine_evidence_states(evidence, metadata),
                    coverage=stats["coverage"],
                    description=data.get("description", ""),
                    segments=[
                        segment_dto(r) for r in sorted(spans, key=lambda r: (r["start_at_ms"], r["id"]))[:3]
                    ],
                    evidence_samples=samples,
                    reference_set_id=row.get("reference_set_id"),
                    model_version=data.get("model_version"),
                    has_manual_revision=data.get("has_manual_revision", False),
                    pending_job_id=pending.get(minute["id"]),
                    sampling=data.get(
                        "sampling",
                        dict(
                            target_frame_count=capture["planned_bitmap"].bit_count(),
                            captured_frame_count=capture["captured_bitmap"].bit_count(),
                            submitted_frame_count=0,
                            successful_frame_count=0,
                        ),
                    ),
                    versions=[],
                )
            )
        return result

    def minute(self, conn, minute_id, revision=None, compact=False):
        minute = one(conn, s.minute_records, minute_id)
        version = revision or minute["current_revision"] or 0
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
            conn,
            minute["start_at_ms"],
            minute["start_at_ms"] + 60000,
            scope,
            spans_override=spans if revision is not None else None,
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
            state, reason = (
                "not_captured",
                capture["gap_reasons_json"].get(
                    str(i), capture["gap_reasons_json"].get("default", "no_frame")
                ),
            )
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
