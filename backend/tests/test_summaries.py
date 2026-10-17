from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select, update

from app.analysis.client import AIClient
from app.capture.supervisor import Supervisor
from app.errors import AppError
from app.repositories import schema as s
from app.repositories.db import one
from app.schemas import SummaryDto, SummaryOutput
from app.summaries.service import Summaries, validate_summary
from app.timeutils import iso
from app.worker.executor import Executor
from app.worker.queue import enqueue
from tests.conftest import make_camera, make_minute, make_provider
from tests.test_analysis import make_person


async def test_B10_summary_refs_failure_preserves_narrative_and_statistics(w, jpeg, clock):
    provider = make_provider(w)
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
            {"summary_id": summary["id"], "provider_id": provider},
            "summary-test",
        )
    sent = []

    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        content = body["messages"][1]["content"]
        assert sum(c["type"] == "image_url" for c in content) == 1
        assert any("本人" in c.get("text", "") for c in content)
        context = json.loads(content[-1]["text"])
        result = {"narrative": "阅读 1 分钟。", "highlights": [], "source_refs": context["source_refs"]}
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}
        )

    client = AIClient(w, httpx.MockTransport(respond))
    try:
        await Executor(w, Supervisor(w), client).execute(w.queue.claim("worker"))
    finally:
        await client.close()
    with w.db.write() as conn:
        result = Summaries(w).get(conn, summary["id"])
        SummaryDto.model_validate(result)
        assert result["narrative"] == "阅读 1 分钟。" and result["coverage"]["classified_ms"] == 60000
        assert result["source_state"] == "current"
        old_revision = result["revision"]
        conn.execute(
            update(s.activity_spans).where(s.activity_spans.c.minute_id == minute).values(activity="writing")
        )
        w.ledger.advance(conn)
        affected = w.ledger.invalidate(conn, start, start + 60000)
        assert affected == [summary["id"]]
        assert Summaries(w).get(conn, summary["id"])["source_state"] == "stale"
        for _ in range(3):
            w.ledger.invalidate(conn, start, start + 60000)
        queued = list(conn.execute(select(s.jobs).where(s.jobs.c.state == "queued")))
        assert len(queued) == 1
        assert Summaries(w).get(conn, summary["id"], old_revision)["activities"] == [
            {"activity": "reading", "duration_ms": 60000}
        ]
    clock.advance(60000)
    client = AIClient(w, httpx.MockTransport(lambda r: httpx.Response(400)))
    try:
        await Executor(w, Supervisor(w), client).execute(w.queue.claim("worker"))
    finally:
        await client.close()
    with w.db.read() as conn:
        latest = Summaries(w).get(conn, summary["id"])
        assert latest["narrative"] == "阅读 1 分钟。"
        assert latest["summary_state"] == "failed"
        assert latest["activities"] == [{"activity": "writing", "duration_ms": 60000}]
        assert (
            latest["narrative_source_fingerprint"]
            != conn.execute(
                select(s.summary_revisions.c.source_fingerprint).where(
                    s.summary_revisions.c.summary_id == summary["id"],
                    s.summary_revisions.c.revision == latest["revision"],
                )
            ).scalar_one()
        )


def test_B10_summary_rejects_wrong_totals_and_foreign_references():
    statistics = {
        "coverage": {"planned_ms": 60000},
        "activities": [{"activity": "reading", "duration_ms": 60000}],
    }
    wrong = SummaryOutput(narrative="阅读 2 分钟。", highlights=[], source_refs=[])
    with pytest.raises(AppError, match="INVALID_MODEL_OUTPUT"):
        validate_summary(wrong, [], statistics)
    wrong = SummaryOutput.model_validate(
        {
            "narrative": "记录可见活动。",
            "highlights": [],
            "source_refs": [
                {
                    "kind": "minute",
                    "id": "fake",
                    "revision": 1,
                    "start_at": "2026-10-17T12:00:00Z",
                    "end_at": "2026-10-17T12:01:00Z",
                }
            ],
        }
    )
    with pytest.raises(AppError, match="INVALID_MODEL_REFERENCES"):
        validate_summary(wrong, [], statistics)


def test_B10_provisional_and_beijing_hour_day_backfill(w, clock):
    make_provider(w)
    camera = make_camera(w)
    start = w.now()
    make_minute(w, camera, start, state="pending")
    clock.advance(25 * 3600000)
    with w.db.write() as conn:
        Summaries(w).schedule(conn)
        rows = list(conn.execute(select(s.summaries)).mappings())
        assert any(row["type"] == "hour" for row in rows)
        # 调度有界补齐，继续一轮完成日报。
        Summaries(w).schedule(conn)
        rows = list(conn.execute(select(s.summaries)).mappings())
        days = [row for row in rows if row["type"] == "day"]
        assert days
        assert iso(days[0]["start_at_ms"]).endswith("16:00:00.000Z")


async def test_B05_provider_capabilities_use_real_test_images(w):
    provider = make_provider(w)
    with w.db.write() as conn:
        enqueue(
            conn,
            w.now(),
            "provider_test",
            {"provider_id": provider, "provider_revision": 1},
            "provider-capability",
        )
    requests = []

    def respond(request):
        import base64
        import io

        from PIL import Image

        body = json.loads(request.content)
        content = body["messages"][1]["content"]
        requests.append(body)
        if "image_labels" in body["messages"][0]["content"]:
            colors = []
            for part in content:
                if part["type"] == "image_url":
                    raw = base64.b64decode(part["image_url"]["url"].split(",")[1])
                    r, g, b = Image.open(io.BytesIO(raw)).getpixel((0, 0))
                    colors.append("red" if r > b else "blue")
            output = {"image_labels": colors}
        else:
            output = {"narrative": "合成统计显示阅读。", "highlights": [], "source_refs": []}
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}]}
        )

    client = AIClient(w, httpx.MockTransport(respond))
    try:
        await Executor(w, Supervisor(w), client).execute(w.queue.claim("worker"))
    finally:
        await client.close()
    assert len(requests) == 4
    with w.db.read() as conn:
        result = one(conn, s.providers, provider)
        assert result["tested_config_revision"] == 1
        assert result["capabilities_json"]["verified_max_images"] == 32
        assert result["capabilities_json"]["verified_max_body_bytes"] is None
