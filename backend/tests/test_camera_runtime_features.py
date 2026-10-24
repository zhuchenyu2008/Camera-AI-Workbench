from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import AppConfig, create_app
from app.models import AuditLog, Camera, Frame


def _app(tmp_path: Path):
    return create_app(AppConfig(
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'app.sqlite3').as_posix()}",
        allowed_origins=("http://localhost:5173",),
        auth_disabled=True,
        master_key_file=tmp_path / "master.key",
    ))


def test_latest_frame_preview_and_camera_config_version(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = {"Origin": "http://localhost:5173"}
        created = client.post(
            "/api/v1/cameras",
            headers=headers,
            json={"name": "Front", "rtsp_url": "rtsp://127.0.0.1:554/front", "enabled": True},
        ).json()["data"]
        frame_dir = tmp_path / "frames" / created["id"]
        thumb_dir = tmp_path / "thumbs" / created["id"]
        frame_dir.mkdir(parents=True)
        thumb_dir.mkdir(parents=True)
        (frame_dir / "frame.jpg").write_bytes(b"jpeg")
        (thumb_dir / "frame.jpg").write_bytes(b"thumb")
        with app.state.SessionLocal() as db:
            camera = db.get(Camera, created["id"])
            camera.status = "online"
            camera.last_frame_at_ms = 1_900_000_000_000
            db.add(Frame(
                id="frame-preview", camera_id=created["id"], captured_at_ms=1_900_000_000_000,
                sequence=1, relative_path=f"frames/{created['id']}/frame.jpg",
                thumbnail_relative_path=f"thumbs/{created['id']}/frame.jpg", width=2, height=2,
                byte_size=5, sha256="a" * 64, state="active",
            ))
            db.commit()
        latest = client.get(f"/api/v1/cameras/{created['id']}/latest-frame")
        assert latest.status_code == 200
        assert latest.json()["data"]["frame"]["id"] == "frame-preview"
        assert latest.json()["data"]["state"] in {"fresh", "stale"}

        updated = client.patch(
            f"/api/v1/cameras/{created['id']}", headers=headers,
            json={"expected_version": 1, "name": "Front updated"},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["version"] == 2
        conflict = client.patch(
            f"/api/v1/cameras/{created['id']}", headers=headers,
            json={"expected_version": 1, "name": "stale draft"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"


def test_camera_disable_preserves_source_and_writes_audit(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = {"Origin": "http://localhost:5173"}
        created = client.post(
            "/api/v1/cameras", headers=headers,
            json={"name": "Front", "rtsp_url": "rtsp://127.0.0.1:554/front"},
        ).json()["data"]
        response = client.delete(f"/api/v1/cameras/{created['id']}?delete_history=false", headers=headers)
        assert response.status_code == 200
        with app.state.SessionLocal() as db:
            camera = db.get(Camera, created["id"])
            assert camera is not None and camera.enabled is False and camera.rtsp_url_encrypted
            actions = {row.action for row in db.scalars(select(AuditLog))}
            assert "camera.disable" in actions
