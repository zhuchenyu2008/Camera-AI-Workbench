"""自动档案是活动分析之后的可失败辅助流程。"""

from __future__ import annotations

from pydantic import Field, ValidationError
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.errors import AppError, require_revision
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.schemas import EntityCandidate, Strict
from app.security.secrets import digest, fingerprint, safe_path
from app.services.assets import decode_image
from app.timeutils import timestamp
from app.worker.queue import enqueue


class MatchGroup(Strict):
    candidate_keys: list[str] = Field(min_length=1, max_length=100)
    entity_id: str | None = None


class MatchOutput(Strict):
    groups: list[MatchGroup] = Field(max_length=100)


class Discovery:
    def __init__(self, w):
        self.w = w
        self.created_paths = []

    @staticmethod
    def clean_output(job, output, frames, members):
        evidence = {f["id"] for f in frames}
        existing = {m["entity_id"]: m["kind"] for m in members}
        keys, kinds, candidates = {}, {}, []
        for raw in output.entity_candidates:
            try:
                candidate = EntityCandidate.model_validate(raw)
            except (ValidationError, TypeError):
                continue
            if candidate.evidence_id not in evidence or candidate.key in keys:
                continue
            key = "candidate_" + fingerprint([job["id"], candidate.key])[:32]
            keys[candidate.key] = key
            kinds[candidate.key] = candidate.kind
            if existing.get(candidate.matched_entity_id) != candidate.kind:
                candidate.matched_entity_id = None
            candidate.key = key
            candidates.append(candidate)
        for minute in output.minutes:
            for observation in minute.observations:
                original = observation.subject_candidate_key
                observation.subject_candidate_key = (
                    keys.get(original) if kinds.get(original) == "person" else None
                )
                observation.object_candidate_keys = [
                    keys[k] for k in observation.object_candidate_keys if kinds.get(k) == "object"
                ]
        output.entity_candidates = candidates

    def stage(self, job, output, frames):
        """候选图在现场 TTL 内裁剪，未归档前仍遵守现场 TTL。"""
        with self.w.db.read() as conn:
            if not self.w.settings_config(conn).auto_discovery_enabled:
                return
        for raw in output.entity_candidates:
            candidate = EntityCandidate.model_validate(raw)
            with self.w.db.read() as conn:
                if conn.execute(
                    select(s.discovery_candidates.c.id).where(s.discovery_candidates.c.id == candidate.key)
                ).first():
                    continue
            frame = next(f for f in frames if f["id"] == candidate.evidence_id)
            prepared = None
            try:
                data, _ = self.w.assets.read_evidence(candidate.evidence_id)
                data, width, height = decode_image(data, "image/jpeg", candidate.crop)
                with self.w.db.read() as conn:
                    cfg = self.w.settings_config(conn)
                    used = conn.execute(
                        select(func.coalesce(func.sum(s.reference_asset_versions.c.bytes), 0)).where(
                            s.reference_asset_versions.c.deleted_at_ms.is_(None)
                        )
                    ).scalar_one()
                    staged = conn.execute(
                        select(s.discovery_candidates.c.prepared_json).where(
                            s.discovery_candidates.c.state == "pending"
                        )
                    ).scalars()
                    used += sum((v or {}).get("bytes", 0) for v in staged)
                if used + len(data) <= cfg.reference_quota_gb * 1e9:
                    path = self.w.assets.store("tmp/discovery", data)
                    prepared = dict(
                        relpath=path, bytes=len(data), width=width, height=height, sha256=digest(data)
                    )
            except (AppError, OSError):
                pass
            links = []
            for minute in output.minutes:
                for obs in minute.observations:
                    if (
                        obs.subject_candidate_key == candidate.key
                        or candidate.key in obs.object_candidate_keys
                    ):
                        links.append(
                            dict(
                                minute_start=minute.minute_start.isoformat(),
                                role="subject" if obs.subject_candidate_key == candidate.key else "object",
                            )
                        )
            try:
                with self.w.db.write() as conn:
                    conn.execute(
                        sqlite_insert(s.discovery_candidates)
                        .values(
                            id=candidate.key,
                            camera_id=frame["camera_id"],
                            captured_at_ms=timestamp(frame["captured_at"]),
                            expires_at_ms=timestamp(frame["expires_at"]),
                            candidate_json=candidate.model_dump(),
                            prepared_json=prepared,
                            links_json=links,
                            state="pending",
                            created_at_ms=self.w.now(),
                        )
                        .on_conflict_do_nothing()
                    )
                    enqueue(
                        conn,
                        self.w.now(),
                        "entity_discovery",
                        {"provider_id": job["payload_json"]["provider_id"]},
                        f"entity-discovery:{self.w.now() // 300000}",
                    )
            finally:
                if prepared:
                    with self.w.db.read() as conn:
                        saved = conn.execute(
                            select(s.discovery_candidates.c.prepared_json).where(
                                s.discovery_candidates.c.id == candidate.key
                            )
                        ).scalar()
                    if not saved or saved.get("relpath") != prepared["relpath"]:
                        safe_path(self.w.config.data_dir, prepared["relpath"]).unlink(missing_ok=True)

    def pending(self, conn, limit=12):
        return [
            dict(r)
            for r in conn.execute(
                select(s.discovery_candidates)
                .where(s.discovery_candidates.c.state == "pending")
                .order_by(s.discovery_candidates.c.created_at_ms, s.discovery_candidates.c.id)
                .limit(limit)
            ).mappings()
        ]

    def resolve(self, conn, entity_id):
        seen = set()
        while entity_id:
            if entity_id in seen:
                raise AppError(409, "ENTITY_CYCLE", "档案合并关系异常")
            seen.add(entity_id)
            row = one(conn, s.entities, entity_id, deleted=True)
            if not row["merged_into_id"]:
                return entity_id
            entity_id = row["merged_into_id"]
        return None

    def new_entity(self, conn, candidate):
        resource_id = new_id("entity")
        name = candidate["name"]
        if candidate["kind"] == "person":
            count = conn.execute(
                select(func.count()).select_from(s.entities).where(s.entities.c.kind == "person")
            ).scalar_one()
            name = f"人物 {count + 1}"
        conn.execute(
            s.entities.insert().values(
                id=resource_id,
                revision=1,
                kind=candidate["kind"],
                name=name,
                description=candidate.get("description", ""),
                enabled=True,
                is_primary_subject=False,
                camera_ids_json=[],
                origin="automatic",
                reviewed=False,
                created_at_ms=self.w.now(),
                updated_at_ms=self.w.now(),
            )
        )
        return resource_id

    def separated(self, conn, a, b):
        left, right = sorted([a, b])
        return bool(
            conn.execute(
                select(s.entity_separations).where(
                    s.entity_separations.c.left_id == left, s.entity_separations.c.right_id == right
                )
            ).first()
        )

    def manual_identity(self, conn, candidate):
        """同一现场图的同一区域复算时，沿用人工修正的出现身份。"""
        box = candidate["candidate_json"]["crop"]
        matches = set()
        rows = conn.execute(
            select(s.entity_occurrences).where(
                s.entity_occurrences.c.manual.is_(True),
                s.entity_occurrences.c.camera_id == candidate["camera_id"],
                s.entity_occurrences.c.evidence_id == candidate["candidate_json"]["evidence_id"],
            )
        ).mappings()
        for row in rows:
            other = row["crop_json"]
            width = max(
                0, min(box["x"] + box["width"], other["x"] + other["width"]) - max(box["x"], other["x"])
            )
            height = max(
                0,
                min(box["y"] + box["height"], other["y"] + other["height"]) - max(box["y"], other["y"]),
            )
            intersection = width * height
            area = box["width"] * box["height"] + other["width"] * other["height"] - intersection
            if not area or intersection / area < 0.7:
                continue
            entity_id = self.resolve(conn, row["entity_id"])
            entity = one(conn, s.entities, entity_id, deleted=True)
            if (
                entity["kind"] == candidate["candidate_json"]["kind"]
                and entity["enabled"]
                and not entity["deleted_at_ms"]
            ):
                matches.add(entity_id)
        return matches.pop() if len(matches) == 1 else None

    def archive(self, conn, pending, groups):
        pending = [r for r in pending if one(conn, s.discovery_candidates, r["id"])["state"] == "pending"]
        if not pending:
            return set()
        by_id = {r["id"]: r for r in pending}
        manual_ids = {r["id"]: self.manual_identity(conn, r) for r in pending}
        seen, affected = set(), set()
        for group in groups:
            keys = group["candidate_keys"]
            if not keys or not set(keys) <= by_id.keys() or seen & set(keys):
                raise AppError(422, "INVALID_MODEL_REFERENCES", "档案匹配引用了无效候选")
            seen.update(keys)
            kinds = {by_id[k]["candidate_json"]["kind"] for k in keys}
            if len(kinds) != 1:
                raise AppError(422, "INVALID_MODEL_REFERENCES", "人物和物品不能合并")
            forced = {manual_ids[k] for k in keys} - {None}
            if len(forced) > 1:
                affected.update(
                    self.archive(
                        conn,
                        [by_id[k] for k in keys],
                        [dict(candidate_keys=[k], entity_id=manual_ids[k]) for k in keys],
                    )
                )
                continue
            proposed = next(iter(forced)) if forced else group.get("entity_id")
            entity_id = self.resolve(conn, proposed) if proposed else None
            entity = one(conn, s.entities, entity_id) if entity_id else None
            if entity and (entity["kind"] not in kinds or not entity["enabled"]):
                entity_id = None
            # 已有人工拆分关系不允许被自动合并回去。
            # 人工修正优先于这张图的旧自动匹配提示。
            hints = {
                manual_ids[k] or self.resolve(conn, by_id[k]["candidate_json"].get("matched_entity_id"))
                for k in keys
            } - {None}
            if any(self.separated(conn, a, b) for a in hints for b in hints if a < b):
                affected.update(
                    self.archive(
                        conn,
                        [by_id[k] for k in keys],
                        [
                            dict(
                                candidate_keys=[k],
                                entity_id=by_id[k]["candidate_json"].get("matched_entity_id"),
                            )
                            for k in keys
                        ],
                    )
                )
                continue
            if entity_id and any(
                self.separated(conn, entity_id, hint) for hint in hints if hint != entity_id
            ):
                entity_id = None
            if not keys:
                continue
            if not entity_id:
                entity_id = self.new_entity(conn, by_id[keys[0]]["candidate_json"])
            for key in keys:
                row = by_id[key]
                current = one(conn, s.discovery_candidates, key)
                if current["state"] != "pending":
                    continue
                candidate = row["candidate_json"]
                conn.execute(
                    s.entity_occurrences.insert().values(
                        id=new_id("occurrence"),
                        entity_id=entity_id,
                        source_key=key,
                        camera_id=row["camera_id"],
                        captured_at_ms=row["captured_at_ms"],
                        evidence_id=candidate["evidence_id"],
                        crop_json=candidate["crop"],
                        links_json=row["links_json"],
                        manual=manual_ids[key] is not None,
                        created_at_ms=self.w.now(),
                    )
                )
                conn.execute(
                    update(s.discovery_candidates)
                    .where(s.discovery_candidates.c.id == key)
                    .values(state="archived", entity_id=entity_id)
                )
                conn.execute(
                    update(s.entities)
                    .where(s.entities.c.id == entity_id)
                    .values(
                        last_seen_at_ms=func.max(
                            func.coalesce(s.entities.c.last_seen_at_ms, 0), row["captured_at_ms"]
                        )
                    )
                )
                prepared = row["prepared_json"]
                has_reference = conn.execute(
                    select(s.reference_assets.c.id).where(
                        s.reference_assets.c.entity_id == entity_id,
                        s.reference_assets.c.deleted_at_ms.is_(None),
                    )
                ).first()
                quota = self.w.settings_config(conn).reference_quota_gb * 1e9
                used = conn.execute(
                    select(func.coalesce(func.sum(s.reference_asset_versions.c.bytes), 0)).where(
                        s.reference_asset_versions.c.deleted_at_ms.is_(None)
                    )
                ).scalar_one()
                try:
                    available = (
                        self.w.assets.metadata(conn, candidate["evidence_id"], check_file=False)["state"]
                        == "available"
                    )
                except AppError:
                    available = False
                if prepared and not has_reference and available and used + prepared["bytes"] <= quota:
                    try:
                        raw = safe_path(self.w.config.data_dir, prepared["relpath"]).read_bytes()
                        path = self.w.assets.store("references", raw)
                        self.created_paths.append(path)
                        asset_id = new_id("asset")
                        conn.execute(
                            s.reference_assets.insert().values(
                                id=asset_id,
                                entity_id=entity_id,
                                revision=1,
                                current_version=1,
                                enabled=True,
                                source_evidence_id=candidate["evidence_id"],
                                source_candidate_key=key,
                                created_at_ms=self.w.now(),
                                updated_at_ms=self.w.now(),
                            )
                        )
                        conn.execute(
                            s.reference_asset_versions.insert().values(
                                asset_id=asset_id,
                                version=1,
                                name_snapshot=one(conn, s.entities, entity_id)["name"],
                                **(prepared | {"relpath": path}),
                                created_at_ms=self.w.now(),
                            )
                        )
                    except OSError:
                        pass
                affected.update(self.assign_occurrence(conn, row | {"entity_id": entity_id}, manual=False))
        # 模型遗漏的候选独立建档，不伪造匹配。
        remaining = [r for r in pending if r["id"] not in seen]
        if remaining:
            affected.update(
                self.archive(
                    conn, remaining, [dict(candidate_keys=[r["id"]], entity_id=None) for r in remaining]
                )
            )
        return affected

    def reassign_ids(self, conn, source_id, target_id):
        """合并也覆盖手动建档的历史关联，原活动与人工修订保持原值。"""
        affected = set()
        minutes = list(conn.execute(select(s.minute_records)).mappings())
        for minute in minutes:
            start = minute["start_at_ms"]
            spans = self.w.ledger.spans(conn, [minute["camera_id"]], start, start + 60000)
            changed = False
            for span in spans:
                if span["subject_id"] == source_id:
                    span["subject_id"] = target_id
                    changed = True
                details = dict(span["details_json"])
                if source_id in details.get("object_ids", []):
                    details["object_ids"] = sorted(
                        {target_id if x == source_id else x for x in details["object_ids"]}
                    )
                    span["details_json"] = details
                    changed = True
            if not changed:
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
            rev = (
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
                    revision=rev,
                    source_kind="reconcile",
                    reference_set_id=old["reference_set_id"],
                    source_fingerprint=fingerprint(
                        [old["source_fingerprint"], "merge", source_id, target_id]
                    ),
                    result_json=old["result_json"],
                    created_at_ms=self.w.now(),
                )
            )
            for span in spans:
                conn.execute(
                    s.activity_spans.insert().values(
                        **(span | {"id": new_id("span"), "minute_revision": rev})
                    )
                )
            conn.execute(
                update(s.minute_records)
                .where(s.minute_records.c.id == minute["id"])
                .values(current_revision=rev)
            )
            self.w.assets.keep_evidence(
                conn, old["result_json"].get("evidence_ids", []), "minute", minute["id"], rev
            )
            affected.add(start)
        return affected

    def assign_occurrence(self, conn, occurrence, *, manual):
        candidate_key = occurrence.get("source_key", occurrence["id"])
        links = occurrence["links_json"]
        affected = set()
        for link in links:
            start = timestamp(link["minute_start"])
            minute = (
                conn.execute(
                    select(s.minute_records).where(
                        s.minute_records.c.camera_id == occurrence["camera_id"],
                        s.minute_records.c.start_at_ms == start,
                    )
                )
                .mappings()
                .first()
            )
            if not minute or not minute["current_revision"]:
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
            if old["result_json"].get("has_manual_revision") and not manual:
                continue
            if not manual and link["role"] == "subject" and old["result_json"].get("manual_subject_id"):
                continue
            spans = self.w.ledger.spans(conn, [minute["camera_id"]], start, start + 60000)
            changed = False
            for span in spans:
                details = dict(span["details_json"])
                if link["role"] == "subject" and (
                    details.get("subject_candidate_key") == candidate_key
                    or (manual and span["subject_id"] == occurrence.get("previous_entity_id"))
                ):
                    span["subject_id"], span["identity_status"] = occurrence["entity_id"], "matched"
                    changed = True
                elif link["role"] == "object" and candidate_key in details.get("object_candidate_keys", []):
                    ids = details.get("object_ids", [])
                    previous = occurrence.get("previous_entity_id")
                    assigned = {i for i in ids if i != previous} | {occurrence["entity_id"]}
                    mapping = {} if manual else old["result_json"].get("manual_object_assignments", {})
                    details["object_ids"] = sorted({mapping.get(i, i) for i in assigned})
                    span["details_json"] = details
                    changed = True
            if not changed:
                continue
            rev = (
                conn.execute(
                    select(func.max(s.minute_revisions.c.revision)).where(
                        s.minute_revisions.c.minute_id == minute["id"]
                    )
                ).scalar_one()
                + 1
            )
            result = dict(old["result_json"]) | {"identity_revision": "discovery-v1"}
            if manual and link["role"] == "subject":
                result["manual_subject_id"] = occurrence["entity_id"]
            if manual and link["role"] == "object":
                result["manual_object_assignments"] = result.get("manual_object_assignments", {}) | {
                    occurrence.get("previous_entity_id", ""): occurrence["entity_id"]
                }
            conn.execute(
                s.minute_revisions.insert().values(
                    minute_id=minute["id"],
                    revision=rev,
                    source_kind="reconcile",
                    source_fingerprint=fingerprint(
                        [old["source_fingerprint"], candidate_key, occurrence["entity_id"], rev]
                    ),
                    reference_set_id=old["reference_set_id"],
                    result_json=result,
                    created_at_ms=self.w.now(),
                )
            )
            for span in spans:
                conn.execute(
                    s.activity_spans.insert().values(
                        **(span | {"id": new_id("span"), "minute_revision": rev})
                    )
                )
            conn.execute(
                update(s.minute_records)
                .where(s.minute_records.c.id == minute["id"])
                .values(current_revision=rev)
            )
            self.w.assets.keep_evidence(conn, result.get("evidence_ids", []), "minute", minute["id"], rev)
            affected.add(start)
        return affected

    def reproject(self, conn, affected):
        if affected:
            self.w.ledger.advance(conn)
            self.w.ledger.project(conn, min(affected), max(affected) + 60000)
            self.w.ledger.invalidate(conn, min(affected), max(affected) + 60000, regenerate=False)

    def merge(self, conn, entity_id, dto, header):
        source, target = one(conn, s.entities, entity_id), one(conn, s.entities, dto.target_entity_id)
        require_revision(source, header)
        require_revision(target, f'"rev-{dto.target_revision}"')
        if (
            entity_id == target["id"]
            or source["kind"] != target["kind"]
            or source["merged_into_id"]
            or target["merged_into_id"]
        ):
            raise AppError(422, "VALIDATION_ERROR", "请选择两个同类型且未合并的档案")
        occurrences = list(
            conn.execute(
                select(s.entity_occurrences).where(s.entity_occurrences.c.entity_id == entity_id)
            ).mappings()
        )
        affected = set()
        for row in occurrences:
            affected.update(
                self.assign_occurrence(
                    conn,
                    dict(row) | {"entity_id": target["id"], "previous_entity_id": entity_id},
                    manual=True,
                )
            )
        affected.update(self.reassign_ids(conn, entity_id, target["id"]))
        conn.execute(
            update(s.entity_occurrences)
            .where(s.entity_occurrences.c.entity_id == entity_id)
            .values(entity_id=target["id"], manual=True)
        )
        conn.execute(
            update(s.discovery_candidates)
            .where(s.discovery_candidates.c.entity_id == entity_id)
            .values(entity_id=target["id"])
        )
        conn.execute(
            update(s.reference_assets)
            .where(s.reference_assets.c.entity_id == entity_id)
            .values(entity_id=target["id"])
        )
        conn.execute(
            update(s.entities)
            .where(s.entities.c.id == entity_id)
            .values(
                merged_into_id=target["id"],
                enabled=False,
                is_primary_subject=False,
                reviewed=True,
                revision=source["revision"] + 1,
            )
        )
        conn.execute(
            update(s.entities)
            .where(s.entities.c.id == target["id"])
            .values(reviewed=True, revision=target["revision"] + 1)
        )
        conn.execute(
            s.entity_changes.insert().values(
                id=new_id("change"),
                entity_id=entity_id,
                kind="merge",
                details_json=dict(target_id=target["id"], occurrence_ids=[r["id"] for r in occurrences]),
                created_at_ms=self.w.now(),
            )
        )
        self.reproject(conn, affected)
        return self.w.assets.entity(conn, target["id"])

    def split(self, conn, entity_id, dto, header):
        source = one(conn, s.entities, entity_id)
        require_revision(source, header)
        rows = list(
            conn.execute(
                select(s.entity_occurrences).where(
                    s.entity_occurrences.c.entity_id == entity_id,
                    s.entity_occurrences.c.id.in_(dto.occurrence_ids),
                )
            ).mappings()
        )
        if len(rows) != len(set(dto.occurrence_ids)) or source["merged_into_id"]:
            raise AppError(422, "VALIDATION_ERROR", "出现记录不属于此档案或档案已合并")
        target_id = self.new_entity(
            conn, {"kind": source["kind"], "name": dto.name, "description": source["description"]}
        )
        conn.execute(
            update(s.entities).where(s.entities.c.id == target_id).values(name=dto.name, reviewed=True)
        )
        affected = set()
        for row in rows:
            affected.update(
                self.assign_occurrence(
                    conn, dict(row) | {"entity_id": target_id, "previous_entity_id": entity_id}, manual=True
                )
            )
        conn.execute(
            update(s.entity_occurrences)
            .where(s.entity_occurrences.c.id.in_(dto.occurrence_ids))
            .values(entity_id=target_id, manual=True)
        )
        # 自动代表图有候选来源，可以准确随所选出现记录移交，无需读取过期现场图。
        conn.execute(
            update(s.reference_assets)
            .where(
                s.reference_assets.c.entity_id == entity_id,
                s.reference_assets.c.source_candidate_key.in_([r["source_key"] for r in rows]),
            )
            .values(entity_id=target_id)
        )
        conn.execute(
            update(s.discovery_candidates)
            .where(s.discovery_candidates.c.id.in_([r["source_key"] for r in rows]))
            .values(entity_id=target_id)
        )
        left, right = sorted([entity_id, target_id])
        conn.execute(
            sqlite_insert(s.entity_separations).values(left_id=left, right_id=right).on_conflict_do_nothing()
        )
        conn.execute(
            update(s.entities)
            .where(s.entities.c.id == entity_id)
            .values(reviewed=True, revision=source["revision"] + 1)
        )
        conn.execute(
            s.entity_changes.insert().values(
                id=new_id("change"),
                entity_id=entity_id,
                kind="split",
                details_json=dict(target_id=target_id, occurrence_ids=dto.occurrence_ids),
                created_at_ms=self.w.now(),
            )
        )
        self.reproject(conn, affected)
        return self.w.assets.entity(conn, target_id)

    def cleanup(self, conn):
        for row in conn.execute(
            select(s.discovery_candidates).where(
                or_(
                    s.discovery_candidates.c.state != "pending",
                    s.discovery_candidates.c.expires_at_ms <= self.w.now(),
                )
            )
        ).mappings():
            if row["prepared_json"]:
                try:
                    safe_path(self.w.config.data_dir, row["prepared_json"]["relpath"]).unlink(missing_ok=True)
                except OSError:
                    continue
            conn.execute(
                update(s.discovery_candidates)
                .where(s.discovery_candidates.c.id == row["id"])
                .values(prepared_json=None, state="expired" if row["state"] == "pending" else row["state"])
            )

    def discard_orphans(self):
        for path in self.created_paths:
            with self.w.db.read() as conn:
                exists = conn.execute(
                    select(s.reference_asset_versions.c.asset_id).where(
                        s.reference_asset_versions.c.relpath == path
                    )
                ).first()
            if not exists:
                safe_path(self.w.config.data_dir, path).unlink(missing_ok=True)
