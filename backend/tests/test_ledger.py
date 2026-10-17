from __future__ import annotations

import random

from sqlalchemy import select, update

from app.repositories import schema as s
from app.repositories.db import new_id
from app.schemas import EventCorrection
from tests.conftest import make_camera, make_minute


def statistics(w, start, end, cameras, subject="me"):
    with w.db.read() as conn:
        return w.ledger.calculate(conn, start, end, {"camera_ids": cameras, "subject_id": subject})


def test_B07_union_conflict_away_and_partition_conservation(w):
    a, b = make_camera(w), make_camera(w)
    start = w.now()
    make_minute(w, a, start, activity="reading", subject="me")
    make_minute(w, b, start, activity="reading", subject="me")
    result = statistics(w, start, start + 60000, [a, b])
    assert result["coverage"]["classified_ms"] == 60000
    assert result["activities"] == [{"activity": "reading", "duration_ms": 60000}]
    with w.db.write() as conn:
        conn.execute(
            update(s.activity_spans).where(s.activity_spans.c.camera_id == b).values(activity="writing")
        )
    result = statistics(w, start, start + 60000, [a, b])
    assert result["coverage"]["unknown_ms"] == result["coverage"]["conflict_ms"] == 60000
    assert result["activities"] == []
    with w.db.write() as conn:
        conn.execute(
            update(s.activity_spans).where(s.activity_spans.c.camera_id == b).values(activity="away")
        )
    assert statistics(w, start, start + 60000, [a, b])["coverage"]["classified_ms"] == 60000
    with w.db.write() as conn:
        conn.execute(
            update(s.activity_spans).where(s.activity_spans.c.camera_id == a).values(activity="away")
        )
        minute_b = conn.execute(
            select(s.minute_records.c.id).where(s.minute_records.c.camera_id == b)
        ).scalar_one()
        conn.execute(
            update(s.minute_capture).where(s.minute_capture.c.minute_id == minute_b).values(captured_bitmap=0)
        )
    assert statistics(w, start, start + 60000, [a, b])["coverage"]["unknown_ms"] == 60000


def test_B07_random_capture_and_splits_conserve(w):
    rng = random.Random(20261017)
    cameras = [make_camera(w), make_camera(w)]
    start = w.now()
    for index in range(30):
        for camera in cameras:
            bitmap = sum((1 << i) for i in range(60) if rng.random() < 0.9)
            make_minute(
                w,
                camera,
                start + index * 60000,
                captured=bitmap,
                activity=rng.choice(["reading", "writing", None]),
                subject="me",
            )
    whole = statistics(w, start, start + 1800000, cameras)
    parts = [
        statistics(w, start + i * 137000, min(start + (i + 1) * 137000, start + 1800000), cameras)
        for i in range(14)
    ]
    for key in whole["coverage"]:
        assert whole["coverage"][key] == sum(p["coverage"][key] for p in parts), key
    c = whole["coverage"]
    assert c["captured_ms"] == c["planned_ms"] - c["gap_ms"]
    assert sum(v["duration_ms"] for v in whole["activities"]) == c["classified_ms"]


def test_B07_expired_analysis_not_away_and_identity_uncertain(w):
    camera = make_camera(w)
    start = w.now()
    make_minute(w, camera, start, state="expired")
    result = statistics(w, start, start + 60000, [camera])
    assert result["coverage"]["unanalysed_expired_ms"] == 60000
    assert result["activities"] == []
    make_minute(w, camera, start + 60000, activity="reading", subject="me", identity="possible")
    assert statistics(w, start + 60000, start + 120000, [camera])["coverage"]["unknown_ms"] == 60000


def test_B01_schedule_revision_does_not_rewrite_history(w, clock):
    camera = make_camera(w)
    start = w.now()
    clock.advance(60000)
    with w.db.write() as conn:
        row = dict(conn.execute(select(s.cameras).where(s.cameras.c.id == camera)).mappings().one())
        new = row | {"enabled": False}
        w.save_schedule(conn, new)
    with w.db.read() as conn:
        assert w.ledger.planned(conn, camera, start, start + 120000) == [(start, start + 60000)]


def test_B07_events_interruptions_and_manual_correction(w, clock):
    camera = make_camera(w)
    start = w.now()
    minute = make_minute(w, camera, start, activity="reading", subject=None)
    with w.db.write() as conn:
        span = (
            conn.execute(select(s.activity_spans).where(s.activity_spans.c.minute_id == minute))
            .mappings()
            .one()
        )
        conn.execute(
            update(s.activity_spans)
            .where(s.activity_spans.c.id == span["id"])
            .values(end_at_ms=start + 20000)
        )
        extra = dict(span) | {"id": new_id("span"), "start_at_ms": start + 40000, "end_at_ms": start + 60000}
        conn.execute(s.activity_spans.insert().values(**extra))
        w.ledger.project(conn)
        event_id = conn.execute(select(s.events.c.id)).scalar_one()
        event = w.ledger.event(conn, event_id)
        assert event["duration_ms"] == 40000
        assert sum(item["duration_ms"] for item in event["interruptions"]) == 20000
        dto = EventCorrection.model_validate(
            {
                "base_source_revision": event["source_revision"],
                "intervals": [
                    {
                        "subject_id": None,
                        "activity": "writing",
                        "start_at": event["start_at"],
                        "end_at": event["end_at"],
                    }
                ],
                "reason": "修正活动",
            }
        )
        result = w.ledger.correct_event(conn, event_id, dto, f'"rev-{event["revision"]}"')
        assert result["projection_revision"] > event["source_revision"]
    assert statistics(w, start, start + 60000, [camera], None)["activities"] == [
        {"activity": "writing", "duration_ms": 60000}
    ]


def test_B07_projected_events_do_not_double_classify_conflict(w):
    cameras = [make_camera(w), make_camera(w)]
    start = w.now()
    make_minute(w, cameras[0], start, activity="reading", subject="me")
    make_minute(w, cameras[1], start, activity="writing", subject="me")
    with w.db.write() as conn:
        w.ledger.project(conn)
        events = [w.ledger.event(conn, row.id) for row in conn.execute(select(s.events.c.id))]
    assert not any(e["activity"] in {"reading", "writing"} and e["duration_ms"] for e in events)
    assert any(e["activity"] == "unknown" for e in events)
