from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.errors import AppError
from app.repositories import schema_v1 as s
from app.schemas import CameraDto, SettingsConfig
from app.timeutils import day_range, schedule_intervals, timestamp
from app.worker.queue import enqueue
from tests.conftest import camera_payload


def test_B01_beijing_week_wrap_and_union():
    a = timestamp("2026-10-18T23:00:00+08:00")
    b = timestamp("2026-10-19T02:00:00+08:00")
    rules = [{"weekdays": [7], "start": "22:00", "end": "01:00", "end_day_offset": 1}, {"weekdays": [1], "start": "00:30", "end": "01:30", "end_day_offset": 0}]
    assert schedule_intervals(rules, a, b) == [(a, a+150*60000)]
    assert day_range("2026-10-17") == (timestamp("2026-10-16T16:00:00Z"), timestamp("2026-10-17T16:00:00Z"))
    assert SettingsConfig().minimum_retention_seconds() == 1350
    with pytest.raises(ValueError):
        SettingsConfig(frame_retention_minutes=22)


def test_B09_auth_csrf_versions_and_idempotency(client, w):
    payload = camera_payload()
    missing = client.post("/api/cameras", json=payload)
    assert missing.status_code == 422
    headers = {"Idempotency-Key": "create-one"}
    response = client.post("/api/cameras", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    camera = response.json()["data"]
    CameraDto.model_validate(camera)
    assert "secret-camera-password" not in response.text
    assert client.post("/api/cameras", json=payload, headers=headers).json()["data"] == camera
    assert client.post("/api/cameras", json=payload | {"name": "不同"}, headers=headers).status_code == 409
    path = f"/api/cameras/{camera['id']}"
    assert client.patch(path, json={"name": "新名称"}, headers={"Idempotency-Key": "patch-missing"}).status_code == 428
    changed = client.patch(path, json={"name": "新名称"}, headers={"Idempotency-Key": "patch-one", "If-Match": '"rev-1"'})
    assert changed.status_code == 200, changed.text
    assert changed.headers["ETag"] == '"rev-2"'
    assert client.patch(path, json={"name": "新名称"}, headers={"Idempotency-Key": "patch-one", "If-Match": '"rev-1"'}).json()["data"]["revision"] == 2
    assert client.patch(path, json={"name": "又改名"}, headers={"Idempotency-Key": "patch-two", "If-Match": '"rev-1"'}).status_code == 412
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/cameras", json=payload, headers={"Idempotency-Key": "no-csrf"}).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert client.get(path).headers["Cache-Control"] == "private, no-store"
    client.delete("/api/session")
    assert client.get("/api/evidence/anything").status_code == 401
    assert client.get("/api/cameras/cam/preview/sid/live.m3u8").status_code == 401


def test_B04_real_sqlite_competing_claims_and_late_result(w, clock):
    with w.db.write() as conn:
        job = enqueue(conn, w.now(), "project_events", {}, "dedup-one")
        assert enqueue(conn, w.now(), "project_events", {}, "dedup-one")["id"] == job["id"]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(w.queue.claim, ["worker-a", "worker-b"]))
    assert sum(result is not None for result in results) == 1
    first = next(r for r in results if r)
    clock.advance(180001)
    w.queue.recover()
    clock.advance(30000)
    second = w.queue.claim("worker-c")
    assert second["id"] == first["id"] and second["lease_token"] != first["lease_token"]
    with pytest.raises(AppError, match="LEASE_LOST"):
        w.queue.finish(first)
    w.queue.finish(second)
    with w.db.read() as conn:
        assert conn.execute(select(s.jobs.c.state).where(s.jobs.c.id == job["id"])).scalar_one() == "succeeded"


def test_B11_routes_match_document(client):
    from pathlib import Path
    import re
    doc = (Path(__file__).resolve().parents[2]/"docs/04-后端详细设计与接口契约.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| (\w+) \| (GET|POST|PATCH|PUT|DELETE) \| (/[^ ]+) \|", doc, re.M)
    schema = client.get("/openapi.json").json()
    actual = {(v["operationId"], method.upper(), path) for path, methods in schema["paths"].items() for method, v in methods.items() if "operationId" in v}
    assert set(rows) == actual
    assert len({r[0] for r in actual}) == len(actual)
