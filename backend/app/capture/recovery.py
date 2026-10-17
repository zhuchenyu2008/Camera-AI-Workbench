from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.repositories import schema as s
from app.repositories.db import new_id


class CaptureRecovery:
    def __init__(self, workbench):
        self.w = workbench

    def finalize_before(self, end=None, *, recovered=False):
        end = (end if end is not None else self.w.now()) // 60000 * 60000
        with self.w.db.read() as conn:
            cursor = (
                conn.execute(select(s.system_state).where(s.system_state.c.id == "capture_finalized"))
                .mappings()
                .first()
            )
            earliest = conn.execute(select(func.min(s.cameras.c.created_at_ms))).scalar()
        start = cursor["value_json"]["until_ms"] if cursor else (earliest or end) // 60000 * 60000
        if start >= end:
            return
        # 每个事务最多补 30 分钟元数据；不补造现场图。
        for chunk in range(start, end, 30 * 60000):
            upper = min(end, chunk + 30 * 60000)
            with self.w.db.write() as conn:
                cameras = list(conn.execute(select(s.cameras)).mappings())
                for camera in cameras:
                    for minute_start in range(
                        max(chunk, camera["created_at_ms"] // 60000 * 60000), upper, 60000
                    ):
                        planned = self.w.ledger.planned(
                            conn, camera["id"], minute_start, minute_start + 60000
                        )
                        bitmap = sum(
                            1 << i
                            for i in range(60)
                            if any(a <= minute_start + i * 1000 < b for a, b in planned)
                        )
                        minute = (
                            conn.execute(
                                select(s.minute_records).where(
                                    s.minute_records.c.camera_id == camera["id"],
                                    s.minute_records.c.start_at_ms == minute_start,
                                )
                            )
                            .mappings()
                            .first()
                        )
                        if not bitmap and minute is None:
                            continue
                        if minute is None:
                            minute = {"id": new_id("min")}
                            conn.execute(
                                s.minute_records.insert().values(
                                    id=minute["id"],
                                    camera_id=camera["id"],
                                    start_at_ms=minute_start,
                                    current_revision=None,
                                    analysis_state="failed",
                                    created_at_ms=self.w.now(),
                                )
                            )
                            conn.execute(
                                s.minute_capture.insert().values(
                                    minute_id=minute["id"],
                                    planned_bitmap=bitmap,
                                    captured_bitmap=0,
                                    evicted_bitmap=0,
                                    missing_bitmap=0,
                                    gap_reasons_json={
                                        "default": "worker_offline" if recovered else "source_offline"
                                    },
                                    capture_config_revision=camera["revision"],
                                    finalized_at_ms=minute_start + 60000,
                                )
                            )
                        else:
                            conn.execute(
                                update(s.minute_capture)
                                .where(s.minute_capture.c.minute_id == minute["id"])
                                .values(planned_bitmap=bitmap, finalized_at_ms=minute_start + 60000)
                            )
                value = {"until_ms": upper}
                conn.execute(
                    sqlite_insert(s.system_state)
                    .values(id="capture_finalized", value_json=value, updated_at_ms=self.w.now())
                    .on_conflict_do_update(
                        index_elements=["id"], set_={"value_json": value, "updated_at_ms": self.w.now()}
                    )
                )
