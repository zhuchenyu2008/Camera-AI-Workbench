"""0001 的冻结表结构；后续结构调整请添加新迁移。时间均为 UTC 毫秒。"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

metadata = MetaData()


def col(name, kind=Text, **kwargs):
    return Column(name, kind, **kwargs)


def pk(name="id"):
    return col(name, String, primary_key=True)


def fk(name, target, **kwargs):
    return Column(name, String, ForeignKey(target), **kwargs)


def stamps():
    return [
        col("created_at_ms", BigInteger, nullable=False),
        col("updated_at_ms", BigInteger, nullable=False),
    ]


def config_columns():
    return [col("revision", Integer, nullable=False, server_default="1"), *stamps()]


users = Table(
    "users",
    metadata,
    pk(),
    col("username", String, unique=True, nullable=False),
    col("password_hash", nullable=False),
    col("password_changed_at_ms", BigInteger, nullable=False),
    CheckConstraint("id = 'admin'"),
)
sessions = Table(
    "sessions",
    metadata,
    pk("id_hash"),
    fk("user_id", "users.id", nullable=False),
    col("csrf_hash", nullable=False),
    col("csrf_cipher", nullable=False),
    col("created_at_ms", BigInteger, nullable=False),
    col("last_seen_at_ms", BigInteger, nullable=False),
    col("expires_at_ms", BigInteger, nullable=False, index=True),
    col("revoked_at_ms", BigInteger),
)
cameras = Table(
    "cameras",
    metadata,
    pk(),
    col("name", nullable=False),
    col("enabled", Boolean, nullable=False, server_default="0"),
    *config_columns(),
    col("source_revision", Integer, nullable=False, server_default="1"),
    col("config_json", JSON, nullable=False),
    col("credentials_cipher"),
    col("deleted_at_ms", BigInteger),
    CheckConstraint("revision > 0"),
)
camera_runtime = Table(
    "camera_runtime",
    metadata,
    fk("camera_id", "cameras.id", primary_key=True),
    col("control_revision", Integer, nullable=False, server_default="0"),
    col("applied_revision", Integer, nullable=False, server_default="0"),
    col("config_applied_revision", Integer, nullable=False, server_default="0"),
    col("capture_state", nullable=False, server_default="stopped"),
    col("density_state"),
    col("stop_reason"),
    col("override_mode"),
    col("override_until_ms", BigInteger),
    col("last_frame_at_ms", BigInteger),
    col("heartbeat_at_ms", BigInteger),
    col("stream_session_id"),
    col("last_error_code"),
    col("diagnostics_json", JSON, nullable=False, default=dict),
)
schedule_versions = Table(
    "schedule_versions",
    metadata,
    fk("camera_id", "cameras.id", primary_key=True),
    col("version", Integer, primary_key=True),
    col("effective_from_ms", BigInteger, nullable=False, index=True),
    col("timezone", nullable=False),
    col("weekly_rules_json", JSON, nullable=False),
    col("override_json", JSON, nullable=False),
)
stream_sessions = Table(
    "stream_sessions",
    metadata,
    pk(),
    fk("camera_id", "cameras.id", nullable=False, index=True),
    col("started_at_ms", BigInteger, nullable=False),
    col("ended_at_ms", BigInteger),
    col("source_revision", Integer, nullable=False),
    col("codec"),
    col("exit_reason"),
    CheckConstraint("ended_at_ms IS NULL OR ended_at_ms >= started_at_ms"),
)
frames = Table(
    "frames",
    metadata,
    pk(),
    fk("camera_id", "cameras.id", nullable=False),
    fk("session_id", "stream_sessions.id", nullable=False),
    col("slot_at_ms", BigInteger, nullable=False),
    col("captured_at_ms", BigInteger, nullable=False),
    col("expires_at_ms", BigInteger, nullable=False),
    col("relpath", unique=True, nullable=False),
    col("bytes", BigInteger, nullable=False),
    col("width", Integer, nullable=False),
    col("height", Integer, nullable=False),
    col("sha256", nullable=False),
    col("state", nullable=False),
    col("reason"),
    col("source_pts"),
    col("receive_sequence", BigInteger),
    col("time_source", nullable=False, server_default="vps_receive"),
    col("is_test", Boolean, nullable=False, server_default="0"),
    UniqueConstraint("camera_id", "slot_at_ms"),
    CheckConstraint(
        "slot_at_ms % 1000 = 0 AND expires_at_ms > captured_at_ms AND bytes > 0 AND width > 0 AND height > 0"
    ),
    CheckConstraint("state IN ('available','expired','evicted','missing')"),
)
Index("frames_expiry", frames.c.state, frames.c.expires_at_ms)
minute_records = Table(
    "minute_records",
    metadata,
    pk(),
    fk("camera_id", "cameras.id", nullable=False),
    col("start_at_ms", BigInteger, nullable=False),
    col("current_revision", Integer, nullable=False, server_default="0"),
    col("analysis_state", nullable=False, server_default="pending"),
    col("created_at_ms", BigInteger, nullable=False),
    UniqueConstraint("camera_id", "start_at_ms"),
)
minute_capture = Table(
    "minute_capture",
    metadata,
    fk("minute_id", "minute_records.id", primary_key=True),
    col("planned_bitmap", BigInteger, nullable=False, server_default="0"),
    col("captured_bitmap", BigInteger, nullable=False, server_default="0"),
    col("evicted_bitmap", BigInteger, nullable=False, server_default="0"),
    col("missing_bitmap", BigInteger, nullable=False, server_default="0"),
    col("gap_reasons_json", JSON, nullable=False, default=dict),
    col("capture_config_revision", Integer, nullable=False),
    col("finalized_at_ms", BigInteger),
)
detection_events = Table(
    "detection_events",
    metadata,
    pk(),
    fk("camera_id", "cameras.id", nullable=False, index=True),
    col("trigger_at_ms", BigInteger, nullable=False),
    col("reason", nullable=False),
    col("score", String),
    col("roi_version", Integer, nullable=False),
    col("desired_start_ms", BigInteger, nullable=False),
    col("desired_end_ms", BigInteger, nullable=False),
    col("job_id"),
    col("resolved", Boolean, nullable=False, server_default="0"),
)
minute_revisions = Table(
    "minute_revisions",
    metadata,
    fk("minute_id", "minute_records.id", primary_key=True),
    col("revision", Integer, primary_key=True),
    col("source_kind", nullable=False),
    col("source_fingerprint", nullable=False),
    col("reference_set_id"),
    col("result_json", JSON, nullable=False),
    col("created_at_ms", BigInteger, nullable=False),
    UniqueConstraint("minute_id", "source_fingerprint"),
)
evidence_refs = Table(
    "evidence_refs",
    metadata,
    pk("evidence_id"),
    fk("camera_id", "cameras.id", nullable=False),
    col("captured_at_ms", BigInteger, nullable=False),
    col("expires_at_ms", BigInteger, nullable=False),
    col("state", nullable=False),
    col("reason"),
    col("width", Integer),
    col("height", Integer),
    col("sha256"),
)
revision_evidence_links = Table(
    "revision_evidence_links",
    metadata,
    col("owner_kind", primary_key=True),
    col("owner_id", primary_key=True),
    col("owner_revision", Integer, primary_key=True),
    fk("evidence_id", "evidence_refs.evidence_id", primary_key=True),
    col("role", nullable=False),
    CheckConstraint("role IN ('observed','supporting')"),
)
activity_spans = Table(
    "activity_spans",
    metadata,
    pk(),
    fk("camera_id", "cameras.id", nullable=False),
    col("subject_id", index=True),
    col("identity_status", nullable=False),
    fk("minute_id", "minute_records.id", nullable=False),
    col("minute_revision", Integer, nullable=False),
    col("start_at_ms", BigInteger, nullable=False, index=True),
    col("end_at_ms", BigInteger, nullable=False),
    col("activity", nullable=False),
    col("origin", nullable=False),
    col("support", nullable=False),
    col("boundary_json", JSON, nullable=False),
    col("evidence_ids_json", JSON, nullable=False),
    col("details_json", JSON, nullable=False),
    CheckConstraint("end_at_ms > start_at_ms"),
)
events = Table(
    "events",
    metadata,
    pk(),
    col("scope_key", nullable=False, index=True),
    col("current_revision", Integer, nullable=False),
    col("created_at_ms", BigInteger, nullable=False),
)
event_revisions = Table(
    "event_revisions",
    metadata,
    fk("event_id", "events.id", primary_key=True),
    col("revision", Integer, primary_key=True),
    col("source_fingerprint", nullable=False),
    col("intervals_json", JSON, nullable=False),
    col("summary_json", JSON, nullable=False),
    col("replacement_event_ids_json", JSON, nullable=False),
    col("created_at_ms", BigInteger, nullable=False),
)
manual_corrections = Table(
    "manual_corrections",
    metadata,
    pk(),
    fk("event_id", "events.id", nullable=False),
    col("base_source_revision", Integer, nullable=False),
    col("correction_json", JSON, nullable=False),
    col("reason", nullable=False),
    col("created_at_ms", BigInteger, nullable=False),
    col("superseded_by"),
)
summaries = Table(
    "summaries",
    metadata,
    pk(),
    col("type", nullable=False),
    col("scope_key", nullable=False),
    col("start_at_ms", BigInteger, nullable=False),
    col("end_at_ms", BigInteger, nullable=False),
    col("current_revision", Integer, nullable=False, server_default="0"),
    col("pending_job_id"),
    col("source_state", nullable=False, server_default="stale"),
    UniqueConstraint("type", "scope_key", "start_at_ms"),
)
summary_revisions = Table(
    "summary_revisions",
    metadata,
    fk("summary_id", "summaries.id", primary_key=True),
    col("revision", Integer, primary_key=True),
    col("source_fingerprint", nullable=False),
    col("source_refs_json", JSON, nullable=False),
    col("reference_set_id"),
    col("coverage_json", JSON, nullable=False),
    col("activities_json", JSON, nullable=False),
    col("narrative", nullable=False),
    col("summary_state", nullable=False),
    col("details_json", JSON, nullable=False),
    col("created_at_ms", BigInteger, nullable=False),
)
entities = Table(
    "entities",
    metadata,
    pk(),
    *config_columns(),
    col("kind", nullable=False),
    col("name", nullable=False),
    col("description", nullable=False),
    col("enabled", Boolean, nullable=False),
    col("is_primary_subject", Boolean, nullable=False),
    col("camera_ids_json", JSON, nullable=False),
    col("deleted_at_ms", BigInteger),
    CheckConstraint("kind IN ('person','object')"),
    CheckConstraint("NOT is_primary_subject OR (kind = 'person' AND enabled = 1 AND deleted_at_ms IS NULL)"),
)
Index(
    "one_primary_subject",
    entities.c.is_primary_subject,
    unique=True,
    sqlite_where=text("is_primary_subject = 1"),
)
reference_assets = Table(
    "reference_assets",
    metadata,
    pk(),
    fk("entity_id", "entities.id", nullable=False),
    *config_columns(),
    col("current_version", Integer, nullable=False),
    col("enabled", Boolean, nullable=False),
    col("name_override"),
    col("source_evidence_id"),
    col("deleted_at_ms", BigInteger),
)
reference_asset_versions = Table(
    "reference_asset_versions",
    metadata,
    fk("asset_id", "reference_assets.id", primary_key=True),
    col("version", Integer, primary_key=True),
    col("name_snapshot", nullable=False),
    col("relpath", nullable=False, unique=True),
    col("sha256", nullable=False),
    col("width", Integer, nullable=False),
    col("height", Integer, nullable=False),
    col("bytes", BigInteger, nullable=False),
    col("created_at_ms", BigInteger, nullable=False),
    col("deleted_at_ms", BigInteger),
)
reference_sets = Table(
    "reference_sets",
    metadata,
    pk(),
    col("fingerprint", nullable=False, unique=True),
    col("members_json", JSON, nullable=False),
    col("created_at_ms", BigInteger, nullable=False),
)
providers = Table(
    "providers",
    metadata,
    pk(),
    *config_columns(),
    col("name", nullable=False),
    col("config_json", JSON, nullable=False),
    col("api_key_cipher"),
    col("capabilities_json", JSON, nullable=False),
    col("tested_config_revision", Integer),
    col("tested_at_ms", BigInteger),
    col("last_test_error"),
    col("paused", Boolean, nullable=False, server_default="0"),
    col("deleted_at_ms", BigInteger),
)
settings = Table(
    "settings",
    metadata,
    pk(),
    *config_columns(),
    col("config_json", JSON, nullable=False),
    CheckConstraint("id = 'singleton'"),
)
jobs = Table(
    "jobs",
    metadata,
    pk(),
    *stamps(),
    col("kind", nullable=False),
    col("dedup_key", nullable=False, unique=True),
    col("payload_json", JSON, nullable=False),
    col("state", nullable=False),
    col("queue_reason"),
    col("phase", nullable=False),
    col("progress_json", JSON, nullable=False),
    col("priority", Integer, nullable=False),
    col("not_before_ms", BigInteger, nullable=False),
    col("deadline_at_ms", BigInteger),
    col("lease_owner"),
    col("lease_token"),
    col("lease_until_ms", BigInteger),
    col("cancel_requested", Boolean, nullable=False),
    col("attempt_count", Integer, nullable=False),
    col("started_at_ms", BigInteger),
    col("finished_at_ms", BigInteger),
    col("result_json", JSON, nullable=False),
    col("error_code"),
    col("error_json", JSON),
    CheckConstraint("state IN ('queued','running','succeeded','partial','failed','expired','cancelled')"),
)
Index("jobs_claim", jobs.c.state, jobs.c.not_before_ms, jobs.c.priority, jobs.c.deadline_at_ms)
job_dependencies = Table(
    "job_dependencies",
    metadata,
    fk("job_id", "jobs.id", primary_key=True),
    fk("depends_on_id", "jobs.id", primary_key=True),
    col("accept_partial", Boolean, nullable=False),
    CheckConstraint("job_id <> depends_on_id"),
)
job_attempts = Table(
    "job_attempts",
    metadata,
    pk(),
    fk("job_id", "jobs.id", nullable=False),
    col("attempt_no", Integer, nullable=False),
    col("lease_token", nullable=False),
    col("started_at_ms", BigInteger, nullable=False),
    col("finished_at_ms", BigInteger),
    col("request_fingerprint", nullable=False),
    col("provider_request_id"),
    col("state", nullable=False),
    col("error_code"),
    UniqueConstraint("job_id", "attempt_no"),
)
ai_usage = Table(
    "ai_usage",
    metadata,
    fk("attempt_id", "job_attempts.id", primary_key=True),
    fk("provider_id", "providers.id", nullable=False, index=True),
    col("model", nullable=False),
    col("purpose", nullable=False),
    col("input_tokens", BigInteger),
    col("output_tokens", BigInteger),
    col("cached_tokens", BigInteger),
    col("image_count", Integer, nullable=False),
    col("reference_image_count", Integer, nullable=False),
    col("request_bytes", BigInteger, nullable=False),
    col("latency_ms", BigInteger),
    col("reported_cost", String),
    col("currency"),
    col("billing_status", nullable=False),
    col("created_at_ms", BigInteger, nullable=False),
    col("state", nullable=False),
)
idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    fk("user_id", "users.id", primary_key=True),
    col("operation_id", primary_key=True),
    col("key", primary_key=True),
    col("request_hash", nullable=False),
    col("resource_id"),
    col("response_status", Integer, nullable=False),
    col("response_json", JSON),
    col("expires_at_ms", BigInteger, nullable=False, index=True),
)
audit_events = Table(
    "audit_events",
    metadata,
    pk(),
    col("occurred_at_ms", BigInteger, nullable=False, index=True),
    col("action", nullable=False),
    col("resource_kind", nullable=False),
    col("resource_id"),
    col("actor", nullable=False),
    col("request_id"),
    col("safe_details_json", JSON, nullable=False),
)
system_state = Table(
    "system_state",
    metadata,
    pk(),
    col("value_json", JSON, nullable=False),
    col("updated_at_ms", BigInteger, nullable=False),
)
rate_limits = Table(
    "rate_limits",
    metadata,
    col("scope", primary_key=True),
    col("window_ms", BigInteger, primary_key=True),
    col("count", Integer, nullable=False),
)
