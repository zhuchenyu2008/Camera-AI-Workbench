from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import AppError
from app.main import create_app
from app.schemas import CameraPatch, Regions
from app.security.secrets import redact, safe_path, validate_provider_url
from tests.conftest import camera_payload


def test_B09_origin_sessions_password_and_limits(w, client):
    with TestClient(create_app(w.config, w), base_url=w.config.allowed_origin) as other:
        assert (
            other.post(
                "/api/session", json={"username": "admin", "password": "correct-horse-battery"}
            ).status_code
            == 403
        )
        assert (
            other.post(
                "/api/session",
                json={"username": "admin", "password": "correct-horse-battery"},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        login = other.post(
            "/api/session",
            json={"username": "admin", "password": "correct-horse-battery"},
            headers={"Origin": w.config.allowed_origin},
        )
        assert login.status_code == 200
        response = client.patch(
            "/api/account/password",
            json={"current_password": "correct-horse-battery", "new_password": "new-correct-password"},
        )
        assert response.status_code == 204 and not response.content
        assert client.get("/api/session").status_code == 401
        assert other.get("/api/session").status_code == 401


def test_B09_request_upload_boundaries_and_no_secret_errors(client):
    response = client.post(
        "/api/cameras",
        json=camera_payload() | {"unknown_secret": "private-test-value"},
        headers={"Idempotency-Key": "invalid"},
    )
    assert response.status_code == 422 and "private-test-value" not in response.text
    response = client.post(
        "/api/cameras", content=b"x" * (1024 * 1024 + 1), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413 and response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
    for path in [
        "/api/evidence/no",
        "/api/reference-assets/no/content",
        "/api/cameras/no/preview/status",
        "/api/status",
    ]:
        response = client.get(path)
        assert "X-Request-ID" in response.headers
        assert response.headers["Cache-Control"] == "private, no-store"


def test_B09_source_and_filesystem_ssrf_guards(w, tmp_path):
    for path in ["//evil/live", "/x\n-y", "/rtsp://evil/live", "/%0aevil", "/foo@evil"]:
        with pytest.raises(ValueError):
            CameraPatch.model_validate({"source": {"path": path}})
    for url in [
        "http://169.254.169.254/latest",
        "https://localhost/v1",
        "https://user:pass@api.example/v1",
        "https://api.example/v1?url=secret",
    ]:
        with pytest.raises(AppError):
            validate_provider_url(url, w.config, resolve=False)
    with pytest.raises(AppError):
        safe_path(w.config.data_dir, "../../outside.txt")
    assert "secret" not in redact("rtsp://user:secret@127.0.0.1/live Authorization: Bearer secret")


def test_B11_keyset_cursor_binding_and_openapi_headers(client):
    for index in range(3):
        assert (
            client.post(
                "/api/cameras",
                json=camera_payload(str(index)),
                headers={"Idempotency-Key": f"camera-{index}"},
            ).status_code
            == 201
        )
    first = client.get("/api/cameras?limit=2").json()
    assert first["page"]["has_more"] and first["page"]["next_cursor"]
    second = client.get("/api/cameras", params={"limit": 2, "cursor": first["page"]["next_cursor"]}).json()
    assert not second["page"]["has_more"] and len(second["data"]) == 1
    assert len({r["id"] for r in first["data"] + second["data"]}) == 3
    assert client.get("/api/providers", params={"cursor": first["page"]["next_cursor"]}).status_code == 400
    assert client.get("/api/cameras?cursor=bad").status_code == 400
    for _ in range(3):
        schema = client.get("/openapi.json").json()
    params = schema["paths"]["/api/cameras/{camera_id}"]["patch"]["parameters"]
    assert len({(p["name"], p["in"]) for p in params}) == len(params)
    assert {p["name"] for p in params} >= {"If-Match", "Idempotency-Key", "X-CSRF-Token"}


def test_B01_invalid_roi_and_naive_time(client):
    with pytest.raises(ValueError):
        Regions.model_validate({"include": [{"id": "self", "points": [[0, 0], [1, 1], [0, 1], [1, 0]]}]})
    assert (
        client.get(
            "/api/minutes", params={"from": "2026-10-17T12:00:00", "to": "2026-10-17T13:00:00"}
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/minutes", params={"from": "2026-10-17T12:00:00Z", "to": "2026-10-19T13:00:00Z"}
        ).status_code
        == 400
    )
