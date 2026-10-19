from sqlalchemy import update

from app.analysis.normalize import normalize
from app.repositories import schema as s
from app.repositories.db import new_id
from app.timeutils import iso
from app.worker.queue import enqueue
from tests.conftest import make_camera, make_minute, make_provider


def _observation(activity, ids, subject_id=None, identity_status="unknown"):
    return {
        "subject_id": subject_id,
        "identity_status": identity_status,
        "activity": activity,
        "labels": [],
        "object_ids": [],
        "start_evidence_id": ids[0],
        "end_evidence_id": ids[-1],
        "supporting_evidence_ids": ids,
        "observation": activity,
        "needs_dense_review": False,
    }


def _output(start, observations):
    return {
        "minutes": [
            {
                "minute_start": iso(start),
                "observations": observations,
                "unresolved_evidence_ids": [],
                "review_reasons": [],
            }
        ]
    }


def test_matching_edges_infer_missing_middle_without_changing_capture_fact():
    frames = [{"id": str(i), "captured_at": iso(i * 1000)} for i in list(range(10)) + list(range(50, 60))]
    captured = [(0, 10000), (50000, 60000)]
    ids = [frame["id"] for frame in frames]

    spans, review = normalize(
        [_output(0, [_observation("writing", ids)])], frames, captured, support="dense"
    )

    assert not review
    assert sum(span["end_at_ms"] - span["start_at_ms"] for span in spans) == 60000
    estimated = [span for span in spans if span["details_json"]["inference_mode"] == "edge_estimated"]
    assert [(span["start_at_ms"], span["end_at_ms"]) for span in estimated] == [(10000, 50000)]
    assert estimated[0]["details_json"]["estimated_ms"] == 40000


def test_other_activity_blocks_edge_inference():
    frames = [{"id": str(i), "captured_at": iso(i * 1000)} for i in (0, 59, 30)]
    spans, _ = normalize(
        [
            _output(
                0,
                [
                    _observation("writing", ["0", "59"]),
                    _observation("reading", ["30"]),
                ],
            )
        ],
        frames,
        [(0, 1000), (30000, 31000), (59000, 60000)],
    )

    assert not any(span["details_json"]["inference_mode"] == "edge_estimated" for span in spans)


def test_edge_evidence_in_different_minutes_does_not_infer_across_boundary():
    frames = [
        {"id": "left", "captured_at": iso(59000)},
        {"id": "right", "captured_at": iso(60000)},
    ]
    spans, _ = normalize(
        [
            _output(0, [_observation("writing", ["left"])]),
            _output(60000, [_observation("writing", ["right"])]),
        ],
        frames,
        [(59000, 60000), (60000, 61000)],
    )

    assert not any(span["details_json"]["inference_mode"] == "edge_estimated" for span in spans)


def test_matching_activity_does_not_fill_a_two_minute_capture_gap():
    first = [str(i) for i in range(10)]
    last = [f"later-{i}" for i in range(10)]
    frames = [
        {"id": str(i), "captured_at": iso(i * 1000)} for i in range(10)
    ] + [
        {"id": f"later-{i}", "captured_at": iso(130000 + i * 1000)} for i in range(10)
    ]
    spans, _ = normalize(
        [
            _output(0, [_observation("writing", first)]),
            _output(120000, [_observation("writing", last)]),
        ],
        frames,
        [(0, 10000), (130000, 140000)],
    )

    assert sum(span["end_at_ms"] - span["start_at_ms"] for span in spans) == 20000
    assert not any(span["details_json"]["inference_mode"] == "edge_estimated" for span in spans)


def test_ledger_reports_capture_gap_and_inferred_activity_separately(w, clock):
    camera = make_camera(w)
    start = clock.value
    captured_bitmap = sum(1 << i for i in list(range(10)) + list(range(50, 60)))
    minute_id = make_minute(w, camera, start, captured=captured_bitmap, state="complete")
    with w.db.write() as conn:
        conn.execute(
            s.minute_revisions.insert().values(
                minute_id=minute_id,
                revision=1,
                source_kind="ai",
                source_fingerprint="inference-test",
                reference_set_id=None,
                result_json={"analysis_state": "complete", "analysis_mode": "dense"},
                created_at_ms=w.now(),
            )
        )
        conn.execute(
            update(s.minute_records)
            .where(s.minute_records.c.id == minute_id)
            .values(current_revision=1)
        )
        for lo, hi, inferred in ((0, 10000, False), (10000, 50000, True), (50000, 60000, False)):
            conn.execute(
                s.activity_spans.insert().values(
                    id=new_id("span"),
                    camera_id=camera,
                    minute_id=minute_id,
                    minute_revision=1,
                    subject_id=None,
                    identity_status="unknown",
                    start_at_ms=start + lo,
                    end_at_ms=start + hi,
                    activity="writing",
                    origin="ai",
                    support="dense",
                    boundary_json={},
                    evidence_ids_json=[],
                    details_json={
                        "labels": [],
                        "object_ids": [],
                        "observation": "写字",
                        "inference_mode": "edge_estimated" if inferred else "observed",
                    },
                )
            )
        result = w.ledger.calculate(
            conn, start, start + 60000, {"camera_ids": [camera], "subject_id": None}
        )

    assert result["coverage"]["captured_ms"] == 20000
    assert result["coverage"]["gap_ms"] == 40000
    assert result["coverage"]["inferred_ms"] == 40000
    assert result["activities"] == [{"activity": "writing", "duration_ms": 60000, "estimated_ms": 40000}]


def test_legacy_minute_decision_job_is_retired_without_model_call(w):
    provider_id = make_provider(w)
    with w.db.write() as conn:
        conn.execute(s.providers.update().where(s.providers.c.id == provider_id).values(paused=True))
        job = enqueue(
            conn,
            w.now(),
            "minute_decision",
            {"provider_id": provider_id},
            "minute-decision-test",
            total=1,
        )
    assert w.queue.claim("test-worker") is None
    with w.db.read() as conn:
        row = conn.execute(s.jobs.select().where(s.jobs.c.id == job["id"])).mappings().one()
        attempts = conn.execute(s.job_attempts.select().where(s.job_attempts.c.job_id == job["id"])).all()
    assert row["state"] == "succeeded"
    assert row["result_json"]["status"] == "skipped"
    assert attempts == []
