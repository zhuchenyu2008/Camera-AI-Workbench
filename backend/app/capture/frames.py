from __future__ import annotations

import io

from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.errors import AppError
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.security.secrets import digest, safe_path


class FrameWriter:
    def __init__(self, workbench):
        self.w = workbench
        self.last_pts = {}

    def save(
        self,
        camera_id: str,
        session_id: str,
        jpeg: bytes,
        captured_at: int,
        source_pts: str,
        receive_sequence: int,
        *,
        is_test=False,
    ) -> dict | None:
        # 不能根据像素是否相同判断新旧，只检查真实解码 PTS 和秒槽。
        previous = self.last_pts.get(session_id)
        try:
            current_pts = float(source_pts)
        except (ValueError, TypeError):
            raise AppError(422, "SOURCE_TIMESTAMP_INVALID", "源帧缺少有效时间戳") from None
        if previous is not None and current_pts <= previous:
            return None
        self.last_pts[session_id] = current_pts
        slot = captured_at // 1000 * 1000
        with self.w.db.read() as conn:
            camera = one(conn, s.cameras, camera_id)
            cfg = self.w.settings_config(conn)
            if conn.execute(
                select(s.frames.c.id).where(s.frames.c.camera_id == camera_id, s.frames.c.slot_at_ms == slot)
            ).first():
                return None
        with Image.open(io.BytesIO(jpeg)) as original:
            original.load()
            image = original.convert("RGB")
            image.thumbnail((cfg.frame_max_long_edge, cfg.frame_max_long_edge), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, "JPEG", quality=80)
            data, width, height = output.getvalue(), image.width, image.height
        ttl = camera["config_json"].get("retention_minutes") or cfg.frame_retention_minutes
        if is_test:
            ttl = min(ttl, 10)
        relpath = self.w.assets.store(f"frames/{camera_id}/{session_id}", data)
        row = dict(
            id=new_id("ev"),
            camera_id=camera_id,
            session_id=session_id,
            slot_at_ms=slot,
            captured_at_ms=captured_at,
            expires_at_ms=captured_at + ttl * 60000,
            relpath=relpath,
            bytes=len(data),
            width=width,
            height=height,
            sha256=digest(data),
            state="available",
            reason=None,
            source_pts=str(source_pts),
            receive_sequence=receive_sequence,
            time_source="vps_receive",
            is_test=is_test,
        )
        try:
            with self.w.db.write() as conn:
                current_camera = one(conn, s.cameras, camera_id)
                runtime = (
                    conn.execute(select(s.camera_runtime).where(s.camera_runtime.c.camera_id == camera_id))
                    .mappings()
                    .one()
                )
                if not is_test and (
                    not current_camera["enabled"]
                    or current_camera["source_revision"] != camera["source_revision"]
                    or runtime["stream_session_id"] != session_id
                    or not self.w.ledger.planned(conn, camera_id, slot, slot + 1000)
                ):
                    return None
                inserted = conn.execute(
                    sqlite_insert(s.frames).values(**row).on_conflict_do_nothing()
                ).rowcount
                if inserted != 1:
                    return None
                if not is_test:
                    conn.execute(
                        update(s.camera_runtime)
                        .where(s.camera_runtime.c.camera_id == camera_id)
                        .values(
                            last_frame_at_ms=captured_at,
                            capture_state="online",
                            stop_reason=None,
                            heartbeat_at_ms=self.w.now(),
                        )
                    )
                    self.minute_slot(conn, camera_id, slot, camera["revision"], captured=True)
            return row
        finally:
            with self.w.db.read() as conn:
                exists = conn.execute(select(s.frames.c.id).where(s.frames.c.id == row["id"])).first()
            if not exists:
                safe_path(self.w.config.data_dir, relpath).unlink(missing_ok=True)

    def minute_slot(self, conn, camera_id: str, slot: int, revision: int, captured=False, reason=None):
        planned = bool(self.w.ledger.planned(conn, camera_id, slot, slot + 1000))
        if not planned and not captured:
            return None
        start = slot // 60000 * 60000
        minute = (
            conn.execute(
                select(s.minute_records).where(
                    s.minute_records.c.camera_id == camera_id, s.minute_records.c.start_at_ms == start
                )
            )
            .mappings()
            .first()
        )
        if minute is None:
            minute = dict(
                id=new_id("min"),
                camera_id=camera_id,
                start_at_ms=start,
                current_revision=None,
                analysis_state="pending",
                created_at_ms=self.w.now(),
            )
            conn.execute(s.minute_records.insert().values(**minute))
            conn.execute(
                s.minute_capture.insert().values(
                    minute_id=minute["id"],
                    planned_bitmap=0,
                    captured_bitmap=0,
                    evicted_bitmap=0,
                    missing_bitmap=0,
                    gap_reasons_json={},
                    capture_config_revision=revision,
                )
            )
        capture = dict(
            conn.execute(select(s.minute_capture).where(s.minute_capture.c.minute_id == minute["id"]))
            .mappings()
            .one()
        )
        bit = 1 << ((slot - start) // 1000)
        changes = {
            "planned_bitmap": capture["planned_bitmap"] | bit if planned else capture["planned_bitmap"]
        }
        if captured:
            changes["captured_bitmap"] = capture["captured_bitmap"] | bit
        elif reason and planned and not capture["captured_bitmap"] & bit:
            changes["gap_reasons_json"] = capture["gap_reasons_json"] | {str((slot - start) // 1000): reason}
        conn.execute(
            update(s.minute_capture).where(s.minute_capture.c.minute_id == minute["id"]).values(**changes)
        )
        return minute["id"]
