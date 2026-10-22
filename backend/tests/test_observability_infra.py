import io
import json
import logging

from app.observability import JsonFormatter, bind_context, configure_logging, sanitize_string, sanitize_value


def test_json_logs_have_context_and_redact_sensitive_fields():
    stream = io.StringIO()
    logger = logging.Logger("test-observability", level=logging.INFO)
    configure_logging(service="worker", stream=stream, logger=logger)
    with bind_context(request_id="req-1", camera_id="cam-1", job_id="job-1", layer="L2"):
        logger.info(
            "request to rtsp://user:pass@camera.local/live?token=private",
            extra={
                "event": "provider.request",
                "api_key": "top-secret",
                "prompt": "private model input",
                "image_bytes": b"jpeg-content",
                "latency_ms": 12,
            },
        )
    payload = json.loads(stream.getvalue())
    assert payload["service"] == "worker"
    assert payload["request_id"] == "req-1"
    assert payload["camera_id"] == "cam-1"
    assert payload["job_id"] == "job-1"
    assert payload["layer"] == "L2"
    assert payload["api_key"] == "<redacted>"
    assert payload["prompt"] == "<payload-redacted>"
    assert payload["image_bytes"] == "<payload-redacted>"
    assert "user:pass" not in payload["message"]
    assert "private" not in payload["message"]
    assert payload["latency_ms"] == 12


def test_sanitizer_strips_signed_http_queries_and_database_urls():
    value = "url=https://user:pw@example.test/path?sig=verysecret and db=postgresql://u:p@host/db"
    result = sanitize_string(value)
    assert "verysecret" not in result
    assert "u:p" not in result
    assert "https://user" not in result
    assert "postgresql://u:p" not in result
    assert sanitize_value({"api_key": "abc", "image": b"data"}) == {
        "api_key": "<redacted>",
        "image": "<payload-redacted>",
    }


def test_formatter_always_emits_json():
    record = logging.LogRecord("test", logging.WARNING, __file__, 10, "warn", (), None)
    value = JsonFormatter(service="scheduler").format(record)
    assert json.loads(value)["level"] == "warning"


def test_structured_log_envelope_redacts_event_and_nonfinite_numbers():
    record = logging.LogRecord("test", logging.INFO, __file__, 10, "ok", (), None)
    record.event = "request https://user:pw@example.test/private?token=secret"
    record.service = "api token=secret"
    record.measurement = float("nan")
    payload = json.loads(JsonFormatter().format(record))
    assert "user:pw" not in payload["event"]
    assert "private" not in payload["event"]
    assert payload["service"].endswith("token=<redacted>")
    assert payload["measurement"] == "<non-finite>"
