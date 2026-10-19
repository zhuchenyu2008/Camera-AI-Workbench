from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import event, select, update

from app.activities.repair import repair_history
from app.analysis.client import AIClient
from app.analysis.normalize import normalize, validate_visual
from app.capture.supervisor import Supervisor
from app.errors import AppError
from app.repositories import schema as s
from app.schemas import EntitySplit, VisualOutput
from app.services.analysis import Analysis
from app.services.discovery import Discovery
from app.services.records import Records
from app.timeutils import iso
from app.worker.cleanup import Cleaner
from app.worker.executor import Executor
from tests.conftest import make_camera, make_minute, make_provider
from tests.test_analysis import drain
from tests.test_assets import make_frame


def visual(w, evidence_id="frame", *, candidate=True):
    return VisualOutput.model_validate(
        {
            "schema_version": "1.0",
            "minutes": [
                {
                    "minute_start": iso(w.now() // 60000 * 60000),
                    "review_reasons": [],
                    "unresolved_evidence_ids": [],
                    "observations": [
                        {
                            "subject_id": None,
                            "identity_status": "unknown",
                            "activity": "睡觉",
                            "labels": ["夜间"],
                            "object_ids": [],
                            "start_evidence_id": evidence_id,
                            "end_evidence_id": evidence_id,
                            "supporting_evidence_ids": [evidence_id],
                            "observation": "人物闭眼躺在床上睡觉",
                            "needs_dense_review": False,
                            "subject_candidate_key": "person1" if candidate else None,
                        }
                    ],
                }
            ],
            "entity_candidates": [
                {
                    "key": "person1",
                    "kind": "person",
                    "name": "人物1",
                    "description": "深色上衣",
                    "evidence_id": evidence_id,
                    "crop": {"x": 0, "y": 0, "width": 1, "height": 1},
                }
            ]
            if candidate
            else [],
        }
    )


def insert_discovery_candidate(w, conn, camera_id, key, kind, *, matched_entity_id=None):
    conn.execute(
        s.discovery_candidates.insert().values(
            id=key,
            camera_id=camera_id,
            captured_at_ms=w.now(),
            expires_at_ms=w.now() + 3600000,
            candidate_json={
                "key": key,
                "kind": kind,
                "name": "人物候选" if kind == "person" else "物品候选",
                "description": "清晰可见的稳定特征",
                "evidence_id": f"evidence-{key}",
                "crop": {"x": 0, "y": 0, "width": 1, "height": 1},
                "matched_entity_id": matched_entity_id,
            },
            prepared_json=None,
            links_json=[],
            state="pending",
            created_at_ms=w.now(),
        )
    )


def insert_entity_separation(conn, left_id, right_id):
    left, right = sorted([left_id, right_id])
    conn.execute(s.entity_separations.insert().values(left_id=left, right_id=right))


def test_scene_activity_does_not_require_identity(w):
    camera = make_camera(w)
    start = w.now()
    make_minute(w, camera, start, activity="睡觉", subject=None, identity="unknown")
    with w.db.read() as conn:
        w.validate_settings_links(
            conn, w.settings_config(conn).model_copy(update={"primary_subject_id": "legacy-missing"})
        )
        scene = w.ledger.calculate(conn, start, start + 60000, {"camera_ids": [camera], "subject_id": None})
        personal = w.ledger.calculate(
            conn, start, start + 60000, {"camera_ids": [camera], "subject_id": "someone"}
        )
    assert scene["activities"] == [{"activity": "睡觉", "duration_ms": 60000}]
    assert personal["coverage"]["unknown_ms"] == 60000


def test_auto_archive_discards_legacy_object_candidates(w):
    camera = make_camera(w)
    service = Discovery(w)
    with w.db.write() as conn:
        insert_discovery_candidate(w, conn, camera, "person-a", "person")
        insert_discovery_candidate(w, conn, camera, "person-b", "person")
        insert_discovery_candidate(w, conn, camera, "object-a", "object")
        insert_discovery_candidate(w, conn, camera, "object-b", "object")
        pending = service.pending(conn)
        service.archive(
            conn,
            pending,
            [
                {"candidate_keys": ["person-a", "person-b"], "entity_id": None},
                {"candidate_keys": ["object-a", "object-b"], "entity_id": None},
            ],
        )
        entities = list(conn.execute(select(s.entities)).mappings())
        occurrences = list(conn.execute(select(s.entity_occurrences)).mappings())

    assert {row["kind"] for row in entities} == {"person"}
    assert len(entities) == 1
    by_source = {row["source_key"]: row["entity_id"] for row in occurrences}
    assert by_source["person-a"] == by_source["person-b"]
    with w.db.read() as conn:
        assert conn.execute(
            select(s.discovery_candidates.c.state).where(s.discovery_candidates.c.id == "object-a")
        ).scalar_one() == "discarded"


def test_auto_archive_uses_candidate_match_and_splits_conflicting_hints(w):
    camera = make_camera(w)
    service = Discovery(w)
    with w.db.write() as conn:
        first = service.new_entity(conn, {"kind": "person", "name": "人物一", "description": "特征一"})
        second = service.new_entity(conn, {"kind": "person", "name": "人物二", "description": "特征二"})
        insert_discovery_candidate(w, conn, camera, "matched", "person", matched_entity_id=first)
        insert_discovery_candidate(w, conn, camera, "hint-a", "person", matched_entity_id=first)
        insert_discovery_candidate(w, conn, camera, "hint-b", "person", matched_entity_id=second)
        pending = service.pending(conn)
        service.archive(
            conn,
            pending,
            [
                {"candidate_keys": ["matched"], "entity_id": None},
                {"candidate_keys": ["hint-a", "hint-b"], "entity_id": first},
            ],
        )
        occurrences = list(conn.execute(select(s.entity_occurrences)).mappings())

    by_source = {row["source_key"]: row["entity_id"] for row in occurrences}
    assert by_source["matched"] == first
    assert by_source["hint-a"] == first
    assert by_source["hint-b"] == second


def test_auto_archive_ignores_legacy_object_in_cross_kind_group_and_respects_separation(w):
    camera = make_camera(w)
    service = Discovery(w)
    with w.db.write() as conn:
        insert_discovery_candidate(w, conn, camera, "person", "person")
        insert_discovery_candidate(w, conn, camera, "object", "object")
        service.archive(
            conn,
            service.pending(conn),
            [{"candidate_keys": ["person", "object"], "entity_id": None}],
        )
        assert conn.execute(
            select(s.discovery_candidates.c.state).where(s.discovery_candidates.c.id == "object")
        ).scalar_one() == "discarded"

    with w.db.write() as conn:
        first = service.new_entity(conn, {"kind": "person", "name": "人物一", "description": "特征一"})
        second = service.new_entity(conn, {"kind": "person", "name": "人物二", "description": "特征二"})
        insert_entity_separation(conn, first, second)
        insert_discovery_candidate(w, conn, camera, "separated-a", "person", matched_entity_id=first)
        insert_discovery_candidate(w, conn, camera, "separated-b", "person", matched_entity_id=second)
        service.archive(
            conn,
            service.pending(conn),
            [{"candidate_keys": ["separated-a", "separated-b"], "entity_id": None}],
        )
        rows = list(
            conn.execute(
                select(s.entity_occurrences).where(
                    s.entity_occurrences.c.source_key.in_(["separated-a", "separated-b"])
                )
            ).mappings()
        )

    assert {row["entity_id"] for row in rows} == {first, second}


@pytest.mark.parametrize("mode", ["sparse", "dense"])
async def test_visual_to_archive_and_cross_camera_matching(w, jpeg, clock, mode):
    make_provider(w)
    calls = []
    forced_wrong_entity = [None]

    def respond(request):
        body = json.loads(request.content)
        content = body["messages"][1]["content"]
        candidates = [c["text"] for c in content if c.get("text", "").startswith("待归档候选：")]
        if candidates:
            candidate = json.loads(candidates[0].split("：", 1)[1])
            reference = json.loads(
                next(
                    c["text"].split("：", 1)[1] for c in content if c.get("text", "").startswith("已有档案：")
                )
            )
            result = {
                "groups": [
                    {
                        "candidate_keys": [candidate["key"]],
                        "entity_id": forced_wrong_entity[0] or reference["entity_id"],
                    }
                ]
            }
            calls.append("match")
        else:
            frame = json.loads(
                next(
                    c["text"].split("：", 1)[1] for c in content if c.get("text", "").startswith("现场证据：")
                )
            )
            result = visual(w, frame["evidence_id"]).model_dump(mode="json")
            calls.append("visual")
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}
        )

    client = AIClient(w, httpx.MockTransport(respond))
    try:
        for _ in range(2):
            camera, _, frame = make_frame(w, jpeg, clock)
            with w.db.write() as conn:
                Analysis(w).plan(conn, camera, [frame], w.assets.reference_set(conn, [camera]), mode)
            await drain(w, Executor(w, Supervisor(w), client))
            # 新的时间桶让第二次归档立即排队。
            with w.db.write() as conn:
                from app.worker.queue import enqueue

                if Discovery(w).pending(conn, 1):
                    enqueue(
                        conn,
                        w.now(),
                        "entity_discovery",
                        {"provider_id": w.settings_config(conn).vision_provider_id},
                        f"archive-test:{camera}",
                    )
            await drain(w, Executor(w, Supervisor(w), client))
        with w.db.read() as conn:
            entities = list(conn.execute(select(s.entities)).mappings())
            occurrences = list(conn.execute(select(s.entity_occurrences)).mappings())
            assert len(entities) == 1 and len(occurrences) == 2
            assert len({r["camera_id"] for r in occurrences}) == 2
            scope = {"camera_ids": [r["camera_id"] for r in occurrences], "subject_id": entities[0]["id"]}
            stats = w.ledger.calculate(conn, w.now(), w.now() + 60000, scope)
            assert stats["activities"] == [{"activity": "睡觉", "duration_ms": 1000}]
            for minute in conn.execute(select(s.minute_records.c.id)).scalars():
                record = Records(w).minute(conn, minute)
                assert record["segments"][0]["subject_id"] == entities[0]["id"]
            assert calls == ["visual", "visual", "match"]
            occurrence = occurrences[0]
            source_entity = entities[0]
        with w.db.write() as conn:
            split = Discovery(w).split(
                conn,
                source_entity["id"],
                EntitySplit(name="另一个人物", occurrence_ids=[occurrence["id"]]),
                f'"rev-{source_entity["revision"]}"',
            )
            frames = [
                dict(r)
                for r in conn.execute(
                    select(s.frames).where(s.frames.c.camera_id == occurrence["camera_id"])
                ).mappings()
            ]
            Analysis(w).plan(
                conn,
                occurrence["camera_id"],
                frames,
                w.assets.reference_set(conn, [occurrence["camera_id"]]),
                "dense",
                manual=True,
            )
        forced_wrong_entity[0] = source_entity["id"]
        await drain(w, Executor(w, Supervisor(w), client))
        with w.db.write() as conn:
            enqueue(
                conn,
                w.now(),
                "entity_discovery",
                {"provider_id": w.settings_config(conn).vision_provider_id},
                "archive-after-manual-split",
            )
        await drain(w, Executor(w, Supervisor(w), client))
        assert calls[-1] == "match"
        with w.db.read() as conn:
            minute_id = conn.execute(
                select(s.minute_records.c.id).where(s.minute_records.c.camera_id == occurrence["camera_id"])
            ).scalar_one()
            assert Records(w).minute(conn, minute_id)["segments"][0]["subject_id"] == split["id"]
            latest = conn.execute(
                select(s.entity_occurrences.c.entity_id).where(
                    s.entity_occurrences.c.camera_id == occurrence["camera_id"],
                    s.entity_occurrences.c.source_key != occurrence["source_key"],
                )
            ).scalar_one()
            assert latest == split["id"]
    finally:
        await client.close()


def test_open_labels_and_uncertain_identity_do_not_request_dense_review(w):
    output = visual(w, candidate=False)
    frames = [{"id": "frame", "captured_at": iso(w.now())}]
    validate_visual(output, frames, [], [])
    spans, review = normalize([output.model_dump(mode="json")], frames, [(w.now(), w.now() + 1000)])
    assert not review and spans[0]["activity"] == "睡觉"
    with pytest.raises(AppError, match="INVALID_MODEL_REFERENCES"):
        validate_visual(output, frames, [], ["学习"])
    raw = output.model_dump(mode="json")
    raw["entity_candidates"] = [{"kind": "person", "crop": {"width": -1}}]
    assert VisualOutput.model_validate(raw).minutes and not VisualOutput.model_validate(raw).entity_candidates
    wrong = visual(w)
    wrong.entity_candidates[0].kind = "object"
    Discovery.clean_output({"id": "wrong-kind"}, wrong, frames, [])
    assert wrong.minutes[0].observations[0].subject_candidate_key is None


def test_auto_archive_idempotent_and_reference_outlives_source(w, jpeg, clock):
    make_provider(w)
    camera, _, frame = make_frame(w, jpeg, clock)
    with w.db.read() as conn:
        meta = w.assets.metadata(conn, frame["id"])
    output = visual(w, frame["id"])
    job = {"id": "visual-1", "payload_json": {"provider_id": None}}
    with w.db.read() as conn:
        job["payload_json"]["provider_id"] = w.settings_config(conn).vision_provider_id
    Discovery.clean_output(job, output, [meta], [])
    service = Discovery(w)
    service.stage(job, output, [meta])
    service.stage(job, output, [meta])
    with w.db.write() as conn:
        pending = service.pending(conn)
        service.archive(conn, pending, [])
        service.archive(conn, pending, [])
        entities = list(conn.execute(select(s.entities)).mappings())
        assets = list(conn.execute(select(s.reference_assets)).mappings())
        assert len(entities) == len(assets) == 1
        assert entities[0]["origin"] == "automatic" and not entities[0]["reviewed"]
        assert conn.execute(select(s.entity_occurrences.c.id)).first()
        asset_id = assets[0]["id"]
    clock.advance(61 * 60000)
    Cleaner(w).run()
    assert w.assets.read_reference(asset_id)
    with w.db.write() as conn:
        cfg = w.settings_config(conn).model_dump() | {"auto_discovery_enabled": False}
        conn.execute(update(s.settings).values(config_json=cfg))
    service.stage({"id": "off", "payload_json": job["payload_json"]}, visual(w, frame["id"]), [meta])
    with w.db.read() as conn:
        assert len(list(conn.execute(select(s.entities)))) == 1
        assert w.assets.metadata(conn, frame["id"])["state"] == "expired"


def test_archive_merge_split_and_revision_conflict(client, w, jpeg, clock):
    provider = make_provider(w)
    _, _, frame = make_frame(w, jpeg, clock)
    service = Discovery(w)
    with w.db.read() as conn:
        meta = w.assets.metadata(conn, frame["id"])
    for n in range(2):
        output = visual(w, frame["id"])
        job = {"id": f"visual-{n}", "payload_json": {"provider_id": provider}}
        service.clean_output(job, output, [meta], [])
        service.stage(job, output, [meta])
        with w.db.write() as conn:
            service.archive(conn, service.pending(conn), [])
    with w.db.read() as conn:
        entities = [w.assets.entity(conn, e) for e in conn.execute(select(s.entities.c.id)).scalars()]
    source, target = entities
    changed = client.patch(
        f"/api/entities/{source['id']}",
        json={"name": "小明"},
        headers={"If-Match": f'"rev-{source["revision"]}"', "Idempotency-Key": "rename-auto"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["data"]["reviewed"]
    url = f"/api/entities/{source['id']}/merge"
    body = {"target_entity_id": target["id"], "target_revision": target["revision"]}
    headers = {"If-Match": f'"rev-{changed.json()["data"]["revision"]}"', "Idempotency-Key": "merge-auto"}
    merged = client.post(url, json=body, headers=headers)
    assert merged.status_code == 200, merged.text
    assert client.post(url, json=body, headers=headers).json()["data"] == merged.json()["data"]
    occurrences = client.get(f"/api/entities/{target['id']}/occurrences").json()["data"]
    assert len(occurrences) == 2
    split = client.post(
        f"/api/entities/{target['id']}/split",
        json={"name": "另一个人", "occurrence_ids": [occurrences[0]["id"]]},
        headers={"If-Match": f'"rev-{merged.json()["data"]["revision"]}"', "Idempotency-Key": "split-auto"},
    )
    assert split.status_code == 201, split.text
    assert split.json()["data"]["name"] == "另一个人"
    with w.db.read() as conn:
        assert len(list(conn.execute(select(s.entity_changes)))) == 2
        assert conn.execute(select(s.entity_separations)).first()
    stale = client.post(
        f"/api/entities/{target['id']}/split",
        json={"name": "不应创建", "occurrence_ids": [occurrences[1]["id"]]},
        headers={"If-Match": '"rev-1"', "Idempotency-Key": "stale-split"},
    )
    assert stale.status_code == 412


def test_reference_capacity_selects_without_blocking_activity(w, jpeg, clock):
    provider = make_provider(w)
    _, _, frame = make_frame(w, jpeg, clock)
    service = Discovery(w)
    with w.db.read() as conn:
        meta = w.assets.metadata(conn, frame["id"])
    for n in range(3):
        output = visual(w, frame["id"])
        job = {"id": f"capacity-{n}", "payload_json": {"provider_id": provider}}
        service.clean_output(job, output, [meta], [])
        service.stage(job, output, [meta])
        with w.db.write() as conn:
            service.archive(conn, service.pending(conn), [])
    with w.db.write() as conn:
        row = conn.execute(select(s.providers)).mappings().one()
        conn.execute(update(s.providers).values(config_json=row["config_json"] | {"max_total_images": 2}))
        w.assets.validate_capacity(conn)
        assert len(w.assets.reference_set(conn, None)["members_json"]) == 0


def test_history_repair_is_local_idempotent_and_conservative(w):
    camera = make_camera(w)
    start = w.now()
    for i, description in enumerate(["人物侧卧在床上，保持静止", "人物正在睡觉", "人物可能正在睡觉"]):
        minute = make_minute(
            w, camera, start + i * 60000, activity="resting", subject=None, identity="unknown"
        )
        with w.db.write() as conn:
            conn.execute(
                update(s.activity_spans)
                .where(s.activity_spans.c.minute_id == minute)
                .values(details_json={"observation": description, "labels": [], "object_ids": []})
            )
    result = repair_history(w)
    assert result["sleep_minutes"] == 1
    assert repair_history(w)["already_applied"]
    with w.db.read() as conn:
        stats = w.ledger.calculate(conn, start, start + 180000, {"camera_ids": [camera], "subject_id": None})
        assert {a["activity"]: a["duration_ms"] for a in stats["activities"]} == {
            "resting": 120000,
            "睡觉": 60000,
        }
        assert not list(conn.execute(select(s.ai_usage)))
        assert not list(conn.execute(select(s.jobs)))


def test_long_event_lists_have_bounded_queries_and_small_payload(client, w, clock):
    camera = make_camera(w)
    start = w.now()
    minute = make_minute(w, camera, start, activity="睡觉", subject=None, identity="unknown")
    ids = [f"evidence_{i}" for i in range(10000)]
    with w.db.write() as conn:
        conn.execute(
            s.evidence_refs.insert(),
            [
                dict(
                    evidence_id=e,
                    camera_id=camera,
                    captured_at_ms=start,
                    expires_at_ms=start + 3600000,
                    state="available",
                    width=160,
                    height=90,
                )
                for e in ids
            ],
        )
        conn.execute(
            update(s.activity_spans)
            .where(s.activity_spans.c.minute_id == minute)
            .values(evidence_ids_json=ids)
        )
        w.ledger.project(conn)
    clock.advance(60000)
    queries = []

    def count(*args):
        queries.append(1)

    event.listen(w.db.engine, "before_cursor_execute", count)
    try:
        params = {"from": iso(start), "to": iso(start + 60000)}
        overview = client.get("/api/overview", params=params)
        assert overview.status_code == 200, overview.text
        assert len(queries) < 25 and len(overview.content) < 10000
        assert "segments" not in overview.json()["data"]["latest_events"][0]
        assert overview.json()["data"]["latest_events"][0]["has_manual_revision"] is False
        queries.clear()
        timeline = client.get("/api/timeline", params=params | {"bucket_seconds": 60})
        assert timeline.status_code == 200, timeline.text
        assert len(queries) < 15
        assert timeline.json()["data"]["buckets"][0]["activities"][0]["activity"] == "睡觉"
    finally:
        event.remove(w.db.engine, "before_cursor_execute", count)
