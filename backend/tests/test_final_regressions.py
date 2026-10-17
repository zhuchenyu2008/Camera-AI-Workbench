from __future__ import annotations

import json

import httpx
from sqlalchemy import select, update

from app.activities.reproject import reproject
from app.analysis.client import AIClient
from app.capture.supervisor import Supervisor
from app.detection.motion import sparse_frames
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.services.analysis import Analysis
from app.services.records import Records
from app.summaries.service import Summaries
from app.worker.cleanup import Cleaner
from app.worker.executor import Executor
from app.worker.queue import enqueue
from tests.conftest import make_camera, make_minute, make_provider
from tests.test_analysis import drain, fake_visual, fill_minute, make_person


async def initial_sparse(w, clock, jpeg):
    make_provider(w)
    person, asset = make_person(w, jpeg)
    camera, start, frames = fill_minute(w, clock, jpeg)
    with w.db.write() as conn:
        refs = w.assets.reference_set(conn, [camera])
        Analysis(w).plan(conn, camera, sparse_frames(frames, start, [0, 20, 40, 59]), refs, "sparse")
    client = AIClient(w, httpx.MockTransport(lambda r: fake_visual(r, person)))
    try:
        await drain(w, Executor(w, Supervisor(w), client))
    finally:
        await client.close()
    return person, camera, start, frames


async def test_B03_local_trigger_backfills_closed_minute_and_bad_ai_does_not_stop_capture(w, clock, jpeg):
    import io
    from types import SimpleNamespace

    import numpy as np
    from PIL import Image

    from app.detection.motion import Detector

    person, camera, start, frames = await initial_sparse(w, clock, jpeg)
    clock.advance(10000)
    with w.db.read() as conn:
        row = one(conn, s.cameras, camera)
    supervisor = Supervisor(w)
    stream = SimpleNamespace(
        camera=row,
        session_id="fixture",
        detector=Detector(started_at=start, state="sparse", previous=np.zeros((90, 160), dtype=np.int16)),
        codec="h264",
        dropped=0,
        sequence=1,
    )
    supervisor.detect(stream, jpeg, w.now())
    with w.db.write() as conn:
        dense = list(
            conn.execute(
                select(s.jobs).where(s.jobs.c.kind == "dense_analysis", s.jobs.c.state == "queued")
            ).mappings()
        )
        assert len(dense) == 3
        assert {e for job in dense for e in job["payload_json"]["evidence_ids"]} == {f["id"] for f in frames}
        conn.execute(update(s.providers).values(tested_config_revision=None))
    stream.detector.state = "sparse"
    image = io.BytesIO()
    Image.new("RGB", (160, 90), "black").save(image, "JPEG")
    supervisor.detect(stream, image.getvalue(), w.now())
    with w.db.write() as conn:
        runtime = (
            conn.execute(select(s.camera_runtime).where(s.camera_runtime.c.camera_id == camera))
            .mappings()
            .one()
        )
        assert runtime["diagnostics_json"]["analysis_error_code"] == "PROVIDER_NOT_VERIFIED"
        assert runtime["capture_state"] == "online"
        Analysis(w).schedule(conn)


async def test_B07_partial_dense_preserves_unrequested_sparse_observations(w, clock, jpeg):
    person, camera, start, frames = await initial_sparse(w, clock, jpeg)
    with w.db.write() as conn:
        refs = w.assets.reference_set(conn, [camera])
        Analysis(w).plan(conn, camera, frames[40:], refs, "dense", manual=True)
    client = AIClient(w, httpx.MockTransport(lambda r: fake_visual(r, person, activity="writing")))
    try:
        await drain(w, Executor(w, Supervisor(w), client))
    finally:
        await client.close()
    with w.db.read() as conn:
        result = w.ledger.calculate(
            conn, start, start + 60000, {"camera_ids": [camera], "subject_id": person}
        )
        assert result["activities"] == [
            {"activity": "reading", "duration_ms": 40000},
            {"activity": "writing", "duration_ms": 20000},
        ]
        minute = conn.execute(select(s.minute_records.c.id)).scalar_one()
        assert Records(w).minute(conn, minute)["analysis_mode"] == "mixed"


async def test_B05_omitted_minute_fails_only_that_minute_and_sampling_is_honest(w, clock, jpeg):
    make_provider(w)
    person, asset = make_person(w, jpeg)
    camera, start, first = fill_minute(w, clock, jpeg)
    camera, second_start, second = fill_minute(w, clock, jpeg, camera)
    selected = sparse_frames(first, start, [0, 20, 40, 59]) + sparse_frames(
        second, second_start, [0, 20, 40, 59]
    )
    with w.db.write() as conn:
        refs = w.assets.reference_set(conn, [camera])
        root = Analysis(w).plan(conn, camera, selected, refs, "sparse")

    def omit(request):
        response = fake_visual(request, person)
        body = response.json()
        result = json.loads(body["choices"][0]["message"]["content"])
        result["minutes"] = result["minutes"][:1]
        body["choices"][0]["message"]["content"] = json.dumps(result)
        return httpx.Response(200, json=body)

    client = AIClient(w, httpx.MockTransport(omit))
    try:
        await drain(w, Executor(w, Supervisor(w), client))
    finally:
        await client.close()
    with w.db.read() as conn:
        minutes = list(
            conn.execute(select(s.minute_records).order_by(s.minute_records.c.start_at_ms)).mappings()
        )
        good, bad = [Records(w).minute(conn, m["id"]) for m in minutes]
        assert good["analysis_state"] == "complete" and good["coverage"]["classified_ms"] == 60000
        assert bad["analysis_state"] == "failed" and bad["coverage"]["unanalysed_failed_ms"] == 60000
        assert (
            bad["sampling"]["submitted_frame_count"] == 4 and bad["sampling"]["successful_frame_count"] == 0
        )
        assert one(conn, s.jobs, root["id"])["state"] == "partial"


async def test_B07_rule_change_reprojects_expired_evidence_without_model(w, clock, jpeg):
    person, camera, start, frames = await initial_sparse(w, clock, jpeg)
    clock.advance(61 * 60000)
    Cleaner(w).run()
    with w.db.write() as conn:
        settings = one(conn, s.settings, "singleton")
        conn.execute(
            update(s.settings).values(
                config_json=settings["config_json"]
                | {
                    "max_inference_gap_seconds": 10,
                    "sparse_sample_offsets_seconds": [0, 10, 20, 30, 40, 50, 59],
                }
            )
        )
        requests = len(list(conn.execute(select(s.ai_usage))))
    reproject(w, {"max_inference_gap_seconds": 10})
    with w.db.read() as conn:
        result = w.ledger.calculate(
            conn, start, start + 60000, {"camera_ids": [camera], "subject_id": person}
        )
        assert result["coverage"]["classified_ms"] == 4000
        assert result["coverage"]["unanalysed_expired_ms"] == 56000
        assert len(list(conn.execute(select(s.ai_usage)))) == requests
    with w.db.write() as conn:
        settings = one(conn, s.settings, "singleton")
        conn.execute(
            update(s.settings).values(config_json=settings["config_json"] | {"max_inference_gap_seconds": 30})
        )
    reproject(w, {"max_inference_gap_seconds": 30})
    with w.db.read() as conn:
        assert (
            w.ledger.calculate(conn, start, start + 60000, {"camera_ids": [camera], "subject_id": person})[
                "coverage"
            ]["classified_ms"]
            == 60000
        )
        assert len(list(conn.execute(select(s.ai_usage)))) == requests


async def test_B10_source_changed_during_model_call_queues_current_recalculation(w, clock, jpeg):
    make_provider(w)
    person, asset = make_person(w, jpeg)
    camera = make_camera(w)
    start = w.now()
    minute = make_minute(w, camera, start, activity="reading", subject=person)
    with w.db.write() as conn:
        summary = Summaries(w).ensure(
            conn, "hour", start, start + 60000, {"camera_ids": [camera], "subject_id": person}
        )
        enqueue(
            conn,
            w.now(),
            "summarize_hour",
            Summaries(w).job_payload(conn, summary, w.settings_config(conn)),
            "stale-model-input",
        )
    calls = 0

    def respond(request):
        nonlocal calls
        body = json.loads(request.content)
        context = json.loads(body["messages"][1]["content"][-1]["text"])
        narrative = "阅读 1 分钟。" if calls == 0 else "书写 1 分钟。"
        if calls == 0:
            with w.db.write() as conn:
                previous = (
                    conn.execute(select(s.minute_revisions).where(s.minute_revisions.c.minute_id == minute))
                    .mappings()
                    .one()
                )
                conn.execute(
                    s.minute_revisions.insert().values(
                        **(dict(previous) | {"revision": 2, "source_fingerprint": "new-source"})
                    )
                )
                span = (
                    conn.execute(select(s.activity_spans).where(s.activity_spans.c.minute_id == minute))
                    .mappings()
                    .one()
                )
                conn.execute(
                    s.activity_spans.insert().values(
                        **(dict(span) | {"id": new_id("span"), "minute_revision": 2, "activity": "writing"})
                    )
                )
                conn.execute(
                    update(s.minute_records).where(s.minute_records.c.id == minute).values(current_revision=2)
                )
                w.ledger.advance(conn)
                w.ledger.invalidate(conn, start, start + 60000)
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "narrative": narrative,
                                    "highlights": [],
                                    "source_refs": context["source_refs"],
                                }
                            )
                        },
                    }
                ]
            },
        )

    client = AIClient(w, httpx.MockTransport(respond))
    executor = Executor(w, Supervisor(w), client)
    try:
        await executor.execute(w.queue.claim("worker"))
        with w.db.read() as conn:
            current = Summaries(w).get(conn, summary["id"])
            assert current["source_state"] == "stale" and current["pending_job_id"]
            assert current["activities"] == [{"activity": "writing", "duration_ms": 60000}]
        clock.advance(60000)
        await executor.execute(w.queue.claim("worker"))
        with w.db.read() as conn:
            current = Summaries(w).get(conn, summary["id"])
            assert current["source_state"] == "current" and current["narrative"] == "书写 1 分钟。"
    finally:
        await client.close()
