"""Structured JSON logging with context propagation and privacy redaction.

This module has no FastAPI dependency. Middleware, workers and capture processes may
use ``bind_context`` and ``log_event`` to attach the same request/job identifiers.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
import traceback
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, TextIO
from urllib.parse import urlsplit, urlunsplit

from .timeutils import utc_rfc3339

_CONTEXT_FIELDS = ("service", "request_id", "camera_id", "job_id", "layer")
_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("caw_log_context", default={})
_SENSITIVE_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "apikey",
    "accesskey",
    "authorization",
    "cookie",
    "credential",
    "privatekey",
    "rtspurl",
    "sessiontoken",
    "refreshtoken",
    "token",
)
_RAW_PAYLOAD_KEYS = {
    "prompt",
    "systemprompt",
    "messages",
    "multimodal",
    "image",
    "images",
    "imagedata",
    "imagebytes",
    "framebytes",
    "rawpayload",
    "modelpayload",
    "requestbody",
    "responsebody",
    "content",
    "payload",
    "request",
    "response",
}
_URL_RE = re.compile(
    r"(?i)\b(?:rtsp|rtsps|https?|postgres(?:ql)?|mysql|mariadb|redis|rediss|sqlite)://[^\s\"'<>]+"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")
_KEY_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization)"
    r"(\s*[=:]\s*)([^\s,;]+)"
)
_TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[opsu]_[A-Za-z0-9]{20,})\b")


def _normalise_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    try:
        parsed = urlsplit(raw)
    except ValueError:
        scheme = raw.split(":", 1)[0].lower()
        return f"{scheme}://<redacted>"
    scheme = parsed.scheme.lower()
    if scheme in {"rtsp", "rtsps", "sqlite", "postgres", "postgresql", "mysql", "mariadb", "redis", "rediss"}:
        # These URLs may contain credentials or local storage paths.
        return f"{scheme}://<redacted>"
    hostname = parsed.hostname or "<redacted-host>"
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
    except ValueError:
        pass
    # URL paths can also contain bearer/object tokens, so omit them as well.
    return urlunsplit((scheme, netloc, "", "", ""))


def sanitize_string(value: str, *, max_length: int = 8_192) -> str:
    """Redact credentials, full RTSP URLs, signed query strings and token patterns."""

    text = str(value)
    text = re.sub(r"(?i)data:image/[a-z0-9.+-]+;base64,[a-z0-9+/=]+", "<image-payload-redacted>", text)
    text = _URL_RE.sub(_redact_url, text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    text = _KEY_VALUE_RE.sub(r"\1\2<redacted>", text)
    text = _TOKEN_RE.sub("<redacted-token>", text)
    if len(text) > max_length:
        text = text[:max_length] + "...[truncated]"
    return text


def sanitize_value(value: Any, *, key: str | None = None, _depth: int = 0) -> Any:
    """Recursively convert log values into JSON-safe, redacted values."""

    if key is not None:
        normalized = _normalise_key(key)
        if any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS):
            return "<redacted>"
        if normalized in _RAW_PAYLOAD_KEYS or normalized.endswith("payload"):
            return "<payload-redacted>"
    if _depth > 16:
        return "<nested-value-redacted>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "<non-finite>"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary-redacted:{len(value)} bytes>"
    if isinstance(value, Path):
        return "<path-redacted>"
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return utc_rfc3339(current)
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize_value(item_value, key=str(item_key), _depth=_depth + 1)
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_value(item, _depth=_depth + 1) for item in value]
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            return sanitize_value(value.model_dump(mode="json"), key=key, _depth=_depth + 1)
        except Exception:
            return "<object-redacted>"
    if hasattr(value, "as_dict") and callable(value.as_dict):
        try:
            return sanitize_value(value.as_dict(), key=key, _depth=_depth + 1)
        except Exception:
            return "<object-redacted>"
    return sanitize_string(str(value))


class ContextFilter(logging.Filter):
    """Copy contextvars into each record without changing application call sites."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = _CONTEXT.get()
        for field in _CONTEXT_FIELDS:
            if not hasattr(record, field):
                value = context.get(field)
                if value is not None:
                    setattr(record, field, value)
        return True


class JsonFormatter(logging.Formatter):
    """Single-line JSON formatter following the backend observability contract."""

    _STANDARD = set(logging.makeLogRecord({}).__dict__) | {
        "message",
        "asctime",
        "exc_text",
        "stack_info",
        "pathname",
        "filename",
        "module",
        "funcName",
        "lineno",
        "process",
        "processName",
        "thread",
        "threadName",
        "taskName",
    }

    def __init__(self, *, service: str = "api", include_exception: bool = True):
        super().__init__()
        self.service = service
        self.include_exception = include_exception

    def format(self, record: logging.LogRecord) -> str:
        context = _CONTEXT.get()
        timestamp = datetime.fromtimestamp(record.created, tz=UTC)
        payload: dict[str, Any] = {
            "timestamp": utc_rfc3339(timestamp),
            "level": record.levelname.lower(),
            "service": sanitize_string(getattr(record, "service", None) or context.get("service") or self.service),
            "event": sanitize_string(getattr(record, "event", None) or record.name),
            "message": sanitize_string(record.getMessage()),
        }
        for field in ("request_id", "camera_id", "job_id", "layer"):
            value = getattr(record, field, None) or context.get(field)
            if value is not None:
                payload[field] = sanitize_value(value, key=field)

        for name, value in record.__dict__.items():
            if name in self._STANDARD or name.startswith("_") or name in _CONTEXT_FIELDS:
                continue
            if name in {"event", "service"}:
                continue
            payload[name] = sanitize_value(value, key=name)

        if self.include_exception and record.exc_info:
            try:
                exception = "".join(traceback.format_exception(*record.exc_info))
            except Exception:
                exception = "<exception formatting failed>"
            payload["exception"] = sanitize_string(exception, max_length=16_384)
        if record.stack_info:
            payload["stack"] = sanitize_string(record.stack_info, max_length=16_384)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@contextmanager
def bind_context(**values: Any) -> Iterator[None]:
    """Temporarily bind request/job fields to current async task or thread context."""

    allowed = set(_CONTEXT_FIELDS)
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unsupported log context fields: {', '.join(sorted(unknown))}")
    merged = dict(_CONTEXT.get())
    merged.update({key: value for key, value in values.items() if value is not None})
    token: Token[dict[str, Any]] = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_context() -> dict[str, Any]:
    """Return a safe copy of the currently bound identifiers."""

    return sanitize_value(_CONTEXT.get())


def configure_logging(
    *,
    service: str = "api",
    level: str | int = "INFO",
    stream: TextIO | None = None,
    logger: logging.Logger | None = None,
    include_exception: bool = True,
) -> logging.Handler:
    """Install an idempotent JSON handler on the root or supplied logger."""

    target = logger or logging.getLogger()
    if isinstance(level, str):
        parsed_level = logging._nameToLevel.get(level.upper())
        if parsed_level is None:
            raise ValueError(f"unknown logging level: {level!r}")
    else:
        parsed_level = level
    if isinstance(parsed_level, bool) or not isinstance(parsed_level, int):
        raise TypeError("logging level must be a name or integer")
    target.setLevel(parsed_level)
    for handler in target.handlers:
        if getattr(handler, "_caw_json_handler", False):
            handler.setFormatter(JsonFormatter(service=service, include_exception=include_exception))
            handler.setLevel(parsed_level)
            if not any(isinstance(item, ContextFilter) for item in handler.filters):
                handler.addFilter(ContextFilter())
            return handler

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setLevel(parsed_level)
    handler.setFormatter(JsonFormatter(service=service, include_exception=include_exception))
    handler.addFilter(ContextFilter())
    handler._caw_json_handler = True  # type: ignore[attr-defined]
    target.addHandler(handler)
    return handler


def log_event(
    logger: logging.Logger,
    level: int | str,
    event: str,
    message: str | None = None,
    **fields: Any,
) -> None:
    """Log a named event with sanitized structured fields."""

    log_level = getattr(logging, str(level).upper(), None) if isinstance(level, str) else level
    if not isinstance(log_level, int):
        raise ValueError(f"invalid log level: {level!r}")
    safe_fields = {key: sanitize_value(value, key=key) for key, value in fields.items()}
    logger.log(log_level, sanitize_string(message or event), extra={"event": event, **safe_fields})


def safe_log_context(**fields: Any) -> dict[str, Any]:
    """Return one sanitized context mapping suitable for a logging ``extra`` value."""

    return {key: sanitize_value(value, key=key) for key, value in fields.items()}


# More explicit names for integrations that prefer a factory-style API.
setup_logging = configure_logging
StructuredJsonFormatter = JsonFormatter


__all__ = [
    "ContextFilter",
    "JsonFormatter",
    "StructuredJsonFormatter",
    "bind_context",
    "configure_logging",
    "current_context",
    "log_event",
    "safe_log_context",
    "sanitize_string",
    "sanitize_value",
    "setup_logging",
]
