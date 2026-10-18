from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.repositories import schema as s
from app.timeutils import BEIJING, iso
from app.worker.queue import IMAGE_KINDS


def status_data(w):
    now = w.now()
    with w.db.read() as conn:
        heartbeat = (
            conn.execute(select(s.system_state).where(s.system_state.c.id == "worker")).mappings().first()
        )
        stamp = heartbeat["updated_at_ms"] if heartbeat else None
        state = (
            "offline"
            if stamp is None or now - stamp > 90000
            else "stale"
            if now - stamp > 30000
            else "online"
        )
        storage = (
            conn.execute(select(s.system_state).where(s.system_state.c.id == "storage")).mappings().first()
        )
        storage_data = (
            dict(storage["value_json"])
            if storage
            else {
                "free_bytes": w.disk_free(),
                "media_bytes": None,
                "reference_bytes": None,
                "hls_bytes": None,
            }
        )
        storage_data["db_bytes"] = w.db.path.stat().st_size
        wal = w.db.path.with_name(w.db.path.name + "-wal")
        storage_data["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
        storage_data["earliest_available_at"] = iso(storage_data.pop("earliest_available_at_ms", None))
        counts = [
            dict(kind=row.kind, job_state=row.state, count=row.count)
            for row in conn.execute(
                select(s.jobs.c.kind, s.jobs.c.state, func.count().label("count")).group_by(
                    s.jobs.c.kind, s.jobs.c.state
                )
            )
        ]
        oldest = conn.execute(
            select(func.min(s.jobs.c.created_at_ms)).where(
                s.jobs.c.kind.in_(IMAGE_KINDS), s.jobs.c.state == "queued"
            )
        ).scalar()
        deadline = conn.execute(
            select(func.min(s.jobs.c.deadline_at_ms)).where(s.jobs.c.state == "queued")
        ).scalar()
        inflight = conn.execute(
            select(func.count()).select_from(s.job_attempts).where(s.job_attempts.c.state == "running")
        ).scalar_one()
        success = conn.execute(
            select(func.max(s.job_attempts.c.finished_at_ms)).where(s.job_attempts.c.state == "succeeded")
        ).scalar()
        cameras = []
        for camera in conn.execute(select(s.cameras).where(s.cameras.c.deleted_at_ms.is_(None))).mappings():
            item = w.camera(conn, camera["id"])
            runtime = (
                conn.execute(select(s.camera_runtime).where(s.camera_runtime.c.camera_id == camera["id"]))
                .mappings()
                .one()
            )
            captured = w.ledger.captured(conn, camera["id"], now - 300000, now)
            planned = w.ledger.planned(conn, camera["id"], now - 300000, now)
            denominator = sum(b - a for a, b in planned)
            cameras.append(
                {
                    "camera_id": camera["id"],
                    "name": camera["name"],
                    **item["runtime"],
                    "valid_slot_ratio_5m": sum(b - a for a, b in captured) / denominator
                    if denominator
                    else None,
                    "diagnostics": runtime["diagnostics_json"],
                }
            )
        providers = [
            {"id": p["id"], "name": p["name"], "paused": p["paused"], "last_error_code": p["last_test_error"]}
            for p in conn.execute(select(s.providers).where(s.providers.c.deleted_at_ms.is_(None))).mappings()
        ]
    warnings = []
    if state != "online":
        warnings.append(
            {
                "code": "WORKER_UNAVAILABLE",
                "message": "worker 心跳已过期或尚未启动",
                "affected_resource_ids": [],
            }
        )
    if oldest and now - oldest > 1200000:
        warnings.append(
            {
                "code": "ANALYSIS_BACKLOG",
                "message": "现场分析队列等待超过 20 分钟",
                "affected_resource_ids": [],
            }
        )
    if storage_data.get("cleanup_errors"):
        warnings.append(
            {
                "code": "CLEANUP_FAILED",
                "message": "部分资产物理清理失败，将在下一轮重试",
                "affected_resource_ids": [],
            }
        )
    if storage_data["db_bytes"] + storage_data["wal_bytes"] >= 800_000_000:
        warnings.append(
            {
                "code": "DATABASE_CAPACITY_WARNING",
                "message": "数据库及 WAL 接近 1 GB 规划，请安排扩容或导出",
                "affected_resource_ids": [],
            }
        )
    return dict(
        server_time=iso(now),
        api_version="1.0",
        db_schema_version="0004",
        worker_heartbeat_at=iso(stamp),
        worker_state=state,
        cameras=cameras,
        queue={
            "counts": counts,
            "oldest_image_job_age_ms": now - oldest if oldest else None,
            "nearest_deadline_at": iso(deadline),
            "inflight_requests": inflight,
            "last_success_at": iso(success),
        },
        storage=storage_data,
        providers=providers,
        warnings=warnings,
    )


def format_cost(val: Decimal) -> str:
    text = f"{val:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
        if text == "":
            text = "0"
    return text


def aggregate_usage(rows):
    result = dict(
        requests=len(rows),
        succeeded=sum(r["state"] == "succeeded" for r in rows),
        failed=sum(r["state"] != "succeeded" for r in rows),
        unknown_usage_requests=sum(r["billing_status"] == "unknown" for r in rows),
        capture_images=sum(r["image_count"] for r in rows),
        reference_images=sum(r["reference_image_count"] for r in rows),
        cost=None,
        currency=None,
        cost_estimated=False,
    )
    for name in ("input_tokens", "output_tokens", "cached_tokens"):
        known = [r[name] for r in rows if r[name] is not None]
        result[name] = {
            "value": sum(known) if known else None,
            "known_count": len(known),
            "unknown_count": len(rows) - len(known),
        }
    costs = [r for r in rows if r["reported_cost"] is not None]
    currencies = {r["currency"] for r in costs}
    if costs and len(costs) == len(rows) and len(currencies) == 1:
        result["cost"] = str(sum(Decimal(r["reported_cost"]) for r in costs))
        result["currency"] = currencies.pop()

    converted = [r for r in rows if r.get("cost_status") == "converted"]
    unconverted = [r for r in rows if r.get("cost_status") == "unconverted"]
    unpriced = [r for r in rows if r.get("cost_status") in ("unpriced", None)]

    cny_amounts = [
        Decimal(r["estimated_cost_cny"]) for r in converted if r.get("estimated_cost_cny") is not None
    ]
    estimated_cny = format_cost(sum(cny_amounts)) if cny_amounts else None

    by_currency: dict[str, list[Decimal]] = {}
    for r in rows:
        curr = (r.get("price_snapshot_json") or {}).get("currency")
        cost = r.get("estimated_cost")
        if curr and cost is not None:
            by_currency.setdefault(curr, []).append(Decimal(cost))
    by_currency_str = {curr: format_cost(sum(amounts)) for curr, amounts in sorted(by_currency.items())}

    reasons = sorted({r["cost_reason"] for r in rows if r.get("cost_reason")})
    price_revisions = sorted({r["pricing_revision"] for r in rows if r.get("pricing_revision") is not None})
    exchange_dates = sorted(
        {
            (r.get("exchange_rate_snapshot_json") or {}).get("rate_date")
            for r in rows
            if (r.get("exchange_rate_snapshot_json") or {}).get("rate_date")
        }
    )

    if len(rows) > 0 and len(converted) == len(rows):
        completeness = "full"
    elif len(converted) > 0:
        completeness = "partial"
    else:
        completeness = "none"

    result["cost_summary"] = {
        "estimated_cny_subtotal": estimated_cny,
        "by_currency": by_currency_str,
        "converted_requests": len(converted),
        "unpriced_requests": len(unpriced),
        "unconverted_requests": len(unconverted),
        "reasons": reasons,
        "applicable_price_revisions": price_revisions,
        "exchange_rate_dates": exchange_dates,
        "completeness": completeness,
    }
    return result


def usage_data(w, start, end, group_by, provider_id):
    with w.db.read() as conn:
        query = select(s.ai_usage).where(
            s.ai_usage.c.created_at_ms >= start, s.ai_usage.c.created_at_ms < end
        )
        if provider_id:
            query = query.where(s.ai_usage.c.provider_id == provider_id)
        rows = [dict(r) for r in conn.execute(query).mappings()]
    groups = defaultdict(list)
    for row in rows:
        if group_by in {"hour", "day"}:
            dt = datetime.fromtimestamp(row["created_at_ms"] / 1000, BEIJING)
            key = dt.strftime("%Y-%m-%dT%H:00:00+08:00") if group_by == "hour" else dt.date().isoformat()
        else:
            key = row["provider_id"] if group_by == "provider" else row["purpose"]
        groups[key].append(row)
    return dict(
        **{"from": iso(start), "to": iso(end)},
        group_by=group_by,
        buckets=[{"key": key, **aggregate_usage(items)} for key, items in sorted(groups.items())],
        totals=aggregate_usage(rows),
    )
