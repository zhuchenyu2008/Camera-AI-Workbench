from __future__ import annotations

import pytest
from sqlalchemy import select

from app.analysis.temporal import (
    LocalObservation,
    PublishedTimeline,
    TemporalToolRequest,
    validate_local_observation,
    validate_published_timeline,
    validate_tool_request,
)
from app.errors import AppError
from app.repositories import schema as s
from app.repositories.db import new_id
from app.services.analysis import Analysis
from app.timeutils import timestamp
from app.worker.queue import enqueue
from tests.conftest import make_camera, make_provider

SLICE_START = "2026-10-17T12:00:00+08:00"
SLICE_END = "2026-10-17T12:00:20+08:00"


def local(**overrides):
    value = {
        "slice_id": "slice-1",
        "slice_start": SLICE_START,
        "slice_end": SLICE_END,
        "coverage": {"mode": "dense", "evidence_ids": ["ev-1"]},
        "observations": [
            {
                "activity": "reading",
                "start_at": "2026-10-17T12:00:01+08:00",
                "end_at": "2026-10-17T12:00:19+08:00",
                "certainty": "direct",
                "evidence_ids": ["ev-1"],
                "observation": "可见人物在活动。",
            }
        ],
    }
    value.update(overrides)
    return LocalObservation.model_validate(value)


def test_local_observation_accepts_only_snapshot_bounds_and_evidence():
    output = local()
    assert validate_local_observation(
        output,
        slice_id="slice-1",
        start_ms=timestamp(SLICE_START),
        end_ms=timestamp(SLICE_END),
        evidence_ids=["ev-1"],
    ) is output


def test_local_observation_rejects_external_evidence_and_outside_range():
    with pytest.raises(AppError, match="INVALID_TEMPORAL_OUTPUT"):
        validate_local_observation(
            local(coverage={"mode": "dense", "evidence_ids": ["ev-external"]}),
            slice_id="slice-1",
            start_ms=timestamp(SLICE_START),
            end_ms=timestamp(SLICE_END),
            evidence_ids=["ev-1"],
        )
    with pytest.raises(ValueError, match="越过时间片边界"):
        local(
            observations=[
                {
                    "activity": "reading",
                    "start_at": "2026-10-17T11:59:59+08:00",
                    "end_at": "2026-10-17T12:00:02+08:00",
                    "certainty": "direct",
                    "evidence_ids": ["ev-1"],
                    "observation": "越界",
                }
            ]
        )


def test_published_timeline_rejects_external_sources():
    timeline = PublishedTimeline.model_validate(
        {
            "review_run_id": "review-1",
            "reviewed_from": SLICE_START,
            "reviewed_to": SLICE_END,
            "segments": [
                {
                    "activity": "reading",
                    "start_at": "2026-10-17T12:00:01+08:00",
                    "end_at": "2026-10-17T12:00:19+08:00",
                    "certainty": "mixed",
                    "evidence_ids": ["ev-1"],
                    "source_slice_ids": ["slice-1"],
                    "reason": "相邻片段一致",
                }
            ],
        }
    )
    assert validate_published_timeline(
        timeline,
        review_run_id="review-1",
        start_ms=timestamp(SLICE_START),
        end_ms=timestamp(SLICE_END),
        evidence_ids=["ev-1"],
        slice_ids=["slice-1"],
    ) is timeline
    with pytest.raises(AppError, match="INVALID_TEMPORAL_OUTPUT"):
        validate_published_timeline(
            timeline,
            review_run_id="review-1",
            start_ms=timestamp(SLICE_START),
            end_ms=timestamp(SLICE_END),
            evidence_ids=["ev-other"],
            slice_ids=["slice-1"],
        )


def test_tool_request_shape_and_allowlist():
    request = TemporalToolRequest.model_validate(
        {"action": "tool", "tool_name": "get_checkpoint", "arguments": {}}
    )
    validate_tool_request(request, allowed_tools={"get_checkpoint"})
    with pytest.raises(AppError, match="TEMPORAL_TOOL_DENIED"):
        validate_tool_request(request, allowed_tools=set())


def test_temporal_review_state_tracks_queued_job_terminal_transitions(w, clock):
    camera_id = make_camera(w)
    review_ids = []
    with w.db.write() as conn:
        for suffix, deadline in (("cancel", clock.value + 60_000), ("expire", clock.value - 1)):
            review_id = new_id("review")
            job = enqueue(
                conn,
                clock.value,
                "temporal_review",
                {"camera_id": camera_id, "review_run_id": review_id},
                f"temporal-state-{suffix}",
                deadline=deadline,
            )
            conn.execute(
                s.temporal_review_runs.insert().values(
                    id=review_id,
                    camera_id=camera_id,
                    job_id=job["id"],
                    base_checkpoint_id=None,
                    reviewed_from_ms=clock.value - 20_000,
                    reviewed_to_ms=clock.value,
                    trigger_kind="rolling",
                    trigger_reason="test",
                    checkpoint_revision=0,
                    tool_protocol_version="temporal-review-1",
                    schema_version="published-timeline-1",
                    budget_json={},
                    status="queued",
                    request_fingerprint=f"fingerprint-{suffix}",
                    result_json=None,
                    error_json=None,
                    created_at_ms=clock.value,
                    updated_at_ms=clock.value,
                    finished_at_ms=None,
                )
            )
            review_ids.append((review_id, job))
        w.queue.cancel(conn, review_ids[0][1])
    with w.db.read() as conn:
        cancelled = conn.execute(
            select(s.temporal_review_runs.c.status).where(s.temporal_review_runs.c.id == review_ids[0][0])
        ).scalar_one()
    assert cancelled == "cancelled"

    assert w.queue.claim("worker") is None
    with w.db.read() as conn:
        expired = conn.execute(
            select(s.temporal_review_runs.c.status).where(s.temporal_review_runs.c.id == review_ids[1][0])
        ).scalar_one()
    assert expired == "expired"


def test_stopped_camera_schedules_finalize_from_latest_checkpoint(w, clock):
    camera_id = make_camera(w)
    provider_id = make_provider(w)
    review_id = new_id("review")
    checkpoint_id = new_id("checkpoint")
    start, end = clock.value - 60_000, clock.value
    with w.db.write() as conn:
        conn.execute(
            s.camera_runtime.update()
            .where(s.camera_runtime.c.camera_id == camera_id)
            .values(capture_state="stopped", stop_reason="outside_schedule", stream_session_id=None)
        )
        conn.execute(
            s.temporal_review_runs.insert().values(
                id=review_id,
                camera_id=camera_id,
                job_id=None,
                base_checkpoint_id=None,
                reviewed_from_ms=start,
                reviewed_to_ms=end,
                trigger_kind="rolling",
                trigger_reason="regular_rollup",
                checkpoint_revision=0,
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
        conn.execute(
            s.temporal_checkpoints.insert().values(
                id=checkpoint_id,
                camera_id=camera_id,
                review_run_id=review_id,
                revision=1,
                reviewed_from_ms=start,
                reviewed_to_ms=end,
                frozen_to_ms=start,
                mutable_tail_start_ms=start,
                timeline_json={"segments": []},
                local_slice_ids_json=[],
                evidence_ids_json=[],
                hard_boundaries_json=[],
                conflicts_json=[],
                unresolved_json=[],
                source_fingerprint=new_id("fingerprint"),
                status="complete",
                next_review_at_ms=None,
                trigger_reason="regular_rollup",
                created_at_ms=end,
            )
        )
        provider = conn.execute(
            select(s.providers).where(s.providers.c.id == provider_id)
        ).mappings().one()
        Analysis(w).schedule_temporal(conn, dict(provider))
        finalize = conn.execute(
            select(s.temporal_review_runs).where(
                s.temporal_review_runs.c.camera_id == camera_id,
                s.temporal_review_runs.c.trigger_kind == "finalize",
            )
        ).mappings().one()
        job = conn.execute(select(s.jobs).where(s.jobs.c.id == finalize["job_id"])).mappings().one()
    assert job["kind"] == "temporal_finalize"
    assert job["state"] == "queued"
    assert finalize["status"] == "queued"
