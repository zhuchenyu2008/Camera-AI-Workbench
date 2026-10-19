from __future__ import annotations

from collections import defaultdict

from sqlalchemy import and_, select, update

from app.activities.names import activity_name
from app.errors import AppError, require_revision
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.schemas import Coverage, EventSummaryDto
from app.security.secrets import canonical, fingerprint
from app.timeutils import intersect, iso, schedule_intervals, timestamp, union
from app.worker.queue import enqueue


def bitmap_intervals(start: int, bitmap: int) -> list[tuple[int, int]]:
    return union([(start + i * 1000, start + (i + 1) * 1000) for i in range(60) if bitmap & (1 << i)])


def segment_dto(row: dict) -> dict:
    details = row.get("details_json", {})
    return dict(
        id=row["id"],
        subject_id=row["subject_id"],
        identity_status=row["identity_status"],
        activity=activity_name(row["activity"]),
        labels=details.get("labels", []),
        object_ids=details.get("object_ids", []),
        start_at=iso(row["start_at_ms"]),
        end_at=iso(row["end_at_ms"]),
        duration_ms=row["end_at_ms"] - row["start_at_ms"],
        boundary_range=row.get("boundary_json", {}),
        observation=details.get("observation", ""),
        evidence_ids=row.get("evidence_ids_json", []),
        origin=row["origin"],
        support=row["support"],
    )


class Ledger:
    def __init__(self, workbench):
        self.w = workbench

    def revision(self, conn) -> int:
        return one(conn, s.system_state, "projection")["value_json"]["revision"]

    def advance(self, conn) -> int:
        revision = self.revision(conn) + 1
        conn.execute(
            update(s.system_state)
            .where(s.system_state.c.id == "projection")
            .values(value_json={"revision": revision}, updated_at_ms=self.w.now())
        )
        return revision

    def scope(self, conn, camera_ids: list[str] | None, subject_id: str | None, start: int, end: int) -> dict:
        if subject_id:
            from app.services.discovery import Discovery

            subject_id = Discovery(self.w).resolve(conn, subject_id)
            entity = one(conn, s.entities, subject_id, deleted=True)
            if entity["kind"] != "person":
                raise AppError(422, "VALIDATION_ERROR", "subject_id 必须为人物")
        if camera_ids:
            ids = sorted(set(camera_ids))
            for camera_id in ids:
                one(conn, s.cameras, camera_id, deleted=True)
        else:
            ids = sorted(conn.execute(select(s.cameras.c.id)).scalars())
            if subject_id:
                applicable = entity["camera_ids_json"]
                ids = [c for c in ids if not applicable or c in applicable]
            ids = [c for c in ids if self.planned(conn, c, start, end)]
        return {"camera_ids": ids, "subject_id": subject_id}

    def planned(self, conn, camera_id: str, start: int, end: int) -> list[tuple[int, int]]:
        versions = (
            conn.execute(
                select(s.schedule_versions)
                .where(
                    s.schedule_versions.c.camera_id == camera_id,
                    s.schedule_versions.c.effective_from_ms < end,
                )
                .order_by(s.schedule_versions.c.effective_from_ms, s.schedule_versions.c.version)
            )
            .mappings()
            .all()
        )
        intervals = []
        for i, version in enumerate(versions):
            a = max(start, version["effective_from_ms"])
            b = min(end, versions[i + 1]["effective_from_ms"] if i + 1 < len(versions) else end)
            override = version["override_json"]
            if a >= b or not override.get("enabled", True):
                continue
            until = override.get("until_ms") or a
            if override.get("mode") and until > a:
                if override["mode"] == "force_on":
                    intervals.append((a, min(b, until)))
                a = min(b, until)
            if a < b:
                intervals.extend(schedule_intervals(version["weekly_rules_json"], a, b))
        return union(intervals)

    def captured(self, conn, camera_id: str, start: int, end: int):
        rows = conn.execute(
            select(s.minute_records.c.start_at_ms, s.minute_capture.c.captured_bitmap)
            .join(s.minute_capture, s.minute_capture.c.minute_id == s.minute_records.c.id)
            .where(
                s.minute_records.c.camera_id == camera_id,
                s.minute_records.c.start_at_ms < end,
                s.minute_records.c.start_at_ms + 60000 > start,
            )
        ).all()
        return intersect(
            [(start, end)],
            [span for row in rows for span in bitmap_intervals(row.start_at_ms, row.captured_bitmap)],
        )

    def spans(self, conn, camera_ids: list[str], start: int, end: int) -> list[dict]:
        rows = (
            conn.execute(
                select(s.activity_spans)
                .join(
                    s.minute_records,
                    and_(
                        s.minute_records.c.id == s.activity_spans.c.minute_id,
                        s.minute_records.c.current_revision == s.activity_spans.c.minute_revision,
                    ),
                )
                .where(
                    s.activity_spans.c.camera_id.in_(camera_ids),
                    s.activity_spans.c.start_at_ms < end,
                    s.activity_spans.c.end_at_ms > start,
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    def load_range(self, conn, start, end, scope):
        planned = {c: self.planned(conn, c, start, end) for c in scope["camera_ids"]}
        captured = {c: intersect(planned[c], self.captured(conn, c, start, end)) for c in planned}
        spans = self.spans(conn, scope["camera_ids"], start, end)
        minutes = (
            conn.execute(
                select(s.minute_records, s.minute_revisions.c.result_json)
                .outerjoin(
                    s.minute_revisions,
                    and_(
                        s.minute_records.c.id == s.minute_revisions.c.minute_id,
                        s.minute_records.c.current_revision == s.minute_revisions.c.revision,
                    ),
                )
                .where(
                    s.minute_records.c.camera_id.in_(scope["camera_ids"]),
                    s.minute_records.c.start_at_ms < end,
                    s.minute_records.c.start_at_ms + 60000 > start,
                )
            )
            .mappings()
            .all()
        )
        return dict(
            planned=planned, captured=captured, spans=spans, minutes=minutes, revision=self.revision(conn)
        )

    def calculate(self, conn, start: int, end: int, scope: dict, spans_override=None, data=None) -> dict:
        data = data or self.load_range(conn, start, end, scope)
        planned = {c: intersect(v, [(start, end)]) for c, v in data["planned"].items()}
        captured = {c: intersect(v, [(start, end)]) for c, v in data["captured"].items()}
        spans = data["spans"] if spans_override is None else spans_override
        spans = [
            r | {"activity": activity_name(r["activity"])}
            for r in spans
            if r["start_at_ms"] < end and r["end_at_ms"] > start
        ]
        minutes = [r for r in data["minutes"] if r["start_at_ms"] < end and r["start_at_ms"] + 60000 > start]
        # 扫描区间端点，避免对长范围逐帧累计，也避免跨机位重复计算。
        changes: dict[int, list] = defaultdict(list)
        for camera, intervals in planned.items():
            for a, b in intervals:
                changes[a].append(("p", camera, True))
                changes[b].append(("p", camera, False))
        for camera, intervals in captured.items():
            for a, b in intervals:
                changes[a].append(("c", camera, True))
                changes[b].append(("c", camera, False))
        for row in spans:
            a, b = max(start, row["start_at_ms"]), min(end, row["end_at_ms"])
            changes[a].append(("s", row["id"], row))
            changes[b].append(("s", row["id"], None))
        for minute in minutes:
            a, b = max(start, minute["start_at_ms"]), min(end, minute["start_at_ms"] + 60000)
            changes[a].append(("m", minute["camera_id"], minute))
            changes[b].append(("m", minute["camera_id"], None))
        changes[start]
        changes[end]
        points = sorted(changes)
        p, c, active, active_minutes = set(), set(), {}, {}
        coverage = Coverage().model_dump()
        activities = defaultdict(int)
        slices = []
        subject = scope["subject_id"]
        for i, a in enumerate(points[:-1]):
            # 同端点先移除再添加，保持左闭右开。
            for kind, key, value in sorted(changes[a], key=lambda x: bool(x[2])):
                if kind in {"p", "c"}:
                    group = p if kind == "p" else c
                    group.add(key) if value else group.discard(key)
                else:
                    group = active if kind == "s" else active_minutes
                    if value:
                        group[key] = value
                    else:
                        group.pop(key, None)
            b = points[i + 1]
            if not p or b <= a:
                continue
            duration = b - a
            coverage["planned_ms"] += duration
            cameras = p & c
            category, activity = "gap", None
            conflict = False
            if cameras:
                coverage["captured_ms"] += duration
                candidates = [r for r in active.values() if r["camera_id"] in cameras]
                matched = [
                    r
                    for r in candidates
                    if (subject is None or r["subject_id"] == subject)
                    and (subject is None or r["identity_status"] == "matched")
                ]
                if any(r["origin"] == "manual" for r in matched):
                    matched = [r for r in matched if r["origin"] == "manual"]
                # 每路优先 dense；人工在整个查询范围中优先。
                selected = []
                for camera in cameras:
                    local = [r for r in matched if r["camera_id"] == camera]
                    if any(r["support"] in {"dense", "mixed"} for r in local):
                        local = [r for r in local if r["support"] in {"dense", "mixed"}]
                    selected.extend(local)
                positive = [r for r in selected if r["activity"] not in {"away", "unknown"}]
                categories = {r["activity"] for r in positive}
                if len(categories) > 1:
                    category, conflict = "unknown", True
                elif len(categories) == 1 and not any(
                    r["activity"] == "unknown" and r["origin"] == "manual" for r in selected
                ):
                    category, activity = "classified", next(iter(categories))
                elif selected:
                    if (
                        all(r["activity"] == "away" for r in selected)
                        and all(
                            any(r["camera_id"] == camera and r["activity"] == "away" for r in selected)
                            for camera in p
                        )
                        and p <= c
                    ):
                        category, activity = "classified", "away"
                    else:
                        category = "unknown"
                elif candidates:
                    category = "unknown"
                else:
                    statuses = [
                        active_minutes[camera]["analysis_state"]
                        for camera in cameras
                        if camera in active_minutes
                    ]
                    reasons = [
                        (active_minutes[camera].get("result_json") or {}).get("unanalysed_reason")
                        for camera in cameras
                        if camera in active_minutes
                    ]
                    if (
                        statuses
                        and all(state in {"complete", "partial"} for state in statuses)
                        and not any(reasons)
                    ):
                        category = "unknown"
                    else:
                        sub = (
                            "pending"
                            if "pending" in reasons
                            else "failed"
                            if "failed" in reasons
                            else "expired"
                            if "expired" in reasons
                            else (
                                "pending"
                                if not statuses or any(st in {"pending", "running"} for st in statuses)
                                else "failed"
                                if "failed" in statuses
                                else "expired"
                            )
                        )
                        category = "unanalysed"
                        coverage[f"unanalysed_{sub}_ms"] += duration
            coverage[f"{category}_ms"] += duration
            if conflict:
                coverage["conflict_ms"] += duration
            if activity:
                activities[activity] += duration
            slices.append(
                {"start": a, "end": b, "kind": category, "activity": activity, "conflict": conflict}
            )
        assert (
            coverage["classified_ms"]
            + coverage["unknown_ms"]
            + coverage["unanalysed_ms"]
            + coverage["gap_ms"]
            == coverage["planned_ms"]
        )
        assert sum(activities.values()) == coverage["classified_ms"]
        return dict(
            coverage=coverage,
            activities=[{"activity": a, "duration_ms": d} for a, d in sorted(activities.items())],
            duration_is_estimated=True,
            projection_revision=data["revision"],
            slices=slices,
        )

    def invalidate(self, conn, start: int, end: int, *, regenerate=True):
        affected = list(
            conn.execute(
                select(s.summaries).where(s.summaries.c.start_at_ms < end, s.summaries.c.end_at_ms > start)
            ).mappings()
        )
        for summary in affected:
            conn.execute(
                update(s.summaries).where(s.summaries.c.id == summary["id"]).values(source_state="stale")
            )
            if not regenerate:
                continue
            existing = (
                conn.execute(
                    select(s.jobs).where(
                        s.jobs.c.kind.in_(["summarize_hour", "summarize_day"]),
                        s.jobs.c.state.in_(["queued", "running"]),
                    )
                )
                .mappings()
                .all()
            )
            if not any(j["payload_json"].get("summary_id") == summary["id"] for j in existing):
                from app.summaries.service import Summaries

                if not self.w.settings_config(conn).summary_provider_id:
                    continue
                payload = Summaries(self.w).job_payload(conn, summary, self.w.settings_config(conn))
                job = enqueue(
                    conn,
                    self.w.now(),
                    f"summarize_{summary['type']}",
                    payload,
                    f"summary-invalidate:{summary['id']}:{self.revision(conn)}",
                    delay=60000,
                )
                conn.execute(
                    update(s.summaries)
                    .where(s.summaries.c.id == summary["id"])
                    .values(pending_job_id=job["id"])
                )
        return [r["id"] for r in affected]

    def project(self, conn, start=None, end=None):
        revision = self.revision(conn)
        ids = list(conn.execute(select(s.cameras.c.id)).scalars())
        merge_gap = self.w.settings_config(conn).merge_gap_seconds * 1000
        if start is not None and end is not None:
            # 扩展至相接事件的完整窗口，避免每分钟扫描全部历史。
            while True:
                bounds = (start, end)
                overlapping = conn.execute(
                    select(s.event_revisions.c.summary_json)
                    .join(
                        s.events,
                        and_(
                            s.events.c.id == s.event_revisions.c.event_id,
                            s.events.c.current_revision == s.event_revisions.c.revision,
                        ),
                    )
                    .where(
                        s.event_revisions.c.summary_json["start_at"].as_string() <= iso(end + merge_gap),
                        s.event_revisions.c.summary_json["end_at"].as_string() >= iso(start - merge_gap),
                    )
                ).scalars()
                for event in overlapping:
                    start = min(start, timestamp(event["start_at"]))
                    end = max(end, timestamp(event["end_at"]))
                if bounds == (start, end):
                    break
        spans = self.spans(conn, ids, start or 0, end or 2**62)
        # 事件和概览使用同一投影，冲突不能再次显示为有效分类。
        projected = []
        by_subject = defaultdict(list)
        for span in spans:
            by_subject[
                (span["subject_id"], span["camera_id"] if span["subject_id"] is None else None)
            ].append(span)
        for (subject, camera), source_spans in by_subject.items():
            lo = min(r["start_at_ms"] for r in source_spans)
            hi = max(r["end_at_ms"] for r in source_spans)
            slices = self.calculate(
                conn, lo, hi, {"camera_ids": [camera] if camera else ids, "subject_id": subject}
            )["slices"]
            for span in source_spans:
                for item in slices:
                    a = max(item["start"], span["start_at_ms"])
                    b = min(item["end"], span["end_at_ms"])
                    keep = item["kind"] == "classified" and item["activity"] == span["activity"]
                    unknown = item["kind"] == "unknown" and (
                        item["conflict"]
                        or span["activity"] == "unknown"
                        or span["identity_status"] != "matched"
                    )
                    if a < b and (keep or unknown):
                        projected.append(
                            span
                            | {
                                "id": "span_" + fingerprint([span["id"], a, b, unknown])[:26],
                                "start_at_ms": a,
                                "end_at_ms": b,
                                "activity": "unknown" if unknown else span["activity"],
                            }
                        )
        spans = projected
        groups = defaultdict(list)
        for span in spans:
            groups[
                (
                    span["subject_id"],
                    activity_name(span["activity"]),
                    span["identity_status"],
                    span["camera_id"] if span["subject_id"] is None else None,
                )
            ].append(span)
        used = set()
        for (subject, activity, identity, _camera), group in groups.items():
            clusters = []
            for span in sorted(group, key=lambda r: (r["start_at_ms"], r["id"])):
                if clusters and span["start_at_ms"] <= max(r["end_at_ms"] for r in clusters[-1]) + merge_gap:
                    clusters[-1].append(span)
                else:
                    clusters.append([span])
            for cluster in clusters:
                a, b = min(r["start_at_ms"] for r in cluster), max(r["end_at_ms"] for r in cluster)
                # ID 以摄像头／首个分钟锚点决定，重算不会随随机 span ID 变化。
                anchor = min(cluster, key=lambda r: (r["start_at_ms"], r["camera_id"]))
                event_id = (
                    "evt_"
                    + fingerprint([subject, activity, identity, anchor["camera_id"], anchor["minute_id"], a])[
                        :26
                    ]
                )
                used.add(event_id)
                intervals = union([(r["start_at_ms"], r["end_at_ms"]) for r in cluster])
                interruptions = []
                for left, right in zip(intervals, intervals[1:]):
                    scope = {"camera_ids": sorted({r["camera_id"] for r in cluster}), "subject_id": subject}
                    for item in self.calculate(conn, left[1], right[0], scope)["slices"]:
                        interruptions.append(
                            dict(
                                start_at=iso(item["start"]),
                                end_at=iso(item["end"]),
                                kind="other_activity" if item["kind"] == "classified" else item["kind"],
                                duration_ms=item["end"] - item["start"],
                            )
                        )
                sources = sorted({(r["minute_id"], r["minute_revision"]) for r in cluster})
                details = dict(
                    id=event_id,
                    revision=1,
                    source_revision=revision,
                    subject_id=subject,
                    identity_status=identity,
                    camera_ids=sorted({r["camera_id"] for r in cluster}),
                    activity=activity,
                    labels=sorted({x for r in cluster for x in r["details_json"].get("labels", [])}),
                    object_ids=sorted({x for r in cluster for x in r["details_json"].get("object_ids", [])}),
                    start_at=iso(a),
                    end_at=iso(b),
                    duration_ms=sum(end - start for start, end in intervals),
                    segments=[segment_dto(r) for r in cluster],
                    interruptions=interruptions,
                    minute_refs=[{"id": minute, "revision": rev} for minute, rev in sources],
                    evidence_state="none",
                    source_state="current",
                    has_manual_revision=any(r["origin"] == "manual" for r in cluster),
                    replacement_event_ids=[],
                )
                source_hash = fingerprint([sources, intervals, details["labels"], details["object_ids"]])
                existing = conn.execute(select(s.events).where(s.events.c.id == event_id)).mappings().first()
                if existing:
                    previous = (
                        conn.execute(
                            select(s.event_revisions).where(
                                s.event_revisions.c.event_id == event_id,
                                s.event_revisions.c.revision == existing["current_revision"],
                            )
                        )
                        .mappings()
                        .one()
                    )
                    if previous["source_fingerprint"] == source_hash:
                        continue
                    rev = existing["current_revision"] + 1
                    conn.execute(
                        update(s.events).where(s.events.c.id == event_id).values(current_revision=rev)
                    )
                else:
                    rev = 1
                    conn.execute(
                        s.events.insert().values(
                            id=event_id,
                            scope_key=canonical({"subject_id": subject, "camera_ids": details["camera_ids"]}),
                            current_revision=rev,
                            created_at_ms=self.w.now(),
                        )
                    )
                details["revision"] = rev
                conn.execute(
                    s.event_revisions.insert().values(
                        event_id=event_id,
                        revision=rev,
                        source_fingerprint=source_hash,
                        intervals_json=intervals,
                        summary_json=details,
                        replacement_event_ids_json=[],
                        created_at_ms=self.w.now(),
                    )
                )
                self.w.assets.keep_evidence(
                    conn, [e for r in cluster for e in r["evidence_ids_json"]], "event", event_id, rev
                )
        # 保留旧入口并标注替代事件。只变动有效入口指针，不改写旧版本。
        for old in conn.execute(select(s.events)).mappings().all():
            if old["id"] in used:
                continue
            previous = (
                conn.execute(
                    select(s.event_revisions).where(
                        s.event_revisions.c.event_id == old["id"],
                        s.event_revisions.c.revision == old["current_revision"],
                    )
                )
                .mappings()
                .one()
            )
            info = dict(previous["summary_json"])
            if (
                start is not None
                and end is not None
                and (
                    timestamp(info["start_at"]) > end + merge_gap
                    or timestamp(info["end_at"]) < start - merge_gap
                )
            ):
                continue
            if info["source_state"] == "stale":
                continue
            replacements = []
            for candidate in used:
                current = self.event(conn, candidate, refresh_evidence=False)
                if (
                    current["subject_id"] == info["subject_id"]
                    and timestamp(current["start_at"]) < timestamp(info["end_at"])
                    and timestamp(current["end_at"]) > timestamp(info["start_at"])
                ):
                    replacements.append(candidate)
            info |= {
                "revision": old["current_revision"] + 1,
                "source_state": "stale",
                "replacement_event_ids": sorted(replacements),
            }
            conn.execute(
                s.event_revisions.insert().values(
                    event_id=old["id"],
                    revision=info["revision"],
                    source_fingerprint=fingerprint([previous["source_fingerprint"], replacements]),
                    intervals_json=previous["intervals_json"],
                    summary_json=info,
                    replacement_event_ids_json=sorted(replacements),
                    created_at_ms=self.w.now(),
                )
            )
            conn.execute(
                update(s.events).where(s.events.c.id == old["id"]).values(current_revision=info["revision"])
            )

    def evidence_state(self, conn, ids):
        return self.combine_evidence_states(ids, self.w.assets.metadata_many(conn, ids))

    @staticmethod
    def combine_evidence_states(ids, metadata):
        states = [metadata.get(e, {"state": "missing"})["state"] for e in set(ids)]
        if not states:
            return "none"
        if all(s == "available" for s in states):
            return "available"
        if "available" in states:
            return "partial"
        return "evicted" if "evicted" in states else "missing" if "missing" in states else "expired"

    def event_summaries(self, conn, event_ids):
        if not event_ids:
            return []
        fields = [k for k in EventSummaryDto.model_fields if k != "evidence_state"]
        rows = list(
            conn.execute(
                select(*[s.event_revisions.c.summary_json[k].label(k) for k in fields])
                .join(
                    s.events,
                    and_(
                        s.events.c.id == s.event_revisions.c.event_id,
                        s.events.c.current_revision == s.event_revisions.c.revision,
                    ),
                )
                .where(s.events.c.id.in_(event_ids))
            ).mappings()
        )
        links = defaultdict(list)
        for owner, evidence_id in conn.execute(
            select(s.revision_evidence_links.c.owner_id, s.revision_evidence_links.c.evidence_id)
            .join(
                s.events,
                and_(
                    s.events.c.id == s.revision_evidence_links.c.owner_id,
                    s.events.c.current_revision == s.revision_evidence_links.c.owner_revision,
                ),
            )
            .where(s.revision_evidence_links.c.owner_kind == "event", s.events.c.id.in_(event_ids))
        ):
            links[owner].append(evidence_id)
        metadata = self.w.assets.metadata_many(conn, [e for values in links.values() for e in values])
        result = {
            r["id"]: dict(r) | {"evidence_state": self.combine_evidence_states(links[r["id"]], metadata)}
            for r in rows
        }
        for item in result.values():
            item["has_manual_revision"] = bool(item["has_manual_revision"])
        return [result[i] for i in event_ids if i in result]

    def event(self, conn, event_id: str, revision=None, refresh_evidence=True):
        event = one(conn, s.events, event_id)
        row = (
            conn.execute(
                select(s.event_revisions).where(
                    s.event_revisions.c.event_id == event_id,
                    s.event_revisions.c.revision == (revision or event["current_revision"]),
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "事件版本不存在")
        data = dict(row["summary_json"])
        if refresh_evidence:
            data["evidence_state"] = self.evidence_state(
                conn, [e for seg in data["segments"] for e in seg["evidence_ids"]]
            )
        return data

    def correct_event(self, conn, event_id, dto, header):
        event = self.event(conn, event_id)
        require_revision(event, header)
        if event["source_revision"] != dto.base_source_revision:
            raise AppError(
                412,
                "REVISION_CONFLICT",
                "事件来源投影已变化",
                details={"revision": event["revision"], "source_revision": event["source_revision"]},
            )
        a, b = timestamp(event["start_at"]), timestamp(event["end_at"])
        scope = {"camera_ids": event["camera_ids"], "subject_id": event["subject_id"]}
        available = union(
            [
                span
                for camera in scope["camera_ids"]
                for span in intersect(self.planned(conn, camera, a, b), self.captured(conn, camera, a, b))
            ]
        )
        for interval in dto.intervals:
            x, y = timestamp(interval.start_at), timestamp(interval.end_at)
            if x < a or y > b or sum(e - s for s, e in intersect([(x, y)], available)) != y - x:
                raise AppError(422, "EDIT_CROSSES_GAP", "修订区间不能超出来源窗口或穿过采集缺口")
            if interval.subject_id:
                entity = one(conn, s.entities, interval.subject_id, deleted=True)
                if entity["kind"] != "person":
                    raise AppError(422, "VALIDATION_ERROR", "主体必须是人物")
            for obj in interval.object_ids:
                if one(conn, s.entities, obj, deleted=True)["kind"] != "object":
                    raise AppError(422, "VALIDATION_ERROR", "object_ids 必须为物品")
        correction_id = new_id("correction")
        conn.execute(
            update(s.manual_corrections)
            .where(
                s.manual_corrections.c.event_id == event_id, s.manual_corrections.c.superseded_by.is_(None)
            )
            .values(superseded_by=correction_id)
        )
        conn.execute(
            s.manual_corrections.insert().values(
                id=correction_id,
                event_id=event_id,
                base_source_revision=dto.base_source_revision,
                correction_json=dto.model_dump(mode="json"),
                reason=dto.reason,
                created_at_ms=self.w.now(),
            )
        )
        minutes = (
            conn.execute(
                select(s.minute_records).where(
                    s.minute_records.c.camera_id.in_(scope["camera_ids"]),
                    s.minute_records.c.start_at_ms < b,
                    s.minute_records.c.start_at_ms + 60000 > a,
                )
            )
            .mappings()
            .all()
        )
        for raw in minutes:
            minute = dict(raw)
            old_spans = self.spans(
                conn, [minute["camera_id"]], minute["start_at_ms"], minute["start_at_ms"] + 60000
            )
            next_revision = (
                conn.execute(
                    select(s.minute_revisions.c.revision)
                    .where(s.minute_revisions.c.minute_id == minute["id"])
                    .order_by(s.minute_revisions.c.revision.desc())
                    .limit(1)
                ).scalar()
                or 0
            ) + 1
            old = (
                conn.execute(
                    select(s.minute_revisions).where(
                        s.minute_revisions.c.minute_id == minute["id"],
                        s.minute_revisions.c.revision == minute["current_revision"],
                    )
                )
                .mappings()
                .first()
            )
            result = dict(old["result_json"]) if old else {}
            result |= {"has_manual_revision": True, "manual_reason": dto.reason}
            conn.execute(
                s.minute_revisions.insert().values(
                    minute_id=minute["id"],
                    revision=next_revision,
                    source_kind="manual",
                    source_fingerprint=fingerprint([correction_id, minute["id"]]),
                    reference_set_id=old["reference_set_id"] if old else None,
                    result_json=result,
                    created_at_ms=self.w.now(),
                )
            )
            conn.execute(
                update(s.minute_records)
                .where(s.minute_records.c.id == minute["id"])
                .values(current_revision=next_revision)
            )
            # 保留修订窗口之外的有效分类；窗口内删掉的分类明确成为 unknown。
            for span in old_spans:
                for x, y in [
                    (span["start_at_ms"], min(span["end_at_ms"], a)),
                    (max(span["start_at_ms"], b), span["end_at_ms"]),
                ]:
                    if x < y:
                        conn.execute(
                            s.activity_spans.insert().values(
                                **(
                                    span
                                    | {
                                        "id": new_id("span"),
                                        "minute_revision": next_revision,
                                        "start_at_ms": x,
                                        "end_at_ms": y,
                                    }
                                )
                            )
                        )
            support = intersect(
                available, [(max(a, minute["start_at_ms"]), min(b, minute["start_at_ms"] + 60000))]
            )
            for x, y in support:
                boundaries = sorted(
                    {
                        x,
                        y,
                        *[
                            max(x, min(y, timestamp(v)))
                            for interval in dto.intervals
                            for v in [interval.start_at, interval.end_at]
                        ],
                    }
                )
                for lo, hi in zip(boundaries, boundaries[1:]):
                    if lo >= hi:
                        continue
                    matching = next(
                        (
                            v
                            for v in dto.intervals
                            if timestamp(v.start_at) <= lo and timestamp(v.end_at) >= hi
                        ),
                        None,
                    )
                    evidence_ids = sorted(
                        {
                            e
                            for span in old_spans
                            if span["start_at_ms"] < hi and span["end_at_ms"] > lo
                            for e in span["evidence_ids_json"]
                        }
                    )
                    conn.execute(
                        s.activity_spans.insert().values(
                            id=new_id("span"),
                            camera_id=minute["camera_id"],
                            subject_id=matching.subject_id if matching else event["subject_id"],
                            identity_status="matched" if matching and matching.subject_id else "unknown",
                            minute_id=minute["id"],
                            minute_revision=next_revision,
                            start_at_ms=lo,
                            end_at_ms=hi,
                            activity=activity_name(matching.activity) if matching else "unknown",
                            origin="manual",
                            support="mixed",
                            boundary_json={},
                            evidence_ids_json=evidence_ids,
                            details_json={
                                "labels": matching.labels if matching else [],
                                "object_ids": matching.object_ids if matching else [],
                                "observation": dto.reason,
                            },
                        )
                    )
                    self.w.assets.keep_evidence(conn, evidence_ids, "minute", minute["id"], next_revision)
        rev = self.advance(conn)
        self.project(conn, a, b)
        affected = self.invalidate(conn, a, b)
        return dict(
            event=self.event(conn, event_id), projection_revision=rev, affected_summary_ids=affected, jobs=[]
        )
