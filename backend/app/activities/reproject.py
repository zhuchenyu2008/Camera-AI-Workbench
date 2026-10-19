from __future__ import annotations

from sqlalchemy import select, update

from app.analysis.normalize import normalize
from app.errors import AppError
from app.repositories import schema as s
from app.repositories.db import new_id
from app.security.secrets import fingerprint
from app.timeutils import intersect, union


def reproject(w, patch):
    """重用历史观察和证据元数据，不读取原图、不调用模型。"""
    if not set(patch) & {"merge_gap_seconds", "max_inference_gap_seconds"}:
        return
    with w.db.read() as conn:
        ids = list(
            conn.execute(select(s.minute_records.c.id).order_by(s.minute_records.c.start_at_ms)).scalars()
        )
    if "max_inference_gap_seconds" in patch:
        for minute_id in ids:
            with w.db.write() as conn:
                minute = dict(
                    conn.execute(select(s.minute_records).where(s.minute_records.c.id == minute_id))
                    .mappings()
                    .one()
                )
                previous = (
                    conn.execute(
                        select(s.minute_revisions).where(
                            s.minute_revisions.c.minute_id == minute_id,
                            s.minute_revisions.c.revision == minute["current_revision"],
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    not previous
                    or previous["result_json"].get("has_manual_revision")
                    or not previous["result_json"].get("visual_outputs")
                ):
                    continue
                result = dict(previous["result_json"])
                if (
                    result.get("projection_max_gap_seconds")
                    == w.settings_config(conn).max_inference_gap_seconds
                ):
                    continue
                result["projection_max_gap_seconds"] = w.settings_config(conn).max_inference_gap_seconds
                frames = []
                for evidence_id in result.get("evidence_ids", []):
                    try:
                        frames.append(w.assets.metadata(conn, evidence_id, check_file=False))
                    except AppError as error:
                        if error.status != 404:
                            raise
                start, end = minute["start_at_ms"], minute["start_at_ms"] + 60000
                captured = w.ledger.captured(conn, minute["camera_id"], start, end)
                candidates, _ = normalize(
                    result["visual_outputs"],
                    frames,
                    captured,
                    max_gap_seconds=w.settings_config(conn).max_inference_gap_seconds,
                    support="sparse",
                    activity_catalog=result.get("activity_catalog", []),
                )
                old_spans = result.get("projection_base_spans") or w.ledger.spans(
                    conn, [minute["camera_id"]], start, end
                )
                result["projection_base_spans"] = old_spans
                result.setdefault("projection_base_analysis_state", result.get("analysis_state", "complete"))
                spans = []
                for old in old_spans:
                    if old["support"] == "dense" or old["origin"] == "manual":
                        spans.append(old)
                        continue
                    valid = [
                        (span["start_at_ms"], span["end_at_ms"])
                        for span in candidates
                        if span["activity"] == old["activity"] and span["subject_id"] == old["subject_id"]
                    ]
                    for a, b in intersect([(old["start_at_ms"], old["end_at_ms"])], valid):
                        spans.append(old | {"start_at_ms": a, "end_at_ms": b})
                old_duration = sum(
                    b - a for a, b in union([(v["start_at_ms"], v["end_at_ms"]) for v in old_spans])
                )
                new_duration = sum(
                    b - a for a, b in union([(v["start_at_ms"], v["end_at_ms"]) for v in spans])
                )
                result["analysis_state"] = (
                    result["projection_base_analysis_state"] if new_duration == old_duration else "partial"
                )
                result["unanalysed_reason"] = (
                    "expired"
                    if frames and all(frame["state"] != "available" for frame in frames)
                    else "failed"
                )
                version = (
                    conn.execute(
                        select(s.minute_revisions.c.revision)
                        .where(s.minute_revisions.c.minute_id == minute_id)
                        .order_by(s.minute_revisions.c.revision.desc())
                        .limit(1)
                    ).scalar()
                    or 0
                ) + 1
                source_hash = fingerprint(
                    [
                        previous["source_fingerprint"],
                        "statistics-v1",
                        w.settings_config(conn).max_inference_gap_seconds,
                    ]
                )
                if conn.execute(
                    select(s.minute_revisions.c.revision).where(
                        s.minute_revisions.c.minute_id == minute_id,
                        s.minute_revisions.c.source_fingerprint == source_hash,
                    )
                ).first():
                    continue
                conn.execute(
                    s.minute_revisions.insert().values(
                        minute_id=minute_id,
                        revision=version,
                        source_kind="reconcile",
                        source_fingerprint=source_hash,
                        reference_set_id=previous["reference_set_id"],
                        result_json=result,
                        created_at_ms=w.now(),
                    )
                )
                for span in spans:
                    conn.execute(
                        s.activity_spans.insert().values(
                            **(span | {"id": new_id("span"), "minute_revision": version})
                        )
                    )
                w.assets.keep_evidence(conn, result.get("evidence_ids", []), "minute", minute_id, version)
                conn.execute(
                    update(s.minute_records)
                    .where(s.minute_records.c.id == minute_id)
                    .values(current_revision=version, analysis_state=result["analysis_state"])
                )
                w.ledger.advance(conn)
                w.ledger.invalidate(conn, start, end)
    with w.db.write() as conn:
        w.ledger.advance(conn)
        w.ledger.project(conn)
        bounds = (
            conn.execute(select(s.minute_records.c.start_at_ms).order_by(s.minute_records.c.start_at_ms))
            .scalars()
            .all()
        )
        if bounds:
            w.ledger.invalidate(conn, bounds[0], bounds[-1] + 60000)
