"""Durable SSE event storage, framing, and replay helpers.

This module defines no API routes or ORM models. SQLiteOutbox uses its own
``sse_event_log`` transport table, so it cannot shadow a domain ``outbox_event``
model. Last-Event-ID cursors are monotonically increasing integer identifiers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Iterable, Iterator, Mapping, Protocol
from urllib.parse import urlsplit

from .timeutils import utc_now_ms, utc_rfc3339

_EVENT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_INTEGER_RE = re.compile(r"^[0-9]+$")
_RTSP_URL_RE = re.compile(r"(?i)\brtsps?://[^\s\"'<>]+")
_DATA_URI_RE = re.compile(r"(?i)data:image/[a-z0-9.+-]+;base64,[a-z0-9+/=]+")
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1_000
_MAX_PAYLOAD_BYTES = 512 * 1024


class EventError(ValueError):
    """Raised when an outbox event or SSE frame is invalid."""


class InvalidLastEventID(EventError):
    """Raised when Last-Event-ID is not a non-negative integer."""


class OutboxGapError(EventError):
    """Raised when requested events have already expired from the outbox."""

    def __init__(self, last_event_id: int, oldest_event_id: int | None):
        self.last_event_id = last_event_id
        self.oldest_event_id = oldest_event_id
        super().__init__("SSE events are outside the replay retention range; reload the current snapshot")

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": "EVENTS_RESYNC_REQUIRED",
            "last_event_id": self.last_event_id,
            "oldest_event_id": self.oldest_event_id,
        }


class OutboxBackend(Protocol):
    def append(self, event: str, data: Any = None, **kwargs: Any) -> "OutboxEvent": ...

    def get_after(
        self,
        last_event_id: int | None = None,
        *,
        limit: int = _DEFAULT_LIMIT,
        event_types: Iterable[str] | None = None,
    ) -> list["OutboxEvent"]: ...

    def oldest_id(self) -> int | None: ...

    def latest_id(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """A replayable SSE event with a durable integer cursor."""

    id: int
    event: str
    data: Any = field(default_factory=dict)
    created_at_ms: int = field(default_factory=utc_now_ms)
    camera_id: str | None = None
    job_id: str | None = None
    layer: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, int) or self.id < 0:
            raise EventError("event id must be a non-negative integer")
        _validate_event_name(self.event)
        if isinstance(self.created_at_ms, bool) or not isinstance(self.created_at_ms, int):
            raise EventError("created_at_ms must be integer epoch milliseconds")
        _encode_data(self.data)

    @property
    def event_id(self) -> int:
        return self.id

    @property
    def name(self) -> str:
        return self.event

    @property
    def payload(self) -> Any:
        return self.data

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "event": self.event,
            "data": self.data,
            "created_at_ms": self.created_at_ms,
        }
        for key in ("camera_id", "job_id", "layer", "request_id"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result

    def sse(self) -> str:
        return format_sse(self)


@dataclass(frozen=True, slots=True)
class SSEEvent:
    """An SSE frame that does not have to originate in the outbox."""

    data: Any = field(default_factory=dict)
    event: str | None = None
    id: int | str | None = None
    retry_ms: int | None = None
    comment: str | None = None

    def __post_init__(self) -> None:
        if self.event is not None:
            _validate_event_name(self.event)
        if self.id is not None:
            value = str(self.id)
            if not _INTEGER_RE.fullmatch(value) or len(value) > 128:
                raise EventError("SSE id must be a non-negative integer")
        if self.retry_ms is not None and (
            isinstance(self.retry_ms, bool) or not isinstance(self.retry_ms, int) or self.retry_ms < 0
        ):
            raise EventError("retry_ms must be a non-negative integer")


def _validate_event_name(event: str) -> str:
    if not isinstance(event, str) or not _EVENT_NAME_RE.fullmatch(event):
        raise EventError("event name is invalid")
    return event


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return utc_rfc3339(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return "<path-redacted>"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary-redacted:{len(value)}>"
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return value.model_dump(mode="json")
    if hasattr(value, "as_dict") and callable(value.as_dict):
        return value.as_dict()
    raise TypeError(f"unsupported JSON payload type: {type(value).__name__}")


def _redact_sse_value(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    if depth > 12:
        return "<nested-redacted>"
    if key is not None:
        normalised = re.sub(r"[^a-z0-9]", "", key.lower())
        if any(marker in normalised for marker in (
            "apikey", "password", "passwd", "secret", "authorization", "cookie",
            "credential", "rtspurl", "accesskey", "refreshtoken", "token",
        )):
            return "<redacted>"
        if normalised in {
            "prompt", "systemprompt", "messages", "multimodal", "image", "images",
            "imagedata", "imagebytes", "framebytes", "rawpayload", "modelpayload",
            "requestbody", "responsebody", "payload", "content", "request", "response",
        } or normalised.endswith("payload"):
            return "<payload-redacted>"
    if isinstance(value, Mapping):
        return {str(k): _redact_sse_value(v, key=str(k), depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact_sse_value(item, depth=depth + 1) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary-redacted:{len(value)}>"
    if isinstance(value, Path):
        return "<path-redacted>"
    if isinstance(value, datetime):
        try:
            return utc_rfc3339(value)
        except (TypeError, ValueError):
            return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        value = _DATA_URI_RE.sub("<image-payload-redacted>", value)
        value = _RTSP_URL_RE.sub(lambda m: m.group(0).split(":", 1)[0] + "://<redacted>", value)
        value = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+", r"\1<redacted>", value)
        value = re.sub(
            r"(?i)\b(api[_-]?key|password|passwd|secret|token)\s*[=:]\s*[^\s,;]+",
            r"\1=<redacted>", value,
        )
        return value[:16_384]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            return _redact_sse_value(value.model_dump(mode="json"), depth=depth + 1)
        except Exception:
            return "<object-redacted>"
    if hasattr(value, "as_dict") and callable(value.as_dict):
        try:
            return _redact_sse_value(value.as_dict(), depth=depth + 1)
        except Exception:
            return "<object-redacted>"
    return str(value)[:16_384]


def _encode_data(data: Any) -> str:
    try:
        encoded = json.dumps(
            _redact_sse_value(data), ensure_ascii=False, separators=(",", ":"),
            default=_json_default, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EventError("event data must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise EventError(f"event data exceeds {_MAX_PAYLOAD_BYTES} bytes")
    return encoded


def parse_last_event_id(value: str | int | None) -> int | None:
    """Parse Last-Event-ID; an absent value represents the initial connection."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise InvalidLastEventID("Last-Event-ID must be a non-negative integer")
    if isinstance(value, int):
        if value < 0:
            raise InvalidLastEventID("Last-Event-ID must be a non-negative integer")
        return value
    candidate = str(value).strip()
    if not _INTEGER_RE.fullmatch(candidate):
        raise InvalidLastEventID("Last-Event-ID must be a non-negative integer")
    return int(candidate)


def last_event_id_from_headers(headers: Mapping[str, Any] | Any) -> int | None:
    """Read Last-Event-ID from Starlette headers or a normal mapping."""
    if headers is None or not hasattr(headers, "get"):
        return None
    value = headers.get("last-event-id")
    if value is None:
        value = headers.get("Last-Event-ID")
    return parse_last_event_id(value)


def _normalise_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
        raise EventError(f"limit must be between 1 and {_MAX_LIMIT}")
    return limit


def format_sse(event: OutboxEvent | SSEEvent | Mapping[str, Any], *, newline: str = "\n") -> str:
    """Serialize one event using standard EventSource field framing."""
    if isinstance(event, OutboxEvent):
        frame = SSEEvent(data=event.data, event=event.event, id=event.id)
    elif isinstance(event, SSEEvent):
        frame = event
    elif isinstance(event, Mapping):
        frame = SSEEvent(
            data=event.get("data", event.get("payload", {})), event=event.get("event"),
            id=event.get("id"), retry_ms=event.get("retry_ms"), comment=event.get("comment"),
        )
    else:
        raise TypeError("event must be OutboxEvent, SSEEvent, or a mapping")
    if newline not in {"\n", "\r\n"}:
        raise EventError("SSE newline must be LF or CRLF")
    lines: list[str] = []
    if frame.comment is not None:
        lines.extend(f": {line}" for line in (str(frame.comment).splitlines() or [""]))
    if frame.id is not None:
        lines.append(f"id: {frame.id}")
    if frame.event is not None:
        lines.append(f"event: {frame.event}")
    if frame.retry_ms is not None:
        lines.append(f"retry: {frame.retry_ms}")
    if frame.comment is None:
        lines.extend(f"data: {line}" for line in (_encode_data(frame.data).splitlines() or [""]))
    return newline.join(lines) + newline + newline


def format_sse_heartbeat(*, comment: str = "heartbeat") -> str:
    safe_comment = re.sub(r"[\r\n]", " ", str(comment))[:256]
    return f": {safe_comment}\n\n"

class SQLiteOutbox:
    """Persistent SQLite SSE log isolated from the domain outbox model."""

    def __init__(
        self,
        database: str | os.PathLike[str] | sqlite3.Connection | Any,
        *,
        table_name: str = "sse_event_log",
        busy_timeout_ms: int = 5_000,
        create_schema: bool = True,
    ) -> None:
        self.database = database
        self.table_name = _safe_identifier(table_name)
        self.state_table_name = _state_table_name(self.table_name)
        self.busy_timeout_ms = max(0, int(busy_timeout_ms))
        self._connection = database if isinstance(database, sqlite3.Connection) else None
        self._lock = threading.RLock()
        if create_schema:
            self.ensure_schema()

    @property
    def path(self) -> Path | None:
        return None if self._connection is not None else _sqlite_path(self.database)

    def _connect(self) -> tuple[sqlite3.Connection, bool]:
        if self._connection is not None:
            self._connection.row_factory = sqlite3.Row
            return self._connection, False
        path = _sqlite_path(self.database)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(path), timeout=max(1, self.busy_timeout_ms / 1_000),
            isolation_level=None, check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        return connection, True

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        connection, owned = self._connect()
        try:
            yield connection
        finally:
            if owned:
                connection.close()

    def ensure_schema(self) -> None:
        raw_index_name = f"ix_{self.table_name}_created_id"
        if len(raw_index_name) > 63:
            digest = hashlib.sha256(raw_index_name.encode("utf-8")).hexdigest()[:16]
            index_name = _safe_identifier(f"sse_ix_{digest}")
        else:
            index_name = _safe_identifier(raw_index_name)
        with self._lock, self._session() as connection:
            was_in_transaction = connection.in_transaction
            if was_in_transaction:
                connection.execute("SAVEPOINT caw_sse_schema")
            try:
                connection.execute(
                    f"""CREATE TABLE IF NOT EXISTS {self.table_name} (
                        id INTEGER PRIMARY KEY,
                        event_name TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at_ms INTEGER NOT NULL,
                        camera_id TEXT NULL,
                        job_id TEXT NULL,
                        layer TEXT NULL,
                        request_id TEXT NULL
                    )"""
                )
                connection.execute(
                    f"CREATE INDEX IF NOT EXISTS {index_name} ON {self.table_name}(created_at_ms, id)"
                )
                connection.execute(
                    f"CREATE TABLE IF NOT EXISTS {self.state_table_name} "
                    "(singleton INTEGER PRIMARY KEY CHECK (singleton = 1), latest_id INTEGER NOT NULL)"
                )
                connection.execute(
                    f"INSERT OR IGNORE INTO {self.state_table_name}(singleton, latest_id) "
                    f"SELECT 1, COALESCE(MAX(id), 0) FROM {self.table_name}"
                )
                connection.execute(
                    f"UPDATE {self.state_table_name} SET latest_id = MAX(latest_id, "
                    f"COALESCE((SELECT MAX(id) FROM {self.table_name}), 0)) WHERE singleton=1"
                )
                if was_in_transaction:
                    connection.execute("RELEASE SAVEPOINT caw_sse_schema")
                elif connection.in_transaction:
                    connection.commit()
            except Exception:
                if was_in_transaction:
                    connection.execute("ROLLBACK TO SAVEPOINT caw_sse_schema")
                    connection.execute("RELEASE SAVEPOINT caw_sse_schema")
                raise

    def append(
        self,
        event: str | OutboxEvent,
        data: Any = None,
        *,
        created_at_ms: int | None = None,
        camera_id: str | None = None,
        job_id: str | None = None,
        layer: str | None = None,
        request_id: str | None = None,
    ) -> OutboxEvent:
        if isinstance(event, OutboxEvent):
            source = event
            event, data = source.event, source.data
            created_at_ms = source.created_at_ms if created_at_ms is None else created_at_ms
            camera_id = source.camera_id if camera_id is None else camera_id
            job_id = source.job_id if job_id is None else job_id
            layer = source.layer if layer is None else layer
            request_id = source.request_id if request_id is None else request_id
        _validate_event_name(event)
        payload_json = _encode_data(data if data is not None else {})
        if created_at_ms is None:
            timestamp = utc_now_ms()
        elif isinstance(created_at_ms, bool) or not isinstance(created_at_ms, int):
            raise EventError("created_at_ms must be an integer epoch timestamp")
        else:
            timestamp = created_at_ms
        with self._lock, self._session() as connection:
            has_outer_transaction = connection.in_transaction
            savepoint = "caw_sse_append"
            connection.execute(f"SAVEPOINT {savepoint}" if has_outer_transaction else "BEGIN IMMEDIATE")
            try:
                current_id = int(connection.execute(
                    f"SELECT latest_id FROM {self.state_table_name} WHERE singleton=1"
                ).fetchone()[0])
                event_id = current_id + 1
                connection.execute(
                    f"INSERT INTO {self.table_name} "
                    "(id, event_name, payload_json, created_at_ms, camera_id, job_id, layer, request_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (event_id, event, payload_json, timestamp, camera_id, job_id, layer, request_id),
                )
                connection.execute(
                    f"UPDATE {self.state_table_name} SET latest_id=? WHERE singleton=1", (event_id,)
                )
                connection.execute(f"RELEASE SAVEPOINT {savepoint}" if has_outer_transaction else "COMMIT")
            except Exception:
                if has_outer_transaction:
                    connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    connection.execute("ROLLBACK")
                raise
        return OutboxEvent(
            id=event_id, event=event, data=json.loads(payload_json), created_at_ms=timestamp,
            camera_id=camera_id, job_id=job_id, layer=layer, request_id=request_id,
        )

    publish = append

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> OutboxEvent:
        try:
            data = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise EventError("outbox contains invalid payload_json") from exc
        return OutboxEvent(
            id=int(row["id"]), event=str(row["event_name"]), data=data,
            created_at_ms=int(row["created_at_ms"]), camera_id=row["camera_id"],
            job_id=row["job_id"], layer=row["layer"], request_id=row["request_id"],
        )

    def get_after(
        self,
        last_event_id: int | None = None,
        *,
        limit: int = _DEFAULT_LIMIT,
        event_types: Iterable[str] | None = None,
    ) -> list[OutboxEvent]:
        limit = _normalise_limit(limit)
        cursor = parse_last_event_id(last_event_id)
        cursor_id = -1 if cursor is None else cursor
        clauses = ["id > ?"]
        params: list[Any] = [cursor_id]
        names = list(event_types or [])
        if names:
            for name in names:
                _validate_event_name(name)
            clauses.append(f"event_name IN ({','.join('?' for _ in names)})")
            params.extend(names)
        params.append(limit)
        with self._session() as connection:
            rows = connection.execute(
                "SELECT id, event_name, payload_json, created_at_ms, camera_id, job_id, layer, request_id "
                f"FROM {self.table_name} WHERE {' AND '.join(clauses)} ORDER BY id ASC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    events_after = get_after

    def oldest_id(self) -> int | None:
        with self._session() as connection:
            row = connection.execute(f"SELECT MIN(id) FROM {self.table_name}").fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def latest_id(self) -> int | None:
        with self._session() as connection:
            row = connection.execute(
                f"SELECT latest_id FROM {self.state_table_name} WHERE singleton=1"
            ).fetchone()
        if row is None or row[0] in {None, 0}:
            return None
        return int(row[0])

    def has_gap(self, last_event_id: int | None) -> bool:
        cursor = parse_last_event_id(last_event_id)
        if cursor is None:
            return False
        oldest = self.oldest_id()
        latest = self.latest_id()
        if oldest is None:
            return latest is not None and cursor < latest
        return cursor < oldest - 1

    def replay(
        self,
        last_event_id: int | None = None,
        *,
        limit: int = _DEFAULT_LIMIT,
        event_types: Iterable[str] | None = None,
        raise_on_gap: bool = True,
    ) -> list[OutboxEvent]:
        cursor = parse_last_event_id(last_event_id)
        if raise_on_gap and self.has_gap(cursor):
            raise OutboxGapError(cursor if cursor is not None else -1, self.oldest_id())
        return self.get_after(cursor, limit=limit, event_types=event_types)

    def prune_before(self, created_before_ms: int, *, limit: int | None = None) -> int:
        if isinstance(created_before_ms, bool) or not isinstance(created_before_ms, int):
            raise EventError("created_before_ms must be an integer epoch timestamp")
        timestamp = created_before_ms
        if limit is not None:
            _normalise_limit(limit)
        with self._lock, self._session() as connection:
            has_outer_transaction = connection.in_transaction
            savepoint = "caw_sse_prune"
            connection.execute(f"SAVEPOINT {savepoint}" if has_outer_transaction else "BEGIN IMMEDIATE")
            try:
                if limit is None:
                    cursor = connection.execute(
                        f"DELETE FROM {self.table_name} WHERE created_at_ms < ?", (timestamp,)
                    )
                else:
                    cursor = connection.execute(
                        f"DELETE FROM {self.table_name} WHERE id IN "
                        f"(SELECT id FROM {self.table_name} WHERE created_at_ms < ? ORDER BY id LIMIT ?)",
                        (timestamp, limit),
                    )
                count = int(cursor.rowcount if cursor.rowcount >= 0 else 0)
                connection.execute(f"RELEASE SAVEPOINT {savepoint}" if has_outer_transaction else "COMMIT")
                return count
            except Exception:
                if has_outer_transaction:
                    connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    connection.execute("ROLLBACK")
                raise

    delete_before = prune_before

    def close(self) -> None:
        # Injected sqlite3 connections are owned by the caller.
        return None

    def __enter__(self) -> "SQLiteOutbox":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


class InMemoryOutbox:
    """Thread-safe, bounded outbox for tests and ephemeral development."""

    def __init__(self, *, max_events: int = 10_000) -> None:
        if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
            raise ValueError("max_events must be greater than zero")
        self.max_events = max_events
        self._events: deque[OutboxEvent] = deque(maxlen=max_events)
        self._next_id = 1
        self._lock = threading.RLock()

    def append(
        self,
        event: str | OutboxEvent,
        data: Any = None,
        *,
        created_at_ms: int | None = None,
        camera_id: str | None = None,
        job_id: str | None = None,
        layer: str | None = None,
        request_id: str | None = None,
    ) -> OutboxEvent:
        if isinstance(event, OutboxEvent):
            source = event
            event, data = source.event, source.data
            created_at_ms = source.created_at_ms if created_at_ms is None else created_at_ms
            camera_id = source.camera_id if camera_id is None else camera_id
            job_id = source.job_id if job_id is None else job_id
            layer = source.layer if layer is None else layer
            request_id = source.request_id if request_id is None else request_id
        _validate_event_name(event)
        encoded = _encode_data(data if data is not None else {})
        if created_at_ms is not None and (isinstance(created_at_ms, bool) or not isinstance(created_at_ms, int)):
            raise EventError("created_at_ms must be an integer epoch timestamp")
        with self._lock:
            item = OutboxEvent(
                id=self._next_id, event=event, data=json.loads(encoded),
                created_at_ms=utc_now_ms() if created_at_ms is None else created_at_ms,
                camera_id=camera_id, job_id=job_id, layer=layer, request_id=request_id,
            )
            self._events.append(item)
            self._next_id += 1
            return item

    publish = append

    def get_after(
        self,
        last_event_id: int | None = None,
        *,
        limit: int = _DEFAULT_LIMIT,
        event_types: Iterable[str] | None = None,
    ) -> list[OutboxEvent]:
        limit = _normalise_limit(limit)
        cursor = parse_last_event_id(last_event_id)
        names = set(event_types or [])
        for name in names:
            _validate_event_name(name)
        with self._lock:
            return [
                event for event in self._events
                if (cursor is None or event.id > cursor) and (not names or event.event in names)
            ][:limit]

    events_after = get_after

    def oldest_id(self) -> int | None:
        with self._lock:
            return self._events[0].id if self._events else None

    def latest_id(self) -> int | None:
        with self._lock:
            return self._events[-1].id if self._events else None

    def has_gap(self, last_event_id: int | None) -> bool:
        cursor = parse_last_event_id(last_event_id)
        oldest = self.oldest_id()
        return cursor is not None and oldest is not None and cursor < oldest - 1

    def replay(
        self,
        last_event_id: int | None = None,
        *,
        limit: int = _DEFAULT_LIMIT,
        event_types: Iterable[str] | None = None,
        raise_on_gap: bool = True,
    ) -> list[OutboxEvent]:
        cursor = parse_last_event_id(last_event_id)
        if raise_on_gap and self.has_gap(cursor):
            raise OutboxGapError(cursor if cursor is not None else -1, self.oldest_id())
        return self.get_after(cursor, limit=limit, event_types=event_types)

    def prune_before(self, created_before_ms: int, *, limit: int | None = None) -> int:
        if isinstance(created_before_ms, bool) or not isinstance(created_before_ms, int):
            raise EventError("created_before_ms must be an integer epoch timestamp")
        if limit is not None:
            _normalise_limit(limit)
        timestamp = created_before_ms
        with self._lock:
            before = len(self._events)
            removed = 0
            kept: list[OutboxEvent] = []
            for event in self._events:
                if event.created_at_ms < timestamp and (limit is None or removed < limit):
                    removed += 1
                else:
                    kept.append(event)
            self._events = deque(kept, maxlen=self.max_events)
            return before - len(self._events)

    delete_before = prune_before


OutboxStore = SQLiteOutbox
DurableOutbox = SQLiteOutbox


def _resolve_future(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


class OutboxBroker:
    """Bridge durable outbox polling with low-latency process notifications."""

    def __init__(self, store: OutboxBackend):
        self.store = store
        self._waiters: dict[asyncio.Future[None], asyncio.AbstractEventLoop] = {}
        self._lock = threading.RLock()

    def publish(self, event: str | OutboxEvent, data: Any = None, **kwargs: Any) -> OutboxEvent:
        item = self.store.append(event, data, **kwargs)
        with self._lock:
            waiters = tuple(self._waiters.items())
            self._waiters.clear()
        for waiter, loop in waiters:
            if not loop.is_closed():
                loop.call_soon_threadsafe(_resolve_future, waiter)
        return item

    append = publish

    async def _wait(self, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        with self._lock:
            self._waiters[future] = loop
        try:
            await asyncio.wait_for(future, timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            return
        finally:
            with self._lock:
                self._waiters.pop(future, None)

    async def stream_events(
        self,
        last_event_id: str | int | None = None,
        *,
        heartbeat_seconds: float = 15.0,
        replay_limit: int = _DEFAULT_LIMIT,
        event_types: Iterable[str] | None = None,
        request: Any | None = None,
    ) -> AsyncGenerator[OutboxEvent | None, None]:
        cursor = parse_last_event_id(last_event_id)
        event_types = tuple(event_types or ())
        while True:
            if request is not None and await _request_disconnected(request):
                return
            if hasattr(self.store, "has_gap") and self.store.has_gap(cursor):
                raise OutboxGapError(cursor if cursor is not None else -1, self.store.oldest_id())
            pending = self.store.get_after(cursor, limit=replay_limit, event_types=event_types)
            if pending:
                for item in pending:
                    cursor = item.id
                    yield item
                continue
            await self._wait(heartbeat_seconds)
            if self.store.get_after(cursor, limit=1, event_types=event_types):
                continue
            yield None

    async def stream_sse(self, *args: Any, **kwargs: Any) -> AsyncGenerator[str, None]:
        async for item in self.stream_events(*args, **kwargs):
            yield format_sse_heartbeat() if item is None else format_sse(item)

    events = stream_events


async def _request_disconnected(request: Any) -> bool:
    checker = getattr(request, "is_disconnected", None)
    if checker is None:
        return False
    value = checker()
    if hasattr(value, "__await__"):
        value = await value
    return bool(value)


async def stream_events(
    store: OutboxBackend,
    last_event_id: str | int | None = None,
    **kwargs: Any,
) -> AsyncGenerator[OutboxEvent | None, None]:
    broker = OutboxBroker(store)
    async for item in broker.stream_events(last_event_id, **kwargs):
        yield item


async def stream_sse(
    store: OutboxBackend,
    last_event_id: str | int | None = None,
    **kwargs: Any,
) -> AsyncGenerator[str, None]:
    broker = OutboxBroker(store)
    async for item in broker.stream_sse(last_event_id, **kwargs):
        yield item


event_stream = stream_events
sse_stream = stream_sse


def sse_response(
    store: OutboxBackend,
    *,
    request: Any | None = None,
    last_event_id: str | int | None = None,
    **kwargs: Any,
) -> Any:
    """Build a Starlette StreamingResponse without registering a route."""
    from starlette.responses import JSONResponse, StreamingResponse

    try:
        if last_event_id is None and request is not None:
            last_event_id = last_event_id_from_headers(getattr(request, "headers", request))
        cursor = parse_last_event_id(last_event_id)
    except InvalidLastEventID:
        return JSONResponse({"error": {"code": "INVALID_LAST_EVENT_ID"}}, status_code=400)
    if cursor is not None and hasattr(store, "has_gap") and store.has_gap(cursor):
        gap = OutboxGapError(cursor, store.oldest_id())
        return JSONResponse({"error": gap.as_dict()}, status_code=409)
    return StreamingResponse(
        stream_sse(store, cursor, request=request, **kwargs),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _safe_identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError("invalid SQLite table identifier")
    return value


def _state_table_name(table_name: str) -> str:
    """Return an isolated sequence table for a custom event table name."""

    if table_name == "sse_event_log":
        return "sse_event_state"
    candidate = f"{table_name}_state"
    if len(candidate) > 63:
        digest = hashlib.sha256(table_name.encode("utf-8")).hexdigest()[:16]
        candidate = f"sse_state_{digest}"
    return _safe_identifier(candidate)


def _sqlite_path(database: Any) -> Path:
    if not isinstance(database, (str, os.PathLike)):
        url = getattr(database, "url", None)
        if url is None:
            url = getattr(getattr(database, "engine", None), "url", None)
        if url is None:
            raise TypeError("database must be a path, SQLite URL, or SQLAlchemy SQLite engine")
        database = str(url)
    value = os.fspath(database)
    if value.startswith("sqlite://"):
        try:
            from sqlalchemy.engine import make_url

            parsed = make_url(value)
            if parsed.get_backend_name() != "sqlite" or parsed.database in {None, ":memory:"}:
                raise ValueError("SQLite outbox requires a file-backed database or sqlite3 connection")
            value = parsed.database
        except ImportError:
            parsed_url = urlsplit(value)
            value = parsed_url.path
            if value in {"", "/", "/:memory:"}:
                raise ValueError("SQLite outbox requires a file-backed database or sqlite3 connection")
            if value.startswith("/./"):
                value = value[1:]
    elif value.startswith("sqlite:"):
        raise ValueError("invalid SQLite URL")
    return Path(value).expanduser().resolve()


__all__ = [
    "DurableOutbox",
    "EventError",
    "InMemoryOutbox",
    "InvalidLastEventID",
    "OutboxBackend",
    "OutboxBroker",
    "OutboxEvent",
    "OutboxGapError",
    "OutboxStore",
    "SSEEvent",
    "SQLiteOutbox",
    "event_stream",
    "format_sse",
    "format_sse_heartbeat",
    "last_event_id_from_headers",
    "parse_last_event_id",
    "sse_response",
    "sse_stream",
    "stream_events",
    "stream_sse",
]
