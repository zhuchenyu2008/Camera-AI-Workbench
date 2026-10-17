from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from app.errors import AppError
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.schemas import SummaryOutput
from app.security.secrets import canonical, fingerprint
from app.timeutils import BEIJING, day_range, iso, timestamp
from app.worker.queue import enqueue, job_dto


def validate_summary(output: SummaryOutput, source_refs: list[dict], statistics: dict):
    allowed = {(r["kind"], r["id"], r["revision"]): r for r in source_refs}
    for ref in output.source_refs + [r for highlight in output.highlights for r in highlight.source_refs]:
        key = (ref.kind, ref.id, ref.revision)
        if (
            key not in allowed
            or timestamp(ref.start_at) != timestamp(allowed[key]["start_at"])
            or timestamp(ref.end_at) != timestamp(allowed[key]["end_at"])
        ):
            raise AppError(422, "INVALID_MODEL_REFERENCES", "汇总引用不属于本次有效来源")
    allowed_durations = set(statistics["coverage"].values()) | {
        v["duration_ms"] for v in statistics["activities"]
    }
    for text in [output.narrative] + [v.text for v in output.highlights]:
        for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(毫秒|分钟|小时|秒)", text):
            duration = round(float(number) * {"毫秒": 1, "秒": 1000, "分钟": 60000, "小时": 3600000}[unit])
            if duration not in allowed_durations:
                raise AppError(422, "INVALID_MODEL_OUTPUT", "模型文案的精确时长与确定性统计不一致")


class Summaries:
    def __init__(self, workbench):
        self.w = workbench

    def ensure(self, conn, kind, start, end, scope):
        scope_key = canonical(scope)
        row = (
            conn.execute(
                select(s.summaries).where(
                    s.summaries.c.type == kind,
                    s.summaries.c.scope_key == scope_key,
                    s.summaries.c.start_at_ms == start,
                )
            )
            .mappings()
            .first()
        )
        if row:
            return dict(row)
        row = dict(
            id=new_id("summary"),
            type=kind,
            scope_key=scope_key,
            start_at_ms=start,
            end_at_ms=end,
            current_revision=0,
            pending_job_id=None,
            source_state="stale",
        )
        conn.execute(s.summaries.insert().values(**row))
        return row

    def job_payload(self, conn, summary, cfg):
        import json

        if not cfg.summary_provider_id:
            raise AppError(422, "PROVIDER_NOT_VERIFIED", "尚未配置汇总模型")
        provider = one(conn, s.providers, cfg.summary_provider_id)
        scope = json.loads(summary["scope_key"])
        refs = self.w.assets.reference_set(conn, scope["camera_ids"])
        return {
            "summary_id": summary["id"],
            "provider_id": provider["id"],
            "provider_revision": provider["revision"],
            "reference_set_id": refs["id"],
        }

    def context(self, conn, summary, reference_set_id=None):
        import json

        scope = json.loads(summary["scope_key"])
        start, end = summary["start_at_ms"], summary["end_at_ms"]
        calculated = self.w.ledger.calculate(conn, start, end, scope)
        records = (
            conn.execute(
                select(s.minute_records)
                .where(
                    s.minute_records.c.camera_id.in_(scope["camera_ids"]),
                    s.minute_records.c.start_at_ms < end,
                    s.minute_records.c.start_at_ms + 60000 > start,
                )
                .order_by(s.minute_records.c.start_at_ms, s.minute_records.c.id)
            )
            .mappings()
            .all()
        )
        sources, minutes = [], []
        for row in records:
            if not row["current_revision"]:
                continue
            revision = (
                conn.execute(
                    select(s.minute_revisions).where(
                        s.minute_revisions.c.minute_id == row["id"],
                        s.minute_revisions.c.revision == row["current_revision"],
                    )
                )
                .mappings()
                .one()
            )
            sources.append(
                dict(
                    kind="minute",
                    id=row["id"],
                    revision=row["current_revision"],
                    start_at=iso(row["start_at_ms"]),
                    end_at=iso(row["start_at_ms"] + 60000),
                )
            )
            minutes.append(
                {
                    "id": row["id"],
                    "revision": row["current_revision"],
                    "description": revision["result_json"].get("description", ""),
                    "analysis_state": row["analysis_state"],
                }
            )
        if summary["type"] == "day":
            hours = (
                conn.execute(
                    select(s.summaries).where(
                        s.summaries.c.type == "hour",
                        s.summaries.c.scope_key == summary["scope_key"],
                        s.summaries.c.start_at_ms >= start,
                        s.summaries.c.end_at_ms <= end,
                    )
                )
                .mappings()
                .all()
            )
            for hour in hours:
                if hour["current_revision"]:
                    sources.append(
                        dict(
                            kind="hour",
                            id=hour["id"],
                            revision=hour["current_revision"],
                            start_at=iso(hour["start_at_ms"]),
                            end_at=iso(hour["end_at_ms"]),
                        )
                    )
        refset = (
            one(conn, s.reference_sets, reference_set_id)
            if reference_set_id
            else self.w.assets.reference_set(conn, scope["camera_ids"])
        )
        stats = {k: v for k, v in calculated.items() if k != "slices"}
        key = fingerprint([sources, stats, refset["fingerprint"], "statistics-v1", "summary-v1"])
        return dict(
            scope=scope,
            source_refs=sources,
            minutes=minutes,
            statistics=stats,
            reference_set=refset,
            fingerprint=key,
            provisional=any(row["analysis_state"] in {"pending", "running"} for row in records),
        )

    def commit(self, conn, summary, context, output: SummaryOutput | None, error=None):
        summary = one(conn, s.summaries, summary["id"])
        fresh = self.context(conn, summary)
        # 调用模型时锁定的来源若变了，旧文案保留为历史，不标为当前来源。
        stale = fresh["fingerprint"] != context["fingerprint"]
        revision = summary["current_revision"] + 1
        statistics = context["statistics"]
        previous = None
        if summary["current_revision"]:
            previous = (
                conn.execute(
                    select(s.summary_revisions).where(
                        s.summary_revisions.c.summary_id == summary["id"],
                        s.summary_revisions.c.revision == summary["current_revision"],
                    )
                )
                .mappings()
                .one()
            )
        narrative = (
            output.narrative if output else previous["narrative"] if previous else self.fallback(statistics)
        )
        state = "failed" if error else "provisional" if context["provisional"] else "complete"
        details = {
            "scope": context["scope"],
            "highlights": [h.model_dump(mode="json") for h in output.highlights]
            if output
            else previous["details_json"].get("highlights", [])
            if previous
            else [],
            "statistics_source_revision": statistics["projection_revision"],
            "narrative_source_fingerprint": context["fingerprint"]
            if output
            else previous["details_json"].get("narrative_source_fingerprint")
            if previous
            else None,
            "error_code": error,
        }
        conn.execute(
            s.summary_revisions.insert().values(
                summary_id=summary["id"],
                revision=revision,
                source_fingerprint=context["fingerprint"],
                source_refs_json=context["source_refs"],
                reference_set_id=context["reference_set"]["id"],
                coverage_json=statistics["coverage"],
                activities_json=statistics["activities"],
                narrative=narrative,
                summary_state=state,
                details_json=details,
                created_at_ms=self.w.now(),
            )
        )
        conn.execute(
            update(s.summaries)
            .where(s.summaries.c.id == summary["id"])
            .values(
                current_revision=revision, source_state="stale" if stale else "current", pending_job_id=None
            )
        )
        if stale:
            cfg = self.w.settings_config(conn)
            if cfg.summary_provider_id:
                job = enqueue(
                    conn,
                    self.w.now(),
                    f"summarize_{summary['type']}",
                    self.job_payload(conn, summary, cfg),
                    f"summary-refresh:{summary['id']}:{fresh['fingerprint']}",
                    delay=60000,
                )
                conn.execute(
                    update(s.summaries)
                    .where(s.summaries.c.id == summary["id"])
                    .values(pending_job_id=job["id"])
                )
        if summary["type"] == "hour":
            for day in conn.execute(
                select(s.summaries).where(
                    s.summaries.c.type == "day",
                    s.summaries.c.scope_key == summary["scope_key"],
                    s.summaries.c.start_at_ms < summary["end_at_ms"],
                    s.summaries.c.end_at_ms > summary["start_at_ms"],
                )
            ).mappings():
                conn.execute(
                    update(s.summaries).where(s.summaries.c.id == day["id"]).values(source_state="stale")
                )
                pending = conn.execute(
                    select(s.jobs.c.id).where(
                        s.jobs.c.state.in_(["queued", "running"]),
                        s.jobs.c.payload_json["summary_id"].as_string() == day["id"],
                    )
                ).first()
                cfg = self.w.settings_config(conn)
                if pending is None and cfg.summary_provider_id:
                    job = enqueue(
                        conn,
                        self.w.now(),
                        "summarize_day",
                        self.job_payload(conn, day, cfg),
                        f"day-after-hour:{day['id']}:{summary['id']}:{revision}",
                        delay=60000,
                    )
                    conn.execute(
                        update(s.summaries)
                        .where(s.summaries.c.id == day["id"])
                        .values(pending_job_id=job["id"])
                    )
        return {
            "resource_kind": "summary",
            "resource_id": summary["id"],
            "revision": revision,
            "label": "小时汇总" if summary["type"] == "hour" else "日报",
        }

    @staticmethod
    def fallback(statistics):
        c = statistics["coverage"]
        return f"计划采集 {c['planned_ms'] // 1000} 秒，实际采集 {c['captured_ms'] // 1000} 秒，已分类 {c['classified_ms'] // 1000} 秒，尚未分析 {c['unanalysed_ms'] // 1000} 秒，采集缺口 {c['gap_ms'] // 1000} 秒。活动时长为估计值。"

    def get(self, conn, summary_id, revision=None):
        summary = one(conn, s.summaries, summary_id)
        row = (
            conn.execute(
                select(s.summary_revisions).where(
                    s.summary_revisions.c.summary_id == summary_id,
                    s.summary_revisions.c.revision == (revision or summary["current_revision"]),
                )
            )
            .mappings()
            .first()
        )
        if row is None and revision is not None:
            raise AppError(404, "RESOURCE_NOT_FOUND", "报表版本不存在")
        import json

        scope = json.loads(summary["scope_key"])
        # 当前统计永远来自当前投影；旧版本读取保留其当时统计。
        stats = (
            self.w.ledger.calculate(conn, summary["start_at_ms"], summary["end_at_ms"], scope)
            if revision is None
            else {
                "coverage": row["coverage_json"],
                "activities": row["activities_json"],
                "projection_revision": row["details_json"]["statistics_source_revision"],
            }
        )
        c = stats["coverage"]
        details = row["details_json"] if row else {}
        return dict(
            id=summary_id,
            revision=row["revision"] if row else 0,
            type=summary["type"],
            local_date=datetime.fromtimestamp(summary["start_at_ms"] / 1000, BEIJING).date().isoformat(),
            start_at=iso(summary["start_at_ms"]),
            end_at=iso(summary["end_at_ms"]),
            scope=scope,
            summary_state=row["summary_state"] if row else "pending",
            source_state=summary["source_state"] if revision is None else "current",
            completeness="with_gaps" if c["unknown_ms"] + c["unanalysed_ms"] + c["gap_ms"] else "full",
            coverage=c,
            activities=stats["activities"],
            narrative=row["narrative"] if row else self.fallback(stats),
            highlights=details.get("highlights", []),
            source_refs=row["source_refs_json"] if row else [],
            generated_at=iso(row["created_at_ms"]) if row else None,
            reference_set_id=row["reference_set_id"] if row else None,
            pending_job_id=summary["pending_job_id"],
            statistics_source_revision=stats["projection_revision"],
            narrative_source_fingerprint=details.get("narrative_source_fingerprint"),
        )

    def regenerate(self, conn, dto):
        from app.services.analysis import Analysis

        Analysis(self.w)._limit_manual(conn)
        summary = one(conn, s.summaries, dto.summary_id)
        if summary["current_revision"] != dto.source_revision:
            raise AppError(
                412, "REVISION_CONFLICT", "汇总版本已变化", details={"revision": summary["current_revision"]}
            )
        cfg = self.w.settings_config(conn)
        job = enqueue(
            conn,
            self.w.now(),
            "manual_records",
            self.job_payload(conn, summary, cfg),
            f"manual-summary:{summary['id']}:{secrets.token_urlsafe(16)}",
            total=1,
        )
        conn.execute(
            update(s.summaries).where(s.summaries.c.id == summary["id"]).values(pending_job_id=job["id"])
        )
        return job_dto(job)

    def schedule(self, conn):
        cfg = self.w.settings_config(conn)
        first = conn.execute(select(func.min(s.minute_records.c.start_at_ms))).scalar()
        if first is None or not cfg.summary_provider_id:
            return
        try:
            self.w.require_provider(one(conn, s.providers, cfg.summary_provider_id))
        except AppError:
            return
        now = self.w.now()
        # 每轮有界补调度，重启不重造采集历史。
        first = max(first, now - 31 * 86400000)
        cameras = list(conn.execute(select(s.cameras.c.id)).scalars())
        scopes = [{"camera_ids": [c], "subject_id": None} for c in cameras]
        if cfg.primary_subject_id:
            scopes.append(self.w.ledger.scope(conn, None, cfg.primary_subject_id, first, now))
        scheduled = 0
        for scope in scopes:
            for start in range(first // 3600000 * 3600000, now, 3600000):
                end = start + 3600000
                if end + cfg.hourly_summary_delay_minutes * 60000 > now:
                    continue
                if not any(self.w.ledger.planned(conn, c, start, end) for c in scope["camera_ids"]):
                    continue
                summary = self.ensure(conn, "hour", start, end, scope)
                if summary["current_revision"] == 0 and summary["pending_job_id"] is None:
                    job = enqueue(
                        conn,
                        now,
                        "summarize_hour",
                        self.job_payload(conn, summary, cfg),
                        f"summary-initial:{summary['id']}",
                    )
                    conn.execute(
                        update(s.summaries)
                        .where(s.summaries.c.id == summary["id"])
                        .values(pending_job_id=job["id"])
                    )
                    scheduled += 1
                if scheduled >= 24:
                    return
            date = datetime.fromtimestamp(first / 1000, BEIJING).date()
            today = datetime.fromtimestamp(now / 1000, BEIJING).date()
            hour, minute = map(int, cfg.daily_summary_local_time.split(":"))
            while date < today:
                start, end = day_range(date.isoformat())
                if end + (hour * 60 + minute) * 60000 <= now and any(
                    self.w.ledger.planned(conn, c, start, end) for c in scope["camera_ids"]
                ):
                    summary = self.ensure(conn, "day", start, end, scope)
                    if not summary["current_revision"] and not summary["pending_job_id"]:
                        job = enqueue(
                            conn,
                            now,
                            "summarize_day",
                            self.job_payload(conn, summary, cfg),
                            f"summary-initial:{summary['id']}",
                        )
                        conn.execute(
                            update(s.summaries)
                            .where(s.summaries.c.id == summary["id"])
                            .values(pending_job_id=job["id"])
                        )
                date += timedelta(days=1)
