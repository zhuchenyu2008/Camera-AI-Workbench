from __future__ import annotations

import sqlite3

import httpx
from fastapi.routing import APIRoute
from sqlalchemy import func, select, update

from app.analysis.client import AIClient
from app.capture.supervisor import Supervisor
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.services.analysis import Analysis
from app.services.records import Records
from app.summaries.service import Summaries
from app.timeutils import iso
from app.worker.cleanup import Cleaner
from app.worker.executor import Executor
from tests.conftest import make_provider
from tests.test_analysis import drain, fake_visual, fill_minute, make_person
from tests.test_assets import make_frame


def test_B08_cleanup_advances_past_500_deleted_tombstones(w, jpeg, clock):
    camera, sid, original = make_frame(w, jpeg, clock)
    start = w.now()
    with w.db.write() as conn:
        for index in range(1, 502):
            path = f"frames/{camera}/{sid}/extra_{index}.jpg"
            (w.config.data_dir / path).write_bytes(jpeg)
            clone = original | {
                "id": new_id("ev"),
                "slot_at_ms": start + index * 1000,
                "captured_at_ms": start + index * 1000,
                "expires_at_ms": start + index * 1000 + 3600000,
                "relpath": path,
            }
            conn.execute(s.frames.insert().values(**clone))
    clock.advance(2 * 3600000)
    Cleaner(w).run()
    with w.db.read() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(s.frames).where(s.frames.c.file_deleted_at_ms.is_(None))
            ).scalar_one()
            == 0
        )
    Cleaner(w).run()
    with w.db.read() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(s.frames).where(s.frames.c.file_deleted_at_ms.is_(None))
            ).scalar_one()
            == 0
        )


def test_B08_missing_file_and_resource_resume(w, jpeg, clock):
    camera, sid, frame = make_frame(w, jpeg, clock)
    (w.config.data_dir / frame["relpath"]).unlink()
    Cleaner(w).run()
    with w.db.read() as conn:
        meta = w.assets.metadata(conn, frame["id"])
        assert meta["state"] == "missing"
        capture = conn.execute(select(s.minute_capture)).mappings().one()
        assert capture["missing_bitmap"] == 1
    assert Cleaner(w).run(free_bytes=500_000_000)["paused"]
    assert Cleaner(w).run(free_bytes=4_000_000_000)["paused"]
    clock.advance(60000)
    assert not Cleaner(w).run(free_bytes=4_000_000_000)["paused"]


async def test_B04_late_ai_retained_as_history_does_not_replace_manual(w, jpeg, clock):
    make_provider(w)
    person, asset = make_person(w, jpeg)
    camera, start, frames = fill_minute(w, clock, jpeg)
    with w.db.write() as conn:
        refs = w.assets.reference_set(conn, [camera])
        Analysis(w).plan(conn, camera, frames, refs, "dense")
        minute = conn.execute(select(s.minute_records)).mappings().one()
        conn.execute(
            s.minute_revisions.insert().values(
                minute_id=minute["id"],
                revision=1,
                source_kind="manual",
                source_fingerprint="manual",
                reference_set_id=refs["id"],
                result_json={
                    "has_manual_revision": True,
                    "analysis_state": "complete",
                    "analysis_mode": "dense",
                },
                created_at_ms=w.now(),
            )
        )
        conn.execute(
            update(s.minute_records)
            .where(s.minute_records.c.id == minute["id"])
            .values(current_revision=1, analysis_state="complete")
        )
        conn.execute(
            s.activity_spans.insert().values(
                id=new_id("span"),
                camera_id=camera,
                minute_id=minute["id"],
                minute_revision=1,
                subject_id=person,
                identity_status="matched",
                start_at_ms=start,
                end_at_ms=start + 60000,
                activity="writing",
                origin="manual",
                support="dense",
                boundary_json={},
                evidence_ids_json=[],
                details_json={"labels": [], "object_ids": [], "observation": "人工确认书写"},
            )
        )
    client = AIClient(w, httpx.MockTransport(lambda r: fake_visual(r, person)))
    try:
        await drain(w, Executor(w, Supervisor(w), client))
    finally:
        await client.close()
    with w.db.read() as conn:
        assert one(conn, s.minute_records, minute["id"])["current_revision"] == 1
        assert conn.execute(select(func.count()).select_from(s.minute_revisions)).scalar_one() == 2
        current = Records(w).minute(conn, minute["id"])
        assert current["has_manual_revision"] and current["segments"][0]["activity"] == "writing"
        assert current["coverage"]["classified_ms"] == 60000


def test_B04_migration_and_consistent_backup(w, tmp_path):
    w.db.migrate()
    with w.db.read() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar_one() == 2
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0007"
    target = tmp_path / "consistent.sqlite3"
    w.db.backup(target)
    with sqlite3.connect(target) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1


def test_B11_actual_responses_validate_against_declared_dtos(client, w, jpeg, clock):
    make_provider(w)
    person, asset = make_person(w, jpeg)
    camera, start, frames = fill_minute(w, clock, jpeg)
    with w.db.write() as conn:
        minute = conn.execute(select(s.minute_records.c.id)).scalar_one()
        summary = Summaries(w).ensure(
            conn, "hour", start, start + 60000, {"camera_ids": [camera], "subject_id": None}
        )
        refs = w.assets.reference_set(conn, [camera])
        root = Analysis(w).plan(conn, camera, frames, refs, "dense")
    params = {"from": iso(start), "to": iso(start + 60000)}
    urls = [
        ("/api/session", {}),
        ("/api/cameras", {}),
        (f"/api/cameras/{camera}", {}),
        (f"/api/cameras/{camera}/preview/status", {}),
        ("/api/overview", params),
        ("/api/timeline", params),
        ("/api/events", params),
        ("/api/minutes", params),
        (f"/api/minutes/{minute}", {}),
        (f"/api/minutes/{minute}/evidence", {}),
        (f"/api/evidence/{frames[0]['id']}/metadata", {}),
        ("/api/entities", {}),
        (f"/api/entities/{person}", {}),
        ("/api/providers", {}),
        ("/api/settings", {}),
        ("/api/status", {}),
        ("/api/usage", params),
        ("/api/jobs", params),
        (f"/api/jobs/{root['id']}", {}),
        (f"/api/summaries/{summary['id']}", {}),
        ("/api/summaries", {"type": "hour", "local_date": "2026-10-17", "camera_ids": camera}),
    ]
    for path, query in urls:
        response = client.get(path, params=query)
        assert response.status_code == 200, (path, response.text)
        route = next(
            r
            for r in client.app.routes
            if isinstance(r, APIRoute) and "GET" in r.methods and r.path_regex.fullmatch(path)
        )
        route.response_model.model_validate(response.json())


def test_B09_settings_preview_validation_and_replay(client, w):
    bad = client.post(
        "/api/settings/preview", json={"patch": {"frame_retention_minutes": 22}, "base_revision": 1}
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "RETENTION_TOO_SHORT"
    bad = client.post("/api/settings/preview", json={"patch": {"ai_concurrency": 4}, "base_revision": 1})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "CAPACITY_UNVERIFIED"
    patch = {"frame_retention_minutes": 120}
    response = client.post("/api/settings/preview", json={"patch": patch, "base_revision": 1})
    assert response.status_code == 200, response.text
    body = {"patch": patch, "preview_token": response.json()["data"]["preview_token"]}
    headers = {"If-Match": '"rev-1"', "Idempotency-Key": "save-settings"}
    changed = client.patch("/api/settings", json=body, headers=headers)
    assert changed.status_code == 200, changed.text
    assert changed.json()["data"]["settings"]["revision"] == 2
    assert client.patch("/api/settings", json=body, headers=headers).json()["data"] == changed.json()["data"]
