from __future__ import annotations

import io

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select, update

from app.config import Config
from app.main import create_app
from app.repositories import schema as s
from app.repositories.db import new_id
from app.schemas import CameraCreate, ProviderWrite
from app.services.workbench import Workbench
from app.timeutils import timestamp


class Clock:
    def __init__(self):
        self.value = timestamp("2026-10-17T12:00:00+08:00")

    def __call__(self):
        return self.value

    def advance(self, milliseconds):
        self.value += milliseconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def w(tmp_path, clock):
    key = tmp_path / "master.key"
    key.write_bytes(Fernet.generate_key())
    data = tmp_path / "data"
    config = Config(
        data,
        f"sqlite:///{(data / 'db/workbench.sqlite3').as_posix()}",
        key,
        provider_local_endpoints=("localhost:8888",),
        clock=clock,
    )
    instance = Workbench(config, migrate=True)
    instance.auth.create_admin("admin", "correct-horse-battery")
    yield instance
    instance.db.close()


@pytest.fixture
def client(w):
    with TestClient(create_app(w.config, w), base_url=w.config.allowed_origin) as session:
        login = session.post(
            "/api/session",
            json={"username": "admin", "password": "correct-horse-battery"},
            headers={"Origin": w.config.allowed_origin},
        )
        assert login.status_code == 200, login.text
        session.headers["X-CSRF-Token"] = login.json()["data"]["csrf_token"]
        yield session


@pytest.fixture
def jpeg():
    buffer = io.BytesIO()
    Image.new("RGB", (160, 90), (120, 130, 140)).save(buffer, "JPEG")
    return buffer.getvalue()


def camera_payload(name="书桌"):
    return dict(
        name=name,
        source={
            "host": "127.0.0.1",
            "port": 15554,
            "path": "/live",
            "username": "reader",
            "password": "secret-camera-password",
        },
        schedule=[{"weekdays": [1, 2, 3, 4, 5, 6, 7], "start": "00:00", "end": "00:00", "end_day_offset": 1}],
    )


def make_camera(w, *, enabled=True):
    with w.db.write() as conn:
        camera = w.create_camera(conn, CameraCreate.model_validate(camera_payload()))
        if enabled:
            conn.execute(update(s.cameras).where(s.cameras.c.id == camera["id"]).values(enabled=True))
            row = dict(conn.execute(select(s.cameras).where(s.cameras.c.id == camera["id"])).mappings().one())
            w.save_schedule(conn, row)
        return camera["id"]


def make_provider(w):
    dto = ProviderWrite(
        name="测试服务",
        base_url="http://localhost:8888/v1",
        model="test-vision",
        api_key="secret-provider-key",
        response_format_mode="json_object",
    )
    with w.db.write() as conn:
        provider = w.write_provider(conn, dto)
        cap = {
            "vision": True,
            "data_url": True,
            "structured_output_modes": ["json_object"],
            "verified_max_images": 32,
        }
        conn.execute(
            update(s.providers)
            .where(s.providers.c.id == provider["id"])
            .values(tested_config_revision=1, capabilities_json=cap)
        )
        settings = dict(conn.execute(select(s.settings)).mappings().one())
        cfg = settings["config_json"] | {
            "vision_provider_id": provider["id"],
            "summary_provider_id": provider["id"],
        }
        conn.execute(update(s.settings).values(config_json=cfg))
    return provider["id"]


def make_minute(
    w,
    camera_id,
    start,
    *,
    captured=(1 << 60) - 1,
    state="pending",
    activity=None,
    subject=None,
    identity="matched",
    support="dense",
):
    with w.db.write() as conn:
        minute_id = new_id("min")
        conn.execute(
            s.minute_records.insert().values(
                id=minute_id,
                camera_id=camera_id,
                start_at_ms=start,
                current_revision=1 if activity else None,
                analysis_state="complete" if activity else state,
                created_at_ms=w.now(),
            )
        )
        conn.execute(
            s.minute_capture.insert().values(
                minute_id=minute_id,
                planned_bitmap=(1 << 60) - 1,
                captured_bitmap=captured,
                evicted_bitmap=0,
                missing_bitmap=0,
                gap_reasons_json={},
                capture_config_revision=1,
            )
        )
        if activity:
            conn.execute(
                s.minute_revisions.insert().values(
                    minute_id=minute_id,
                    revision=1,
                    source_kind="ai",
                    source_fingerprint=new_id("fp"),
                    reference_set_id=None,
                    result_json={"analysis_state": "complete", "analysis_mode": support},
                    created_at_ms=w.now(),
                )
            )
            conn.execute(
                s.activity_spans.insert().values(
                    id=new_id("span"),
                    camera_id=camera_id,
                    minute_id=minute_id,
                    minute_revision=1,
                    subject_id=subject,
                    identity_status=identity,
                    start_at_ms=start,
                    end_at_ms=start + 60000,
                    activity=activity,
                    origin="ai",
                    support=support,
                    boundary_json={},
                    evidence_ids_json=[],
                    details_json={"observation": "观察", "labels": [], "object_ids": []},
                )
            )
    return minute_id
