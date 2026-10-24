"""API 请求/响应 DTO。

该模块只描述公共 HTTP 契约，不依赖 ORM 模型。所有业务层可以通过
``model_validate`` 或 ``from_attributes`` 直接适配这些 DTO，避免路由层把
数据库模型泄漏给前端。
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


UTC = timezone.utc
T = TypeVar("T")


class StrictModel(BaseModel):
    """默认禁止未知字段，避免把前端拼写错误静默写入配置。"""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
    )


class LooseModel(BaseModel):
    """用于模型输出或向前兼容的 DTO；未知字段可以保留。"""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        from_attributes=True,
    )


class Meta(StrictModel):
    request_id: str
    server_time: datetime
    timezone: str = "Asia/Shanghai"
    schema_version: str = "1.0"
    next_cursor: str | None = None
    limit: int | None = None
    has_more: bool | None = None


class Envelope(StrictModel, Generic[T]):
    data: T
    meta: Meta


class ErrorBody(StrictModel):
    code: str
    message: str
    request_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class ErrorEnvelope(StrictModel):
    error: ErrorBody


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)


# 兼容旧调用方
Login = LoginRequest


class SessionDto(StrictModel):
    username: str
    display_name: str
    csrf_token: str
    expires_at: datetime


class PasswordChangeRequest(StrictModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


PasswordChange = PasswordChangeRequest


class HealthDto(LooseModel):
    status: Literal["ok", "degraded", "not_ready"] = "ok"
    alive: bool = True
    ready: bool | None = None
    api_version: str = "1.0"
    database: str | None = None
    migrations: str | None = None
    dependencies: dict[str, Any] = Field(default_factory=dict)


class CursorQuery(StrictModel):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=4096)


class TimeRangeQuery(StrictModel):
    from_: datetime = Field(alias="from")
    to: datetime
    camera_id: str | None = None
    include_observations: bool = False
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("from_", "to")
    @classmethod
    def require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("时间必须包含时区并使用 RFC3339")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def valid_range(self) -> "TimeRangeQuery":
        if self.to <= self.from_:
            raise ValueError("to 必须晚于 from")
        if (self.to - self.from_).total_seconds() > 31 * 24 * 3600:
            raise ValueError("查询范围不能超过 31 天")
        return self


class CameraSource(StrictModel):
    """摄像头源的写入格式；凭据只接受写入，不在任何响应 DTO 中回显。"""

    rtsp_url: str = Field(min_length=8, max_length=4096)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=512)

    @field_validator("rtsp_url")
    @classmethod
    def validate_rtsp(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"rtsp", "rtsps"} or not parts.hostname:
            raise ValueError("rtsp_url 必须是 rtsp:// 或 rtsps:// 地址")
        if any(ch in value for ch in "\r\n\x00"):
            raise ValueError("rtsp_url 包含非法字符")
        return value


class CameraCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    rtsp_url: str | None = Field(default=None, min_length=8, max_length=4096)
    source: CameraSource | None = None
    enabled: bool = False
    sampling_interval_ms: int = Field(default=1000, ge=100, le=60_000)
    frame_width: int | None = Field(default=None, ge=16, le=16_384)
    frame_height: int | None = Field(default=None, ge=16, le=16_384)
    retention_minutes: int | None = Field(default=None, ge=1, le=365 * 24 * 60)
    timezone: str = "Asia/Shanghai"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def source_required(self) -> "CameraCreateRequest":
        if self.rtsp_url is None and self.source is None:
            raise ValueError("必须提供 rtsp_url 或 source")
        if self.rtsp_url is not None:
            CameraSource(rtsp_url=self.rtsp_url)
        return self


CameraCreate = CameraCreateRequest


class CameraUpdateRequest(StrictModel):
    expected_version: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    rtsp_url: str | None = Field(default=None, min_length=8, max_length=4096)
    source: CameraSource | None = None
    enabled: bool | None = None
    sampling_interval_ms: int | None = Field(default=None, ge=100, le=60_000)
    frame_width: int | None = Field(default=None, ge=16, le=16_384)
    frame_height: int | None = Field(default=None, ge=16, le=16_384)
    retention_minutes: int | None = Field(default=None, ge=1, le=365 * 24 * 60)
    timezone: str | None = None
    metadata: dict[str, Any] | None = None
    clear_credentials: bool = False

    @model_validator(mode="after")
    def at_least_one(self) -> "CameraUpdateRequest":
        if not self.model_fields_set - {"expected_version"}:
            raise ValueError("至少提供一个待修改字段")
        if self.rtsp_url is not None:
            CameraSource(rtsp_url=self.rtsp_url)
        return self


CameraPatch = CameraUpdateRequest


class CameraDto(LooseModel):
    id: str
    name: str
    enabled: bool = False
    status: str | None = None
    source_configured: bool = False
    source_host: str | None = None
    sampling_interval_ms: int | None = None
    frame_width: int | None = None
    frame_height: int | None = None
    last_frame_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int = 1


class CameraTestRequest(StrictModel):
    timeout_ms: int = Field(default=10_000, ge=100, le=120_000)
    save_test_frame: bool = False


class CameraReconnectRequest(StrictModel):
    force: bool = False


class CameraHealthDto(LooseModel):
    camera_id: str
    status: str
    last_frame_at: datetime | None = None
    fps: float | None = None
    reconnect_count: int | None = None
    gaps: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class JobDto(LooseModel):
    id: str
    kind: str | None = None
    layer: str | None = None
    state: str
    progress: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    available_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None
    attempt_count: int = 0
    retryable: bool | None = None


class JobRetryRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=500)


class ObservationDto(LooseModel):
    id: str | None = None
    camera_id: str | None = None
    start: datetime
    end: datetime
    description: str | None = None
    evidence_frame_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    state: str | None = None

    @model_validator(mode="after")
    def valid_interval(self) -> "ObservationDto":
        if self.end <= self.start:
            raise ValueError("end 必须晚于 start")
        return self


class ActivityIntervalDto(LooseModel):
    id: str | None = None
    start: datetime
    end: datetime
    evidence_kind: Literal["direct", "inferred", "unknown"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    reason: str | None = None
    evidence_frame_ids: list[str] = Field(default_factory=list)
    anchor_frame_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_interval(self) -> "ActivityIntervalDto":
        if self.end <= self.start:
            raise ValueError("end 必须晚于 start")
        if self.evidence_kind == "inferred" and not self.reason:
            raise ValueError("inferred 区间必须提供 reason")
        return self


class SegmentDto(LooseModel):
    id: str
    camera_id: str
    start: datetime
    end: datetime
    duration_ms: int | None = Field(default=None, ge=0)
    canonical_activity_id: str | None = None
    canonical_activity: str | None = None
    display_description: str | None = None
    category: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence_coverage: float | None = Field(default=None, ge=0, le=1)
    state: str = "unknown"
    locked: bool = False
    version: int = 1
    intervals: list[ActivityIntervalDto] = Field(default_factory=list)
    source_observation_ids: list[str] = Field(default_factory=list)
    needs_review: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TimelineDto(LooseModel):
    segments: list[SegmentDto] = Field(default_factory=list)
    observations: list[ObservationDto] = Field(default_factory=list)
    camera_gaps: list[dict[str, Any]] = Field(default_factory=list)
    window_states: list[dict[str, Any]] = Field(default_factory=list)


class SegmentEditRequest(StrictModel):
    expected_version: int = Field(ge=1)
    start: datetime | None = None
    end: datetime | None = None
    canonical_activity_id: str | None = None
    display_description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=128)
    locked: bool | None = None
    edit_reason: str = Field(min_length=1, max_length=500)

    @field_validator("start", "end")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("时间必须包含时区")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def valid_range_if_both(self) -> "SegmentEditRequest":
        if self.start and self.end and self.end <= self.start:
            raise ValueError("end 必须晚于 start")
        return self


class SegmentSplitRequest(StrictModel):
    split_at: datetime
    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("split_at")
    @classmethod
    def split_time_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("split_at 必须包含时区")
        return value.astimezone(UTC)


class SegmentMergeRequest(StrictModel):
    segment_ids: list[str] = Field(min_length=2, max_length=100)
    expected_versions: dict[str, int] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("segment_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("segment_ids 不能重复")
        return value


class SegmentReprocessRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=500)
    force: bool = False
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None

    @field_validator("from_", "to")
    @classmethod
    def reprocess_time_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("时间必须包含时区")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def range_valid(self) -> "SegmentReprocessRequest":
        if (self.from_ is None) != (self.to is None):
            raise ValueError("from 和 to 必须同时提供")
        if self.from_ and self.to and self.to <= self.from_:
            raise ValueError("to 必须晚于 from")
        return self


class FrameDto(LooseModel):
    id: str
    camera_id: str
    captured_at: datetime
    width: int | None = None
    height: int | None = None
    byte_size: int | None = None
    state: str = "available"
    favorite: bool = False
    expired: bool = False
    content_url: str | None = None


class FrameFavoriteRequest(StrictModel):
    favorite: bool = True


class SummaryDto(LooseModel):
    id: str
    camera_id: str | None = None
    period_kind: Literal["hour", "day"] | str
    period_start: datetime
    period_end: datetime
    timezone: str = "Asia/Shanghai"
    body: str | None = None
    statistics: dict[str, Any] = Field(default_factory=dict)
    source_segment_ids: list[str] = Field(default_factory=list)
    state: str = "unknown"
    version: int = 1
    stale: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SummaryRegenerateRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=500)
    force: bool = False


class ActivityCatalogCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    category: str | None = Field(default=None, max_length=128)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = True
    description: str | None = Field(default=None, max_length=1000)


class ActivityCatalogUpdateRequest(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    category: str | None = Field(default=None, max_length=128)
    aliases: list[str] | None = Field(default=None, max_length=100)
    enabled: bool | None = None
    description: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def non_empty(self) -> "ActivityCatalogUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("至少提供一个待修改字段")
        return self


class ActivityCatalogDto(LooseModel):
    id: str
    name: str
    category: str | None = None
    aliases: list[str] = Field(default_factory=list)
    enabled: bool = True
    description: str | None = None
    usage_count: int | None = None
    version: int = 1


class ProviderCreateRequest(StrictModel):
    label: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=8, max_length=2048)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    timeout_ms: int = Field(default=60_000, ge=100, le=300_000)
    enabled: bool = True
    allow_insecure_local: bool = False

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"https", "http"} or not parts.hostname:
            raise ValueError("base_url 必须是带主机名的 HTTP(S) 地址")
        if parts.username or parts.password or parts.fragment:
            raise ValueError("base_url 不得包含用户凭据或 fragment")
        return value.rstrip("/")


ProviderWrite = ProviderCreateRequest


class ProviderUpdateRequest(StrictModel):
    label: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = Field(default=None, min_length=8, max_length=2048)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    clear_api_key: bool = False
    timeout_ms: int | None = Field(default=None, ge=100, le=300_000)
    enabled: bool | None = None
    allow_insecure_local: bool | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return ProviderCreateRequest.validate_base_url(value)

    @model_validator(mode="after")
    def non_empty(self) -> "ProviderUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("至少提供一个待修改字段")
        return self


class ProviderDto(LooseModel):
    id: str
    label: str
    base_url: str
    base_host: str | None = None
    timeout_ms: int = 60_000
    enabled: bool = True
    api_key_configured: bool = False
    api_key_suffix: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    last_test_at: datetime | None = None
    last_test_error: str | None = None
    version: int = 1


class ProviderTestRequest(StrictModel):
    timeout_ms: int | None = Field(default=None, ge=100, le=120_000)
    test_text: bool = True
    test_vision: bool = True
    test_tools: bool = True
    test_usage: bool = True


ProviderTest = ProviderTestRequest


class BindingDto(LooseModel):
    layer: Literal["l1", "l2", "l3"] | str
    provider_id: str
    model_name: str
    fallback_provider_id: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    concurrency: int = Field(default=1, ge=1, le=100)
    timeout_ms: int | None = Field(default=None, ge=100, le=300_000)


class BindingsUpdateRequest(StrictModel):
    bindings: list[BindingDto] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_layers(self) -> "BindingsUpdateRequest":
        layers = [item.layer for item in self.bindings]
        if len(set(layers)) != len(layers):
            raise ValueError("每个分析层只能有一个 binding")
        return self


class UsageQuery(StrictModel):
    from_: datetime = Field(alias="from")
    to: datetime
    camera_id: str | None = None
    provider_id: str | None = None
    layer: str | None = None
    group_by: Literal["hour", "day", "provider", "purpose", "layer"] = "day"
    limit: int = Field(default=200, ge=1, le=200)
    cursor: str | None = None

    @field_validator("from_", "to")
    @classmethod
    def usage_time_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("时间必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def usage_range(self) -> "UsageQuery":
        if self.to <= self.from_:
            raise ValueError("to 必须晚于 from")
        return self


class UsageDto(LooseModel):
    totals: dict[str, Any] = Field(default_factory=dict)
    buckets: list[dict[str, Any]] = Field(default_factory=list)
    by_provider: list[dict[str, Any]] = Field(default_factory=list)
    by_layer: list[dict[str, Any]] = Field(default_factory=list)


class JobQuery(StrictModel):
    state: str | None = None
    kind: str | None = None
    camera_id: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


class RetentionSettingsDto(LooseModel):
    ordinary_frame_minutes: int = Field(default=60, ge=1, le=365 * 24 * 60)
    event_frame_minutes: int = Field(default=360, ge=1, le=365 * 24 * 60)
    favorite_keeps_forever: bool = True
    keep_timeline_after_expiry: bool = True
    low_disk_watermark_bytes: int | None = Field(default=None, ge=0)
    revision: int = 1


class RetentionSettingsUpdate(StrictModel):
    ordinary_frame_minutes: int | None = Field(default=None, ge=1, le=365 * 24 * 60)
    event_frame_minutes: int | None = Field(default=None, ge=1, le=365 * 24 * 60)
    favorite_keeps_forever: bool | None = None
    keep_timeline_after_expiry: bool | None = None
    low_disk_watermark_bytes: int | None = Field(default=None, ge=0)
    expected_version: int | None = Field(default=None, ge=1)


class BackupCreateRequest(StrictModel):
    include_images: bool = False


class BackupNameRequest(StrictModel):
    name: str = Field(min_length=1, max_length=255)


class BackupCleanupRequest(StrictModel):
    names: list[str] = Field(default_factory=list, max_length=100)


class DiagnosticsActionRequest(StrictModel):
    action: Literal["reconnect_camera", "retry_job"]
    target_id: str = Field(min_length=1, max_length=128)


class AnalysisSettingsDto(LooseModel):
    sampling_interval_ms: int = Field(default=1000, ge=100, le=60_000)
    stable_sample_seconds: int = Field(default=10, ge=1, le=120)
    minute_window_seconds: int = Field(default=60, ge=10, le=600)
    l2_window_minutes: int = Field(default=5, ge=1, le=60)
    change_threshold: float = Field(default=0.2, ge=0, le=1)
    max_inference_gap_minutes: int = Field(default=5, ge=0, le=60)
    strict_mode: bool = False
    revision: int = 1


class AnalysisSettingsUpdate(StrictModel):
    sampling_interval_ms: int | None = Field(default=None, ge=100, le=60_000)
    stable_sample_seconds: int | None = Field(default=None, ge=1, le=120)
    minute_window_seconds: int | None = Field(default=None, ge=10, le=600)
    l2_window_minutes: int | None = Field(default=None, ge=1, le=60)
    change_threshold: float | None = Field(default=None, ge=0, le=1)
    max_inference_gap_minutes: int | None = Field(default=None, ge=0, le=60)
    strict_mode: bool | None = None
    expected_version: int | None = Field(default=None, ge=1)


class PersonDto(LooseModel):
    id: str
    label: str = "匿名人物"
    enabled: bool = True
    occurrence_count: int | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    version: int = 1


class PersonMergeRequest(StrictModel):
    target_person_id: str
    source_person_ids: list[str] = Field(min_length=1, max_length=100)
    reason: str | None = Field(default=None, max_length=500)


class PersonDisableRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=500)


class OverviewDto(LooseModel):
    cameras: list[CameraDto] = Field(default_factory=list)
    current_window: dict[str, Any] | None = None
    jobs: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    storage: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class EventDto(LooseModel):
    id: str
    event_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None
    sequence: int | None = None


class EventQuery(StrictModel):
    since: str | None = None
    camera_id: str | None = None
    last_event_id: str | None = None


class ResourceDto(LooseModel):
    resource_type: str
    version: str
    path: str
    available: bool
    content: dict[str, Any] | None = None
