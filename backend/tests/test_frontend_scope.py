from sqlalchemy import select

from app.repositories import schema as s
from app.schemas import AnalysisCreate, AnalysisRequest
from app.services.analysis import Analysis
from app.timeutils import iso
from tests.conftest import make_camera, make_provider, make_minute
from tests.test_analysis import make_person


def test_records_only_keeps_selected_subject_in_preview_and_job(w, jpeg):
    make_provider(w)
    person, _ = make_person(w, jpeg)
    camera = make_camera(w)
    body = dict(
        mode="records_only", camera_ids=[camera], subject_id=person,
        **{"from": iso(w.now() - 60000), "to": iso(w.now())},
    )
    with w.db.write() as conn:
        preview, _, _ = Analysis(w).preview(conn, AnalysisRequest.model_validate(body))
        created = Analysis(w).create(
            conn, AnalysisCreate.model_validate(body | {"preview_token": preview["preview_token"]})
        )
        job = conn.execute(select(s.jobs).where(s.jobs.c.id == created["id"])).mappings().one()
        assert job["payload_json"]["subject_id"] == person
        assert job["payload_json"]["camera_ids"] == [camera]


def test_browser_timeline_numeric_query_and_latest_first_pagination(client, w, clock):
    camera = make_camera(w)
    start = w.now()
    make_minute(w, camera, start, activity="reading")
    make_minute(w, camera, start + 120000, activity="writing")
    with w.db.write() as conn:
        w.ledger.project(conn)
    clock.advance(180000)
    query = {"from": iso(start), "to": iso(start + 180000), "bucket_seconds": "300"}
    timeline = client.get("/api/timeline", params=query)
    assert timeline.status_code == 200, timeline.text
    assert timeline.json()["data"]["bucket_seconds"] == 300
    first = client.get("/api/events", params={"from": iso(start), "to": iso(start + 180000), "limit": 1})
    assert first.status_code == 200, first.text
    assert first.json()["data"][0]["activity"] == "writing"
    second = client.get("/api/events", params={"from": iso(start), "to": iso(start + 180000), "limit": 1, "cursor": first.json()["page"]["next_cursor"]})
    assert second.status_code == 200, second.text
    assert second.json()["data"][0]["activity"] == "reading"
