from __future__ import annotations

import math
import shutil

from sqlalchemy import delete, func, select, update

from app.config import Config
from app.errors import AppError, require_revision
from app.repositories import schema as s
from app.repositories.db import Database, new_id, one
from app.schemas import CameraCreate, ProviderWrite, Regions, SettingsConfig
from app.security.auth import Auth
from app.security.secrets import Secrets, fingerprint, validate_provider_url
from app.timeutils import iso, now_ms, schedule_intervals, timestamp
from app.worker.queue import Queue, enqueue, job_dto


class Workbench:
    def __init__(self, config: Config, *, migrate=False):
        self.config = config
        config.prepare()
        self.secrets = Secrets(config.master_key_file)
        self.db = Database(config.database_url)
        if migrate:
            self.db.migrate()
        else:
            self.db.check_schema()
        self.auth = Auth(self)
        self.queue = Queue(self)
        from app.activities.ledger import Ledger
        from app.services.assets import Assets

        self.assets = Assets(self)
        self.ledger = Ledger(self)

    def now(self) -> int:
        return self.config.clock() if self.config.clock else now_ms()

    def settings_config(self, conn) -> SettingsConfig:
        return SettingsConfig.model_validate(one(conn, s.settings, "singleton")["config_json"])

    def settings(self, conn) -> dict:
        row = one(conn, s.settings, "singleton")
        cfg = SettingsConfig.model_validate(row["config_json"])
        limits = {}
        for purpose in ("vision", "summary"):
            provider_id = getattr(cfg, f"{purpose}_provider_id")
            if provider_id:
                p = one(conn, s.providers, provider_id)
                limits[purpose] = self.effective_limits(cfg, p)
            else:
                limits[purpose] = None
        return dict(
            revision=row["revision"],
            config=cfg.model_dump(),
            effective_limits=limits,
            defaults=SettingsConfig().model_dump(),
            validation_hints={
                "minimum_retention_minutes": math.ceil(cfg.minimum_retention_seconds() / 60),
                "capacity_requires_target_validation": True,
            },
        )

    @staticmethod
    def effective_limits(cfg: SettingsConfig, provider: dict) -> dict:
        p, cap = provider["config_json"], provider["capabilities_json"]
        return dict(
            max_total_images=min(
                cfg.max_total_images_per_request,
                p["max_total_images"],
                cap.get("verified_max_images") or p["max_total_images"],
            ),
            max_capture_images=min(cfg.max_capture_images_per_request, p["max_capture_images"]),
            max_request_body_bytes=min(
                cfg.max_request_body_mib * 1024**2,
                p["max_request_body_mib"] * 1024**2,
                cap.get("verified_max_body_bytes") or p["max_request_body_mib"] * 1024**2,
            ),
            timeout_seconds=min(cfg.ai_timeout_seconds, p["timeout_seconds"]),
        )

    def replay(self, conn, user_id: str, operation: str, key: str, request_hash: str):
        row = (
            conn.execute(
                select(s.idempotency_keys).where(
                    s.idempotency_keys.c.user_id == user_id,
                    s.idempotency_keys.c.operation_id == operation,
                    s.idempotency_keys.c.key == key,
                    s.idempotency_keys.c.expires_at_ms > self.now(),
                )
            )
            .mappings()
            .first()
        )
        if row:
            if row["request_hash"] != request_hash:
                raise AppError(409, "IDEMPOTENCY_CONFLICT", "此幂等键已用于不同请求")
            return row["response_status"], row["response_json"]
        return None

    def write(
        self,
        user_id: str,
        operation: str,
        key: str | None,
        payload: dict,
        mutation,
        request_id: str | None = None,
    ) -> tuple[int, dict | None]:
        if not key or not 1 <= len(key) <= 200:
            raise AppError(
                422,
                "VALIDATION_ERROR",
                "请提供长度为 1～200 的 Idempotency-Key",
                fields=[{"path": "Idempotency-Key", "code": "required", "message": "缺少幂等键"}],
            )
        request_hash = fingerprint(payload)
        with self.db.write() as conn:
            replay = self.replay(conn, user_id, operation, key, request_hash)
            if replay:
                return replay
            status, data = mutation(conn)
            conn.execute(
                delete(s.idempotency_keys).where(
                    s.idempotency_keys.c.user_id == user_id,
                    s.idempotency_keys.c.operation_id == operation,
                    s.idempotency_keys.c.key == key,
                )
            )
            conn.execute(
                s.idempotency_keys.insert().values(
                    user_id=user_id,
                    operation_id=operation,
                    key=key,
                    request_hash=request_hash,
                    resource_id=data.get("id") if isinstance(data, dict) else None,
                    response_status=status,
                    response_json=data,
                    expires_at_ms=self.now() + 86400000,
                )
            )
            conn.execute(
                s.audit_events.insert().values(
                    id=new_id("audit"),
                    occurred_at_ms=self.now(),
                    action=operation,
                    resource_kind=operation,
                    resource_id=data.get("id") if isinstance(data, dict) else None,
                    actor=user_id,
                    request_id=request_id,
                    safe_details_json={},
                )
            )
            return status, data

    def preview_token(
        self, operation: str, resource_id: str, revision: int, patch: dict, extra: dict, ttl=300000
    ) -> tuple[str, int]:
        expires = self.now() + ttl
        return self.secrets.sign(
            {
                "operation": operation,
                "id": resource_id,
                "revision": revision,
                "patch": fingerprint(patch),
                "extra": extra,
                "expires": expires,
            }
        ), expires

    def check_preview(
        self, token: str | None, operation: str, resource_id: str, revision: int, patch: dict, extra: dict
    ):
        if not token:
            raise AppError(409, "PREVIEW_STALE", "此修改需要先预检影响")
        info = self.secrets.verify(token)
        if info.get("expires", 0) <= self.now() or any(
            info.get(k) != v
            for k, v in {
                "operation": operation,
                "id": resource_id,
                "revision": revision,
                "patch": fingerprint(patch),
                "extra": extra,
            }.items()
        ):
            raise AppError(409, "PREVIEW_STALE", "预检已过期或配置发生变化，请重新预检")

    def config_fingerprint(self, conn) -> dict:
        return {
            "settings": one(conn, s.settings, "singleton")["revision"],
            "cameras": [
                [r.id, r.revision]
                for r in conn.execute(select(s.cameras.c.id, s.cameras.c.revision).order_by(s.cameras.c.id))
            ],
            "providers": [
                [r.id, r.revision, r.tested_config_revision]
                for r in conn.execute(
                    select(
                        s.providers.c.id, s.providers.c.revision, s.providers.c.tested_config_revision
                    ).order_by(s.providers.c.id)
                )
            ],
            "references": self.assets.reference_fingerprint(conn),
        }

    def camera(self, conn, resource_id: str, deleted=False) -> dict:
        row = one(conn, s.cameras, resource_id, deleted=deleted)
        cfg = row["config_json"]
        runtime = dict(
            conn.execute(select(s.camera_runtime).where(s.camera_runtime.c.camera_id == resource_id))
            .mappings()
            .one()
        )
        return dict(
            id=row["id"],
            name=row["name"],
            enabled=row["enabled"],
            revision=row["revision"],
            source_revision=row["source_revision"],
            source=cfg["source"] | {"credential_configured": bool(row["credentials_cipher"])},
            credential_configured=bool(row["credentials_cipher"]),
            schedule=cfg["schedule"],
            retention_minutes=cfg["retention_minutes"],
            effective_retention_minutes=cfg["retention_minutes"]
            or self.settings_config(conn).frame_retention_minutes,
            regions=cfg["regions"],
            regions_state=cfg.get("regions_state", "current"),
            last_test=cfg.get("last_test"),
            runtime=dict(
                capture_state=runtime["capture_state"],
                density_state=runtime["density_state"],
                stop_reason=runtime["stop_reason"],
                last_frame_at=iso(runtime["last_frame_at_ms"]),
                stream_session_id=runtime["stream_session_id"],
                control_revision=runtime["control_revision"],
                applied_revision=runtime["applied_revision"],
                config_applied_revision=runtime["config_applied_revision"],
                override_mode=runtime["override_mode"],
                override_expires_at=iso(runtime["override_until_ms"]),
                last_error_code=runtime["last_error_code"],
            ),
            deleted_at=iso(row["deleted_at_ms"]),
        )

    def validate_source(self, source: dict):
        if source["host"] not in self.config.rtsp_allowed_hosts:
            raise AppError(
                422,
                "VALIDATION_ERROR",
                "RTSP 主机不在部署允许清单中",
                fields=[{"path": "source.host", "code": "not_allowed", "message": "主机不在白名单中"}],
            )
        if source["port"] not in self.config.rtsp_allowed_ports:
            raise AppError(
                422,
                "VALIDATION_ERROR",
                "RTSP 端口不在部署允许清单中",
                fields=[{"path": "source.port", "code": "not_allowed", "message": "端口不在白名单中"}],
            )

    def validate_camera(self, conn, cfg: dict):
        self.validate_source(cfg["source"])
        effective = cfg["retention_minutes"] or self.settings_config(conn).frame_retention_minutes
        if effective * 60 < self.settings_config(conn).minimum_retention_seconds():
            raise AppError(422, "RETENTION_TOO_SHORT", "摄像头保留期不足以覆盖提交和重试窗口")
        from app.detection.motion import region_mask

        region_mask(Regions.model_validate(cfg["regions"]).model_dump(), 320, 180)

    def save_schedule(self, conn, camera: dict, runtime: dict | None = None):
        version = (
            conn.execute(
                select(func.max(s.schedule_versions.c.version)).where(
                    s.schedule_versions.c.camera_id == camera["id"]
                )
            ).scalar()
            or 0
        ) + 1
        if runtime is None:
            runtime = dict(
                conn.execute(select(s.camera_runtime).where(s.camera_runtime.c.camera_id == camera["id"]))
                .mappings()
                .one()
            )
        conn.execute(
            s.schedule_versions.insert().values(
                camera_id=camera["id"],
                version=version,
                effective_from_ms=((self.now() + 999) // 1000) * 1000,
                timezone="Asia/Shanghai",
                weekly_rules_json=camera["config_json"]["schedule"],
                override_json={
                    "enabled": camera["enabled"] and camera.get("deleted_at_ms") is None,
                    "mode": runtime["override_mode"],
                    "until_ms": runtime["override_until_ms"],
                },
            )
        )

    def create_camera(self, conn, dto: CameraCreate):
        cfg = dto.model_dump(exclude={"name", "enabled"})
        secret = cfg["source"].pop("password", None)
        cfg["source"].pop("credential_action", None)
        self.validate_camera(conn, cfg)
        now = self.now()
        camera = dict(
            id=new_id("cam"),
            name=dto.name,
            enabled=False,
            revision=1,
            source_revision=1,
            config_json=cfg,
            credentials_cipher=self.secrets.encrypt(secret) if secret else None,
            created_at_ms=now,
            updated_at_ms=now,
            deleted_at_ms=None,
        )
        conn.execute(s.cameras.insert().values(**camera))
        conn.execute(
            s.camera_runtime.insert().values(
                camera_id=camera["id"], stop_reason="disabled", diagnostics_json={}
            )
        )
        self.save_schedule(conn, camera)
        return self.camera(conn, camera["id"])

    def merged_camera(self, row: dict, patch: dict) -> tuple[dict, str | None]:
        cfg = dict(row["config_json"])
        credentials = row["credentials_cipher"]
        if "source" in patch:
            source_patch = dict(patch["source"])
            password = source_patch.pop("password", None)
            action = source_patch.pop("credential_action", "keep")
            cfg["source"] = cfg["source"] | source_patch
            if password:
                credentials = self.secrets.encrypt(password)
            if action == "clear":
                credentials = None
        cfg.update({k: v for k, v in patch.items() if k in {"schedule", "retention_minutes", "regions"}})
        return cfg, credentials

    def capacity(
        self, conn, cfg: SettingsConfig, enabled_ids: list[str], retention_override: dict | None = None
    ) -> dict:
        estimates, warnings = [], []
        for camera_id in enabled_ids:
            camera = one(conn, s.cameras, camera_id)
            samples = conn.execute(
                select(s.frames.c.bytes, s.frames.c.captured_at_ms)
                .where(
                    s.frames.c.camera_id == camera_id,
                    s.frames.c.is_test.is_(False),
                    s.frames.c.captured_at_ms >= self.now() - 600000,
                )
                .order_by(s.frames.c.captured_at_ms)
            ).all()
            retention = (
                (retention_override or {}).get(camera_id)
                or camera["config_json"].get("retention_minutes")
                or cfg.frame_retention_minutes
            )
            verified = (
                len(samples) >= 250 and samples[-1].captured_at_ms - samples[0].captured_at_ms >= 299000
            )
            if not verified:
                if len(enabled_ids) > 1:
                    raise AppError(
                        422, "CAPACITY_UNVERIFIED", "新增多路前需取得每路至少 5 分钟的图像大小样本"
                    )
                warnings.append(
                    {
                        "code": "CAPACITY_UNVERIFIED",
                        "message": "尚无五分钟样本，单路试采受配额和磁盘保护",
                        "affected_resource_ids": [camera_id],
                    }
                )
            sizes = sorted(r.bytes for r in samples)
            estimates.append(
                {
                    "camera_id": camera_id,
                    "median": sizes[len(sizes) // 2] * retention * 60 if sizes else None,
                    "p95": sizes[min(len(sizes) - 1, math.ceil(len(sizes) * 0.95) - 1)] * retention * 60
                    if sizes
                    else None,
                }
            )
        known = [e["p95"] for e in estimates if e["p95"] is not None]
        if sum(known) > cfg.media_quota_gb * 1e9:
            raise AppError(422, "CAPACITY_EXCEEDED", "预计现场图容量超过媒体配额")
        return {
            "cameras": estimates,
            "median": sum(e["median"] for e in estimates)
            if estimates and all(e["median"] is not None for e in estimates)
            else None,
            "p95": sum(known) if len(known) == len(estimates) else None,
            "warnings": warnings,
        }

    def impact(self, conn, patch: dict, resource_id="singleton", base_revision=None, disk_free=None) -> dict:
        global_row = one(conn, s.settings, "singleton")
        cfg = (
            SettingsConfig.model_validate(global_row["config_json"] | patch)
            if resource_id == "singleton"
            else self.settings_config(conn)
        )
        current = global_row if resource_id == "singleton" else one(conn, s.cameras, resource_id)
        if cfg.ai_concurrency > self.config.validated_ai_concurrency:
            raise AppError(422, "CAPACITY_UNVERIFIED", "此并发值尚未通过部署环境容量验证")
        if base_revision != current["revision"]:
            raise AppError(
                412, "REVISION_CONFLICT", "预检基线已过期", details={"revision": current["revision"]}
            )
        enabled_ids = list(
            conn.execute(
                select(s.cameras.c.id).where(
                    s.cameras.c.enabled.is_(True), s.cameras.c.deleted_at_ms.is_(None)
                )
            ).scalars()
        )
        override = {}
        if resource_id != "singleton":
            camera_cfg, _ = self.merged_camera(current, patch)
            self.validate_camera(conn, camera_cfg)
            override[resource_id] = camera_cfg.get("retention_minutes")
            if patch.get("enabled") is True and resource_id not in enabled_ids:
                enabled_ids.append(resource_id)
            elif patch.get("enabled") is False and resource_id in enabled_ids:
                enabled_ids.remove(resource_id)
        self.validate_settings_links(conn, cfg)
        estimate = self.capacity(conn, cfg, enabled_ids, override)
        if disk_free is not None and enabled_ids and disk_free < cfg.disk_pressure_free_gb * 1e9:
            raise AppError(503, "STORAGE_PRESSURE", "可用空间低于启用阈值", retryable=True)
        self.assets.validate_capacity(conn, cfg)
        frames = conn.execute(
            select(s.frames.c.camera_id, s.frames.c.captured_at_ms, s.frames.c.bytes).where(
                s.frames.c.state == "available"
            )
        ).all()
        retention_map = {}
        for camera in conn.execute(select(s.cameras)).mappings():
            retention_map[camera["id"]] = (
                override.get(camera["id"], camera["config_json"].get("retention_minutes"))
                or cfg.frame_retention_minutes
            )
        expired = [f for f in frames if f.captured_at_ms + retention_map[f.camera_id] * 60000 <= self.now()]
        token, expiry = self.preview_token(
            "settings" if resource_id == "singleton" else "camera",
            resource_id,
            current["revision"],
            patch,
            self.config_fingerprint(conn),
        )
        return dict(
            preview_token=token,
            expires_at=iso(expiry),
            affected_camera_ids=enabled_ids if resource_id == "singleton" else [resource_id],
            will_expire_frame_count=len(expired),
            will_expire_bytes=sum(f.bytes for f in expired),
            estimated_media_bytes={k: v for k, v in estimate.items() if k != "warnings"},
            requires_reconnect="source" in patch,
            requires_roi_review="source" in patch,
            warnings=estimate["warnings"],
            blocking_errors=[],
        )

    def validate_settings_links(self, conn, cfg: SettingsConfig):
        for provider_id in {cfg.vision_provider_id, cfg.summary_provider_id} - {None}:
            p = one(conn, s.providers, provider_id)
            self.require_provider(p)

    @staticmethod
    def require_provider(provider):
        cap = provider["capabilities_json"]
        if (
            provider["tested_config_revision"] != provider["revision"]
            or not cap.get("vision")
            or not cap.get("data_url")
            or provider["config_json"]["response_format_mode"] not in cap.get("structured_output_modes", [])
        ):
            raise AppError(422, "PROVIDER_NOT_VERIFIED", "模型服务需通过当前配置的图像与结构化输出测试")

    def update_settings(self, conn, dto, header, disk_free=None):
        row = one(conn, s.settings, "singleton")
        require_revision(row, header)
        cfg = SettingsConfig.model_validate(row["config_json"] | dto.patch)
        self.validate_settings_links(conn, cfg)
        self.assets.validate_capacity(conn, cfg)
        self.check_preview(
            dto.preview_token,
            "settings",
            "singleton",
            row["revision"],
            dto.patch,
            self.config_fingerprint(conn),
        )
        if cfg.ai_concurrency > self.config.validated_ai_concurrency:
            raise AppError(422, "CAPACITY_UNVERIFIED", "此并发值尚未通过部署环境容量验证")
        self.impact(conn, dto.patch, base_revision=row["revision"], disk_free=disk_free)
        for camera in conn.execute(select(s.cameras).where(s.cameras.c.deleted_at_ms.is_(None))).mappings():
            ttl = camera["config_json"].get("retention_minutes") or cfg.frame_retention_minutes
            if ttl * 60 < cfg.minimum_retention_seconds():
                raise AppError(422, "RETENTION_TOO_SHORT", "现有摄像头覆盖保留期小于新配置要求")
        if cfg.primary_subject_id != row["config_json"].get("primary_subject_id"):
            conn.execute(
                update(s.entities)
                .where(s.entities.c.is_primary_subject.is_(True))
                .values(
                    is_primary_subject=False, revision=s.entities.c.revision + 1, updated_at_ms=self.now()
                )
            )
            if cfg.primary_subject_id:
                conn.execute(
                    update(s.entities)
                    .where(s.entities.c.id == cfg.primary_subject_id)
                    .values(
                        is_primary_subject=True, revision=s.entities.c.revision + 1, updated_at_ms=self.now()
                    )
                )
        # 在更新 TTL 前，固定旧策略下已经过期的资产状态，延长不能复活它们。
        self.assets.freeze_expired(conn)
        conn.execute(
            update(s.settings)
            .where(s.settings.c.id == "singleton")
            .values(config_json=cfg.model_dump(), revision=row["revision"] + 1, updated_at_ms=self.now())
        )
        self.assets.sync_expiry(conn)
        job = enqueue(
            conn,
            self.now(),
            "apply_settings",
            {"revision": row["revision"] + 1, "patch": dto.patch},
            f"settings:{row['revision'] + 1}",
        )
        return {"settings": self.settings(conn), "apply_job": job_dto(job)}

    def update_camera(self, conn, resource_id, patch: dict, token, header, disk_free=None):
        row = one(conn, s.cameras, resource_id)
        require_revision(row, header)
        cfg, credentials = self.merged_camera(row, patch)
        self.validate_camera(conn, cfg)
        impactful = bool(set(patch) & {"enabled", "source", "retention_minutes"})
        if impactful:
            self.check_preview(
                token, "camera", resource_id, row["revision"], patch, self.config_fingerprint(conn)
            )
            self.impact(conn, patch, resource_id, row["revision"], disk_free=disk_free)
        enabled = patch.get("enabled", row["enabled"])
        settings = self.settings_config(conn)
        if enabled and (not settings.vision_provider_id or not settings.summary_provider_id):
            raise AppError(422, "PROVIDER_NOT_VERIFIED", "启用前请配置并测试视觉和汇总服务")
        source_changed = bool(set(patch) & {"source", "regions"})
        if "source" in patch:
            cfg["regions_state"] = "needs_review"
        if "regions" in patch:
            cfg["regions_state"] = "current"
        self.assets.freeze_expired(conn)
        updated = row | {
            "name": patch.get("name", row["name"]),
            "enabled": enabled,
            "revision": row["revision"] + 1,
            "source_revision": row["source_revision"] + int(source_changed),
            "config_json": cfg,
            "credentials_cipher": credentials,
            "updated_at_ms": self.now(),
        }
        conn.execute(
            update(s.cameras)
            .where(s.cameras.c.id == resource_id)
            .values(**{k: v for k, v in updated.items() if k != "id"})
        )
        conn.execute(
            update(s.camera_runtime)
            .where(s.camera_runtime.c.camera_id == resource_id)
            .values(control_revision=s.camera_runtime.c.control_revision + 1)
        )
        self.save_schedule(conn, updated)
        self.assets.sync_expiry(conn, camera_id=resource_id)
        return self.camera(conn, resource_id)

    def delete_camera(self, conn, resource_id, header):
        row = one(conn, s.cameras, resource_id)
        require_revision(row, header)
        now = self.now()
        conn.execute(
            update(s.cameras)
            .where(s.cameras.c.id == resource_id)
            .values(enabled=False, deleted_at_ms=now, revision=row["revision"] + 1, updated_at_ms=now)
        )
        conn.execute(
            update(s.camera_runtime)
            .where(s.camera_runtime.c.camera_id == resource_id)
            .values(
                control_revision=s.camera_runtime.c.control_revision + 1,
                override_mode=None,
                override_until_ms=None,
                stop_reason="deleted",
            )
        )
        self.save_schedule(conn, row | {"enabled": False, "deleted_at_ms": now})
        return job_dto(
            enqueue(
                conn,
                now,
                "apply_settings",
                {"camera_id": resource_id, "deleted": True},
                f"delete-camera:{resource_id}:{row['revision']}",
            )
        )

    def control_camera(self, conn, resource_id, mode, expires_at, header):
        row = one(conn, s.cameras, resource_id)
        require_revision(row, header)
        if not row["enabled"]:
            raise AppError(409, "CAMERA_DISABLED", "摄像头已在配置中禁用")
        now = self.now()
        until = timestamp(expires_at) if expires_at else None
        if mode:
            if until is None:
                windows = schedule_intervals(row["config_json"]["schedule"], now, now + 8 * 86400000)
                boundaries = [p for span in windows for p in span if now < p <= now + 86400000]
                until = min(boundaries) if boundaries else now + 3600000
            if not now < until <= now + 86400000:
                raise AppError(422, "VALIDATION_ERROR", "临时覆盖必须在未来且不超过 24 小时")
        rt = dict(
            conn.execute(select(s.camera_runtime).where(s.camera_runtime.c.camera_id == resource_id))
            .mappings()
            .one()
        )
        changes = dict(
            control_revision=rt["control_revision"] + 1,
            override_mode=mode,
            override_until_ms=until if mode else None,
        )
        conn.execute(
            update(s.camera_runtime).where(s.camera_runtime.c.camera_id == resource_id).values(**changes)
        )
        conn.execute(
            update(s.cameras)
            .where(s.cameras.c.id == resource_id)
            .values(revision=row["revision"] + 1, updated_at_ms=now)
        )
        self.save_schedule(conn, row, rt | changes)
        desired = (
            "online"
            if mode == "force_on"
            or (mode is None and schedule_intervals(row["config_json"]["schedule"], now, now + 1))
            else "stopped"
        )
        return dict(
            camera_id=resource_id,
            revision=row["revision"] + 1,
            control_revision=changes["control_revision"],
            applied_revision=rt["applied_revision"],
            override_mode=mode,
            override_expires_at=iso(until) if mode else None,
            desired_capture_state=desired,
        )

    def provider(self, conn, resource_id):
        row = one(conn, s.providers, resource_id)
        cfg = row["config_json"]
        return dict(
            id=row["id"],
            revision=row["revision"],
            name=row["name"],
            base_url=cfg["base_url"],
            model=cfg["model"],
            api_key_configured=bool(row["api_key_cipher"]),
            config=cfg,
            capabilities=row["capabilities_json"],
            tested_config_revision=row["tested_config_revision"],
            tested_at=iso(row["tested_at_ms"]),
            last_test_error=row["last_test_error"],
        )

    def write_provider(self, conn, dto: ProviderWrite, resource_id=None, header=None):
        # DNS 和出站连接使用时再次验证；保存只检查部署白名单，避免事务中等待网络。
        cfg = dto.model_dump(exclude={"name", "api_key", "api_key_action"})
        cfg["base_url"] = validate_provider_url(dto.base_url, self.config, resolve=False)
        now = self.now()
        if resource_id:
            row = one(conn, s.providers, resource_id)
            require_revision(row, header)
            cipher = self.secrets.encrypt(dto.api_key) if dto.api_key else row["api_key_cipher"]
            if dto.api_key_action == "clear":
                cipher = None
            conn.execute(
                update(s.providers)
                .where(s.providers.c.id == resource_id)
                .values(
                    name=dto.name,
                    config_json=cfg,
                    api_key_cipher=cipher,
                    revision=row["revision"] + 1,
                    updated_at_ms=now,
                    tested_config_revision=None,
                    tested_at_ms=None,
                    capabilities_json={},
                    last_test_error=None,
                    paused=False,
                )
            )
        else:
            resource_id = new_id("provider")
            conn.execute(
                s.providers.insert().values(
                    id=resource_id,
                    name=dto.name,
                    config_json=cfg,
                    api_key_cipher=self.secrets.encrypt(dto.api_key) if dto.api_key else None,
                    capabilities_json={},
                    revision=1,
                    created_at_ms=now,
                    updated_at_ms=now,
                    paused=False,
                )
            )
            conn.execute(
                s.provider_pricing.insert().values(
                    id=new_id("pricing"),
                    provider_id=resource_id,
                    revision=1,
                    created_at_ms=now,
                    updated_at_ms=now,
                )
            )
        return self.provider(conn, resource_id)

    def provider_pricing(self, conn, provider_id: str) -> dict:
        provider = one(conn, s.providers, provider_id)
        row = (
            conn.execute(select(s.provider_pricing).where(s.provider_pricing.c.provider_id == provider_id))
            .mappings()
            .first()
        )
        if not row:
            now = self.now()
            pricing_id = new_id("pricing")
            conn.execute(
                s.provider_pricing.insert().values(
                    id=pricing_id,
                    provider_id=provider_id,
                    revision=1,
                    created_at_ms=now,
                    updated_at_ms=now,
                )
            )
            row = (
                conn.execute(select(s.provider_pricing).where(s.provider_pricing.c.id == pricing_id))
                .mappings()
                .one()
            )

        cfg = provider["config_json"]
        is_configured = bool(row["currency"] and row["input_price"] and row["output_price"])
        applicable = bool(
            is_configured and row["base_url"] == cfg["base_url"] and row["model"] == cfg["model"]
        )
        return dict(
            provider_id=provider_id,
            pricing_revision=row["revision"],
            provider_revision=provider["revision"],
            applicable=applicable,
            price_configured=is_configured,
            pricing=dict(
                currency=row["currency"],
                input_price=row["input_price"],
                output_price=row["output_price"],
                cached_input_price=row["cached_input_price"],
                source_note=row["source_note"],
            )
            if is_configured
            else None,
            bound_base_url=row["base_url"],
            bound_model=row["model"],
            updated_at=iso(row["updated_at_ms"]),
        )

    def write_provider_pricing(self, conn, provider_id: str, dto, header: str | None) -> dict:
        provider = one(conn, s.providers, provider_id)
        if provider["revision"] != dto.provider_revision:
            raise AppError(
                412,
                "REVISION_CONFLICT",
                "模型服务配置版本已变化，请刷新后重试",
                details={"provider_revision": provider["revision"]},
            )
        row = (
            conn.execute(select(s.provider_pricing).where(s.provider_pricing.c.provider_id == provider_id))
            .mappings()
            .first()
        )
        if not row:
            now = self.now()
            pricing_id = new_id("pricing")
            conn.execute(
                s.provider_pricing.insert().values(
                    id=pricing_id,
                    provider_id=provider_id,
                    revision=1,
                    created_at_ms=now,
                    updated_at_ms=now,
                )
            )
            row = (
                conn.execute(select(s.provider_pricing).where(s.provider_pricing.c.id == pricing_id))
                .mappings()
                .one()
            )

        require_revision(row, header)
        now = self.now()
        new_rev = row["revision"] + 1

        if dto.clear:
            conn.execute(
                update(s.provider_pricing)
                .where(s.provider_pricing.c.id == row["id"])
                .values(
                    base_url=None,
                    model=None,
                    currency=None,
                    input_price=None,
                    output_price=None,
                    cached_input_price=None,
                    source_note=None,
                    revision=new_rev,
                    updated_at_ms=now,
                )
            )
        else:
            cfg = provider["config_json"]
            conn.execute(
                update(s.provider_pricing)
                .where(s.provider_pricing.c.id == row["id"])
                .values(
                    base_url=cfg["base_url"],
                    model=cfg["model"],
                    currency=dto.currency,
                    input_price=dto.input_price,
                    output_price=dto.output_price,
                    cached_input_price=dto.cached_input_price,
                    source_note=dto.source_note,
                    revision=new_rev,
                    updated_at_ms=now,
                )
            )
            if dto.currency == "USD":
                rate_row = (
                    conn.execute(select(s.exchange_rates).where(s.exchange_rates.c.id == "usd_cny"))
                    .mappings()
                    .first()
                )
                if not rate_row or not self.is_rate_valid(rate_row["rate_date"]):
                    from app.worker.queue import enqueue

                    enqueue(
                        conn,
                        now,
                        "exchange_rate_refresh",
                        {"reason": "initial_usd_pricing"},
                        f"exchange-rate-initial:{now}",
                        total=1,
                    )
        return self.provider_pricing(conn, provider_id)

    def has_usd_pricing(self, conn) -> bool:
        count = conn.execute(
            select(func.count()).select_from(s.provider_pricing).where(s.provider_pricing.c.currency == "USD")
        ).scalar_one()
        return count > 0

    def has_valid_rate(self, conn) -> bool:
        row = (
            conn.execute(select(s.exchange_rates).where(s.exchange_rates.c.id == "usd_cny"))
            .mappings()
            .first()
        )
        return bool(row and self.is_rate_valid(row["rate_date"]))

    def is_rate_valid(self, rate_date_str: str | None) -> bool:
        if not rate_date_str:
            return False
        try:
            from datetime import date, datetime

            from app.timeutils import BEIJING

            rate_d = date.fromisoformat(rate_date_str)
            curr_d = datetime.fromtimestamp(self.now() / 1000, BEIJING).date()
            diff = (curr_d - rate_d).days
            return -1 <= diff <= 7
        except Exception:
            return False

    def delete_provider(self, conn, resource_id, header):
        row = one(conn, s.providers, resource_id)
        require_revision(row, header)
        cfg = self.settings_config(conn)
        if resource_id in (cfg.vision_provider_id, cfg.summary_provider_id):
            raise AppError(409, "PROVIDER_IN_USE", "模型服务仍被当前配置使用")
        conn.execute(
            update(s.providers)
            .where(s.providers.c.id == resource_id)
            .values(deleted_at_ms=self.now(), revision=row["revision"] + 1, updated_at_ms=self.now())
        )

    def disk_free(self):
        return shutil.disk_usage(self.config.data_dir).free
