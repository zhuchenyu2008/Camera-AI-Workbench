from __future__ import annotations

import io
import json
import os
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.errors import AppError, require_revision
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.schemas import Crop, EntityCreate
from app.security.secrets import fingerprint, safe_path
from app.timeutils import iso
from app.worker.queue import enqueue, job_dto


def decode_image(data: bytes, mime: str | None = None, crop: Crop | None = None) -> tuple[bytes, int, int]:
    if len(data) > 8 * 1024 * 1024:
        raise AppError(413, "UPLOAD_TOO_LARGE", "单张参考图不能超过 8 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                detected = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(
                    original.format
                )
                if detected is None or (mime and mime != detected) or getattr(original, "is_animated", False):
                    raise AppError(415, "UNSUPPORTED_IMAGE_TYPE", "仅支持真实的静态 JPEG、PNG、WebP 图片")
                if original.width * original.height > 16_000_000:
                    raise AppError(413, "UPLOAD_TOO_LARGE", "图片不能超过 1600 万像素")
                original.load()
                image = ImageOps.exif_transpose(original)
                if crop:
                    w, h = image.size
                    box = (
                        round(crop.x * w),
                        round(crop.y * h),
                        round((crop.x + crop.width) * w),
                        round((crop.y + crop.height) * h),
                    )
                    if box[2] <= box[0] or box[3] <= box[1]:
                        raise AppError(422, "VALIDATION_ERROR", "裁剪后图像为空")
                    image = image.crop(box)
                image = image.convert("RGB")
                image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, "JPEG", quality=90, optimize=True)
                return output.getvalue(), image.width, image.height
    except AppError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise AppError(415, "UNSUPPORTED_IMAGE_TYPE", "无法解码图片") from None


class Assets:
    def __init__(self, workbench):
        self.w = workbench

    def store(self, directory: str, data: bytes) -> str:
        relpath = f"{directory}/{new_id('img')}.jpg"
        target = safe_path(self.w.config.data_dir, relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".part")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return relpath

    def effective_expiry(self, conn, row):
        camera = one(conn, s.cameras, row["camera_id"], deleted=True)
        ttl = (
            camera["config_json"].get("retention_minutes")
            or self.w.settings_config(conn).frame_retention_minutes
        )
        # 测试图另有更短的硬上限。
        if row.get("is_test"):
            ttl = min(ttl, 10)
        return min(row["expires_at_ms"], row["captured_at_ms"] + ttl * 60000)

    def metadata(self, conn, evidence_id: str, *, check_file=True):
        raw = conn.execute(select(s.frames).where(s.frames.c.id == evidence_id)).mappings().first()
        if raw:
            row = dict(raw)
        else:
            raw = (
                conn.execute(select(s.evidence_refs).where(s.evidence_refs.c.evidence_id == evidence_id))
                .mappings()
                .first()
            )
            if not raw:
                raise AppError(404, "RESOURCE_NOT_FOUND", "证据不存在")
            row = dict(raw) | {"id": evidence_id}
        expiry = self.effective_expiry(conn, row)
        state, reason = row["state"], row.get("reason")
        if state == "available" and expiry <= self.w.now():
            state, reason = "expired", "ttl"
        if state == "available" and (
            not row.get("relpath")
            or (check_file and not safe_path(self.w.config.data_dir, row["relpath"]).is_file())
        ):
            state, reason = "missing", "missing_file"
        return dict(
            id=evidence_id,
            camera_id=row["camera_id"],
            captured_at=iso(row["captured_at_ms"]),
            expires_at=iso(expiry),
            state=state,
            width=row["width"],
            height=row["height"],
            content_url=f"/api/evidence/{evidence_id}" if state == "available" else None,
            reason=reason,
        )

    def metadata_many(self, conn, evidence_ids):
        """列表路径只批量读取元数据；实际媒体读取仍检查文件和 TTL。"""
        ids = sorted(set(evidence_ids))
        if not ids:
            return {}
        members = func.json_each(json.dumps(ids)).table_valued("value")
        rows = {
            r["evidence_id"]: dict(r) | {"id": r["evidence_id"]}
            for r in conn.execute(
                select(s.evidence_refs).where(s.evidence_refs.c.evidence_id.in_(select(members.c.value)))
            ).mappings()
        }
        rows.update(
            {
                r["id"]: dict(r)
                for r in conn.execute(
                    select(s.frames).where(s.frames.c.id.in_(select(members.c.value)))
                ).mappings()
            }
        )
        cfg = self.w.settings_config(conn)
        retention = {
            r.id: r.config_json.get("retention_minutes") or cfg.frame_retention_minutes
            for r in conn.execute(select(s.cameras.c.id, s.cameras.c.config_json))
        }
        now = self.w.now()
        for row in rows.values():
            ttl = retention[row["camera_id"]]
            if row.get("is_test"):
                ttl = min(ttl, 10)
            expiry = min(row["expires_at_ms"], row["captured_at_ms"] + ttl * 60000)
            if row["state"] == "available" and expiry <= now:
                row["state"] = "expired"
            row["effective_expiry_ms"] = expiry
        return rows

    def read_evidence(self, evidence_id: str) -> tuple[bytes, dict]:
        with self.w.db.read() as conn:
            meta = self.metadata(conn, evidence_id)
            row = conn.execute(select(s.frames).where(s.frames.c.id == evidence_id)).mappings().first()
        if meta["state"] != "available":
            code = {
                "expired": "EVIDENCE_EXPIRED",
                "evicted": "EVIDENCE_EVICTED",
                "missing": "EVIDENCE_MISSING",
            }[meta["state"]]
            raise AppError(
                410,
                code,
                "现场图片已不可用",
                details={"evidence_id": evidence_id, "expired_at": meta["expires_at"]},
            )
        try:
            data = safe_path(self.w.config.data_dir, row["relpath"]).read_bytes()
        except OSError:
            raise AppError(410, "EVIDENCE_MISSING", "现场图片文件已丢失") from None
        # 读取期间可能保存了更短的 TTL，出站前再次授权。
        with self.w.db.read() as conn:
            fresh = self.metadata(conn, evidence_id, check_file=False)
        if fresh["state"] != "available":
            raise AppError(410, "EVIDENCE_EXPIRED", "现场图片已不可用")
        return data, fresh

    def freeze_expired(self, conn):
        self.sync_expiry(conn, freeze_only=True)

    def sync_expiry(self, conn, camera_id=None, freeze_only=False):
        cfg = self.w.settings_config(conn)
        retention = {
            row["id"]: row["config_json"].get("retention_minutes") or cfg.frame_retention_minutes
            for row in conn.execute(select(s.cameras)).mappings()
        }
        # 每路使用批量 SQL，避免保留期增大后在事务里逐帧循环。
        for resource_id, ttl in retention.items():
            if camera_id and resource_id != camera_id:
                continue
            frame_target = s.frames.c.captured_at_ms + case(
                (s.frames.c.is_test.is_(True), min(ttl, 10) * 60000), else_=ttl * 60000
            )
            frame_expiry = func.min(s.frames.c.expires_at_ms, frame_target)
            filters = (s.frames.c.camera_id == resource_id, s.frames.c.state == "available")
            conn.execute(
                update(s.frames)
                .where(*filters, frame_expiry <= self.w.now())
                .values(state="expired", reason="ttl", expires_at_ms=frame_expiry)
            )
            test_ids = select(s.frames.c.id).where(s.frames.c.is_test.is_(True))
            ref_target = s.evidence_refs.c.captured_at_ms + case(
                (s.evidence_refs.c.evidence_id.in_(test_ids), min(ttl, 10) * 60000), else_=ttl * 60000
            )
            ref_expiry = func.min(s.evidence_refs.c.expires_at_ms, ref_target)
            ref_filters = (s.evidence_refs.c.camera_id == resource_id, s.evidence_refs.c.state == "available")
            conn.execute(
                update(s.evidence_refs)
                .where(*ref_filters, ref_expiry <= self.w.now())
                .values(state="expired", reason="ttl", expires_at_ms=ref_expiry)
            )
            if not freeze_only:
                conn.execute(update(s.frames).where(*filters).values(expires_at_ms=frame_target))
                conn.execute(update(s.evidence_refs).where(*ref_filters).values(expires_at_ms=ref_target))

    def keep_evidence(self, conn, evidence_ids: list[str], kind: str, owner_id: str, revision: int):
        ids = sorted(set(evidence_ids))
        if not ids:
            return
        from sqlalchemy import literal

        member = func.json_each(json.dumps(ids)).table_valued("value")
        keys = [
            "camera_id",
            "captured_at_ms",
            "expires_at_ms",
            "state",
            "reason",
            "width",
            "height",
            "sha256",
        ]
        conn.execute(
            sqlite_insert(s.evidence_refs)
            .from_select(
                ["evidence_id", *keys],
                select(s.frames.c.id, *[s.frames.c[k] for k in keys]).where(
                    s.frames.c.id.in_(select(member.c.value))
                ),
            )
            .on_conflict_do_nothing()
        )
        conn.execute(
            sqlite_insert(s.revision_evidence_links)
            .from_select(
                ["owner_kind", "owner_id", "owner_revision", "evidence_id", "role"],
                select(
                    literal(kind),
                    literal(owner_id),
                    literal(revision),
                    s.evidence_refs.c.evidence_id,
                    literal("observed"),
                ).where(s.evidence_refs.c.evidence_id.in_(select(member.c.value))),
            )
            .on_conflict_do_nothing()
        )

    def reference_fingerprint(self, conn):
        return fingerprint(self.members(conn, None))

    def members(self, conn, camera_ids: list[str] | None):
        query = (
            select(
                s.reference_assets,
                s.reference_asset_versions,
                s.entities.c.name.label("entity_name"),
                s.entities.c.kind,
                s.entities.c.description,
                s.entities.c.camera_ids_json,
                s.entities.c.origin,
                s.entities.c.reviewed,
                s.entities.c.last_seen_at_ms,
            )
            .join(
                s.reference_asset_versions,
                (s.reference_assets.c.id == s.reference_asset_versions.c.asset_id)
                & (s.reference_assets.c.current_version == s.reference_asset_versions.c.version),
            )
            .join(s.entities, s.entities.c.id == s.reference_assets.c.entity_id)
            .where(
                s.reference_assets.c.deleted_at_ms.is_(None),
                s.reference_assets.c.enabled.is_(True),
                s.entities.c.deleted_at_ms.is_(None),
                s.entities.c.enabled.is_(True),
                # 参考资料只用于人物身份；普通物品不再进入任何模型请求。
                s.entities.c.kind == "person",
            )
            .order_by(s.reference_assets.c.id)
        )
        result = []
        for row in conn.execute(query).mappings():
            if (
                camera_ids is not None
                and row["camera_ids_json"]
                and not set(camera_ids) & set(row["camera_ids_json"])
            ):
                continue
            result.append(
                dict(
                    asset_id=row["id"],
                    version=row["current_version"],
                    name=row["name_override"] or row["name_snapshot"],
                    entity_id=row["entity_id"],
                    entity_name=row["entity_name"],
                    kind=row["kind"],
                    description=row["description"],
                    camera_ids=row["camera_ids_json"],
                    sha256=row["sha256"],
                    bytes=row["bytes"],
                    origin=row["origin"],
                    reviewed=row["reviewed"],
                    last_seen_at_ms=row["last_seen_at_ms"],
                )
            )
        return result

    def reference_set(self, conn, camera_ids: list[str] | None) -> dict:
        members = self.members(conn, camera_ids)
        cfg = self.w.settings_config(conn)
        providers = [
            one(conn, s.providers, p) for p in {cfg.vision_provider_id, cfg.summary_provider_id} - {None}
        ]
        image_limit = (
            min(
                [cfg.max_total_images_per_request]
                + [self.w.effective_limits(cfg, p)["max_total_images"] for p in providers]
            )
            - min(
                [cfg.max_capture_images_per_request]
                + [self.w.effective_limits(cfg, p)["max_capture_images"] for p in providers]
            )
        )
        byte_limit = (
            min(
                [cfg.max_request_body_mib * 1024**2]
                + [self.w.effective_limits(cfg, p)["max_request_body_bytes"] for p in providers]
            )
            // 2
        )
        members.sort(
            key=lambda m: (
                not m["reviewed"],
                m["origin"] != "manual",
                not bool(set(m["camera_ids"]) & set(camera_ids or [])),
                -(m["last_seen_at_ms"] or 0),
                m["asset_id"],
            )
        )
        # Give every person one representative before allowing additional
        # views. This keeps a large profile from consuming the现场图 quota;
        # the second pass provides deterministic rotation among extra views.
        first_by_person, extras = [], []
        seen_people = set()
        for member in members:
            if member["entity_id"] in seen_people:
                extras.append(member)
            else:
                first_by_person.append(member)
                seen_people.add(member["entity_id"])
        selected, used = [], 0
        for member in first_by_person + extras:
            size = (member["bytes"] + 2) // 3 * 4 + 2048
            if len(selected) >= image_limit or used + size > byte_limit:
                continue
            selected.append(member)
            used += size
        members = selected
        key = fingerprint(members)
        row = (
            conn.execute(select(s.reference_sets).where(s.reference_sets.c.fingerprint == key))
            .mappings()
            .first()
        )
        if row:
            return dict(row)
        row = dict(id=new_id("refs"), fingerprint=key, members_json=members, created_at_ms=self.w.now())
        conn.execute(s.reference_sets.insert().values(**row))
        return row

    def validate_capacity(self, conn, cfg=None):
        cfg = cfg or self.w.settings_config(conn)
        # 模型输入按相关性选取，档案增长不会阻断现场分析；独立存储配额仍生效。
        total = conn.execute(
            select(func.coalesce(func.sum(s.reference_asset_versions.c.bytes), 0)).where(
                s.reference_asset_versions.c.deleted_at_ms.is_(None)
            )
        ).scalar_one()
        if total > cfg.reference_quota_gb * 1e9:
            raise AppError(422, "REFERENCE_CAPACITY_EXCEEDED", "参考图及其历史版本超过参考配额")

    def reference(self, conn, asset_id: str, *, deleted=False, version=None):
        asset = one(conn, s.reference_assets, asset_id, deleted=deleted)
        entity = one(conn, s.entities, asset["entity_id"], deleted=True)
        query = select(s.reference_asset_versions).where(
            s.reference_asset_versions.c.asset_id == asset_id,
            s.reference_asset_versions.c.version == (version or asset["current_version"]),
        )
        row = conn.execute(query).mappings().first()
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "参考版本不存在")
        return dict(
            asset_id=asset_id,
            entity_id=asset["entity_id"],
            entity_revision=entity["revision"],
            asset_version=row["version"],
            revision=asset["revision"],
            name_snapshot=(asset["name_override"] or row["name_snapshot"])
            if version is None
            else row["name_snapshot"],
            enabled=asset["enabled"],
            width=row["width"],
            height=row["height"],
            sha256=row["sha256"],
            created_at=iso(row["created_at_ms"]),
            deleted_at=iso(asset["deleted_at_ms"]),
            content_url=None
            if asset["deleted_at_ms"]
            else f"/api/reference-assets/{asset_id}/content?version={row['version']}",
            source_evidence_id=asset["source_evidence_id"],
        )

    def read_reference(self, asset_id: str, version: int | None = None) -> bytes:
        with self.w.db.read() as conn:
            asset = one(conn, s.reference_assets, asset_id, deleted=True)
            if asset["deleted_at_ms"] is not None:
                raise AppError(410, "REFERENCE_DELETED", "参考资产已删除，所有版本停止访问")
            row = (
                conn.execute(
                    select(s.reference_asset_versions).where(
                        s.reference_asset_versions.c.asset_id == asset_id,
                        s.reference_asset_versions.c.version == (version or asset["current_version"]),
                    )
                )
                .mappings()
                .first()
            )
        if not row or row["deleted_at_ms"] is not None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "参考版本不存在")
        try:
            data = safe_path(self.w.config.data_dir, row["relpath"]).read_bytes()
        except OSError:
            raise AppError(410, "REFERENCE_DELETED", "参考文件已不可用") from None
        with self.w.db.read() as conn:
            if one(conn, s.reference_assets, asset_id, deleted=True)["deleted_at_ms"] is not None:
                raise AppError(410, "REFERENCE_DELETED", "参考资产已删除")
        return data

    def entity(self, conn, entity_id: str):
        row = one(conn, s.entities, entity_id)
        ids = (
            conn.execute(
                select(s.reference_assets.c.id)
                .where(
                    s.reference_assets.c.entity_id == entity_id, s.reference_assets.c.deleted_at_ms.is_(None)
                )
                .order_by(s.reference_assets.c.id)
            )
            .scalars()
            .all()
        )
        return dict(
            id=entity_id,
            revision=row["revision"],
            kind=row["kind"],
            name=row["name"],
            description=row["description"],
            enabled=row["enabled"],
            is_primary_subject=row["is_primary_subject"],
            origin=row["origin"],
            reviewed=row["reviewed"],
            merged_into_id=row["merged_into_id"],
            last_seen_at=iso(row["last_seen_at_ms"]),
            camera_ids=row["camera_ids_json"],
            references=[self.reference(conn, asset) for asset in ids],
            reference_capacity={
                "active_reference_count": sum(
                    1 for asset in ids if one(conn, s.reference_assets, asset)["enabled"]
                )
            },
            deleted_at=iso(row["deleted_at_ms"]),
        )

    def create_entity(self, conn, dto: EntityCreate):
        if dto.kind != "person":
            raise AppError(422, "VALIDATION_ERROR", "只允许创建人物档案")
        if dto.enabled or dto.is_primary_subject:
            raise AppError(422, "REFERENCE_REQUIRED", "请先保存草稿并上传启用参考图，再启用档案")
        for camera_id in dto.camera_ids:
            one(conn, s.cameras, camera_id)
        resource_id = new_id("entity")
        conn.execute(
            s.entities.insert().values(
                id=resource_id,
                revision=1,
                kind=dto.kind,
                name=dto.name,
                description=dto.description,
                enabled=False,
                is_primary_subject=False,
                camera_ids_json=sorted(set(dto.camera_ids)),
                created_at_ms=self.w.now(),
                updated_at_ms=self.w.now(),
            )
        )
        return self.entity(conn, resource_id)

    def sync_primary(self, conn, entity_id: str | None):
        settings = one(conn, s.settings, "singleton")
        cfg = dict(settings["config_json"])
        if cfg.get("primary_subject_id") == entity_id:
            return
        conn.execute(
            update(s.entities)
            .where(s.entities.c.is_primary_subject.is_(True), s.entities.c.id != (entity_id or ""))
            .values(is_primary_subject=False, revision=s.entities.c.revision + 1, updated_at_ms=self.w.now())
        )
        cfg["primary_subject_id"] = entity_id
        conn.execute(
            update(s.settings)
            .where(s.settings.c.id == "singleton")
            .values(config_json=cfg, revision=settings["revision"] + 1, updated_at_ms=self.w.now())
        )

    def update_entity(self, conn, entity_id, patch, header):
        row = one(conn, s.entities, entity_id)
        require_revision(row, header)
        enabled = patch.get("enabled", row["enabled"])
        primary = patch.get("is_primary_subject", row["is_primary_subject"]) and enabled
        if primary and row["kind"] != "person":
            raise AppError(422, "VALIDATION_ERROR", "物品不能设为主人物")
        if "camera_ids" in patch:
            for camera in patch["camera_ids"]:
                one(conn, s.cameras, camera)
        if primary:
            self.sync_primary(conn, entity_id)
        elif row["is_primary_subject"]:
            self.sync_primary(conn, None)
        changes = {k: v for k, v in patch.items() if k in {"name", "description", "enabled"}}
        if "camera_ids" in patch:
            changes["camera_ids_json"] = sorted(set(patch["camera_ids"]))
        changes |= dict(
            is_primary_subject=primary,
            reviewed=True,
            revision=row["revision"] + 1,
            updated_at_ms=self.w.now(),
        )
        conn.execute(update(s.entities).where(s.entities.c.id == entity_id).values(**changes))
        self.validate_capacity(conn)
        self.reference_set(conn, None)
        return self.entity(conn, entity_id)

    def attach_reference(
        self, conn, entity_id, prepared, name, enabled, header, source_evidence_id=None, asset_id=None
    ):
        entity = one(conn, s.entities, entity_id)
        now = self.w.now()
        if asset_id:
            asset = one(conn, s.reference_assets, asset_id)
            require_revision(asset, header)
            version = asset["current_version"] + 1
            conn.execute(
                update(s.reference_assets)
                .where(s.reference_assets.c.id == asset_id)
                .values(current_version=version, revision=asset["revision"] + 1, updated_at_ms=now)
            )
            old = self.reference(conn, asset_id, version=asset["current_version"])
            name = name or old["name_snapshot"]
        else:
            require_revision(entity, header)
            asset_id, version = new_id("asset"), 1
            conn.execute(
                s.reference_assets.insert().values(
                    id=asset_id,
                    entity_id=entity_id,
                    current_version=version,
                    enabled=enabled,
                    revision=1,
                    created_at_ms=now,
                    updated_at_ms=now,
                    source_evidence_id=source_evidence_id,
                )
            )
        if source_evidence_id:
            if self.metadata(conn, source_evidence_id)["state"] != "available":
                raise AppError(410, "EVIDENCE_EXPIRED", "现场图已不可用")
        conn.execute(
            s.reference_asset_versions.insert().values(
                asset_id=asset_id,
                version=version,
                name_snapshot=name or entity["name"],
                relpath=prepared["relpath"],
                sha256=prepared["sha256"],
                width=prepared["width"],
                height=prepared["height"],
                bytes=prepared["bytes"],
                created_at_ms=now,
            )
        )
        conn.execute(
            update(s.entities)
            .where(s.entities.c.id == entity_id)
            .values(revision=entity["revision"] + 1, updated_at_ms=now)
        )
        self.validate_capacity(conn)
        self.reference_set(conn, None)
        return self.reference(conn, asset_id)

    def update_reference(self, conn, asset_id, patch, header):
        asset = one(conn, s.reference_assets, asset_id)
        require_revision(asset, header)
        entity = one(conn, s.entities, asset["entity_id"])
        changes = dict(revision=asset["revision"] + 1, updated_at_ms=self.w.now())
        if "enabled" in patch:
            changes["enabled"] = patch["enabled"]
        if "name" in patch:
            changes["name_override"] = patch["name"]
        conn.execute(update(s.reference_assets).where(s.reference_assets.c.id == asset_id).values(**changes))
        count = conn.execute(
            select(func.count())
            .select_from(s.reference_assets)
            .where(
                s.reference_assets.c.entity_id == entity["id"],
                s.reference_assets.c.enabled.is_(True),
                s.reference_assets.c.deleted_at_ms.is_(None),
            )
        ).scalar_one()
        if entity["enabled"] and not count:
            raise AppError(422, "REFERENCE_REQUIRED", "启用档案不能停用最后一张参考图，请先停用档案")
        conn.execute(
            update(s.entities)
            .where(s.entities.c.id == entity["id"])
            .values(revision=entity["revision"] + 1, updated_at_ms=self.w.now())
        )
        self.validate_capacity(conn)
        self.reference_set(conn, None)
        return self.reference(conn, asset_id)

    def delete_reference(self, conn, asset_id, header=None, *, internal=False):
        asset = one(conn, s.reference_assets, asset_id)
        if not internal:
            require_revision(asset, header)
        conn.execute(
            update(s.reference_assets)
            .where(s.reference_assets.c.id == asset_id)
            .values(
                deleted_at_ms=self.w.now(),
                enabled=False,
                revision=asset["revision"] + 1,
                updated_at_ms=self.w.now(),
            )
        )
        entity = one(conn, s.entities, asset["entity_id"], deleted=True)
        remaining = conn.execute(
            select(func.count())
            .select_from(s.reference_assets)
            .where(
                s.reference_assets.c.entity_id == entity["id"],
                s.reference_assets.c.enabled.is_(True),
                s.reference_assets.c.deleted_at_ms.is_(None),
            )
        ).scalar_one()
        if not remaining and entity["is_primary_subject"]:
            self.sync_primary(conn, None)
        conn.execute(
            update(s.entities)
            .where(s.entities.c.id == entity["id"])
            .values(
                revision=entity["revision"] + 1,
                updated_at_ms=self.w.now(),
                **({"enabled": False, "is_primary_subject": False} if not remaining else {}),
            )
        )
        self.reference_set(conn, None)
        return job_dto(
            enqueue(
                conn, self.w.now(), "cleanup_assets", {"asset_id": asset_id}, f"delete-reference:{asset_id}"
            )
        )

    def delete_entity(self, conn, entity_id, header):
        entity = one(conn, s.entities, entity_id)
        require_revision(entity, header)
        ids = list(
            conn.execute(
                select(s.reference_assets.c.id).where(
                    s.reference_assets.c.entity_id == entity_id, s.reference_assets.c.deleted_at_ms.is_(None)
                )
            ).scalars()
        )
        for asset_id in ids:
            self.delete_reference(conn, asset_id, internal=True)
        if entity["is_primary_subject"]:
            self.sync_primary(conn, None)
        conn.execute(
            update(s.entities)
            .where(s.entities.c.id == entity_id)
            .values(
                enabled=False,
                is_primary_subject=False,
                deleted_at_ms=self.w.now(),
                revision=s.entities.c.revision + 1,
                updated_at_ms=self.w.now(),
            )
        )
        return job_dto(
            enqueue(
                conn, self.w.now(), "cleanup_assets", {"entity_id": entity_id}, f"delete-entity:{entity_id}"
            )
        )
