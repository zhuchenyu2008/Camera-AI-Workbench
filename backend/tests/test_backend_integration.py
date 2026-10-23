"""Small integration tests for the full backend surface."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai_gateway import ChatRequest, OpenAICompatibleGateway, ProviderError
from app.ai_schemas import L1Response, L2Response, validate_authorized_evidence
from app.ai_tools import ToolContext, ToolExecutor, ToolAuthorizationError
from app.config import Settings
from app.db import Base, create_engine_for_settings
from app.main import AppConfig, create_app
from app.models import AIJob, Camera
from app.queue import SqlAlchemyJobRepository, TaskQueue


def _config(tmp_path: Path):
    return AppConfig(data_dir=tmp_path, database_url=f"sqlite:///{(tmp_path/'app.sqlite3').as_posix()}",
                     allowed_origins=("http://localhost:5173",), auth_disabled=True,
                     master_key_file=tmp_path/"master.key")


def test_camera_api_and_secret_ciphertext(tmp_path):
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        response = client.post("/api/v1/cameras", headers={"Origin":"http://localhost:5173"},
                               json={"name":"Front","rtsp_url":"rtsp://127.0.0.1:554/secret"})
        assert response.status_code == 201
        data=response.json()["data"]
        assert "secret" not in response.text
        assert data["source_configured"]
        assert client.get("/api/v1/cameras").status_code == 200
        assert client.get("/health/ready").status_code == 200


def test_sqlalchemy_queue_persists_job_and_lease_token(tmp_path):
    settings=Settings(_env_file=None, APP_DATA_DIR=tmp_path, APP_DB_URL=f"sqlite:///{(tmp_path/'queue.sqlite3').as_posix()}")
    engine=create_engine_for_settings(settings);Base.metadata.create_all(engine)
    from sqlalchemy.orm import sessionmaker
    factory=sessionmaker(engine,expire_on_commit=False)
    queue=TaskQueue(SqlAlchemyJobRepository(AIJob),session_factory=factory)
    job=queue.enqueue(layer="L1",idempotency_key="test:camera:1",camera_id=None,period_start_ms=0,period_end_ms=60_000,
                      payload={"hello":"world"},input_frame_ids=[])
    claimed=queue.claim("worker",now_ms=job.available_at_ms+1)
    assert claimed and claimed.lease_token
    reloaded=SqlAlchemyJobRepository(AIJob)
    with factory() as db:
        row=reloaded.get(db,claimed.id)
        assert row and row.lease_token==claimed.lease_token
        assert row.payload["hello"]=="world"
    engine.dispose()


def test_schemas_require_authorized_evidence_and_inference_anchors():
    import pytest
    from pydantic import ValidationError
    valid={"window":{"start":"2026-10-22T00:00:00Z","end":"2026-10-22T00:05:00Z"},
      "segments":[{"start":"2026-10-22T00:00:00Z","end":"2026-10-22T00:05:00Z","canonical_activity":"学习",
        "display_description":"学习","confidence":0.7,"intervals":[{"start":"2026-10-22T00:00:00Z","end":"2026-10-22T00:05:00Z",
        "evidence_kind":"inferred","confidence":0.5,"reason":"两端一致","anchor_frame_ids":["a","b"]}]}]}
    result=L2Response.model_validate(valid)
    validate_authorized_evidence(result,allowed_frame_ids={"a","b"},max_inference_gap_seconds=300)
    with pytest.raises(ValueError):validate_authorized_evidence(result,allowed_frame_ids={"a"})
    valid["segments"][0]["intervals"][0]["anchor_frame_ids"]=["a"]
    with pytest.raises(ValidationError):L2Response.model_validate(valid)


def test_tool_executor_denies_arbitrary_path_and_out_of_window():
    ctx=ToolContext("cam",0,100,allowed_frame_ids=frozenset({"f1"}))
    ex=ToolExecutor(ctx,{"get_frames":lambda **kw: {"frame_ids":kw.get("frame_ids",[])}})
    import pytest
    with pytest.raises(ToolAuthorizationError):ex.execute("get_frames",{"start_ms":-1,"end_ms":50,"limit":1})
    with pytest.raises(ToolAuthorizationError):ex.execute("get_frames",{"frame_ids":["../../etc/passwd"]})


def test_provider_policy_rejects_private_network_by_default():
    import pytest
    from app.ai_gateway import validate_provider_url
    with pytest.raises(ProviderError):validate_provider_url("http://127.0.0.1:9000/v1",allowed_hosts=("127.0.0.1",))


def test_segment_response_includes_server_calculated_duration():
    from types import SimpleNamespace
    from app.api.common import segment_dto
    from app.schemas import SegmentDto

    segment = SimpleNamespace(
        id="segment-1", camera_id="camera-1", l2_window_id=None,
        start_at_ms=1_800_000_000_000, end_at_ms=1_800_000_123_456,
        canonical_activity_id=None, canonical_activity=None,
        display_description=None, category=None, confidence=None,
        evidence_coverage=None, state="final", locked=False, version=1,
        source_observation_ids_json=[], intervals=[],
    )
    payload = segment_dto(segment)
    assert payload["duration_ms"] == 123_456
    assert SegmentDto.model_validate(payload).duration_ms == 123_456
