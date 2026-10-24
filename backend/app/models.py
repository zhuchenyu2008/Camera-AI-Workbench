from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Float, ForeignKey, Index, Integer,
                        JSON, LargeBinary, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utc_now_ms() -> int:
    """UTC epoch milliseconds; all persisted timestamps use this representation."""
    return int(time.time() * 1000)


def new_uuid() -> str:
    return str(uuid4())


def obj() -> dict[str, Any]:
    return {}


def arr() -> list[Any]:
    return []


class TimestampMixin:
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms,
                                               onupdate=utc_now_ms, nullable=False)


class RetentionPolicy(TimestampMixin, Base):
    __tablename__ = "retention_policy"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    ordinary_frame_days: Mapped[float] = mapped_column(Float, default=(1 / 24), nullable=False)
    event_frame_days: Mapped[float] = mapped_column(Float, default=(6 / 24), nullable=False)
    favorite_frame_days: Mapped[int] = mapped_column(Integer, default=365, nullable=False)
    max_storage_bytes: Mapped[int | None] = mapped_column(BigInteger)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=obj, nullable=False)
    cameras: Mapped[list[Camera]] = relationship(back_populates="retention_policy")


class Camera(TimestampMixin, Base):
    __tablename__ = "camera"
    __table_args__ = (Index("ix_camera_enabled_status", "enabled", "status"),
                      CheckConstraint("sampling_interval_ms > 0", name="ck_camera_interval"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    rtsp_url_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    sampling_interval_ms: Mapped[int] = mapped_column(Integer, default=1000, server_default="1000", nullable=False)
    frame_width: Mapped[int | None] = mapped_column(Integer)
    frame_height: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="offline", server_default="offline", nullable=False)
    last_frame_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    # 乐观并发控制：每次配置写入都会递增，API 不再用常量版本号响应。
    config_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    retention_policy_id: Mapped[str | None] = mapped_column(ForeignKey("retention_policy.id", ondelete="SET NULL"))
    retention_policy: Mapped[RetentionPolicy | None] = relationship(back_populates="cameras")
    frames: Mapped[list[Frame]] = relationship(back_populates="camera", cascade="all, delete-orphan", passive_deletes=True)
    gaps: Mapped[list[CameraGap]] = relationship(back_populates="camera", cascade="all, delete-orphan", passive_deletes=True)
    change_events: Mapped[list[ChangeEvent]] = relationship(back_populates="camera", cascade="all, delete-orphan", passive_deletes=True)
    minute_observations: Mapped[list[MinuteObservation]] = relationship(back_populates="camera", cascade="all, delete-orphan", passive_deletes=True)
    l2_windows: Mapped[list[L2Window]] = relationship(back_populates="camera", cascade="all, delete-orphan", passive_deletes=True)
    activity_segments: Mapped[list[ActivitySegment]] = relationship(back_populates="camera", cascade="all, delete-orphan", passive_deletes=True)
    summaries: Mapped[list[Summary]] = relationship(back_populates="camera", cascade="all, delete-orphan", passive_deletes=True)
    ai_jobs: Mapped[list[AIJob]] = relationship(back_populates="camera")
    person_clusters: Mapped[list[PersonCluster]] = relationship(back_populates="camera", cascade="all, delete-orphan", passive_deletes=True)


class FrameBlob(Base):
    __tablename__ = "frame_blob"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    relative_path: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), default="image/jpeg", nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    frames: Mapped[list[Frame]] = relationship(back_populates="blob")


class Frame(Base):
    __tablename__ = "frame"
    __table_args__ = (UniqueConstraint("camera_id", "sequence", name="uq_frame_camera_sequence"),
                      Index("ix_frame_camera_captured", "camera_id", "captured_at_ms"),
                      Index("ix_frame_retained_favorite", "retained_until_ms", "favorite"),
                      CheckConstraint("byte_size >= 0", name="ck_frame_bytes"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    camera_id: Mapped[str] = mapped_column(ForeignKey("camera.id", ondelete="CASCADE"), nullable=False)
    captured_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ingested_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    thumbnail_relative_path: Mapped[str | None] = mapped_column(String(1024))
    local_signal_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    pin_until_ms: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    blob_sha256: Mapped[str | None] = mapped_column(ForeignKey("frame_blob.sha256", ondelete="SET NULL"))
    perceptual_hash: Mapped[str | None] = mapped_column(String(128))
    diff_score: Mapped[float | None]
    duplicate_of_id: Mapped[str | None] = mapped_column(ForeignKey("frame.id", ondelete="SET NULL"))
    event_id: Mapped[str | None] = mapped_column(ForeignKey("change_event.id", ondelete="SET NULL"))
    retained_until_ms: Mapped[int | None] = mapped_column(BigInteger)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="active", server_default="active", nullable=False)
    camera: Mapped[Camera] = relationship(back_populates="frames")
    blob: Mapped[FrameBlob | None] = relationship(back_populates="frames")
    duplicate_of: Mapped[Frame | None] = relationship(remote_side="Frame.id", back_populates="duplicates")
    duplicates: Mapped[list[Frame]] = relationship(back_populates="duplicate_of")
    event: Mapped[ChangeEvent | None] = relationship(back_populates="frames")


class CameraGap(Base):
    __tablename__ = "camera_gap"
    __table_args__ = (Index("ix_camera_gap_camera_start", "camera_id", "start_at_ms"),
                      CheckConstraint("end_at_ms IS NULL OR end_at_ms > start_at_ms", name="ck_camera_gap_interval"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    camera_id: Mapped[str] = mapped_column(ForeignKey("camera.id", ondelete="CASCADE"), nullable=False)
    start_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(80), nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recovered_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    camera: Mapped[Camera] = relationship(back_populates="gaps")


class ChangeEvent(TimestampMixin, Base):
    __tablename__ = "change_event"
    __table_args__ = (Index("ix_change_event_camera_start", "camera_id", "start_at_ms"),
                      CheckConstraint("end_at_ms IS NULL OR end_at_ms > start_at_ms", name="ck_change_event_interval"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    camera_id: Mapped[str] = mapped_column(ForeignKey("camera.id", ondelete="CASCADE"), nullable=False)
    start_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    trigger_score: Mapped[float] = mapped_column(nullable=False)
    trigger_reason: Mapped[str] = mapped_column(String(120), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), default="normal", nullable=False)
    pre_roll_start_ms: Mapped[int | None] = mapped_column(BigInteger)
    post_roll_end_ms: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(24), default="open", nullable=False)
    config_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=obj, nullable=False)
    camera: Mapped[Camera] = relationship(back_populates="change_events")
    frames: Mapped[list[Frame]] = relationship(back_populates="event")

class MinuteObservation(Base):
    __tablename__ = "minute_observation"
    __table_args__ = (UniqueConstraint("camera_id", "start_at_ms", "end_at_ms", "version", name="uq_minute_observation_version"),
                      Index("ix_minute_observation_camera_window", "camera_id", "start_at_ms", "end_at_ms"),
                      CheckConstraint("end_at_ms > start_at_ms", name="ck_minute_observation_interval"),
                      CheckConstraint("coverage_ratio IS NULL OR (coverage_ratio >= 0 AND coverage_ratio <= 1)", name="ck_minute_coverage"),
                      CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_minute_confidence"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    camera_id: Mapped[str] = mapped_column(ForeignKey("camera.id", ondelete="CASCADE"), nullable=False)
    start_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="complete", nullable=False)
    observation_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=obj, nullable=False)
    candidates_json: Mapped[list[Any]] = mapped_column(JSON, default=arr, nullable=False)
    evidence_frame_ids_json: Mapped[list[str]] = mapped_column(JSON, default=arr, nullable=False)
    gaps_json: Mapped[list[Any]] = mapped_column(JSON, default=arr, nullable=False)
    coverage_ratio: Mapped[float | None]
    confidence: Mapped[float | None]
    ai_job_id: Mapped[str | None] = mapped_column(ForeignKey("ai_job.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    camera: Mapped[Camera] = relationship(back_populates="minute_observations")
    ai_job: Mapped[AIJob | None] = relationship(back_populates="minute_observations")


class L2Window(TimestampMixin, Base):
    """A versioned five-minute analysis window anchoring generated segments."""
    __tablename__ = "l2_window"
    __table_args__ = (UniqueConstraint("camera_id", "start_at_ms", "end_at_ms", "version", name="uq_l2_window_version"),
                      CheckConstraint("end_at_ms > start_at_ms", name="ck_l2_window_interval"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    camera_id: Mapped[str] = mapped_column(ForeignKey("camera.id", ondelete="CASCADE"), nullable=False)
    start_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    input_observation_ids_json: Mapped[list[str]] = mapped_column(JSON, default=arr, nullable=False)
    ai_job_id: Mapped[str | None] = mapped_column(ForeignKey("ai_job.id", ondelete="SET NULL"))
    camera: Mapped[Camera] = relationship(back_populates="l2_windows")
    ai_job: Mapped[AIJob | None] = relationship()
    segments: Mapped[list[ActivitySegment]] = relationship(back_populates="l2_window")


class ActivityCatalog(TimestampMixin, Base):
    __tablename__ = "activity_catalog"
    __table_args__ = (Index("ix_activity_catalog_enabled_name", "enabled", "name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str | None] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list[ActivityAlias]] = relationship(back_populates="catalog", cascade="all, delete-orphan", passive_deletes=True)
    segments: Mapped[list[ActivitySegment]] = relationship(back_populates="canonical_activity")


class ActivityAlias(Base):
    __tablename__ = "activity_alias"
    __table_args__ = (UniqueConstraint("catalog_id", "normalized_alias", name="uq_activity_alias_normalized"),
                      Index("ix_activity_alias_normalized", "normalized_alias"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    catalog_id: Mapped[str] = mapped_column(ForeignKey("activity_catalog.id", ondelete="CASCADE"), nullable=False)
    alias: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    catalog: Mapped[ActivityCatalog] = relationship(back_populates="aliases")


class ActivitySegment(TimestampMixin, Base):
    __tablename__ = "activity_segment"
    __table_args__ = (Index("ix_activity_segment_camera_window", "camera_id", "start_at_ms", "end_at_ms"),
                      Index("ix_activity_segment_l2_window", "l2_window_id"),
                      CheckConstraint("end_at_ms > start_at_ms", name="ck_segment_interval"),
                      CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_segment_confidence"),
                      CheckConstraint("evidence_coverage IS NULL OR (evidence_coverage >= 0 AND evidence_coverage <= 1)", name="ck_segment_coverage"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    camera_id: Mapped[str] = mapped_column(ForeignKey("camera.id", ondelete="CASCADE"), nullable=False)
    l2_window_id: Mapped[str | None] = mapped_column(ForeignKey("l2_window.id", ondelete="SET NULL"))
    start_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    canonical_activity_id: Mapped[str | None] = mapped_column(ForeignKey("activity_catalog.id", ondelete="SET NULL"))
    display_description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(100))
    confidence: Mapped[float | None]
    evidence_coverage: Mapped[float | None]
    state: Mapped[str] = mapped_column(String(32), default="provisional", nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("activity_segment.id", ondelete="SET NULL"))
    source_observation_ids_json: Mapped[list[str]] = mapped_column(JSON, default=arr, nullable=False)
    camera: Mapped[Camera] = relationship(back_populates="activity_segments")
    l2_window: Mapped[L2Window | None] = relationship(back_populates="segments")
    canonical_activity: Mapped[ActivityCatalog | None] = relationship(back_populates="segments")
    supersedes: Mapped[ActivitySegment | None] = relationship(remote_side="ActivitySegment.id", back_populates="superseded_by")
    superseded_by: Mapped[list[ActivitySegment]] = relationship(back_populates="supersedes")
    intervals: Mapped[list[ActivityInterval]] = relationship(back_populates="segment", cascade="all, delete-orphan", passive_deletes=True)


class ActivityInterval(Base):
    __tablename__ = "activity_interval"
    __table_args__ = (Index("ix_activity_interval_segment_start", "segment_id", "start_at_ms"),
                      CheckConstraint("end_at_ms > start_at_ms", name="ck_activity_interval_interval"),
                      CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_activity_interval_confidence"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    segment_id: Mapped[str] = mapped_column(ForeignKey("activity_segment.id", ondelete="CASCADE"), nullable=False)
    start_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    confidence: Mapped[float | None]
    reason: Mapped[str | None] = mapped_column(Text)
    anchor_frame_ids_json: Mapped[list[str]] = mapped_column(JSON, default=arr, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    segment: Mapped[ActivitySegment] = relationship(back_populates="intervals")

class Summary(TimestampMixin, Base):
    __tablename__ = "summary"
    __table_args__ = (UniqueConstraint("camera_id", "period_kind", "period_start_ms", "version", name="uq_summary_period_version"),
                      Index("ix_summary_camera_period", "camera_id", "period_kind", "period_start_ms"),
                      CheckConstraint("period_end_ms > period_start_ms", name="ck_summary_interval"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    camera_id: Mapped[str | None] = mapped_column(ForeignKey("camera.id", ondelete="CASCADE"))
    period_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    statistics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=obj, nullable=False)
    source_segment_ids_json: Mapped[list[str]] = mapped_column(JSON, default=arr, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    ai_job_id: Mapped[str | None] = mapped_column(ForeignKey("ai_job.id", ondelete="SET NULL"))
    camera: Mapped[Camera | None] = relationship(back_populates="summaries")
    ai_job: Mapped[AIJob | None] = relationship(back_populates="summaries")


class AIProvider(TimestampMixin, Base):
    __tablename__ = "ai_provider"
    __table_args__ = (Index("ix_ai_provider_enabled_label", "enabled", "label"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    api_key_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    allow_insecure_local: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    capability_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=obj, nullable=False)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=30000, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    bindings: Mapped[list[AIBinding]] = relationship(back_populates="provider", foreign_keys="AIBinding.provider_id")
    fallback_bindings: Mapped[list[AIBinding]] = relationship(back_populates="fallback_provider", foreign_keys="AIBinding.fallback_provider_id")
    usages: Mapped[list[AIUsage]] = relationship(back_populates="provider")


class AIBinding(TimestampMixin, Base):
    __tablename__ = "ai_binding"
    __table_args__ = (UniqueConstraint("layer", name="uq_ai_binding_layer"),
                      CheckConstraint("temperature IS NULL OR temperature >= 0", name="ck_binding_temperature"),
                      CheckConstraint("max_output_tokens IS NULL OR max_output_tokens > 0", name="ck_binding_tokens"),
                      CheckConstraint("concurrency > 0", name="ck_binding_concurrency"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    layer: Mapped[str] = mapped_column(String(8), nullable=False)
    provider_id: Mapped[str] = mapped_column(ForeignKey("ai_provider.id", ondelete="RESTRICT"), nullable=False)
    model_name: Mapped[str] = mapped_column(String(240), nullable=False)
    fallback_provider_id: Mapped[str | None] = mapped_column(ForeignKey("ai_provider.id", ondelete="SET NULL"))
    temperature: Mapped[float | None]
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    concurrency: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    image_support: Mapped[bool | None] = mapped_column(Boolean)
    tools_support: Mapped[bool | None] = mapped_column(Boolean)
    json_mode_support: Mapped[bool | None] = mapped_column(Boolean)
    provider: Mapped[AIProvider] = relationship(back_populates="bindings", foreign_keys=[provider_id])
    fallback_provider: Mapped[AIProvider | None] = relationship(back_populates="fallback_bindings", foreign_keys=[fallback_provider_id])


class AIJob(TimestampMixin, Base):
    __tablename__ = "ai_job"
    __table_args__ = (Index("ix_ai_job_claim", "state", "available_at_ms", "priority"),
                      Index("ix_ai_job_camera_window", "camera_id", "period_start_ms", "period_end_ms"),
                      CheckConstraint("attempts >= 0", name="ck_job_attempts"),
                      CheckConstraint("max_attempts > 0", name="ck_job_max_attempts"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    layer: Mapped[str] = mapped_column(String(8), nullable=False)
    camera_id: Mapped[str | None] = mapped_column(ForeignKey("camera.id", ondelete="CASCADE"))
    period_start_ms: Mapped[int | None] = mapped_column(BigInteger)
    period_end_ms: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), default="v1", nullable=False)
    schema_version: Mapped[str] = mapped_column(String(80), default="v1", nullable=False)
    input_frame_ids_json: Mapped[list[str]] = mapped_column(JSON, default=arr, nullable=False)
    input_ref_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=obj, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    # Opaque per-lease fencing token; prevents a stale worker from committing after lease recovery.
    lease_token: Mapped[str | None] = mapped_column(String(256))
    deadline_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    lease_expires_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    heartbeat_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    started_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    finished_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    result_ref: Mapped[str | None] = mapped_column(String(1024))
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_safe_message: Mapped[str | None] = mapped_column(Text)
    camera: Mapped[Camera | None] = relationship(back_populates="ai_jobs")
    minute_observations: Mapped[list[MinuteObservation]] = relationship(back_populates="ai_job")
    summaries: Mapped[list[Summary]] = relationship(back_populates="ai_job")
    usages: Mapped[list[AIUsage]] = relationship(back_populates="job", cascade="all, delete-orphan", passive_deletes=True)
    tool_calls: Mapped[list[ToolCall]] = relationship(back_populates="job", cascade="all, delete-orphan", passive_deletes=True)


class AIUsage(Base):
    __tablename__ = "ai_usage"
    __table_args__ = (Index("ix_ai_usage_created_provider", "created_at_ms", "provider_id"),
                      CheckConstraint("frame_count >= 0 AND image_bytes >= 0", name="ck_usage_nonnegative"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("ai_job.id", ondelete="SET NULL"))
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("ai_provider.id", ondelete="SET NULL"))
    layer: Mapped[str | None] = mapped_column(String(8))
    model_name: Mapped[str | None] = mapped_column(String(240))
    request_id: Mapped[str | None] = mapped_column(String(240))
    frame_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    image_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    retry_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_estimate: Mapped[float | None]
    currency: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str | None] = mapped_column(String(80))
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    job: Mapped[AIJob | None] = relationship(back_populates="usages")
    provider: Mapped[AIProvider | None] = relationship(back_populates="usages")


class ToolCall(Base):
    __tablename__ = "tool_call"
    __table_args__ = (Index("ix_tool_call_job_created", "job_id", "created_at_ms"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("ai_job.id", ondelete="CASCADE"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    safe_arguments_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=obj, nullable=False)
    frame_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    affected_final_conclusion: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    job: Mapped[AIJob] = relationship(back_populates="tool_calls")

class PersonCluster(TimestampMixin, Base):
    __tablename__ = "person_cluster"
    __table_args__ = (Index("ix_person_cluster_camera_last_seen", "camera_id", "last_seen_at_ms"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    camera_id: Mapped[str] = mapped_column(ForeignKey("camera.id", ondelete="CASCADE"), nullable=False)
    anonymous_label: Mapped[str] = mapped_column(String(120), nullable=False)
    first_seen_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    last_seen_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    confidence: Mapped[float | None]
    representative_frame_id: Mapped[str | None] = mapped_column(ForeignKey("frame.id", ondelete="SET NULL"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    feature_ref: Mapped[str | None] = mapped_column(String(1024))
    camera: Mapped[Camera] = relationship(back_populates="person_clusters")
    representative_frame: Mapped[Frame | None] = relationship()
    appearances: Mapped[list[PersonAppearance]] = relationship(back_populates="cluster", cascade="all, delete-orphan", passive_deletes=True)


class PersonAppearance(Base):
    __tablename__ = "person_appearance"
    __table_args__ = (Index("ix_person_appearance_cluster_time", "cluster_id", "seen_at_ms"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cluster_id: Mapped[str] = mapped_column(ForeignKey("person_cluster.id", ondelete="CASCADE"), nullable=False)
    frame_id: Mapped[str | None] = mapped_column(ForeignKey("frame.id", ondelete="SET NULL"))
    seen_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    confidence: Mapped[float | None]
    bounding_box_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    cluster: Mapped[PersonCluster] = relationship(back_populates="appearances")
    frame: Mapped[Frame | None] = relationship()


class Admin(TimestampMixin, Base):
    __tablename__ = "admin"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(String(80), unique=True, default="admin", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until_ms: Mapped[int | None] = mapped_column(BigInteger)
    password_changed_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    sessions: Mapped[list[AuthSession]] = relationship(back_populates="admin", cascade="all, delete-orphan", passive_deletes=True)
    user_edits: Mapped[list[UserEdit]] = relationship(back_populates="admin")
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="admin")


class AuthSession(Base):
    __tablename__ = "session"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_session_token_hash"),
                      Index("ix_session_active_expiry", "revoked_at_ms", "expires_at_ms"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    admin_id: Mapped[str] = mapped_column(ForeignKey("admin.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    revoked_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    admin: Mapped[Admin] = relationship(back_populates="sessions")


# Keep the short documented model name available without shadowing the ORM
# Session class used by app.db integrations.
SessionRecord = AuthSession


class UserEdit(Base):
    __tablename__ = "user_edit"
    __table_args__ = (Index("ix_user_edit_target_created", "target_type", "target_id", "created_at_ms"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    target_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)
    admin_id: Mapped[str | None] = mapped_column(ForeignKey("admin.id", ondelete="SET NULL"))
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    admin: Mapped[Admin | None] = relationship(back_populates="user_edits")


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_created_action", "created_at_ms", "action"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    admin_id: Mapped[str | None] = mapped_column(ForeignKey("admin.id", ondelete="SET NULL"))
    # API 会话使用独立的 auth_users 表；actor_id 保留操作者标识，避免把
    # 非 ORM 会话 ID 强行写入 admin 外键。
    actor_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(80))
    target_id: Mapped[str | None] = mapped_column(String(36))
    request_id: Mapped[str | None] = mapped_column(String(120))
    # Callers must remove passwords, tokens, provider payloads and credentials.
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=obj, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)
    admin: Mapped[Admin | None] = relationship(back_populates="audit_logs")


class OutboxEvent(Base):
    __tablename__ = "outbox_event"
    __table_args__ = (Index("ix_outbox_event_pending", "published_at_ms", "created_at_ms"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=obj, nullable=False)
    sequence: Mapped[int | None] = mapped_column(BigInteger)
    published_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, default=utc_now_ms, nullable=False)


class AppSetting(TimestampMixin, Base):
    __tablename__ = "app_setting"
    __table_args__ = (UniqueConstraint("key", name="uq_app_setting_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


# Public alias matches the design doc terminology (Session token persistence).
Session = AuthSession


__all__ = [
    "AIJob", "AIBinding", "AIProvider", "AIUsage", "ActivityAlias", "ActivityCatalog",
    "ActivityInterval", "ActivitySegment", "Admin", "AppSetting", "AuditLog", "Base",
    "Camera", "CameraGap", "ChangeEvent", "Frame", "FrameBlob", "L2Window",
    "MinuteObservation", "OutboxEvent", "PersonAppearance", "PersonCluster",
    "RetentionPolicy", "Session", "SessionRecord", "Summary", "ToolCall", "UserEdit",
    "new_uuid", "utc_now_ms",
]


# The API compatibility adapter in app.main uses these persistence tables. They
# share this Base so one Alembic head initializes both the documented domain
# schema and the generic-resource auth/session layer without runtime fake data.
class ApiResource(Base):
    __tablename__ = "api_resources"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deleted_at_ms: Mapped[int | None] = mapped_column(BigInteger)


class ApiIdempotency(Base):
    __tablename__ = "api_idempotency"
    scope: Mapped[str] = mapped_column(String(255), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ApiAuthUser(Base):
    __tablename__ = "api_auth_users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ApiAuthSession(Base):
    __tablename__ = "api_auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_cipher: Mapped[str] = mapped_column(Text, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at_ms: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    revoked_at_ms: Mapped[int | None] = mapped_column(BigInteger)


__all__.extend(["ApiResource", "ApiIdempotency", "ApiAuthUser", "ApiAuthSession"])
