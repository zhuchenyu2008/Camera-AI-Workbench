from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.ai_gateway import ChatResult, ChatUsage, ProviderError, ToolCall, OpenAICompatibleGateway
from app.ai_tools import TOOL_SCHEMAS, ToolAuthorizationError, ToolContext, ToolExecutor
from app.analysis_service import AnalysisRequest, validate_model_output
from app.db import Base, create_engine_for_settings
from app.config import Settings
from app.models import AIJob, AIBinding, AIProvider, Camera, Frame, ToolCall as OrmToolCall
from app.queue import JobRecord
import app.worker_main as worker


def test_tool_executor_enforces_cumulative_initial_and_tool_image_budget():
    authorized: set[str] = set()
    invoked = []
    executor = ToolExecutor(
        ToolContext("cam", 0, 300_000, max_calls=4, max_images=30, images=29),
        {"get_frames": lambda **args: invoked.append(args) or {
            "images": [b"new-frame"], "_authorized_frame_ids": ["frame-new"],
        }},
        on_authorize=authorized.update,
    )

    assert executor.images_used == 29
    assert executor.images_remaining == 1
    assert executor.calls_remaining == 4
    result = executor.execute("get_frames", {"start_ms": 0, "end_ms": 100, "limit": 1})
    assert result["images"] == [b"new-frame"]
    assert authorized == {"frame-new"}
    assert executor.images_used == 30
    assert executor.images_remaining == 0
    assert executor.calls_used == 1

    with pytest.raises(ToolAuthorizationError, match="图片预算"):
        executor.execute("get_frames", {"start_ms": 0, "end_ms": 100, "limit": 1})
    assert len(invoked) == 1
    assert executor.calls_used == 2  # denied attempts count toward the four-call cap


def test_tool_executor_allows_exactly_four_calls_and_rejects_unknown_or_malformed():
    invoked = []
    executor = ToolExecutor(
        ToolContext("cam", 0, 300_000, max_calls=4, max_images=30),
        {"get_local_signals": lambda **args: invoked.append(args) or {"frame_count": 1}},
    )
    for _ in range(4):
        executor.execute("get_local_signals", {"start_ms": 0, "end_ms": 100})
    assert executor.calls_used == 4
    assert executor.calls_remaining == 0
    with pytest.raises(ToolAuthorizationError, match="调用次数"):
        executor.execute("get_local_signals", {"start_ms": 0, "end_ms": 100})
    assert len(invoked) == 4

    denied = ToolExecutor(ToolContext("cam", 0, 300_000), {"get_frames": lambda **args: pytest.fail("must not run")})
    with pytest.raises(ToolAuthorizationError, match="白名单"):
        denied.execute("run_shell", {"command": "whoami"})
    assert denied.calls_used == 1

    malformed = ToolExecutor(ToolContext("cam", 0, 300_000), {"get_frames": lambda **args: pytest.fail("must not run")})
    with pytest.raises(ToolAuthorizationError, match="schema"):
        malformed.execute("get_frames", {"start_ms": 0, "end_ms": 100, "limit": 1, "path": "../../secret"})


def test_provider_parser_rejects_malformed_tool_call_payload():
    response = httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "call-1", "function": {"name": "get_frames", "arguments": "{not-json"}},
    ]}}]})
    with pytest.raises(ProviderError) as error:
        OpenAICompatibleGateway._parse_result(response.json(), 1, response)
    assert error.value.code == "PROVIDER_INVALID_TOOL_CALL"


def test_tool_result_and_audit_argument_sanitizers_drop_paths_urls_and_payloads():
    frame_id = str(uuid4())
    raw = {
        "frame_ids": [frame_id],
        "images": [b"RAW_IMAGE_BYTES"],
        "_authorized_frame_ids": [frame_id],
        "file_path": "/private/camera.jpg",
        "rtsp_url": "rtsp://user:password@camera/private",
        "signals": {frame_id: {"diff_score": 0.2, "debug_path": "/tmp/frame.jpg"}},
    }
    safe = worker.OrmAnalysisGateway._tool_result_for_model(raw)
    serialized = json.dumps(safe)
    assert frame_id in serialized
    assert "RAW_IMAGE_BYTES" not in serialized
    assert "/private" not in serialized
    assert "rtsp://" not in serialized
    assert "debug_path" not in serialized

    args = worker.OrmAnalysisGateway._safe_tool_arguments({
        "start_ms": 1, "end_ms": 2, "limit": 1,
        "frame_id": frame_id, "path": "C:/private/file", "api_key": "secret",
    })
    assert args == {"start_ms": 1, "end_ms": 2, "limit": 1, "frame_id": frame_id}


def _setup_worker_db(tmp_path: Path, monkeypatch):
    settings = Settings(
        _env_file=None,
        APP_DB_URL=f"sqlite:///{(tmp_path / 'worker-tools.sqlite3').as_posix()}",
    )
    engine = create_engine_for_settings(settings)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "get_session_factory", lambda: factory)
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(
        provider_allowed_hosts_list=(), provider_local_endpoints_list=(), is_production=False,
    ))
    return engine, factory


def _store_camera_and_frame(factory, data_dir: Path, *, layer: str = "L2"):
    start = 1_700_000_000_000
    end = start + 300_000
    camera_id, provider_id, job_id = str(uuid4()), str(uuid4()), str(uuid4())
    frame_id = str(uuid4())
    relative = f"frames/{camera_id}/{frame_id}.jpg"
    path = data_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8tiny-jpeg\xff\xd9")
    with factory() as db:
        camera = Camera(id=camera_id, name="Test camera")
        provider = AIProvider(id=provider_id, label="Test", base_url="https://provider.example/v1", enabled=True, timeout_ms=1000, capability_json={"tools": True})
        binding = AIBinding(layer=layer, provider_id=provider_id, model_name="test-model")
        db.add_all([camera, provider, binding])
        db.flush()
        db.add(Frame(id=frame_id, camera_id=camera_id, captured_at_ms=start + 10_000, sequence=1,
                     relative_path=relative, byte_size=16, sha256="a" * 64, state="active"))
        job = AIJob(id=job_id, idempotency_key=f"test:{job_id}", layer=layer, camera_id=camera_id,
                    period_start_ms=start, period_end_ms=end, state="running", input_frame_ids_json=[],
                    input_ref_json={"authorized_candidate_frame_ids": [frame_id],
                                    "authorized_frame_range_ms": [start, end], "observation_ids": []})
        db.add(job)
        db.commit()
    return start, end, camera_id, frame_id, job_id, relative


class _FakeChatGateway:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.requests = []
        self.step = 0
        self.__class__.instances.append(self)

    async def complete(self, request):
        self.requests.append(copy.deepcopy(request))
        self.step += 1
        if self.step == 1:
            return ChatResult(content="", tool_calls=[ToolCall(
                id="call-frames", name="get_frames",
                arguments={"start_ms": 1_700_000_000_000, "end_ms": 1_700_000_300_000, "limit": 1},
            )], usage=ChatUsage(prompt_tokens=4, completion_tokens=1, total_tokens=5), provider_latency_ms=3)
        frame_id = str(self.requests and self._frame_id)
        start = 1_700_000_000_000
        end = start + 300_000
        data = {
            "window": {"start": "2023-11-14T22:13:20Z", "end": "2023-11-14T22:18:20Z"},
            "segments": [{
                "start": "2023-11-14T22:13:20Z", "end": "2023-11-14T22:18:20Z",
                "canonical_activity": "学习", "display_description": "桌前活动",
                "confidence": 0.8, "intervals": [{
                    "start": "2023-11-14T22:13:20Z", "end": "2023-11-14T22:18:20Z",
                    "evidence_kind": "direct", "confidence": 0.8, "evidence_frame_ids": [frame_id],
                }],
            }], "unknown_intervals": [], "transitions": [], "tool_summary": [],
        }
        return ChatResult(content=json.dumps(data), usage=ChatUsage(prompt_tokens=5, completion_tokens=10, total_tokens=15), provider_latency_ms=7, request_id="fake-request")

    async def aclose(self):
        return None


def test_orm_gateway_executes_l2_tools_sends_safe_followup_and_persists_audit(tmp_path, monkeypatch):
    engine, factory = _setup_worker_db(tmp_path, monkeypatch)
    start, end, camera_id, frame_id, job_id, _relative = _store_camera_and_frame(factory, tmp_path)
    _FakeChatGateway.instances.clear()
    monkeypatch.setattr(worker, "OpenAICompatibleGateway", _FakeChatGateway)
    gateway = worker.OrmAnalysisGateway(tmp_path)
    request = AnalysisRequest(job_id=job_id, layer="L2", camera_id=camera_id, start_at_ms=start, end_at_ms=end,
                              input_frame_ids=(), payload={"authorized_candidate_frame_ids": [frame_id],
                              "authorized_frame_range_ms": [start, end], "observation_ids": []}, attempt=1,
                              prompt_version="v1", schema_version="v1")
    _FakeChatGateway._frame_id = frame_id
    response = __import__("asyncio").run(gateway.analyze(request))

    assert response.authorized_frame_ids == frozenset({frame_id})
    validated = validate_model_output(JobRecord(id=job_id, idempotency_key="test", layer="L2", camera_id=camera_id,
        period_start_ms=start, period_end_ms=end, input_frame_ids=[]), response.data,
        authorized_frame_ids=response.authorized_frame_ids)
    assert validated["segments"][0]["intervals"][0]["evidence_frame_ids"] == [frame_id]

    fake = _FakeChatGateway.instances[-1]
    assert len(fake.requests) == 2
    assert fake.requests[0].tools == TOOL_SCHEMAS
    assert not any(part.get("type") == "image_url" for msg in fake.requests[0].messages if isinstance(msg.get("content"), list) for part in msg["content"])
    second = fake.requests[1]
    assert second.tools == TOOL_SCHEMAS
    tool_message = next(message for message in second.messages if message.get("role") == "tool")
    assert "_authorized_frame_ids" not in tool_message["content"]
    assert "images" not in tool_message["content"]
    image_parts = [part for message in second.messages if isinstance(message.get("content"), list) for part in message["content"] if part.get("type") == "image_url"]
    assert len(image_parts) == 1

    with factory() as db:
        audit = db.scalar(select(OrmToolCall).where(OrmToolCall.job_id == job_id))
        assert audit is not None
        assert audit.tool_name == "get_frames"
        assert audit.safe_arguments_json == {"start_ms": start, "end_ms": end, "limit": 1}
        assert audit.frame_count == 1
        assert audit.duration_ms is not None
        assert audit.affected_final_conclusion is True
        assert "/frames/" not in (audit.result_summary or "")
        assert "jpeg" not in (audit.result_summary or "")
    engine.dispose()


def test_orm_gateway_fails_closed_when_l2_candidate_snapshot_is_missing(tmp_path, monkeypatch):
    engine, factory = _setup_worker_db(tmp_path, monkeypatch)
    start, end, camera_id, frame_id, job_id, _relative = _store_camera_and_frame(factory, tmp_path)
    _FakeChatGateway.instances.clear()
    monkeypatch.setattr(worker, "OpenAICompatibleGateway", _FakeChatGateway)
    gateway = worker.OrmAnalysisGateway(tmp_path)
    request = AnalysisRequest(
        job_id=job_id,
        layer="L2",
        camera_id=camera_id,
        start_at_ms=start,
        end_at_ms=end,
        input_frame_ids=(),
        payload={"authorized_frame_range_ms": [start, end]},
        attempt=1,
        prompt_version="v1",
        schema_version="v1",
    )
    _FakeChatGateway._frame_id = frame_id

    response = __import__("asyncio").run(gateway.analyze(request))

    assert response.authorized_frame_ids == frozenset()
    fake = _FakeChatGateway.instances[-1]
    assert len(fake.requests) == 2
    tool_message = next(message for message in fake.requests[1].messages if message.get("role") == "tool")
    assert json.loads(tool_message["content"])["frame_ids"] == []
    assert not any(
        part.get("type") == "image_url"
        for message in fake.requests[1].messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
    )
    engine.dispose()

def test_orm_gateway_exposes_no_tools_for_l1(tmp_path, monkeypatch):
    engine, factory = _setup_worker_db(tmp_path, monkeypatch)
    start, end, camera_id, frame_id, job_id, _relative = _store_camera_and_frame(factory, tmp_path, layer="L1")
    _FakeChatGateway.instances.clear()
    monkeypatch.setattr(worker, "OpenAICompatibleGateway", _FakeChatGateway)
    gateway = worker.OrmAnalysisGateway(tmp_path)
    request = AnalysisRequest(job_id=job_id, layer="L1", camera_id=camera_id, start_at_ms=start, end_at_ms=end,
                              input_frame_ids=(frame_id,), payload={}, attempt=1, prompt_version="v1", schema_version="v1")
    _FakeChatGateway._frame_id = frame_id
    # The first fake response requests a tool, which an L1 gateway must reject without sending tools.
    with pytest.raises(ProviderError) as error:
        __import__("asyncio").run(gateway.analyze(request))
    assert error.value.code == "UNSUPPORTED_TOOLS"
    assert _FakeChatGateway.instances[-1].requests[0].tools is None
    engine.dispose()
