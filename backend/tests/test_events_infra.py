import asyncio
import sqlite3

import pytest

from app.events import (
    EventError,
    InMemoryOutbox,
    OutboxBroker,
    OutboxGapError,
    SQLiteOutbox,
    format_sse,
    parse_last_event_id,
)


def test_in_memory_outbox_allocates_ordered_ids_and_resumes():
    outbox = InMemoryOutbox(max_events=8)
    first = outbox.append("job.updated", {"state": "queued"})
    second = outbox.append("timeline.updated", {"version": 2})
    assert (first.id, second.id) == (1, 2)
    assert [event.id for event in outbox.replay("1")] == [2]
    assert parse_last_event_id(None) is None
    with pytest.raises(EventError):
        parse_last_event_id("1x")


def test_sqlite_outbox_persists_and_detects_pruned_cursor(tmp_path):
    path = tmp_path / "events.sqlite3"
    outbox = SQLiteOutbox(path)
    events = [outbox.append("camera.status", {"status": str(i)}, created_at_ms=i) for i in range(3)]
    assert [event.id for event in SQLiteOutbox(path).replay("1")] == [2, 3]
    assert outbox.prune_before(2) == 2
    with pytest.raises(OutboxGapError):
        outbox.replay("0")
    assert [event.id for event in outbox.replay("2")] == [3]
    assert events[-1].id == 3


def test_sse_framing_and_payload_secrets_are_redacted():
    outbox = InMemoryOutbox()
    item = outbox.append(
        "camera.status",
        {
            "status": "online",
            "api_key": "not-for-logs",
            "rtsp": "rtsp://user:pass@example.test/path?password=pw",
            "image": "data:image/jpeg;base64,AAAA",
        },
    )
    frame = format_sse(item)
    assert frame.startswith("id: 1\nevent: camera.status\ndata: {")
    assert "not-for-logs" not in frame
    assert "user:pass" not in frame
    assert "data:image" not in frame
    assert frame.endswith("\n\n")
    with pytest.raises(EventError):
        format_sse({"id": "1\nforged: yes", "data": {}})


def test_async_broker_replays_and_wakes_on_publish():
    async def scenario():
        outbox = InMemoryOutbox()
        broker = OutboxBroker(outbox)
        outbox.append("job.updated", {"id": "earlier"})
        generator = broker.stream_events("0", heartbeat_seconds=1)
        first = await anext(generator)
        assert first.id == 1
        pending = asyncio.create_task(anext(generator))
        await asyncio.sleep(0.01)
        broker.publish("job.updated", {"id": "later"})
        second = await asyncio.wait_for(pending, timeout=0.5)
        assert second.data == {"id": "later"}
        await generator.aclose()

    asyncio.run(scenario())


def test_sqlalchemy_model_outbox_table_is_not_shadowed(tmp_path):
    path = tmp_path / "models.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE outbox_event (id TEXT PRIMARY KEY, event_type TEXT NOT NULL, sequence INTEGER)"
    )
    connection.commit()
    store = SQLiteOutbox(connection)
    created = store.append("job.updated", {"state": "done"})
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "outbox_event" in tables
    assert "sse_event_log" in tables
    assert created.id == 1
    connection.close()


def test_sqlite_outbox_detects_resync_when_all_rows_were_pruned(tmp_path):
    outbox = SQLiteOutbox(tmp_path / "empty-after-prune.sqlite3")
    outbox.append("job.updated", {"state": "done"}, created_at_ms=1)
    outbox.append("job.updated", {"state": "done"}, created_at_ms=2)
    assert outbox.prune_before(3) == 2
    assert outbox.oldest_id() is None
    assert outbox.latest_id() == 2
    assert outbox.has_gap(0)
    with pytest.raises(OutboxGapError):
        outbox.replay(0)


def test_outbox_ids_remain_monotonic_after_full_prune(tmp_path):
    outbox = SQLiteOutbox(tmp_path / "monotonic.sqlite3")
    outbox.append("camera.status", {}, created_at_ms=1)
    outbox.prune_before(2)
    assert outbox.append("camera.status", {}, created_at_ms=3).id == 2


def test_custom_outbox_table_has_isolated_sequence_state(tmp_path):
    path = tmp_path / "custom-events.sqlite3"
    first = SQLiteOutbox(path, table_name="tenant_events")
    assert first.append("job.updated", {}).id == 1
    second = SQLiteOutbox(path, table_name="tenant_events")
    assert second.append("job.updated", {}).id == 2
    tables = {row[0] for row in sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "tenant_events_state" in tables


def test_sqlite_outbox_savepoints_preserve_outer_transactions(tmp_path):
    connection = sqlite3.connect(tmp_path / "outer-transaction.sqlite3")
    outbox = SQLiteOutbox(connection)
    connection.commit()

    connection.execute("BEGIN")
    first = outbox.append("job.updated", {"state": "queued"}, created_at_ms=1)
    assert first.id == 1
    assert outbox.prune_before(2) == 1
    connection.rollback()

    assert outbox.get_after(0) == []
    assert outbox.latest_id() is None
    connection.close()
