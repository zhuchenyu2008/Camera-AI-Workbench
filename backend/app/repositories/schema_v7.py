"""0007：时间片观察与上级裁决的最小持久化结构。"""

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
    UniqueConstraint,
)

from app.repositories import schema_v6

metadata = MetaData()
for original in schema_v6.metadata.tables.values():
    globals()[original.name] = original.to_metadata(metadata)


temporal_slices = Table(
    "temporal_slices",
    metadata,
    Column("id", String, primary_key=True),
    Column("camera_id", String, ForeignKey("cameras.id"), nullable=False, index=True),
    Column("session_id", String, ForeignKey("stream_sessions.id")),
    Column("start_at_ms", BigInteger, nullable=False),
    Column("end_at_ms", BigInteger, nullable=False),
    Column("overlap_from_ms", BigInteger),
    Column("overlap_to_ms", BigInteger),
    Column("planned_coverage_json", JSON, nullable=False),
    Column("evidence_ids_json", JSON, nullable=False),
    Column("evidence_fingerprint", String, nullable=False),
    Column("missing_ranges_json", JSON, nullable=False),
    Column("actual_start_at_ms", BigInteger),
    Column("actual_end_at_ms", BigInteger),
    Column("coverage_mode", String, nullable=False),
    Column("source_revision", Integer, nullable=False),
    Column("reference_set_id", String),
    Column("prompt_version", String),
    Column("capability_version", String),
    Column("earliest_expires_at_ms", BigInteger),
    Column("deadline_at_ms", BigInteger),
    Column("state", String, nullable=False, server_default="open"),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("updated_at_ms", BigInteger, nullable=False),
    UniqueConstraint("camera_id", "start_at_ms", "end_at_ms", "source_revision", "evidence_fingerprint"),
    CheckConstraint("end_at_ms > start_at_ms"),
    CheckConstraint("coverage_mode IN ('dense','sparse','mixed')"),
    CheckConstraint("state IN ('open','observing','complete','partial','expired','cancelled')"),
    CheckConstraint("overlap_to_ms IS NULL OR overlap_from_ms IS NOT NULL"),
    CheckConstraint("overlap_to_ms IS NULL OR overlap_to_ms > overlap_from_ms"),
)


local_observation_runs = Table(
    "local_observation_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("slice_id", String, ForeignKey("temporal_slices.id"), nullable=False, index=True),
    Column("job_id", String, ForeignKey("jobs.id")),
    Column("provider_id", String, ForeignKey("providers.id")),
    Column("model", String),
    Column("prompt_version", String),
    Column("capability_version", String),
    Column("schema_version", String, nullable=False),
    Column("request_fingerprint", String, nullable=False),
    Column("attempt_no", Integer, nullable=False, server_default="1"),
    Column("status", String, nullable=False, server_default="queued"),
    Column("input_snapshot_json", JSON, nullable=False),
    Column("result_json", JSON),
    Column("evidence_ids_json", JSON, nullable=False),
    Column("error_json", JSON),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("updated_at_ms", BigInteger, nullable=False),
    Column("finished_at_ms", BigInteger),
    UniqueConstraint("slice_id", "request_fingerprint"),
    CheckConstraint("attempt_no > 0"),
    CheckConstraint("status IN ('queued','running','succeeded','partial','failed','expired','cancelled')"),
)


temporal_review_runs = Table(
    "temporal_review_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("camera_id", String, ForeignKey("cameras.id"), nullable=False, index=True),
    Column("job_id", String, ForeignKey("jobs.id")),
    # Temporal checkpoints point back to their review run; SQLite accepts the
    # forward reference here and the migration creates both tables with FK
    # checks temporarily disabled.
    Column("base_checkpoint_id", String, ForeignKey("temporal_checkpoints.id")),
    Column("reviewed_from_ms", BigInteger, nullable=False),
    Column("reviewed_to_ms", BigInteger, nullable=False),
    Column("trigger_kind", String, nullable=False),
    Column("trigger_reason", String),
    Column("checkpoint_revision", Integer, nullable=False, server_default="0"),
    Column("tool_protocol_version", String),
    Column("schema_version", String, nullable=False),
    Column("budget_json", JSON, nullable=False),
    Column("status", String, nullable=False, server_default="queued"),
    Column("request_fingerprint", String, nullable=False, unique=True),
    Column("result_json", JSON),
    Column("error_json", JSON),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("updated_at_ms", BigInteger, nullable=False),
    Column("finished_at_ms", BigInteger),
    CheckConstraint("reviewed_to_ms > reviewed_from_ms"),
    CheckConstraint("trigger_kind IN ('rolling','issue','finalize','manual')"),
    CheckConstraint("status IN ('queued','running','succeeded','partial','failed','expired','cancelled')"),
)


temporal_checkpoints = Table(
    "temporal_checkpoints",
    metadata,
    Column("id", String, primary_key=True),
    Column("camera_id", String, ForeignKey("cameras.id"), nullable=False, index=True),
    Column("review_run_id", String, ForeignKey("temporal_review_runs.id"), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("reviewed_from_ms", BigInteger, nullable=False),
    Column("reviewed_to_ms", BigInteger, nullable=False),
    Column("frozen_to_ms", BigInteger),
    Column("mutable_tail_start_ms", BigInteger),
    Column("timeline_json", JSON, nullable=False),
    Column("local_slice_ids_json", JSON, nullable=False),
    Column("evidence_ids_json", JSON, nullable=False),
    Column("hard_boundaries_json", JSON, nullable=False),
    Column("conflicts_json", JSON, nullable=False),
    Column("unresolved_json", JSON, nullable=False),
    Column("source_fingerprint", String, nullable=False),
    Column("status", String, nullable=False, server_default="complete"),
    Column("next_review_at_ms", BigInteger),
    Column("trigger_reason", String),
    Column("created_at_ms", BigInteger, nullable=False),
    UniqueConstraint("camera_id", "revision"),
    UniqueConstraint("camera_id", "source_fingerprint"),
    CheckConstraint("reviewed_to_ms > reviewed_from_ms"),
    CheckConstraint("frozen_to_ms IS NULL OR frozen_to_ms <= reviewed_to_ms"),
    CheckConstraint("status IN ('complete','partial','superseded')"),
)


published_timelines = Table(
    "published_timelines",
    metadata,
    Column("id", String, primary_key=True),
    Column("camera_id", String, ForeignKey("cameras.id"), nullable=False, index=True),
    Column("checkpoint_id", String, ForeignKey("temporal_checkpoints.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("reviewed_from_ms", BigInteger, nullable=False),
    Column("reviewed_to_ms", BigInteger, nullable=False),
    Column("segments_json", JSON, nullable=False),
    Column("unresolved_json", JSON, nullable=False),
    Column("source_fingerprint", String, nullable=False),
    Column("status", String, nullable=False, server_default="published"),
    Column("is_current", Boolean, nullable=False, server_default="1"),
    Column("published_at_ms", BigInteger, nullable=False),
    Column("created_at_ms", BigInteger, nullable=False),
    UniqueConstraint("camera_id", "version"),
    CheckConstraint("reviewed_to_ms > reviewed_from_ms"),
    CheckConstraint("status IN ('published','partial','superseded')"),
)
Index(
    "temporal_published_timelines_current",
    published_timelines.c.camera_id,
    unique=True,
    sqlite_where=published_timelines.c.is_current.is_(True),
)


for name, table in (
    ("temporal_slices", temporal_slices),
    ("local_observation_runs", local_observation_runs),
    ("temporal_review_runs", temporal_review_runs),
    ("temporal_checkpoints", temporal_checkpoints),
    ("published_timelines", published_timelines),
):
    globals()[name] = table
