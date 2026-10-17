from __future__ import annotations

import math
import secrets

from sqlalchemy import select, update

from app.analysis.client import PROMPT_VERSION, VISUAL_PROMPT, build_payload
from app.analysis.normalize import normalize
from app.detection.motion import sparse_frames
from app.errors import AppError
from app.repositories import schema_v1 as s
from app.repositories.db import new_id, one
from app.schemas import VisualOutput
from app.security.secrets import canonical, fingerprint
from app.timeutils import intersect, iso, timestamp, union
from app.worker.queue import enqueue, job_dto


class Analysis:
    def __init__(self, workbench):
        self.w = workbench

    def available(self, conn, camera_ids, start, end):
        rows = (
            conn.execute(
                select(s.frames)
                .where(
                    s.frames.c.camera_id.in_(camera_ids),
                    s.frames.c.captured_at_ms >= start,
                    s.frames.c.captured_at_ms < end,
                    s.frames.c.is_test.is_(False),
                )
                .order_by(s.frames.c.captured_at_ms, s.frames.c.id)
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows if self.w.assets.metadata(conn, row["id"])["state"] == "available"]

    def preview(self, conn, dto, *, create=False):
        start, end = timestamp(dto.from_), timestamp(dto.to)
        for camera in dto.camera_ids:
            one(conn, s.cameras, camera, deleted=True)
        refset = self.w.assets.reference_set(conn, sorted(set(dto.camera_ids)))
        frames = self.available(conn, dto.camera_ids, start, end) if dto.mode == "visual" else []
        ranges = (
            intersect([(start, end)], union([(f["slot_at_ms"], f["slot_at_ms"] + 1000) for f in frames]))
            if dto.mode == "visual"
            else [(start, end)]
        )
        missing = []
        position = start
        for a, b in ranges:
            if position < a:
                missing.append((position, a))
            position = b
        if position < end:
            missing.append((position, end))
        if dto.mode == "visual" and not frames:
            raise AppError(410, "EVIDENCE_EXPIRED", "所选区间没有可用现场图")
        if dto.mode == "visual" and missing and not dto.allow_partial:
            raise AppError(
                409,
                "EVIDENCE_PARTIAL",
                "仅部分现场图可用，请确认是否允许部分分析",
                details={"missing_ranges": [{"from": iso(a), "to": iso(b)} for a, b in missing]},
            )
        params = dto.model_dump(mode="json", by_alias=True, exclude={"preview_token"})
        extra = self.w.config_fingerprint(conn) | {"reference_set_id": refset["id"]}
        if create:
            self.w.check_preview(dto.preview_token, "analysis", "run", 1, params, extra)
        token, expiry = self.w.preview_token("analysis", "run", 1, params, extra, ttl=30000)
        return (
            dict(
                preview_token=token,
                expires_at=iso(expiry),
                available_ranges=[{"from": iso(a), "to": iso(b)} for a, b in ranges],
                missing_ranges=[{"from": iso(a), "to": iso(b)} for a, b in missing],
                available_frame_count=len(frames),
                earliest_expires_at=iso(min((f["expires_at_ms"] for f in frames), default=None)),
                reference_set_id=refset["id"],
                estimated_batches=math.ceil(
                    len(frames) / max(1, self.w.settings_config(conn).max_capture_images_per_request)
                ),
                warnings=[],
            ),
            frames,
            refset,
        )

    def create(self, conn, dto):
        preview, frames, refset = self.preview(conn, dto, create=True)
        self._limit_manual(conn)
        if dto.mode == "records_only":
            payload = {
                "camera_ids": sorted(set(dto.camera_ids)),
                "subject_id": None,
                "start": timestamp(dto.from_),
                "end": timestamp(dto.to),
                "reference_set_id": refset["id"],
                "provider_id": self.w.settings_config(conn).summary_provider_id,
            }
            return job_dto(
                enqueue(
                    conn,
                    self.w.now(),
                    "manual_records",
                    payload,
                    f"manual-records:{secrets.token_urlsafe(18)}",
                    total=1,
                )
            )
        root = self.plan(
            conn, dto.camera_ids[0], frames, refset, "dense", manual=True, missing=preview["missing_ranges"]
        )
        return job_dto(root)

    def _limit_manual(self, conn):
        now = self.w.now()
        count = conn.execute(
            select(s.jobs.c.id).where(
                s.jobs.c.created_at_ms > now - 60000, s.jobs.c.kind.in_(["manual_visual", "manual_records"])
            )
        ).all()
        if len(count) >= 6:
            raise AppError(
                429,
                "RATE_LIMITED",
                "每分钟最多创建 6 个手动分析任务",
                details={"retry_after_seconds": 60},
                retryable=True,
            )

    def estimate_size(self, provider, refset, frames, labels):
        content, encoded_bytes = [], 0
        detail = provider["config_json"]["detail_mode"]

        def placeholder():
            image = {"url": "data:image/jpeg;base64,"}
            if detail != "omit":
                image["detail"] = detail
            return {"type": "image_url", "image_url": image}

        for member in refset["members_json"]:
            content += [{"type": "text", "text": "参考资料（不是现场）：" + canonical(member)}, placeholder()]
            encoded_bytes += (member["bytes"] + 2) // 3 * 4
        for frame in frames:
            content += [
                {
                    "type": "text",
                    "text": "现场证据："
                    + canonical(
                        {
                            "evidence_id": frame["id"],
                            "camera_id": frame["camera_id"],
                            "captured_at": iso(frame["captured_at_ms"]),
                        }
                    ),
                },
                placeholder(),
            ]
            encoded_bytes += (frame["bytes"] + 2) // 3 * 4
        content.append({"type": "text", "text": "允许标签：" + canonical(labels)})
        return (
            len(canonical(build_payload(provider, VisualOutput, content, VISUAL_PROMPT)).encode())
            + encoded_bytes
        )

    def plan(self, conn, camera_id, frames, refset, mode, *, manual=False, missing=None):
        camera = one(conn, s.cameras, camera_id, deleted=True)
        cfg = self.w.settings_config(conn)
        if not cfg.vision_provider_id:
            raise AppError(422, "PROVIDER_NOT_VERIFIED", "未配置视觉模型")
        provider = one(conn, s.providers, cfg.vision_provider_id)
        self.w.validate_settings_links(conn, cfg)
        limits = self.w.effective_limits(cfg, provider)
        capacity = min(limits["max_capture_images"], limits["max_total_images"] - len(refset["members_json"]))
        if capacity < 1:
            raise AppError(422, "REFERENCE_CAPACITY_EXCEEDED", "完整参考集没有为现场图留下容量")
        batches, batch = [], []
        for frame in frames:
            candidate = batch + [frame]
            if (
                len(candidate) > capacity
                or self.estimate_size(provider, refset, candidate, cfg.activity_labels)
                > limits["max_request_body_bytes"]
            ):
                if not batch:
                    raise AppError(
                        422, "REFERENCE_CAPACITY_EXCEEDED", "最小现场图与完整参考集仍超过请求体上限"
                    )
                batches.append(batch)
                batch = [frame]
                if (
                    self.estimate_size(provider, refset, batch, cfg.activity_labels)
                    > limits["max_request_body_bytes"]
                ):
                    raise AppError(422, "REFERENCE_CAPACITY_EXCEEDED", "单图与完整参考集超过请求体上限")
            else:
                batch = candidate
        if batch:
            batches.append(batch)
        minutes = sorted({f["slot_at_ms"] // 60000 * 60000 for f in frames})
        baselines = []
        for start in minutes:
            row = (
                conn.execute(
                    select(s.minute_records).where(
                        s.minute_records.c.camera_id == camera_id, s.minute_records.c.start_at_ms == start
                    )
                )
                .mappings()
                .one()
            )
            baselines.append({"id": row["id"], "start": start, "revision": row["current_revision"]})
        stamp = self.w.now()
        common = dict(
            camera_id=camera_id,
            provider_id=provider["id"],
            provider_revision=provider["revision"],
            source_revision=camera["source_revision"],
            reference_set_id=refset["id"],
            prompt_version=PROMPT_VERSION,
            mode=mode,
            run_created_at=stamp,
            baselines=baselines,
        )
        key = fingerprint(
            {k: v for k, v in common.items() if k not in {"run_created_at", "baselines"}}
            | {
                "evidence_ids": sorted(f["id"] for f in frames),
                "nonce": secrets.token_urlsafe(18) if manual else None,
            }
        )
        children = []
        for index, batch in enumerate(batches):
            payload = common | {"evidence_ids": [f["id"] for f in batch]}
            child = enqueue(
                conn,
                stamp,
                "dense_analysis" if mode == "dense" else "sparse_analysis",
                payload,
                f"batch:{key}:{index}",
                deadline=min(f["expires_at_ms"] for f in batch),
                total=1,
            )
            children.append(child["id"])
        root = enqueue(
            conn,
            stamp,
            "manual_visual" if manual else "reconcile_minutes",
            common
            | {
                "children": children,
                "evidence_ids": [f["id"] for f in frames],
                "missing_ranges": missing or [],
            },
            f"run:{key}",
            dependencies=children,
            total=len(baselines),
        )
        return root

    def reconcile(self, conn, job):
        payload = job["payload_json"]
        camera = one(conn, s.cameras, payload["camera_id"], deleted=True)
        children = [one(conn, s.jobs, child) for child in payload["children"]]
        outputs = [
            child["result_json"]["output"]
            for child in children
            if child["state"] == "succeeded" and "output" in child["result_json"]
        ]
        metas = [
            meta
            for child in children
            if child["state"] == "succeeded"
            for meta in child["result_json"].get("frames", [])
        ]
        failed = any(child["state"] in {"failed", "cancelled"} for child in children)
        expired = any(child["state"] == "expired" for child in children)
        links = []
        for baseline in payload["baselines"]:
            minute = one(conn, s.minute_records, baseline["id"])
            start, end = minute["start_at_ms"], minute["start_at_ms"] + 60000
            local_outputs = [
                {"minutes": [m for m in out["minutes"] if timestamp(m["minute_start"]) == start]}
                for out in outputs
            ]
            local_metas = list(
                {f["id"]: f for f in metas if timestamp(f["captured_at"]) // 60000 * 60000 == start}.values()
            )
            has_result = any(o["minutes"] for o in local_outputs)
            state = (
                "partial"
                if has_result and (failed or expired)
                else "complete"
                if has_result
                else "expired"
                if expired
                else "failed"
            )
            captured = self.w.ledger.captured(conn, minute["camera_id"], start, end)
            blocked = []
            if payload["mode"] == "sparse":
                blocked = [
                    (r.desired_start_ms, r.desired_end_ms)
                    for r in conn.execute(
                        select(
                            s.detection_events.c.desired_start_ms, s.detection_events.c.desired_end_ms
                        ).where(
                            s.detection_events.c.camera_id == minute["camera_id"],
                            s.detection_events.c.resolved.is_(False),
                            s.detection_events.c.desired_start_ms < end,
                            s.detection_events.c.desired_end_ms > start,
                        )
                    )
                ]
            spans, review = normalize(
                local_outputs,
                local_metas,
                captured,
                max_gap_seconds=self.w.settings_config(conn).max_inference_gap_seconds,
                blocked=blocked,
                support=payload["mode"],
            )
            previous = (
                conn.execute(
                    select(s.minute_revisions).where(
                        s.minute_revisions.c.minute_id == minute["id"],
                        s.minute_revisions.c.revision == minute["current_revision"],
                    )
                )
                .mappings()
                .first()
            )
            existing = previous["result_json"] if previous else {}
            fingerprint_value = fingerprint([job["id"], minute["id"], local_outputs])
            if conn.execute(
                select(s.minute_revisions.c.revision).where(
                    s.minute_revisions.c.minute_id == minute["id"],
                    s.minute_revisions.c.source_fingerprint == fingerprint_value,
                )
            ).first():
                continue
            revision = (
                conn.execute(
                    select(s.minute_revisions.c.revision)
                    .where(s.minute_revisions.c.minute_id == minute["id"])
                    .order_by(s.minute_revisions.c.revision.desc())
                    .limit(1)
                ).scalar()
                or 0
            ) + 1
            result = dict(
                analysis_mode=payload["mode"],
                analysis_state=state,
                description="；".join(dict.fromkeys(span["details_json"]["observation"] for span in spans))[
                    :3000
                ],
                model_version=one(conn, s.providers, payload["provider_id"], deleted=True)["config_json"][
                    "model"
                ],
                evidence_ids=[f["id"] for f in local_metas],
                has_manual_revision=False,
                run_created_at=payload["run_created_at"],
                sampling={
                    "target_frame_count": 60
                    if payload["mode"] == "dense"
                    else len(self.w.settings_config(conn).sparse_sample_offsets_seconds),
                    "captured_frame_count": sum(b - a for a, b in captured) // 1000,
                    "submitted_frame_count": len(
                        {
                            f
                            for child in children
                            for f in child["payload_json"]["evidence_ids"]
                            if f in {m["id"] for m in local_metas}
                        }
                    ),
                    "successful_frame_count": len(local_metas),
                },
            )
            conn.execute(
                s.minute_revisions.insert().values(
                    minute_id=minute["id"],
                    revision=revision,
                    source_kind="ai",
                    source_fingerprint=fingerprint_value,
                    reference_set_id=payload["reference_set_id"],
                    result_json=result,
                    created_at_ms=self.w.now(),
                )
            )
            for span in spans:
                conn.execute(
                    s.activity_spans.insert().values(
                        **span,
                        id=new_id("span"),
                        camera_id=minute["camera_id"],
                        minute_id=minute["id"],
                        minute_revision=revision,
                    )
                )
            self.w.assets.keep_evidence(conn, result["evidence_ids"], "minute", minute["id"], revision)
            can_replace = (
                has_result
                and not existing.get("has_manual_revision")
                and camera["source_revision"] == payload["source_revision"]
                and (
                    minute["current_revision"] == baseline["revision"]
                    or (payload["mode"] == "dense" and existing.get("analysis_mode") == "sparse")
                    or payload["run_created_at"] > existing.get("run_created_at", 0)
                )
            )
            if can_replace or minute["current_revision"] == 0:
                conn.execute(
                    update(s.minute_records)
                    .where(s.minute_records.c.id == minute["id"])
                    .values(current_revision=revision, analysis_state=state)
                )
                self.w.ledger.advance(conn)
                self.w.ledger.invalidate(conn, start, end)
            links.append(
                {
                    "resource_kind": "minute",
                    "resource_id": minute["id"],
                    "revision": revision,
                    "label": "分钟记录",
                }
            )
            if review and payload["mode"] == "sparse":
                self.backfill(conn, minute["camera_id"], start, end)
        self.w.ledger.project(conn)
        return {
            "links": links,
            "missing_ranges": payload.get("missing_ranges", []),
        }, "partial" if failed or expired or payload.get("missing_ranges") else "succeeded"

    def backfill(self, conn, camera_id, start, end):
        cfg = self.w.settings_config(conn)
        start -= cfg.pre_trigger_seconds * 1000
        refset = self.w.assets.reference_set(conn, [camera_id])
        for minute_start in range(start // 60000 * 60000, end, 60000):
            frames = self.available(
                conn, [camera_id], max(start, minute_start), min(end, minute_start + 60000)
            )
            if frames:
                self.plan(conn, camera_id, frames, refset, "dense")

    def schedule(self, conn):
        now = self.w.now()
        cfg = self.w.settings_config(conn)
        if not cfg.vision_provider_id:
            return
        minutes = (
            conn.execute(
                select(s.minute_records)
                .where(
                    s.minute_records.c.start_at_ms + 60000 <= now,
                    s.minute_records.c.analysis_state.in_(["pending", "running"]),
                )
                .order_by(s.minute_records.c.start_at_ms)
                .limit(300)
            )
            .mappings()
            .all()
        )
        active_jobs = (
            conn.execute(
                select(s.jobs.c.payload_json).where(s.jobs.c.kind.in_(["reconcile_minutes", "manual_visual"]))
            )
            .scalars()
            .all()
        )
        planned = {base["id"] for p in active_jobs for base in p.get("baselines", [])}
        grouped = {}
        for minute in minutes:
            if minute["id"] in planned:
                continue
            frames = self.available(
                conn, [minute["camera_id"]], minute["start_at_ms"], minute["start_at_ms"] + 60000
            )
            if not frames:
                conn.execute(
                    update(s.minute_records)
                    .where(s.minute_records.c.id == minute["id"])
                    .values(
                        analysis_state="expired"
                        if minute["start_at_ms"] + cfg.frame_retention_minutes * 60000 <= now
                        else "failed"
                    )
                )
                continue
            dense = conn.execute(
                select(s.detection_events.c.id)
                .where(
                    s.detection_events.c.camera_id == minute["camera_id"],
                    s.detection_events.c.desired_start_ms < minute["start_at_ms"] + 60000,
                    s.detection_events.c.desired_end_ms > minute["start_at_ms"],
                )
                .limit(1)
            ).first()
            group_end = (
                (minute["start_at_ms"] // (cfg.sparse_submit_interval_minutes * 60000) + 1)
                * cfg.sparse_submit_interval_minutes
                * 60000
            )
            camera = one(conn, s.cameras, minute["camera_id"], deleted=True)
            runtime = (
                conn.execute(
                    select(s.camera_runtime).where(s.camera_runtime.c.camera_id == minute["camera_id"])
                )
                .mappings()
                .one()
            )
            if (
                not dense
                and now < group_end
                and camera["enabled"]
                and runtime["capture_state"] in {"online", "starting", "offline"}
            ):
                continue
            mode = "dense" if dense else "sparse"
            selected = (
                frames
                if dense
                else sparse_frames(frames, minute["start_at_ms"], cfg.sparse_sample_offsets_seconds)
            )
            if not selected:
                conn.execute(
                    update(s.minute_records)
                    .where(s.minute_records.c.id == minute["id"])
                    .values(analysis_state="failed")
                )
                continue
            key = (minute["camera_id"], mode, minute["start_at_ms"] if dense else group_end)
            grouped.setdefault(key, []).extend(selected)
        for (camera, mode, _), frames in grouped.items():
            refset = self.w.assets.reference_set(conn, [camera])
            self.plan(conn, camera, frames, refset, mode)
