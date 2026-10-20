from sqlalchemy import update

from app.repositories import schema as s
from app.repositories.db import new_id
from app.timeutils import iso
from tests.conftest import make_camera, make_minute


def test_timeline_exposes_published_temporal_result(client, w, clock):
    camera = make_camera(w)
    start = clock.value - 60_000
    end = clock.value
    make_minute(w, camera, start, activity="writing")
    review_id = new_id("review")
    checkpoint_id = new_id("checkpoint")
    timeline_id = new_id("timeline")
    with w.db.write() as conn:
        conn.execute(
            s.temporal_review_runs.insert().values(
                id=review_id,
                camera_id=camera,
                job_id=None,
                base_checkpoint_id=None,
                reviewed_from_ms=start,
                reviewed_to_ms=end,
                trigger_kind="rolling",
                trigger_reason="regular_rollup",
                checkpoint_revision=1,
                tool_protocol_version="temporal-review-1",
                schema_version="published-timeline-1",
                budget_json={},
                status="succeeded",
                request_fingerprint=new_id("fingerprint"),
                result_json={},
                error_json=None,
                created_at_ms=start,
                updated_at_ms=end,
                finished_at_ms=end,
            )
        )
        segments = [
            {
                "activity": "reading",
                "start_at": iso(start + 10_000),
                "end_at": iso(end - 10_000),
                "certainty": "mixed",
                "evidence_ids": ["ev-1"],
                "source_slice_ids": ["slice-1"],
                "reason": "相邻时间片观察一致",
            }
        ]
        conn.execute(
            s.temporal_checkpoints.insert().values(
                id=checkpoint_id,
                camera_id=camera,
                review_run_id=review_id,
                revision=1,
                reviewed_from_ms=start,
                reviewed_to_ms=end,
                frozen_to_ms=start + 30_000,
                mutable_tail_start_ms=start + 30_000,
                timeline_json={"segments": segments},
                local_slice_ids_json=["slice-1"],
                evidence_ids_json=["ev-1"],
                hard_boundaries_json=[],
                conflicts_json=[],
                unresolved_json=[],
                source_fingerprint=new_id("fingerprint"),
                status="complete",
                next_review_at_ms=end + 120_000,
                trigger_reason="regular_rollup",
                created_at_ms=end,
            )
        )
        conn.execute(
            s.published_timelines.insert().values(
                id=timeline_id,
                camera_id=camera,
                checkpoint_id=checkpoint_id,
                version=1,
                reviewed_from_ms=start,
                reviewed_to_ms=end,
                segments_json=segments,
                unresolved_json=[],
                source_fingerprint=new_id("fingerprint"),
                status="published",
                is_current=True,
                published_at_ms=end,
                created_at_ms=end,
            )
        )
    response = client.get(
        "/api/timeline",
        params={"from": iso(start), "to": iso(end), "camera_ids": camera, "bucket_seconds": 60},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["published_timeline"]["revision"] == 1
    assert data["published_timeline"]["frozen_to"] == iso(start + 30_000)
    assert data["temporal_review"]["state"] == "complete"
    assert data["buckets"][0]["activities"] == [{"activity": "reading", "duration_ms": 40_000}]

    with w.db.write() as conn:
        conn.execute(update(s.published_timelines).where(s.published_timelines.c.id == timeline_id).values(segments_json=[]))
    empty_response = client.get(
        "/api/timeline",
        params={"from": iso(start), "to": iso(end), "camera_ids": camera, "bucket_seconds": 60},
    )
    assert empty_response.status_code == 200, empty_response.text
    assert empty_response.json()["data"]["buckets"][0]["activities"] == []
