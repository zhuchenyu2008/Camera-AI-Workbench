"""只重算已有观察，不读取现场图、不调用模型。"""

from sqlalchemy import func, select, update

from app.repositories import schema as s
from app.repositories.db import new_id
from app.security.secrets import fingerprint


def repair_history(w):
    with w.db.write() as conn:
        marker = (
            conn.execute(select(s.system_state).where(s.system_state.c.id == "activity-rules"))
            .mappings()
            .first()
        )
        if marker and marker["value_json"].get("version") == "statistics-v2":
            return {"already_applied": True, "sleep_minutes": 0}
        changed = 0
        minutes = list(conn.execute(select(s.minute_records)).mappings())
        for minute in minutes:
            if not minute["current_revision"]:
                continue
            old = (
                conn.execute(
                    select(s.minute_revisions).where(
                        s.minute_revisions.c.minute_id == minute["id"],
                        s.minute_revisions.c.revision == minute["current_revision"],
                    )
                )
                .mappings()
                .one()
            )
            if old["result_json"].get("has_manual_revision"):
                continue
            start = minute["start_at_ms"]
            spans = w.ledger.spans(conn, [minute["camera_id"]], start, start + 60000)
            edited = False
            for span in spans:
                text = span["details_json"].get("observation", "")
                if (
                    span["activity"] in {"resting", "休息"}
                    and any(term in text for term in ("睡觉", "睡眠", "入睡"))
                    and not any(
                        term in text for term in ("无法", "不确定", "可能", "疑似", "未能", "不能", "没有")
                    )
                ):
                    span["activity"] = "睡觉"
                    edited = True
            if not edited:
                continue
            revision = (
                conn.execute(
                    select(func.max(s.minute_revisions.c.revision)).where(
                        s.minute_revisions.c.minute_id == minute["id"]
                    )
                ).scalar_one()
                + 1
            )
            conn.execute(
                s.minute_revisions.insert().values(
                    minute_id=minute["id"],
                    revision=revision,
                    source_kind="reconcile",
                    source_fingerprint=fingerprint([old["source_fingerprint"], "statistics-v2"]),
                    reference_set_id=old["reference_set_id"],
                    result_json=old["result_json"] | {"repair_version": "statistics-v2"},
                    created_at_ms=w.now(),
                )
            )
            for span in spans:
                conn.execute(
                    s.activity_spans.insert().values(
                        **(span | {"id": new_id("span"), "minute_revision": revision})
                    )
                )
            w.assets.keep_evidence(
                conn, old["result_json"].get("evidence_ids", []), "minute", minute["id"], revision
            )
            conn.execute(
                update(s.minute_records)
                .where(s.minute_records.c.id == minute["id"])
                .values(current_revision=revision)
            )
            changed += 1
        w.ledger.advance(conn)
        w.ledger.project(conn)
        if minutes:
            w.ledger.invalidate(
                conn,
                min(m["start_at_ms"] for m in minutes),
                max(m["start_at_ms"] for m in minutes) + 60000,
                regenerate=False,
            )
        if marker:
            conn.execute(
                update(s.system_state)
                .where(s.system_state.c.id == "activity-rules")
                .values(value_json={"version": "statistics-v2"}, updated_at_ms=w.now())
            )
        else:
            conn.execute(
                s.system_state.insert().values(
                    id="activity-rules", value_json={"version": "statistics-v2"}, updated_at_ms=w.now()
                )
            )
        return {"already_applied": False, "sleep_minutes": changed, "minutes": len(minutes)}
