from __future__ import annotations

import json
from collections import defaultdict

import httpx
import pytest
from sqlalchemy import select, update

from app.activities.names import activity_name, match_activity_name
from app.analysis.client import AIClient
from app.analysis.normalize import normalize, validate_visual
from app.capture.frames import FrameWriter
from app.capture.supervisor import Supervisor
from app.errors import AppError
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.schemas import EntityCreate, VisualOutput
from app.security.secrets import digest
from app.services.analysis import Analysis
from app.services.assets import decode_image
from app.services.records import Records
from app.timeutils import iso, timestamp
from app.worker.executor import Executor
from tests.conftest import make_camera, make_minute, make_provider


def make_person(w, jpeg):
    data, width, height = decode_image(jpeg, "image/jpeg")
    path = w.assets.store("references", data)
    with w.db.write() as conn:
        person = w.assets.create_entity(conn, EntityCreate(name="我", kind="person"))
        reference = w.assets.attach_reference(
            conn,
            person["id"],
            {"relpath": path, "bytes": len(data), "width": width, "height": height, "sha256": digest(data)},
            "本人",
            True,
            '"rev-1"',
        )
        w.assets.update_entity(conn, person["id"], {"enabled": True}, '"rev-2"')
    return person["id"], reference["asset_id"]


def fill_minute(w, clock, jpeg, camera=None):
    camera = camera or make_camera(w)
    start = clock.value
    sid = new_id("stream")
    with w.db.write() as conn:
        conn.execute(
            s.stream_sessions.insert().values(
                id=sid, camera_id=camera, started_at_ms=start, source_revision=1
            )
        )
        conn.execute(
            update(s.camera_runtime)
            .where(s.camera_runtime.c.camera_id == camera)
            .values(stream_session_id=sid)
        )
    writer = FrameWriter(w)
    frames = []
    for index in range(60):
        clock.value = start + index * 1000
        frame = writer.save(camera, sid, jpeg, clock.value, str(index), index)
        assert frame is not None
        frames.append(frame)
    clock.value = start + 60000
    return camera, start, frames


def fake_visual(request, person, *, activity="reading", ref_assert=None):
    payload = json.loads(request.content)
    content = payload["messages"][1]["content"]
    if ref_assert:
        ref_assert(payload)
    refs = [c for c in content if c.get("text", "").startswith("参考资料")]
    assert refs
    frames = [
        json.loads(c["text"].split("：", 1)[1]) for c in content if c.get("text", "").startswith("现场证据：")
    ]
    minutes = defaultdict(list)
    for frame in frames:
        minutes[timestamp(frame["captured_at"]) // 60000 * 60000].append(frame)
    result = {"schema_version": "1.0", "minutes": []}
    for start, local in sorted(minutes.items()):
        ids = [frame["evidence_id"] for frame in local]
        observation = {
            "subject_id": person,
            "identity_status": "matched",
            "activity": activity,
            "labels": [],
            "object_ids": [],
            "start_evidence_id": ids[0],
            "end_evidence_id": ids[-1],
            "supporting_evidence_ids": ids,
            "observation": "人物面向书页，正在阅读。",
            "needs_dense_review": False,
        }
        result["minutes"].append(
            {
                "minute_start": iso(start),
                "observations": [observation],
                "unresolved_evidence_ids": [],
                "review_reasons": [],
            }
        )
    return httpx.Response(
        200,
        json={
            "id": "provider-req",
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30},
        },
    )


def _reset_job_for_retry(w, job_id):
    with w.db.write() as conn:
        conn.execute(s.jobs.update().where(s.jobs.c.id == job_id).values(not_before_ms=w.now()))


def test_activity_catalog_uses_current_camera_history_and_caps(w, clock):
    camera = make_camera(w)
    recent = clock.value - 60_000
    make_minute(w, camera, recent, activity="开关门进出房间")
    make_minute(w, camera, recent - 60_000, activity="离开画面")
    make_minute(w, camera, recent - 120_000, activity="unknown")
    make_minute(w, camera, clock.value - 31 * 24 * 60 * 60 * 1000, activity="过期活动")
    for index in range(51):
        make_minute(w, camera, recent - (index + 3) * 60_000, activity=f"活动{index}")

    with w.db.read() as conn:
        catalog = Analysis(w).activity_catalog(conn, camera)

    names = [item["name"] for item in catalog]
    assert len(catalog) == 50
    assert "开关门进出房间" in names
    assert "离开画面" not in names
    assert "unknown" not in names
    assert "过期活动" not in names


def test_activity_name_normalizes_text_and_reuses_catalog_name():
    assert activity_name("  １２３  ") == "123"
    assert activity_name("阅读") == "reading"
    assert match_activity_name("  开关门进出房间 ", [{"name": "开关门进出房间"}]) == "开关门进出房间"


def test_activity_catalog_is_sent_to_visual_model_and_reused(w, clock, jpeg):
    make_provider(w)
    camera = make_camera(w)
    make_minute(w, camera, clock.value - 60_000, activity="开关门进出房间")
    person, _ = make_person(w, jpeg)
    camera, start, frames = fill_minute(w, clock, jpeg, camera)
    with w.db.write() as conn:
        refs = w.assets.reference_set(conn, [camera])
        root = Analysis(w).plan(conn, camera, frames, refs, "dense", manual=True)
        child = one(
            conn,
            s.jobs,
            list(
                conn.execute(
                    select(s.jobs.c.id).where(s.jobs.c.kind == "dense_analysis")
                ).scalars()
            )[0],
        )
        assert child["payload_json"]["activity_catalog"][0]["name"] == "开关门进出房间"

    def check(payload):
        catalog = [
            item["text"]
            for item in payload["messages"][1]["content"]
            if item.get("text", "").startswith("已有活动目录")
        ]
        assert len(catalog) == 1 and "开关门进出房间" in catalog[0]

    client = AIClient(
        w,
        httpx.MockTransport(
            lambda request: fake_visual(
                request,
                person,
                activity="开关门进出房间",
                ref_assert=check,
            )
        ),
    )
    try:
        import asyncio

        asyncio.run(drain(w, Executor(w, Supervisor(w), client)))
    finally:
        import asyncio

        asyncio.run(client.close())

    with w.db.read() as conn:
        assert one(conn, s.jobs, root["id"])["state"] == "succeeded"
        current = w.ledger.spans(conn, [camera], start, start + 60_000)
        assert {span["activity"] for span in current} == {"开关门进出房间"}
        minute_revision = conn.execute(
            select(s.minute_revisions.c.result_json)
            .where(s.minute_revisions.c.minute_id == current[0]["minute_id"])
            .order_by(s.minute_revisions.c.revision.desc())
            .limit(1)
        ).scalar_one()
        assert "unanalysed_reason" not in minute_revision


def test_model_protocol_error_gets_only_one_retry(w):
    from app.worker.queue import enqueue

    with w.db.write() as conn:
        job = enqueue(conn, w.now(), "sparse_analysis", {}, "model-protocol-retry")
    error = AppError(422, "INVALID_MODEL_REFERENCES", "引用无效")

    first = w.queue.claim("worker")
    w.queue.fail(first, error)
    with w.db.read() as conn:
        saved = one(conn, s.jobs, job["id"])
        assert saved["state"] == "queued" and saved["failure_count"] == 1

    _reset_job_for_retry(w, job["id"])
    second = w.queue.claim("worker")
    w.queue.fail(second, error)
    with w.db.read() as conn:
        saved = one(conn, s.jobs, job["id"])
        assert saved["state"] == "failed" and saved["failure_count"] == 2


def test_retryable_model_error_uses_configured_retry_count(w):
    from app.worker.queue import enqueue

    with w.db.write() as conn:
        job = enqueue(conn, w.now(), "sparse_analysis", {}, "model-transient-retry")
    error = AppError(503, "MODEL_TIMEOUT", "模型超时", retryable=True)

    for failure_number in range(1, 5):
        claimed = w.queue.claim("worker")
        w.queue.fail(claimed, error)
        with w.db.read() as conn:
            saved = one(conn, s.jobs, job["id"])
            expected_state = "queued" if failure_number < 4 else "failed"
            assert saved["state"] == expected_state
            assert saved["failure_count"] == failure_number
        if failure_number < 4:
            _reset_job_for_retry(w, job["id"])


async def drain(w, executor, maximum=20):
    for _ in range(maximum):
        job = w.queue.claim("test-worker")
        if not job:
            return
        await executor.execute(job)
    raise AssertionError("任务未收敛")


async def test_B05_B06_dense_batches_reconcile_full_refs_and_usage(w, jpeg, clock):
    make_provider(w)
    person, asset = make_person(w, jpeg)
    camera, start, frames = fill_minute(w, clock, jpeg)
    with w.db.write() as conn:
        refs = w.assets.reference_set(conn, [camera])
        root = Analysis(w).plan(conn, camera, frames, refs, "dense", manual=True)
    calls = []

    def check(payload):
        calls.append(payload)
        images = [c for c in payload["messages"][1]["content"] if c["type"] == "image_url"]
        assert len(images) <= 21
        assert "本人" in json.dumps(payload, ensure_ascii=False)

    client = AIClient(w, httpx.MockTransport(lambda r: fake_visual(r, person, ref_assert=check)))
    try:
        await drain(w, Executor(w, Supervisor(w), client))
    finally:
        await client.close()
    assert len(calls) == 3
    with w.db.read() as conn:
        assert one(conn, s.jobs, root["id"])["state"] == "succeeded"
        minute = conn.execute(select(s.minute_records.c.id)).scalar_one()
        record = Records(w).minute(conn, minute)
        assert record["sampling"]["successful_frame_count"] == 60
        assert record["coverage"]["classified_ms"] == 60000
        usage = list(conn.execute(select(s.ai_usage)).mappings())
        assert sum(r["image_count"] for r in usage) == 60
        assert sum(r["reference_image_count"] for r in usage) == 3
        assert all(r["request_bytes"] > len(jpeg) * 20 for r in usage)
        assert len(list(conn.execute(select(s.evidence_refs)))) == 60


def test_visual_batches_do_not_mix_minutes(w):
    make_provider(w)
    with w.db.read() as conn:
        provider = conn.execute(select(s.providers)).mappings().one()
        cfg = w.settings_config(conn)
    frames = [
        {
            "id": f"ev_{index}",
            "camera_id": "cam",
            "captured_at_ms": minute + offset,
            "slot_at_ms": minute + offset,
            "bytes": 1,
        }
        for minute in (0, 60000)
        for index, offset in enumerate((0, 20000, 40000, 59000))
    ]

    batches = Analysis(w).pack_frames(cfg, provider, {"members_json": []}, frames)

    assert len(batches) == 2
    assert [
        {frame["slot_at_ms"] // 60000 * 60000 for frame in batch} for batch in batches
    ] == [{0}, {60000}]


async def test_B05_retry_after_and_provider_pause(w, jpeg, clock):
    provider = make_provider(w)
    person, asset = make_person(w, jpeg)
    camera, start, frames = fill_minute(w, clock, jpeg)
    with w.db.write() as conn:
        refs = w.assets.reference_set(conn, [camera])
        Analysis(w).plan(conn, camera, frames[:1], refs, "dense")
    client = AIClient(
        w,
        httpx.MockTransport(
            lambda r: httpx.Response(
                429,
                headers={"Retry-After": "180", "x-request-id": "req-rate-limit-42"},
                json={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "type": "requests",
                        "message": "Rate limit for Bearer sk-testsecret123456; see https://provider.example/error",
                    }
                },
            )
        ),
    )
    executor = Executor(w, Supervisor(w), client)
    try:
        job = w.queue.claim("worker")
        await executor.execute(job)
        with w.db.read() as conn:
            saved = one(conn, s.jobs, job["id"])
            assert saved["state"] == "queued" and saved["queue_reason"] == "retry_wait"
            assert saved["not_before_ms"] >= clock.value + 180000
            assert saved["error_json"]["safe_details"] == {
                "provider_http_status": 429,
                "provider_request_id": "req-rate-limit-42",
                "provider_error_code": "rate_limit_exceeded",
                "provider_error_type": "requests",
                "provider_error_message": "Rate limit for Bearer [redacted]; see [URL]",
            }
            usage = conn.execute(select(s.ai_usage)).mappings().one()
            assert usage["input_tokens"] is None and usage["billing_status"] == "unknown"
        clock.advance(180000)
    finally:
        await client.close()
    client = AIClient(w, httpx.MockTransport(lambda r: httpx.Response(401)))
    try:
        await Executor(w, Supervisor(w), client).execute(w.queue.claim("worker"))
        with w.db.read() as conn:
            assert one(conn, s.providers, provider)["paused"]
            saved = one(conn, s.jobs, job["id"])
            assert saved["queue_reason"] == "provider_paused"
            assert saved["error_json"]["safe_details"]["provider_http_status"] == 401
        assert w.queue.claim("worker") is None
    finally:
        await client.close()


async def test_B08_sent_before_ttl_response_after_ttl_can_commit(w, jpeg, clock):
    make_provider(w)
    person, asset = make_person(w, jpeg)
    camera, start, frames = fill_minute(w, clock, jpeg)
    with w.db.write() as conn:
        for frame in frames:
            conn.execute(
                update(s.frames).where(s.frames.c.id == frame["id"]).values(expires_at_ms=clock.value + 500)
            )
            frame["expires_at_ms"] = clock.value + 500
        refs = w.assets.reference_set(conn, [camera])
        root = Analysis(w).plan(conn, camera, frames, refs, "dense")

    def response(request):
        result = fake_visual(request, person)
        clock.advance(1000)
        return result

    client = AIClient(w, httpx.MockTransport(response))
    try:
        await drain(w, Executor(w, Supervisor(w), client))
    finally:
        await client.close()
    with w.db.read() as conn:
        # 第一批已发送可落库，其余批次如实过期，父任务为 partial。
        assert one(conn, s.jobs, root["id"])["state"] == "partial"
        minute = conn.execute(select(s.minute_records.c.id)).scalar_one()
        record = Records(w).minute(conn, minute)
        assert record["analysis_state"] == "partial" and record["evidence_state"] == "expired"
        assert record["coverage"]["unanalysed_expired_ms"] == 40000


def test_B05_reject_reference_as_scene_and_forbid_inference_over_gap():
    frame = {"id": "ev_real", "camera_id": "cam", "captured_at": "2026-10-17T12:00:00Z"}
    output = VisualOutput.model_validate(
        {
            "schema_version": "1.0",
            "minutes": [
                {
                    "minute_start": "2026-10-17T12:00:00Z",
                    "observations": [
                        {
                            "subject_id": None,
                            "identity_status": "unknown",
                            "activity": "unknown",
                            "labels": [],
                            "object_ids": [],
                            "start_evidence_id": "asset_reference",
                            "end_evidence_id": "asset_reference",
                            "supporting_evidence_ids": ["asset_reference"],
                            "observation": "不确定",
                            "needs_dense_review": False,
                        }
                    ],
                    "unresolved_evidence_ids": [],
                    "review_reasons": [],
                }
            ],
        }
    )
    with pytest.raises(AppError, match="INVALID_MODEL_REFERENCES"):
        validate_visual(output, [frame], [], [])
    output.minutes[0].observations[0].start_evidence_id = "ev_real"
    output.minutes[0].observations[0].end_evidence_id = "ev_end"
    output.minutes[0].observations[0].supporting_evidence_ids = ["ev_real", "ev_end"]
    output.minutes[0].observations[0].activity = "reading"
    frames = [frame, {"id": "ev_end", "camera_id": "cam", "captured_at": "2026-10-17T12:00:20Z"}]
    start = timestamp(frame["captured_at"])
    segments, _ = normalize(
        [output.model_dump(mode="json")],
        frames,
        [(start, start + 1000), (start + 20000, start + 21000)],
        support="sparse",
    )
    assert sum(s["end_at_ms"] - s["start_at_ms"] for s in segments) == 2000


def test_B03_sparse_sampler_detector_roi_and_long_dense():
    import numpy as np
    from PIL import Image

    from app.detection.motion import Detector, sparse_frames
    from app.schemas import SettingsConfig

    cfg = SettingsConfig()
    frames = [{"id": str(i), "slot_at_ms": i * 1000} for i in range(60)]
    assert [r["id"] for r in sparse_frames(frames, 0, [0, 20, 40, 59])] == ["0", "20", "40", "59"]
    detector = Detector(0)
    regions = {"include": [], "exclude": []}
    grey = np.zeros((100, 100), dtype=np.uint8)
    for index in range(91):
        result = detector.feed(Image.fromarray(grey), index * 1000, regions, cfg)
    assert result["state"] == "sparse"
    changed = grey.copy()
    changed[:20, :20] = 255
    assert detector.feed(Image.fromarray(changed), 92000, regions, cfg)["state"] == "dense"
    for index in range(93, 93 + 90 * 60):
        result = detector.feed(Image.fromarray(grey if index % 2 else changed), index * 1000, regions, cfg)
        assert result["state"] == "dense"
    excluded = {"include": [], "exclude": [{"points": [[0, 0], [0.3, 0], [0.3, 0.3], [0, 0.3]]}]}
    detector = Detector(0)
    detector.feed(Image.fromarray(grey), 0, excluded, cfg)
    assert detector.feed(Image.fromarray(changed), 1000, excluded, cfg)["score"] == 0
