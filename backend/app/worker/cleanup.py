from __future__ import annotations

import shutil

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.errors import AppError
from app.repositories import schema as s
from app.security.secrets import safe_path


class Cleaner:
    def __init__(self, workbench):
        self.w = workbench

    def mark(self, conn, frame, state, reason):
        conn.execute(update(s.frames).where(s.frames.c.id == frame["id"]).values(state=state, reason=reason))
        conn.execute(
            update(s.evidence_refs)
            .where(s.evidence_refs.c.evidence_id == frame["id"])
            .values(state=state, reason=reason)
        )
        minute = (
            conn.execute(
                select(s.minute_records).where(
                    s.minute_records.c.camera_id == frame["camera_id"],
                    s.minute_records.c.start_at_ms == frame["slot_at_ms"] // 60000 * 60000,
                )
            )
            .mappings()
            .first()
        )
        if minute and state in {"evicted", "missing"}:
            capture = (
                conn.execute(select(s.minute_capture).where(s.minute_capture.c.minute_id == minute["id"]))
                .mappings()
                .one()
            )
            column = "evicted_bitmap" if state == "evicted" else "missing_bitmap"
            bit = 1 << ((frame["slot_at_ms"] % 60000) // 1000)
            reasons = capture["gap_reasons_json"] | {str((frame["slot_at_ms"] % 60000) // 1000): reason}
            conn.execute(
                update(s.minute_capture)
                .where(s.minute_capture.c.minute_id == minute["id"])
                .values(**{column: capture[column] | bit, "gap_reasons_json": reasons})
            )

    def run(self, *, free_bytes=None) -> dict:
        now = self.w.now()
        with self.w.db.write() as conn:
            self.w.assets.sync_expiry(conn)
            from app.services.discovery import Discovery

            Discovery(self.w).cleanup(conn)
            cfg = self.w.settings_config(conn)
            expired = [
                dict(row)
                for row in conn.execute(
                    select(s.frames)
                    .where(s.frames.c.state != "available", s.frames.c.file_deleted_at_ms.is_(None))
                    .order_by(s.frames.c.expires_at_ms)
                    .limit(500)
                ).mappings()
            ]
            deleted_assets = [
                dict(row)
                for row in conn.execute(
                    select(s.reference_asset_versions)
                    .join(
                        s.reference_assets, s.reference_assets.c.id == s.reference_asset_versions.c.asset_id
                    )
                    .where(
                        s.reference_assets.c.deleted_at_ms.is_not(None),
                        s.reference_asset_versions.c.deleted_at_ms.is_(None),
                    )
                    .limit(500)
                ).mappings()
            ]
        removed, failed = 0, 0
        failed_ids = set()
        while expired:
            for frame in expired:
                try:
                    safe_path(self.w.config.data_dir, frame["relpath"]).unlink(missing_ok=True)
                    with self.w.db.write() as conn:
                        conn.execute(
                            update(s.frames)
                            .where(s.frames.c.id == frame["id"])
                            .values(file_deleted_at_ms=now)
                        )
                    removed += 1
                except (OSError, AppError):
                    failed += 1
                    failed_ids.add(frame["id"])
            with self.w.db.read() as conn:
                expired = [
                    dict(row)
                    for row in conn.execute(
                        select(s.frames)
                        .where(
                            s.frames.c.state != "available",
                            s.frames.c.file_deleted_at_ms.is_(None),
                            s.frames.c.id.not_in(failed_ids),
                        )
                        .order_by(s.frames.c.expires_at_ms)
                        .limit(500)
                    ).mappings()
                ]
        for asset in deleted_assets:
            try:
                safe_path(self.w.config.data_dir, asset["relpath"]).unlink(missing_ok=True)
                with self.w.db.write() as conn:
                    conn.execute(
                        update(s.reference_asset_versions)
                        .where(
                            s.reference_asset_versions.c.asset_id == asset["asset_id"],
                            s.reference_asset_versions.c.version == asset["version"],
                        )
                        .values(deleted_at_ms=now)
                    )
                removed += 1
            except OSError:
                failed += 1
        free = shutil.disk_usage(self.w.config.data_dir).free if free_bytes is None else free_bytes
        evicted = 0
        with self.w.db.read() as conn:
            media = conn.execute(
                select(func.coalesce(func.sum(s.frames.c.bytes), 0)).where(s.frames.c.state == "available")
            ).scalar_one()
            oldest = [
                dict(row)
                for row in conn.execute(
                    select(s.frames)
                    .where(s.frames.c.state == "available")
                    .order_by(s.frames.c.expires_at_ms, s.frames.c.id)
                    .limit(500)
                ).mappings()
            ]
            previous_scan = conn.execute(
                select(s.system_state.c.value_json).where(s.system_state.c.id == "storage")
            ).scalar()
            marker = (previous_scan or {}).get("frame_scan_cursor") or ""
            scanning = [
                dict(row)
                for row in conn.execute(
                    select(s.frames)
                    .where(s.frames.c.state == "available", s.frames.c.id > marker)
                    .order_by(s.frames.c.id)
                    .limit(500)
                ).mappings()
            ]
        missing_ids = set()
        for frame in scanning:
            try:
                exists = safe_path(self.w.config.data_dir, frame["relpath"]).is_file()
            except AppError:
                exists = False
            if not exists:
                missing_ids.add(frame["id"])
                with self.w.db.write() as conn:
                    self.mark(conn, frame, "missing", "missing_file")
                media -= frame["bytes"]
        # 容量回收只处理仍可用的资产，缺失文件不会重复充当“已释放字节”。
        oldest = [frame for frame in oldest if frame["id"] not in missing_ids]
        for frame in oldest:
            if media <= cfg.media_quota_gb * 1e9 and free >= cfg.disk_pressure_free_gb * 1e9:
                break
            with self.w.db.write() as conn:
                self.mark(
                    conn, frame, "evicted", "quota" if media > cfg.media_quota_gb * 1e9 else "disk_pressure"
                )
            try:
                safe_path(self.w.config.data_dir, frame["relpath"]).unlink(missing_ok=True)
                with self.w.db.write() as conn:
                    conn.execute(
                        update(s.frames).where(s.frames.c.id == frame["id"]).values(file_deleted_at_ms=now)
                    )
                media -= frame["bytes"]
                free += frame["bytes"]
                evicted += 1
            except OSError:
                failed += 1
        with self.w.db.read() as conn:
            indexed = set(conn.execute(select(s.frames.c.relpath)).scalars()) | set(
                conn.execute(
                    select(s.reference_asset_versions.c.relpath).where(
                        s.reference_asset_versions.c.deleted_at_ms.is_(None)
                    )
                ).scalars()
            )
        scanned = 0
        for directory in ("frames", "references", "tmp"):
            for path in (self.w.config.data_dir / directory).rglob("*"):
                if scanned >= 5000:
                    break
                scanned += 1
                if path.is_symlink() or not path.is_file():
                    continue
                if (
                    path.relative_to(self.w.config.data_dir).as_posix() not in indexed
                    and path.stat().st_mtime * 1000 < now - 120000
                ):
                    try:
                        safe_path(
                            self.w.config.data_dir, path.relative_to(self.w.config.data_dir).as_posix()
                        ).unlink()
                        removed += 1
                    except (OSError, AppError):
                        failed += 1
        hls_bytes = 0
        for directory in (self.w.config.data_dir / "hls").iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            files = sorted(
                (p for p in directory.iterdir() if p.is_file() and not p.is_symlink()),
                key=lambda p: p.stat().st_mtime,
            )
            total = sum(p.stat().st_size for p in files)
            for path in files:
                if path.stat().st_mtime * 1000 < now - 60000 or total > 64 * 1024**2:
                    size = path.stat().st_size
                    path.unlink(missing_ok=True)
                    total -= size
            hls_bytes += total
        free = shutil.disk_usage(self.w.config.data_dir).free if free_bytes is None else free
        with self.w.db.write() as conn:
            conn.execute(
                delete(s.frames).where(
                    s.frames.c.state != "available", s.frames.c.expires_at_ms < now - 86400000
                )
            )
            conn.execute(delete(s.idempotency_keys).where(s.idempotency_keys.c.expires_at_ms < now))
            conn.execute(delete(s.sessions).where(s.sessions.c.expires_at_ms < now))
            conn.execute(
                delete(s.audit_events).where(
                    s.audit_events.c.occurred_at_ms < now - cfg.detailed_audit_days * 86400000
                )
            )
            # 历史文字、修订、用量及引用不在常规清理中删除。
            references = conn.execute(
                select(func.coalesce(func.sum(s.reference_asset_versions.c.bytes), 0)).where(
                    s.reference_asset_versions.c.deleted_at_ms.is_(None)
                )
            ).scalar_one()
            earliest = conn.execute(
                select(func.min(s.frames.c.captured_at_ms)).where(s.frames.c.state == "available")
            ).scalar()
            previous = (
                conn.execute(select(s.system_state).where(s.system_state.c.id == "storage"))
                .mappings()
                .first()
            )
            prior = previous["value_json"] if previous else {}
            safe = free >= cfg.disk_pressure_free_gb * 1e9 and media < cfg.media_quota_gb * 1e9
            safe_since = prior.get("safe_since_ms") if safe else None
            safe_since = (safe_since or now) if safe else None
            paused = free < cfg.disk_stop_free_gb * 1e9 or (
                prior.get("paused", False) and (not safe_since or now - safe_since < 60000)
            )
            state = dict(
                frame_scan_cursor=scanning[-1]["id"] if scanning else None,
                total_bytes=shutil.disk_usage(self.w.config.data_dir).total,
                free_bytes=free,
                media_bytes=media,
                reference_bytes=references,
                hls_bytes=hls_bytes,
                earliest_available_at_ms=earliest,
                paused=paused,
                safe_since_ms=safe_since,
                last_cleanup_at_ms=now,
                cleanup_removed=removed,
                cleanup_evicted=evicted,
                cleanup_errors=failed,
            )
            conn.execute(
                sqlite_insert(s.system_state)
                .values(id="storage", value_json=state, updated_at_ms=now)
                .on_conflict_do_update(
                    index_elements=["id"], set_={"value_json": state, "updated_at_ms": now}
                )
            )
        return state
