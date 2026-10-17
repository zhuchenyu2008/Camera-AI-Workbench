from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy import select, update

from app.capture.frames import FrameWriter
from app.errors import AppError
from app.repositories import schema as s
from app.repositories.db import new_id
from app.schemas import Crop
from app.services.assets import decode_image
from app.services.records import Records
from app.worker.cleanup import Cleaner
from tests.conftest import make_camera


def make_frame(w, jpeg, clock, *, is_test=False):
    camera = make_camera(w)
    sid = new_id("stream")
    with w.db.write() as conn:
        conn.execute(
            s.stream_sessions.insert().values(
                id=sid, camera_id=camera, started_at_ms=w.now(), source_revision=1
            )
        )
        conn.execute(
            update(s.camera_runtime)
            .where(s.camera_runtime.c.camera_id == camera)
            .values(stream_session_id=sid)
        )
    frame = FrameWriter(w).save(camera, sid, jpeg, w.now(), "1", 1, is_test=is_test)
    return camera, sid, frame


def test_B08_ttl_read_reject_cleanup_and_no_resurrection(w, jpeg, clock):
    camera, sid, frame = make_frame(w, jpeg, clock)
    assert w.assets.read_evidence(frame["id"])[0]
    clock.advance(61 * 60000)
    with pytest.raises(AppError, match="EVIDENCE_EXPIRED"):
        w.assets.read_evidence(frame["id"])
    path = w.config.data_dir / frame["relpath"]
    assert path.exists()
    with w.db.write() as conn:
        w.assets.freeze_expired(conn)
        row = conn.execute(select(s.settings)).mappings().one()
        conn.execute(
            update(s.settings).values(config_json=row["config_json"] | {"frame_retention_minutes": 120})
        )
        w.assets.sync_expiry(conn)
    with pytest.raises(AppError, match="EVIDENCE_EXPIRED"):
        w.assets.read_evidence(frame["id"])
    Cleaner(w).run()
    assert not path.exists()
    with w.db.read() as conn:
        assert conn.execute(select(s.minute_capture.c.captured_bitmap)).scalar_one() == 1


def test_B02_real_pts_no_duplicate_or_pixel_based_drop(w, jpeg, clock):
    camera, sid, frame = make_frame(w, jpeg, clock)
    writer = FrameWriter(w)
    assert writer.save(camera, sid, jpeg, w.now(), "2", 2) is None
    clock.advance(1000)
    assert writer.save(camera, sid, jpeg, w.now(), "2", 3) is None
    assert writer.save(camera, sid, jpeg, w.now(), "3", 4) is not None
    with w.db.read() as conn:
        rows = conn.execute(select(s.frames).where(s.frames.c.camera_id == camera)).mappings().all()
        assert len(rows) == 2
        assert rows[0]["sha256"] == rows[1]["sha256"]


def test_B08_eviction_remains_distinguishable_after_index_purge(w, jpeg, clock):
    camera, sid, frame = make_frame(w, jpeg, clock)
    with w.db.write() as conn:
        row = conn.execute(select(s.settings)).mappings().one()
        conn.execute(
            update(s.settings).values(config_json=row["config_json"] | {"media_quota_gb": 0.00000001})
        )
    Cleaner(w).run()
    with w.db.read() as conn:
        assert w.assets.metadata(conn, frame["id"])["state"] == "evicted"
        minute = conn.execute(select(s.minute_records.c.id)).scalar_one()
    clock.advance(26 * 3600000)
    Cleaner(w).run()
    with w.db.read() as conn:
        slot = Records(w).evidence_slots(conn, minute)[0]
        assert slot["slot_state"] == "evicted" and slot["evidence_id"] is None


def test_B09_upload_versions_capacity_and_revocation(client, w, jpeg):
    created = client.post(
        "/api/entities", json={"kind": "person", "name": "我"}, headers={"Idempotency-Key": "entity"}
    )
    assert created.status_code == 201, created.text
    entity = created.json()["data"]
    url = f"/api/entities/{entity['id']}/references"
    headers = {"Idempotency-Key": "upload", "If-Match": '"rev-1"'}
    uploaded = client.post(url, files={"file": ("reference.jpg", jpeg, "image/jpeg")}, headers=headers)
    assert uploaded.status_code == 201, uploaded.text
    reference = uploaded.json()["data"]
    assert reference["entity_revision"] == 2
    assert (
        client.post(url, files={"file": ("reference.jpg", jpeg, "image/jpeg")}, headers=headers).json()[
            "data"
        ]
        == reference
    )
    enable = client.patch(
        f"/api/entities/{entity['id']}",
        json={"enabled": True, "is_primary_subject": True},
        headers={"If-Match": '"rev-2"', "Idempotency-Key": "enable"},
    )
    assert enable.status_code == 200, enable.text
    assert client.get("/api/settings").json()["data"]["config"]["primary_subject_id"] == entity["id"]
    asset = reference["asset_id"]
    rename = client.patch(
        f"/api/reference-assets/{asset}",
        json={"name": "本人正面"},
        headers={"If-Match": '"rev-1"', "Idempotency-Key": "rename"},
    )
    assert rename.status_code == 200 and rename.json()["data"]["name_snapshot"] == "本人正面"
    with w.db.write() as conn:
        refs = w.assets.reference_set(conn, None)
        assert refs["members_json"][0]["name"] == "本人正面"
    replaced = client.put(
        f"/api/reference-assets/{asset}/content",
        files={"file": ("new.jpg", jpeg, "image/jpeg")},
        headers={"If-Match": '"rev-2"', "Idempotency-Key": "replace"},
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["data"]["asset_version"] == 2
    assert client.get(f"/api/reference-assets/{asset}/content?version=1").status_code == 200
    deleted = client.delete(
        f"/api/reference-assets/{asset}", headers={"If-Match": '"rev-3"', "Idempotency-Key": "delete"}
    )
    assert deleted.status_code == 202, deleted.text
    assert client.get(f"/api/reference-assets/{asset}/content?version=1").status_code == 410
    assert client.get("/api/settings").json()["data"]["config"]["primary_subject_id"] is None
    Cleaner(w).run()
    with w.db.read() as conn:
        assert all(row.deleted_at_ms for row in conn.execute(select(s.reference_asset_versions)))


def test_B09_image_spoof_animation_and_crop(jpeg):
    with pytest.raises(AppError):
        decode_image(jpeg, "image/png")
    with pytest.raises(AppError):
        decode_image(b"<svg onload='steal()'/>", "image/svg+xml")
    with pytest.raises(AppError):
        decode_image(b"x" * (8 * 1024 * 1024 + 1))
    data, width, height = decode_image(jpeg, "image/jpeg", Crop(x=0, y=0, width=0.5, height=1))
    assert (width, height) == (80, 90)
    assert Image.open(io.BytesIO(data)).getexif() == {}
