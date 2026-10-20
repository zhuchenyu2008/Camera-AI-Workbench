from __future__ import annotations

import secrets

from sqlalchemy import select, update

from app.activities.names import activity_name
from app.analysis.client import PROMPT_VERSION, VISUAL_PROMPT, build_payload
from app.analysis.normalize import normalize
from app.detection.motion import sparse_frames
from app.errors import AppError
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.schemas import VisualOutput
from app.security.secrets import canonical, fingerprint
from app.timeutils import intersect, iso, subtract, timestamp, union
from app.worker.queue import enqueue, job_dto


class Analysis:
    def __init__(self, workbench):
        self.w = workbench

    def plan_temporal_slice(
        self,
        conn,
        camera_id: str,
        start_ms: int,
        end_ms: int,
        provider_id: str,
        *,
        reference_set_id: str | None = None,
        dense: bool = True,
        evidence_ids: list[str] | None = None,
        source_revision: int | None = None,
        reobserve_question: str | None = None,
        task_kind: str = "temporal_local_observation",
    ) -> dict:
        """Create one immutable lower-level time-slice observation task.

        The caller supplies the capture snapshot. This keeps scheduling separate
        from semantic inference and makes retries idempotent by slice/source.
        """
        if end_ms <= start_ms or not evidence_ids:
            raise AppError(422, "INVALID_TEMPORAL_TASK", "时间片必须有正长度和现场证据")
        camera = one(conn, s.cameras, camera_id, deleted=True)
        provider = one(conn, s.providers, provider_id)
        refs = one(conn, s.reference_sets, reference_set_id) if reference_set_id else None
        evidence_key = fingerprint(sorted(set(evidence_ids)))[:16]
        slice_id = f"slice_{camera_id}_{start_ms}_{end_ms}_{evidence_key}"
        payload = {
            "slice_id": slice_id,
            "camera_id": camera_id,
            "slice_start_ms": start_ms,
            "slice_end_ms": end_ms,
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "coverage_mode": "dense" if dense else "sparse",
            "coverage": {"mode": "dense" if dense else "sparse", "evidence_ids": evidence_ids},
            "provider_id": provider_id,
            "provider_revision": provider["revision"],
            "source_revision": source_revision or camera["source_revision"],
            "reference_set_id": reference_set_id,
            "activity_catalog": self.activity_catalog(conn, camera_id),
            "source_fingerprint": fingerprint(
                {
                    "slice_id": slice_id,
                    "evidence_ids": sorted(set(evidence_ids)),
                    "source_revision": source_revision or camera["source_revision"],
                    "reference_set_id": reference_set_id,
                    "reference_set_revision": refs["revision"] if refs else None,
                    "provider_id": provider_id,
                    "provider_revision": provider["revision"],
                    "prompt_version": PROMPT_VERSION,
                    "schema_version": "local-observation-1",
                    "activity_catalog": self.activity_catalog(conn, camera_id),
                    "reobserve_question": reobserve_question,
                }
            ),
        }
        if reobserve_question:
            payload["reobserve_question"] = reobserve_question[:500]
        if refs:
            payload["reference_set_revision"] = refs["revision"]
        expiry_values = []
        evidence_slots = []
        for evidence_id in evidence_ids:
            meta = self.w.assets.metadata(conn, evidence_id, check_file=False)
            if meta["camera_id"] != camera_id:
                raise AppError(422, "INVALID_TEMPORAL_TASK", "证据不属于任务摄像头")
            expiry_values.append(timestamp(meta["expires_at"]))
            frame = conn.execute(
                select(s.frames.c.slot_at_ms).where(s.frames.c.id == evidence_id)
            ).first()
            if frame:
                evidence_slots.append((frame.slot_at_ms, frame.slot_at_ms + 1000))
        planned = self.w.ledger.planned(conn, camera_id, start_ms, end_ms)
        covered = intersect(planned, union(evidence_slots))
        missing = subtract([(start_ms, end_ms)], covered)
        payload["coverage"] = {
            "mode": payload["coverage_mode"],
            "evidence_ids": payload["evidence_ids"],
            "planned_ranges": [{"from": iso(a), "to": iso(b)} for a, b in planned],
            "covered_ranges": [{"from": iso(a), "to": iso(b)} for a, b in covered],
            "hard_boundaries": [{"from": iso(a), "to": iso(b)} for a, b in missing],
            "missing_ranges": [
                {"start_at": iso(a), "end_at": iso(b)} for a, b in missing if a < b
            ],
        }
        job = enqueue(
            conn,
            self.w.now(),
            task_kind,
            payload,
            f"{task_kind}:{payload['source_fingerprint']}",
            deadline=min(expiry_values),
            total=1,
            unit="slice",
        )
        now = self.w.now()
        slice_exists = conn.execute(
            select(s.temporal_slices.c.id).where(s.temporal_slices.c.id == slice_id)
        ).first()
        if not slice_exists:
            conn.execute(
                s.temporal_slices.insert().values(
                    id=slice_id,
                    camera_id=camera_id,
                    session_id=None,
                    start_at_ms=start_ms,
                    end_at_ms=end_ms,
                    overlap_from_ms=None,
                    overlap_to_ms=None,
                    planned_coverage_json=payload["coverage"],
                    evidence_ids_json=payload["evidence_ids"],
                    evidence_fingerprint=fingerprint(sorted(payload["evidence_ids"])),
                    missing_ranges_json=payload["coverage"]["hard_boundaries"],
                    actual_start_at_ms=start_ms,
                    actual_end_at_ms=end_ms,
                    coverage_mode=payload["coverage_mode"],
                    source_revision=payload["source_revision"],
                    reference_set_id=reference_set_id,
                    prompt_version=PROMPT_VERSION,
                    capability_version=None,
                    earliest_expires_at_ms=min(expiry_values),
                    deadline_at_ms=min(expiry_values),
                    state="open",
                    created_at_ms=now,
                    updated_at_ms=now,
                )
            )
        run_exists = conn.execute(
            select(s.local_observation_runs.c.id).where(
                s.local_observation_runs.c.slice_id == slice_id,
                s.local_observation_runs.c.request_fingerprint == payload["source_fingerprint"],
            )
        ).first()
        if not run_exists:
            conn.execute(
                s.local_observation_runs.insert().values(
                    id=new_id("localrun"),
                    slice_id=slice_id,
                    job_id=job["id"],
                    provider_id=provider_id,
                    model=provider["config_json"]["model"],
                    prompt_version=PROMPT_VERSION,
                    capability_version=None,
                    schema_version="local-observation-1",
                    request_fingerprint=payload["source_fingerprint"],
                    attempt_no=1,
                    status="queued",
                    input_snapshot_json=payload,
                    result_json=None,
                    evidence_ids_json=payload["evidence_ids"],
                    error_json=None,
                    created_at_ms=now,
                    updated_at_ms=now,
                    finished_at_ms=None,
                )
            )
        return job

    def plan_temporal_review(
        self,
        conn,
        camera_id: str,
        start_ms: int,
        end_ms: int,
        provider_id: str,
        *,
        local_observations: list[dict] | None = None,
        slice_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        checkpoint: dict | None = None,
        coverage: dict | None = None,
        reason: str = "regular_rollup",
        finalize: bool = False,
        max_review_rounds: int = 8,
    ) -> dict:
        """Create an upper-level review/finalize task over immutable snapshots."""
        if end_ms <= start_ms:
            raise AppError(422, "INVALID_TEMPORAL_TASK", "复核范围必须为正长度")
        provider = one(conn, s.providers, provider_id)
        run_id = new_id("review")
        if checkpoint:
            start_ms = min(start_ms, checkpoint.get("reviewed_from_ms", start_ms))
            slice_ids = list(dict.fromkeys([*(checkpoint.get("local_slice_ids_json") or []), *(slice_ids or [])]))
            evidence_ids = list(dict.fromkeys([*(checkpoint.get("evidence_ids_json") or []), *(evidence_ids or [])]))
            coverage = dict(coverage or {})
            coverage["hard_boundaries"] = list(
                dict.fromkeys(
                    [
                        *(checkpoint.get("hard_boundaries_json") or []),
                        *(coverage.get("hard_boundaries") or []),
                    ]
                )
            )
        payload = {
            "review_run_id": run_id,
            "camera_id": camera_id,
            "review_start_ms": start_ms,
            "review_end_ms": end_ms,
            "provider_id": provider_id,
            "provider_revision": provider["revision"],
            "local_observations": local_observations or [],
            "slice_ids": list(dict.fromkeys(slice_ids or [])),
            "evidence_ids": list(dict.fromkeys(evidence_ids or [])),
            "checkpoint": checkpoint,
            "coverage": coverage or {},
            "max_review_rounds": max(0, min(8, int(max_review_rounds))),
            "reason": reason,
        }
        payload["source_fingerprint"] = fingerprint(
            {
                "camera_id": camera_id,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "slice_ids": payload["slice_ids"],
                "evidence_ids": payload["evidence_ids"],
                "checkpoint": checkpoint,
                "local_observations": payload["local_observations"],
                "coverage": payload["coverage"],
                "reason": reason,
                "provider_id": provider_id,
                "provider_revision": provider["revision"],
                "tool_protocol_version": "temporal-review-1",
                "schema_version": "published-timeline-1",
                "max_review_rounds": max(0, min(8, int(max_review_rounds))),
            }
        )
        kind = "temporal_finalize" if finalize else "temporal_review"
        job = enqueue(
            conn,
            self.w.now(),
            kind,
            payload,
            f"temporal-review:{payload['source_fingerprint']}",
            deadline=self.w.now() + 10 * 60 * 1000,
            total=1,
            unit="review",
        )
        now = self.w.now()
        trigger_kind = "finalize" if finalize else "rolling" if reason == "regular_rollup" else "issue"
        if not conn.execute(
            select(s.temporal_review_runs.c.id).where(
                s.temporal_review_runs.c.request_fingerprint == payload["source_fingerprint"]
            )
        ).first():
            conn.execute(
                s.temporal_review_runs.insert().values(
                    id=run_id,
                    camera_id=camera_id,
                    job_id=job["id"],
                    base_checkpoint_id=(checkpoint or {}).get("id"),
                    reviewed_from_ms=start_ms,
                    reviewed_to_ms=end_ms,
                    trigger_kind=trigger_kind,
                    trigger_reason=reason,
                    checkpoint_revision=(checkpoint or {}).get("revision", 0),
                    tool_protocol_version="temporal-review-1",
                    schema_version="published-timeline-1",
                    budget_json={"max_review_rounds": payload["max_review_rounds"]},
                    status="queued",
                    request_fingerprint=payload["source_fingerprint"],
                    result_json=None,
                    error_json=None,
                    created_at_ms=now,
                    updated_at_ms=now,
                    finished_at_ms=None,
                )
            )
        return job

    def activity_catalog(self, conn, camera_id: str) -> list[dict]:
        """Return the recent positive activity names for one camera."""
        now = self.w.now()
        lookback = now - 30 * 24 * 60 * 60 * 1000
        rows = conn.execute(
            select(
                s.activity_spans.c.activity,
                s.activity_spans.c.start_at_ms,
                s.activity_spans.c.end_at_ms,
            )
            .join(
                s.minute_records,
                (s.minute_records.c.id == s.activity_spans.c.minute_id)
                & (s.minute_records.c.current_revision == s.activity_spans.c.minute_revision),
            )
            .where(
                s.activity_spans.c.camera_id == camera_id,
                s.activity_spans.c.end_at_ms > lookback,
                s.activity_spans.c.start_at_ms < now,
            )
        ).mappings()
        grouped = {}
        for row in rows:
            name = activity_name(row["activity"])
            if name in {"unknown", "away"}:
                continue
            start = max(lookback, row["start_at_ms"])
            end = min(now, row["end_at_ms"])
            if start >= end:
                continue
            item = grouped.setdefault(name, {"name": name, "duration_ms": 0, "last_seen_at_ms": 0})
            item["duration_ms"] += end - start
            item["last_seen_at_ms"] = max(item["last_seen_at_ms"], end)
        ordered = sorted(
            grouped.values(), key=lambda item: (-item["duration_ms"], -item["last_seen_at_ms"], item["name"])
        )[:50]
        return [
            {
                "name": item["name"],
                "duration_ms": item["duration_ms"],
                "last_seen_at": iso(item["last_seen_at_ms"]),
            }
            for item in ordered
        ]

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
        if dto.subject_id:
            subject = one(conn, s.entities, dto.subject_id)
            if subject["kind"] != "person":
                raise AppError(422, "VALIDATION_ERROR", "汇总主体必须是人物")
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
        cfg = self.w.settings_config(conn)
        provider_id = cfg.vision_provider_id if dto.mode == "visual" else cfg.summary_provider_id
        if not provider_id:
            raise AppError(422, "PROVIDER_NOT_VERIFIED", "尚未配置所需的模型服务")
        self.w.require_provider(one(conn, s.providers, provider_id))
        activity_catalog = self.activity_catalog(conn, dto.camera_ids[0]) if dto.mode == "visual" else []
        estimated_batches = (
            len(
                self.pack_frames(
                    cfg,
                    one(conn, s.providers, provider_id),
                    refset,
                    frames,
                    activity_catalog,
                )
            )
            if dto.mode == "visual"
            else 1
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
                estimated_batches=estimated_batches,
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
                "subject_id": dto.subject_id,
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

    def estimate_size(self, provider, refset, frames, labels, activity_catalog=None):
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
        content.append({"type": "text", "text": "已有活动目录：" + canonical(activity_catalog or [])})
        return (
            len(canonical(build_payload(provider, VisualOutput, content, VISUAL_PROMPT)).encode())
            + encoded_bytes
        )

    def pack_frames(self, cfg, provider, refset, frames, activity_catalog=None):
        limits = self.w.effective_limits(cfg, provider)
        capacity = min(limits["max_capture_images"], limits["max_total_images"] - len(refset["members_json"]))
        if capacity < 1:
            raise AppError(422, "REFERENCE_CAPACITY_EXCEEDED", "完整参考集没有为现场图留下容量")
        batches, batch = [], []
        batch_minute = None
        for frame in frames:
            minute = frame["slot_at_ms"] // 60000 * 60000
            if batch and minute != batch_minute:
                batches.append(batch)
                batch = []
            batch_minute = minute
            candidate = batch + [frame]
            if (
                len(candidate) > capacity
                or self.estimate_size(provider, refset, candidate, cfg.activity_labels, activity_catalog)
                > limits["max_request_body_bytes"]
            ):
                if not batch:
                    raise AppError(
                        422, "REFERENCE_CAPACITY_EXCEEDED", "最小现场图与完整参考集仍超过请求体上限"
                    )
                batches.append(batch)
                batch = [frame]
                if (
                    self.estimate_size(provider, refset, batch, cfg.activity_labels, activity_catalog)
                    > limits["max_request_body_bytes"]
                ):
                    raise AppError(422, "REFERENCE_CAPACITY_EXCEEDED", "单图与完整参考集超过请求体上限")
                batch_minute = minute
            else:
                batch = candidate
        if batch:
            batches.append(batch)
        return batches

    def plan(self, conn, camera_id, frames, refset, mode, *, manual=False, missing=None):
        camera = one(conn, s.cameras, camera_id, deleted=True)
        cfg = self.w.settings_config(conn)
        if not cfg.vision_provider_id:
            raise AppError(422, "PROVIDER_NOT_VERIFIED", "未配置视觉模型")
        provider = one(conn, s.providers, cfg.vision_provider_id)
        self.w.require_provider(provider)
        activity_catalog = self.activity_catalog(conn, camera_id)
        batches = self.pack_frames(cfg, provider, refset, frames, activity_catalog)
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
            baselines.append(
                {
                    "id": row["id"],
                    "start": start,
                    "revision": row["current_revision"],
                    "evidence_ids": [f["id"] for f in frames if f["slot_at_ms"] // 60000 * 60000 == start],
                }
            )
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
            activity_labels=cfg.activity_labels,
            activity_catalog=activity_catalog,
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
                unit="batch",
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
            unit="minute",
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
        [
            meta
            for child in children
            if child["state"] == "succeeded"
            for meta in child["result_json"].get("frames", [])
        ]
        failed = any(child["state"] in {"failed", "cancelled"} for child in children)
        expired = any(child["state"] == "expired" for child in children)
        links = []
        completed_units, failed_units, expired_units, omitted_any = 0, 0, 0, False
        for baseline in payload["baselines"]:
            minute = one(conn, s.minute_records, baseline["id"])
            start, end = minute["start_at_ms"], minute["start_at_ms"] + 60000
            local_outputs = [
                {"minutes": [m for m in out["minutes"] if timestamp(m["minute_start"]) == start]}
                for out in outputs
            ]
            expected_ids = set(baseline.get("evidence_ids", []))
            local_children = [
                child for child in children if expected_ids & set(child["payload_json"]["evidence_ids"])
            ]
            local_metas = list(
                {
                    f["id"]: f
                    for child in local_children
                    if child["state"] == "succeeded"
                    and any(
                        timestamp(m["minute_start"]) == start
                        for m in child["result_json"].get("output", {}).get("minutes", [])
                    )
                    for f in child["result_json"].get("frames", [])
                    if f["id"] in expected_ids
                }.values()
            )
            has_result = any(o["minutes"] for o in local_outputs)
            omitted = any(
                child["state"] == "succeeded"
                and not any(
                    timestamp(m["minute_start"]) == start
                    for m in child["result_json"].get("output", {}).get("minutes", [])
                )
                for child in local_children
            )
            omitted_any |= omitted or not has_result
            local_failed = (
                any(child["state"] in {"failed", "cancelled"} for child in local_children) or omitted
            )
            local_expired = any(child["state"] == "expired" for child in local_children)
            completed_units += int(has_result)
            failed_units += int(not has_result and not local_expired)
            expired_units += int(not has_result and local_expired)
            state = (
                "partial"
                if has_result and (local_failed or local_expired)
                else "complete"
                if has_result
                else "expired"
                if local_expired
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
                activity_catalog=payload.get("activity_catalog", []),
            )
            # 归档可能先于分钟协调完成，使用当前候选映射接回身份，不影响活动。
            for span in spans:
                details = span["details_json"]
                key = details.get("subject_candidate_key")
                candidate = (
                    conn.execute(
                        select(s.discovery_candidates).where(
                            s.discovery_candidates.c.id == key, s.discovery_candidates.c.state == "archived"
                        )
                    )
                    .mappings()
                    .first()
                    if key
                    else None
                )
                if candidate:
                    span["subject_id"], span["identity_status"] = candidate["entity_id"], "matched"
                details["object_ids"] = []
                details["object_candidate_keys"] = []
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
            from app.services.discovery import Discovery

            resolver = Discovery(self.w)
            for span in spans:
                if existing.get("manual_subject_id"):
                    span["subject_id"], span["identity_status"] = existing["manual_subject_id"], "matched"
                if span["subject_id"]:
                    span["subject_id"] = resolver.resolve(conn, span["subject_id"])
                span["details_json"]["object_ids"] = []
                span["details_json"]["object_candidate_keys"] = []
            preserved = False
            # 部分重分析只替换新结果实际支持的区间，保留窗口外已经有效的观察。
            if not existing.get("has_manual_revision"):
                replaced = [(span["start_at_ms"], span["end_at_ms"]) for span in spans]
                old_spans = self.w.ledger.spans(conn, [minute["camera_id"]], start, end)
                for old in old_spans:
                    for lo, hi in subtract([(old["start_at_ms"], old["end_at_ms"])], replaced):
                        preserved = True
                        spans.append(
                            {
                                k: v
                                for k, v in old.items()
                                if k not in {"id", "camera_id", "minute_id", "minute_revision"}
                            }
                            | {"start_at_ms": lo, "end_at_ms": hi}
                        )
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
            unanalysed_reason = (
                "expired" if local_expired else "failed" if local_failed or not has_result else None
            )
            result = dict(
                visual_outputs=(existing.get("visual_outputs", []) if preserved else []) + local_outputs,
                activity_catalog=payload.get("activity_catalog", []),
                analysis_mode="mixed" if len({span["support"] for span in spans}) > 1 else payload["mode"],
                analysis_state=state,
                description="；".join(dict.fromkeys(span["details_json"]["observation"] for span in spans))[
                    :3000
                ],
                model_version=one(conn, s.providers, payload["provider_id"], deleted=True)["config_json"][
                    "model"
                ],
                evidence_ids=sorted(
                    {f["id"] for f in local_metas} | {e for span in spans for e in span["evidence_ids_json"]}
                ),
                has_manual_revision=False,
                run_created_at=payload["run_created_at"],
                sampling={
                    "target_frame_count": 60
                    if payload["mode"] == "dense"
                    else len(self.w.settings_config(conn).sparse_sample_offsets_seconds),
                    "captured_frame_count": sum(b - a for a, b in captured) // 1000,
                    "submitted_frame_count": len(
                        {
                            e
                            for child in local_children
                            if child["attempt_count"] > 0
                            for e in child["payload_json"]["evidence_ids"]
                            if e in expected_ids
                        }
                    ),
                    "successful_frame_count": len(local_metas),
                },
            )
            if unanalysed_reason:
                result["unanalysed_reason"] = unanalysed_reason
            for key in ("manual_subject_id",):
                if key in existing:
                    result[key] = existing[key]
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
                and not (existing.get("analysis_mode") in {"dense", "mixed"} and payload["mode"] == "sparse")
                and camera["source_revision"] == payload["source_revision"]
                and (
                    minute["current_revision"] == baseline["revision"]
                    or (payload["mode"] == "dense" and existing.get("analysis_mode") == "sparse")
                    or payload["run_created_at"] > existing.get("run_created_at", 0)
                )
            )
            if can_replace or not minute["current_revision"]:
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
        if payload["baselines"]:
            self.w.ledger.project(
                conn,
                min(b["start"] for b in payload["baselines"]),
                max(b["start"] for b in payload["baselines"]) + 60000,
            )
        conn.execute(
            update(s.jobs)
            .where(s.jobs.c.id == job["id"])
            .values(
                progress_json={
                    "completed_units": completed_units,
                    "total_units": len(payload["baselines"]),
                    "failed_units": failed_units,
                    "expired_units": expired_units,
                    "unit": "minute",
                }
            )
        )
        final_state = (
            "expired"
            if not completed_units and expired_units
            else "failed"
            if not completed_units
            else "partial"
            if failed or expired or omitted_any or payload.get("missing_ranges")
            else "succeeded"
        )
        return {
            "links": links,
            "missing_ranges": payload.get("missing_ranges", []),
        }, final_state

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
        try:
            provider = one(conn, s.providers, cfg.vision_provider_id)
            self.w.require_provider(provider)
        except AppError:
            return
        capabilities = provider["capabilities_json"] or {}
        if capabilities.get("temporal_reasoning") and capabilities.get("tool_calling"):
            self.schedule_temporal(conn, provider)
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

    def schedule_temporal(self, conn, provider):
        """Roll completed capture into 20-second observations and reviews.

        Temporal scheduling is capability-gated so an ordinary verified vision
        provider continues using the compatible minute pipeline until its
        temporal replay test has passed.
        """
        now = self.w.now()
        slice_seconds = 20_000
        slice_step = 15_000
        review_start = now - 120_000
        cameras = (
            conn.execute(
                select(
                    s.cameras,
                    s.camera_runtime.c.capture_state,
                    s.camera_runtime.c.stop_reason,
                    s.camera_runtime.c.stream_session_id,
                )
                .join(s.camera_runtime, s.camera_runtime.c.camera_id == s.cameras.c.id)
                .where(s.cameras.c.deleted_at_ms.is_(None))
            )
            .mappings()
            .all()
        )
        for camera in cameras:
            camera_id = camera["id"]
            stopped = (
                camera["capture_state"] == "stopped"
                and camera["stream_session_id"] is None
                and camera["stop_reason"] in {"disabled", "outside_schedule", "manual_override"}
            )
            frames = self.available(conn, [camera_id], review_start - 60_000, now)
            first_start = frames[0]["slot_at_ms"] // slice_step * slice_step if frames else None
            last_start = frames[-1]["slot_at_ms"] // slice_step * slice_step if frames else None
            windows = []
            for start in range(first_start, last_start + 1, slice_step) if frames else []:
                end = start + slice_seconds
                if end > now:
                    continue
                local = [frame for frame in frames if start <= frame["slot_at_ms"] < end]
                if not local:
                    continue
                refset = self.w.assets.reference_set(conn, [camera_id])
                self.plan_temporal_slice(
                    conn,
                    camera_id,
                    start,
                    end,
                    provider["id"],
                    reference_set_id=refset["id"],
                    dense=True,
                    evidence_ids=[frame["id"] for frame in local],
                    source_revision=camera["source_revision"],
                )
                windows.append((start, end))
            if not windows and not stopped:
                continue
            latest_end = max((end for _, end in windows), default=now)
            checkpoint = (
                conn.execute(
                    select(s.temporal_checkpoints)
                    .where(s.temporal_checkpoints.c.camera_id == camera_id)
                    .order_by(s.temporal_checkpoints.c.revision.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            baseline = checkpoint["reviewed_to_ms"] if checkpoint else min(
                (start for start, _ in windows), default=now
            )
            camera_review_start = (
                min(review_start, baseline - 120_000) if stopped and checkpoint else
                min(review_start, now - 300_000) if stopped else
                max(review_start, baseline - 120_000)
            )
            completed = (
                conn.execute(
                    select(
                        s.temporal_slices.c.id.label("slice_id"),
                        s.temporal_slices.c.start_at_ms,
                        s.temporal_slices.c.end_at_ms,
                        s.temporal_slices.c.evidence_ids_json,
                        s.temporal_slices.c.planned_coverage_json,
                        s.local_observation_runs.c.result_json,
                    )
                    .join(s.local_observation_runs, s.local_observation_runs.c.slice_id == s.temporal_slices.c.id)
                    .where(
                        s.temporal_slices.c.camera_id == camera_id,
                        s.temporal_slices.c.start_at_ms >= camera_review_start,
                        s.temporal_slices.c.end_at_ms <= latest_end,
                        s.local_observation_runs.c.status == "succeeded",
                    )
                    .order_by(s.temporal_slices.c.start_at_ms)
                )
                .mappings()
                .all()
            )
            if not completed and not (stopped and checkpoint):
                continue
            active = conn.execute(
                select(s.temporal_review_runs.c.id).where(
                    s.temporal_review_runs.c.camera_id == camera_id,
                    s.temporal_review_runs.c.status.in_(["queued", "running"]),
                    s.temporal_review_runs.c.reviewed_to_ms > camera_review_start,
                )
            ).first()
            if active:
                continue
            observations = [
                row["result_json"]["local_observation"]
                for row in completed
                if row["result_json"] and row["result_json"].get("local_observation")
            ]
            issue = any(item.get("review_requests") or item.get("transition_candidates") for item in observations)
            if stopped:
                if not completed and not checkpoint:
                    continue
                pending_local = conn.execute(
                    select(s.local_observation_runs.c.id)
                    .join(s.temporal_slices, s.temporal_slices.c.id == s.local_observation_runs.c.slice_id)
                    .where(
                        s.temporal_slices.c.camera_id == camera_id,
                        s.temporal_slices.c.start_at_ms >= camera_review_start,
                        s.local_observation_runs.c.status.in_(["queued", "running"]),
                    )
                ).first()
                if pending_local:
                    continue
                final_to = max(
                    checkpoint["reviewed_to_ms"] if checkpoint else 0,
                    completed[-1]["end_at_ms"] if completed else 0,
                )
                final_exists = conn.execute(
                    select(s.temporal_review_runs.c.id)
                    .where(
                        s.temporal_review_runs.c.camera_id == camera_id,
                        s.temporal_review_runs.c.trigger_kind == "finalize",
                        s.temporal_review_runs.c.reviewed_to_ms >= final_to,
                        s.temporal_review_runs.c.status.in_(
                            ["queued", "running", "succeeded", "partial", "failed", "expired", "cancelled"]
                        ),
                    )
                    .limit(1)
                ).first()
                if final_exists:
                    continue
                if completed:
                    first, last = completed[0], completed[-1]
                    review_from = min(first["start_at_ms"], camera_review_start)
                    review_to = max(last["end_at_ms"], checkpoint["reviewed_to_ms"] if checkpoint else 0)
                    covered_ranges = union(
                        [
                            (timestamp(item["from"]), timestamp(item["to"]))
                            for row in completed
                            for item in row["planned_coverage_json"].get("covered_ranges", [])
                        ]
                    )
                    hard_boundaries = [
                        {"from": iso(start), "to": iso(end)}
                        for start, end in subtract([(review_from, last["end_at_ms"])], covered_ranges)
                    ]
                    if checkpoint and checkpoint["reviewed_to_ms"] < review_from:
                        hard_boundaries.append(
                            {"from": iso(checkpoint["reviewed_to_ms"]), "to": iso(review_from)}
                        )
                    review_start_ms = review_from
                    local_observations = observations
                    slice_ids = [row["slice_id"] for row in completed]
                    evidence_ids = list(
                        dict.fromkeys(
                            evidence_id for row in completed for evidence_id in row["evidence_ids_json"]
                        )
                    )
                    coverage = {
                        "slices": [row["planned_coverage_json"] for row in completed],
                        "hard_boundaries": hard_boundaries,
                    }
                else:
                    review_start_ms = checkpoint["reviewed_from_ms"]
                    review_to = checkpoint["reviewed_to_ms"]
                    local_observations = []
                    slice_ids = []
                    evidence_ids = []
                    coverage = {"hard_boundaries": checkpoint["hard_boundaries_json"] or []}
                self.plan_temporal_review(
                    conn,
                    camera_id,
                    review_start_ms,
                    review_to,
                    provider["id"],
                    local_observations=local_observations,
                    slice_ids=slice_ids,
                    evidence_ids=evidence_ids,
                    checkpoint=dict(checkpoint) if checkpoint else None,
                    coverage=coverage,
                    reason="capture_stopped",
                    finalize=True,
                )
                continue
            if len(observations) < 2 or (latest_end - baseline < 120_000 and not issue):
                continue
            first, last = completed[0], completed[-1]
            covered_ranges = union(
                [
                    (timestamp(item["from"]), timestamp(item["to"]))
                    for row in completed
                    for item in row["planned_coverage_json"].get("covered_ranges", [])
                ]
            )
            review_from = min(first["start_at_ms"], camera_review_start)
            review_to = last["end_at_ms"]
            hard_boundaries = [
                {"from": iso(start), "to": iso(end)}
                for start, end in subtract([(review_from, review_to)], covered_ranges)
            ]
            if checkpoint and checkpoint["reviewed_to_ms"] < review_from:
                hard_boundaries.append(
                    {"from": iso(checkpoint["reviewed_to_ms"]), "to": iso(review_from)}
                )
            self.plan_temporal_review(
                conn,
                camera_id,
                review_from,
                review_to,
                provider["id"],
                local_observations=observations,
                slice_ids=[row["slice_id"] for row in completed],
                evidence_ids=list(
                    dict.fromkeys(
                        evidence_id
                        for row in completed
                        for evidence_id in row["evidence_ids_json"]
                    )
                ),
                coverage={
                    "slices": [row["planned_coverage_json"] for row in completed],
                    "hard_boundaries": hard_boundaries,
                },
                checkpoint=dict(checkpoint) if checkpoint else None,
                reason="review_request" if issue else "regular_rollup",
            )
