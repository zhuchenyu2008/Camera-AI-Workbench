"""Small shared runtime state helpers for service heartbeats and SSE events."""
from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import select

from .db import get_session_factory
from .events import InMemoryOutbox, SQLiteOutbox
from .models import AppSetting, utc_now_ms

HEARTBEAT_PREFIX = "service.heartbeat."
_outbox_lock = threading.RLock()
_outboxes: dict[str, SQLiteOutbox | InMemoryOutbox] = {}


def _heartbeat_key(service: str) -> str:
    value = str(service).strip().lower()
    if not value or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for ch in value):
        raise ValueError("service name is invalid")
    return f"{HEARTBEAT_PREFIX}{value}"


def write_heartbeat(service: str, *, status: str = "running", metrics: dict[str, Any] | None = None,
                    now_ms: int | None = None) -> dict[str, Any]:
    """Persist one bounded, non-sensitive heartbeat snapshot."""
    stamp = utc_now_ms() if now_ms is None else int(now_ms)
    payload = {"service": str(service), "status": str(status), "updated_at_ms": stamp}
    if metrics:
        payload["metrics"] = {str(key): value for key, value in metrics.items()
                               if isinstance(value, (str, int, float, bool)) or value is None}
    factory = get_session_factory()
    db = factory()
    try:
        row = db.scalar(select(AppSetting).where(AppSetting.key == _heartbeat_key(service)))
        if row is None:
            row = AppSetting(key=_heartbeat_key(service), value_json=payload,
                             description=f"{service} service heartbeat")
            db.add(row)
        else:
            row.value_json = payload
            row.updated_at_ms = stamp
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return payload


def read_heartbeats(db, *, now_ms: int | None = None, stale_after_ms: int = 30_000) -> dict[str, dict[str, Any]]:
    stamp = utc_now_ms() if now_ms is None else int(now_ms)
    rows = db.scalars(select(AppSetting).where(AppSetting.key.like(f"{HEARTBEAT_PREFIX}%")))
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        service = row.key[len(HEARTBEAT_PREFIX):]
        value = row.value_json if isinstance(row.value_json, dict) else {}
        updated = int(value.get("updated_at_ms") or row.updated_at_ms or 0)
        age = max(0, stamp - updated) if updated else None
        fresh = age is not None and age <= stale_after_ms
        state = str(value.get("status") or "unknown") if fresh else "offline"
        result[service] = {**value, "service": service, "updated_at_ms": updated,
                           "age_ms": age, "fresh": fresh, "status": state}
    return result


def publish_runtime_event(database_url: str, event: str, data: dict[str, Any], *,
                          camera_id: str | None = None, job_id: str | None = None,
                          layer: str | None = None, request_id: str | None = None) -> None:
    """Publish from a background process into the same SSE log API uses."""
    with _outbox_lock:
        if database_url not in _outboxes:
            _outboxes[database_url] = SQLiteOutbox(database_url) if ":memory:" not in database_url else InMemoryOutbox()
        outbox = _outboxes[database_url]
    outbox.publish(event, data, camera_id=camera_id, job_id=job_id, layer=layer, request_id=request_id)
