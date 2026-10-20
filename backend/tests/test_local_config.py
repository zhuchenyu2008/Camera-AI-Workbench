from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.capture.supervisor import Supervisor
from app.config import Config
from app.errors import AppError
from app.main import create_app
from app.repositories import schema as s
from app.repositories.db import one
from app.schemas import Source, SourcePatch
from tests.conftest import camera_payload, make_camera


def test_local_http_requires_opt_in_and_preserves_session_csrf(monkeypatch, w):
    monkeypatch.setenv("CAW_ALLOWED_ORIGIN", "http://127.0.0.1:5173")
    monkeypatch.delenv("CAW_LOCAL_DEV", raising=False)
    with pytest.raises(ValueError):
        Config.from_env()
    monkeypatch.setenv("CAW_LOCAL_DEV", "1")
    local = Config.from_env()
    assert not local.secure_cookies
    config = dataclasses.replace(w.config, allowed_origin=local.allowed_origin, secure_cookies=False)
    w.config = config
    with TestClient(create_app(config, w), base_url=config.allowed_origin) as client:
        response = client.post(
            "/api/session",
            json={"username": "admin", "password": "correct-horse-battery"},
            headers={"Origin": config.allowed_origin},
        )
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "Secure" not in response.headers["set-cookie"]
        assert client.get("/api/session").status_code == 200
        headers = {"Origin": config.allowed_origin, "Idempotency-Key": "local-create"}
        assert client.post("/api/cameras", json=camera_payload(), headers=headers).status_code == 403
        headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]
        assert client.post("/api/cameras", json=camera_payload(), headers=headers).status_code == 201


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.168.0.160:5173",
        "http://example.com",
        "https://example.com/path",
        "https://user:password@example.com",
        "https://example.com?query=yes",
        "https://example.com#fragment",
        "https://example.com:invalid",
    ],
)
def test_local_mode_does_not_allow_remote_http_or_malformed_origins(monkeypatch, origin):
    monkeypatch.setenv("CAW_LOCAL_DEV", "1")
    monkeypatch.setenv("CAW_ALLOWED_ORIGIN", origin)
    with pytest.raises(ValueError):
        Config.from_env()


def test_local_development_mode_needs_no_network_allowlists(monkeypatch):
    monkeypatch.setenv("CAW_LOCAL_DEV", "1")
    monkeypatch.setenv("CAW_ALLOWED_ORIGIN", "http://127.0.0.1:5173")
    monkeypatch.setenv("CAW_RTSP_ALLOWED_HOSTS", "")
    monkeypatch.setenv("CAW_RTSP_ALLOWED_PORTS", "")
    monkeypatch.setenv("CAW_PROVIDER_ALLOWED_HOSTS", "")
    monkeypatch.setenv("CAW_PROVIDER_LOCAL_ENDPOINTS", "")

    config = Config.from_env()

    assert config.development_mode
    assert config.rtsp_allowed_hosts == ()
    assert config.rtsp_allowed_ports == ()
    assert config.provider_allowed_hosts == ()
    assert config.provider_local_endpoints == ()


def test_development_mode_cannot_use_a_non_loopback_origin(monkeypatch):
    monkeypatch.setenv("CAW_LOCAL_DEV", "1")
    monkeypatch.setenv("CAW_ALLOWED_ORIGIN", "https://camera.example.com")
    with pytest.raises(ValueError, match="回环 HTTP"):
        Config.from_env()


def test_production_https_keeps_secure_cookies(monkeypatch):
    monkeypatch.setenv("CAW_ALLOWED_ORIGIN", "https://camera.example.com")
    monkeypatch.delenv("CAW_LOCAL_DEV", raising=False)
    assert Config.from_env().secure_cookies


def test_rtsp_host_and_port_both_require_explicit_deployment_allowlist(w, client):
    payload = camera_payload()
    payload["source"]["host"] = "203.0.113.10"
    headers = {"Idempotency-Key": "public-source"}
    response = client.post("/api/cameras", json=payload, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["field_errors"][0]["path"] == "source.host"
    w.config = dataclasses.replace(w.config, rtsp_allowed_hosts=("127.0.0.1", "203.0.113.10"))
    response = client.post("/api/cameras", json=payload, headers=headers)
    assert response.status_code == 201
    assert response.json()["data"]["source"]["host"] == "203.0.113.10"
    assert payload["source"]["password"] not in response.text
    payload["source"]["port"] = 554
    response = client.post("/api/cameras", json=payload, headers={"Idempotency-Key": "blocked-port"})
    assert response.status_code == 422
    assert response.json()["error"]["field_errors"][0]["path"] == "source.port"


@pytest.mark.parametrize("host", ["example.com", "127.1", "127.0.0.1@evil", "::1", "999.0.0.1"])
def test_rtsp_source_rejects_hostnames_and_ambiguous_addresses(host):
    with pytest.raises(ValidationError):
        Source(host=host, port=15554, path="/live")
    with pytest.raises(ValidationError):
        SourcePatch(host=host)


def test_rtsp_source_needs_no_allowlist_in_development_mode(w, client):
    w.config = dataclasses.replace(
        w.config,
        development_mode=True,
        rtsp_allowed_hosts=(),
        rtsp_allowed_ports=(),
    )
    payload = camera_payload()
    payload["source"]["host"] = "192.168.0.130"
    payload["source"]["port"] = 554

    response = client.post("/api/cameras", json=payload, headers={"Idempotency-Key": "dev-source"})

    assert response.status_code == 201, response.text
    assert response.json()["data"]["source"]["host"] == "192.168.0.130"
    assert response.json()["data"]["source"]["port"] == 554


async def test_revoked_rtsp_host_blocks_capture_and_offline_probe(w, monkeypatch):
    camera_id = make_camera(w, enabled=False)
    with w.db.read() as conn:
        camera = one(conn, s.cameras, camera_id)
    w.config = dataclasses.replace(w.config, rtsp_allowed_hosts=())

    async def unexpected_process(*args, **kwargs):
        pytest.fail("撤销白名单后不应启动 FFmpeg")

    monkeypatch.setattr("asyncio.create_subprocess_exec", unexpected_process)
    supervisor = Supervisor(w)
    with pytest.raises(AppError):
        await supervisor.start(camera)
    with pytest.raises(AppError):
        await supervisor.test(camera_id)
