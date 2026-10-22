from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.orm import sessionmaker

from app.ai_gateway import ProviderError, validate_provider_url
from app.api import settings as settings_routes
from app.capture import FFmpegCameraSource
from app.config import Settings
from app.db import Base, create_engine_for_settings
from app.main import APIError, AppConfig, create_app
from app.models import AIJob, AppSetting, Camera, Frame, RetentionPolicy
from app.retention import SqlAlchemyRetentionRepository


def _config(tmp_path: Path, **changes):
    values = {
        "data_dir": tmp_path,
        "database_url": f"sqlite:///{(tmp_path / 'api.sqlite3').as_posix()}",
        "allowed_origins": ("http://localhost:5173",),
        "auth_disabled": True,
        "master_key_file": tmp_path / "master.key",
    }
    values.update(changes)
    return AppConfig(**values)


def _request_config(environment: str, *, local_endpoints=(), allowed_hosts=()):
    config = SimpleNamespace(
        environment=environment,
        provider_allowed_hosts=tuple(allowed_hosts),
        provider_local_endpoints=tuple(local_endpoints),
    )
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config)))


def test_provider_local_allowlist_is_exact_and_never_a_development_mode_bypass():
    request = _request_config("production", local_endpoints=("127.0.0.1:11434",))

    with pytest.raises(APIError):
        settings_routes.policy(request, "http://127.0.0.1:11435/v1", allow_local=True)
    with pytest.raises(APIError):
        settings_routes.policy(request, "http://127.0.0.1:11434/v1", allow_local=False)

    settings_routes.policy(request, "http://127.0.0.1:11434/v1", allow_local=True)

    # A bare hostname is intentionally insufficient: otherwise one approval
    # would authorize every service port on that host.
    with pytest.raises(ProviderError):
        validate_provider_url("http://127.0.0.1:11434/v1", local_endpoints=("127.0.0.1",))


def test_provider_local_opt_in_does_not_bypass_public_host_allowlist():
    request = _request_config("production", local_endpoints=("127.0.0.1:11434",))
    with pytest.raises(APIError):
        settings_routes.policy(request, "https://unapproved.example/v1", allow_local=True)


def test_camera_create_idempotency_is_atomic_and_retention_minutes_are_exact(tmp_path):
    app = create_app(_config(tmp_path))
    payload = {
        "name": "Front",
        "rtsp_url": "rtsp://camera.local/live",
        "retention_minutes": 1,
    }
    with TestClient(app) as client:
        first = client.post("/api/v1/cameras", headers={"Idempotency-Key": "camera-create-1"}, json=payload)
        replay = client.post("/api/v1/cameras", headers={"Idempotency-Key": "camera-create-1"}, json=payload)
        conflict = client.post(
            "/api/v1/cameras",
            headers={"Idempotency-Key": "camera-create-1"},
            json={**payload, "name": "Other"},
        )
        assert first.status_code == replay.status_code == 201
        assert first.json()["data"]["id"] == replay.json()["data"]["id"]
        assert conflict.status_code == 409

    with app.state.SessionLocal() as db:
        cameras = list(db.scalars(select(Camera)))
        assert len(cameras) == 1
        assert cameras[0].retention_policy.ordinary_frame_days == pytest.approx(1 / 1440)
        assert db.scalar(select(AppSetting).where(AppSetting.key == "retention.last_run_ms")) is None


def test_camera_connection_test_uses_ffmpeg_adapter_and_always_stops(tmp_path, monkeypatch):
    import app.api.cameras as camera_routes

    instances = []

    class FakeSource:
        def __init__(self, config):
            self.config = config
            self.stopped = False
            instances.append(self)

        def start(self):
            return None

        def read_frame(self, timeout_s=None):
            return SimpleNamespace(frame=np.zeros((7, 11, 3), dtype=np.uint8))

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(camera_routes, "FFmpegCameraSource", FakeSource)
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/cameras",
            json={"name": "Probe", "rtsp_url": "rtsp://camera.local/live"},
        )
        camera_id = created.json()["data"]["id"]
        response = client.post(f"/api/v1/cameras/{camera_id}/test", json={"timeout_ms": 1000})
        assert response.status_code == 200
        assert response.json()["data"] == {
            "ok": True,
            "status": "online",
            "width": 11,
            "height": 7,
        }
    assert len(instances) == 1
    assert instances[0].stopped
    assert isinstance(instances[0].config, camera_routes.CameraConfig)


def test_task_reference_without_deadline_uses_finite_created_time_grace(tmp_path):
    settings = Settings(_env_file=None, APP_DB_URL=f"sqlite:///{(tmp_path / 'retention.sqlite3').as_posix()}")
    engine = create_engine_for_settings(settings)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    created = 1_000
    with factory() as db:
        db.add(
            AIJob(
                idempotency_key="pin-without-deadline",
                layer="L1",
                state="queued",
                created_at_ms=created,
                available_at_ms=created,
                input_frame_ids_json=["frame-1"],
            )
        )
        db.commit()
        until = SqlAlchemyRetentionRepository()._task_pins(db, "frame-1")
    assert until == created + 6 * 60 * 60 * 1000
    engine.dispose()


def test_retention_once_applies_camera_override_and_preserves_global_default(tmp_path, monkeypatch):
    import app.worker_main as worker

    data_dir = tmp_path / "data"
    (data_dir / "frames" / "override").mkdir(parents=True)
    (data_dir / "frames" / "default").mkdir(parents=True)
    now = 20_000_000
    old = now - 5 * 60_000
    override_file = data_dir / "frames" / "override" / "expired.jpg"
    default_file = data_dir / "frames" / "default" / "kept.jpg"
    override_file.write_bytes(b"override")
    default_file.write_bytes(b"default")

    db_settings = Settings(_env_file=None, APP_DB_URL=f"sqlite:///{(tmp_path / 'cleanup.sqlite3').as_posix()}")
    engine = create_engine_for_settings(db_settings)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    with factory() as db:
        override = RetentionPolicy(
            id="policy-override",
            name="one minute",
            ordinary_frame_days=1 / 1440,
            event_frame_days=6 / 1440,
        )
        cam_override = Camera(id="camera-override", name="Override", retention_policy=override)
        cam_default = Camera(id="camera-default", name="Default")
        db.add_all([cam_override, cam_default])
        db.flush()
        db.add_all([
            Frame(
                id="frame-override", camera_id=cam_override.id, captured_at_ms=old,
                sequence=1, relative_path="frames/override/expired.jpg", byte_size=8,
                sha256="a" * 64, state="active",
            ),
            Frame(
                id="frame-default", camera_id=cam_default.id, captured_at_ms=old,
                sequence=1, relative_path="frames/default/kept.jpg", byte_size=7,
                sha256="b" * 64, state="active",
            ),
        ])
        db.add(AppSetting(
            key="retention",
            value_json={"ordinary_frame_minutes": 60, "event_frame_minutes": 360},
            description="test policy",
        ))
        db.commit()

    monkeypatch.setattr(worker, "_settings", lambda: SimpleNamespace(data_dir=data_dir))
    monkeypatch.setattr(worker, "get_session_factory", lambda: factory)
    result = worker.retention_once(now_ms=now)

    assert result["deleted"] == 1
    assert not override_file.exists()
    assert default_file.exists()
    with factory() as db:
        assert db.get(Frame, "frame-override").state == "expired"
        assert db.get(Frame, "frame-default").state == "active"
    engine.dispose()


def test_alembic_upgrades_legacy_missing_columns_and_fresh_database(tmp_path, monkeypatch):
    config_path = Path(__file__).resolve().parents[1] / "alembic.ini"

    legacy_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy_path) as connection:
        connection.executescript(
            "CREATE TABLE ai_job (id VARCHAR(36) PRIMARY KEY);"
            "CREATE TABLE frame (id VARCHAR(36) PRIMARY KEY);"
            "CREATE TABLE ai_provider (id VARCHAR(36) PRIMARY KEY);"
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);"
            "INSERT INTO alembic_version(version_num) VALUES ('0001_initial');"
        )
    monkeypatch.setenv("APP_DB_URL", f"sqlite:///{legacy_path.as_posix()}")
    command.upgrade(Config(str(config_path)), "head")
    with sqlite3.connect(legacy_path) as connection:
        assert {row[1] for row in connection.execute("PRAGMA table_info(ai_job)")} >= {
            "lease_token", "deadline_at_ms"
        }
        assert {row[1] for row in connection.execute("PRAGMA table_info(frame)")} >= {
            "thumbnail_relative_path", "local_signal_json", "pin_until_ms"
        }
        assert "allow_insecure_local" in {
            row[1] for row in connection.execute("PRAGMA table_info(ai_provider)")
        }

    fresh_path = tmp_path / "fresh.sqlite3"
    monkeypatch.setenv("APP_DB_URL", f"sqlite:///{fresh_path.as_posix()}")
    command.upgrade(Config(str(config_path)), "head")
    with sqlite3.connect(fresh_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"camera", "frame", "ai_job", "ai_provider", "summary"}.issubset(tables)