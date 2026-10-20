from __future__ import annotations

import re
from decimal import Decimal
from ipaddress import IPv4Address
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.timeutils import timestamp

Activity = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Identity = Literal["matched", "possible", "unknown"]
CaptureState = Literal["stopped", "starting", "online", "offline", "resource_paused", "error"]
Density = Literal["sparse", "dense", "settling"]
AnalysisState = Literal["pending", "running", "complete", "partial", "failed", "expired"]
EvidenceState = Literal["available", "partial", "expired", "evicted", "missing", "none"]
JobState = Literal["queued", "running", "succeeded", "partial", "failed", "expired", "cancelled"]
QueueReason = Literal["scheduled", "retry_wait", "dependency", "capacity", "provider_paused"]
Name = Annotated[str, Field(min_length=1, max_length=64)]
TimeOfDay = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, allow_inf_nan=False)


class HealthDto(Strict):
    alive: bool
    api_version: str
    database_schema: str


class Meta(Strict):
    request_id: str
    server_time: AwareDatetime
    schema_version: Literal["1.0"] = "1.0"


T = TypeVar("T")


class Envelope(Strict, Generic[T]):
    data: T
    meta: Meta


class Page(Strict):
    limit: int
    next_cursor: str | None
    has_more: bool


class ListEnvelope(Envelope[list[T]], Generic[T]):
    page: Page


class FieldError(Strict):
    path: str
    code: str
    message: str


class ErrorDetail(Strict):
    code: str
    message: str
    field_errors: list[FieldError] = []
    retryable: bool = False
    details: dict = {}


class ErrorEnvelope(Strict):
    error: ErrorDetail
    meta: Meta


class Login(Strict):
    username: Annotated[str, Field(min_length=1, max_length=64)]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class PasswordChange(Strict):
    current_password: Annotated[str, Field(min_length=1, max_length=256)]
    new_password: Annotated[str, Field(min_length=12, max_length=256)]


class SessionDto(Strict):
    username: str
    display_name: str
    csrf_token: str
    expires_at: AwareDatetime


class ScheduleRule(Strict):
    weekdays: Annotated[list[Annotated[int, Field(ge=1, le=7)]], Field(min_length=1, max_length=7)]
    start: TimeOfDay
    end: TimeOfDay
    end_day_offset: Literal[0, 1] = 0

    @model_validator(mode="after")
    def check(self):
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("weekdays 不得重复")
        if self.end_day_offset == 0 and self.end <= self.start:
            raise ValueError("同日结束时间必须晚于开始时间")
        return self


class Source(Strict):
    host: Annotated[str, Field(min_length=7, max_length=15)] = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)]
    path: Annotated[str, Field(min_length=1, max_length=2048)]
    username: Annotated[str, Field(max_length=128)] = ""
    password: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    credential_action: Literal["keep", "clear"] = "keep"

    @field_validator("host")
    @classmethod
    def ipv4_only(cls, value):
        return str(IPv4Address(value))

    @field_validator("path")
    @classmethod
    def path_only(cls, value):
        from urllib.parse import unquote

        raw = unquote(value)
        if (
            not raw.startswith("/")
            or raw.startswith("//")
            or any(c in raw for c in "\r\n\\\x00@")
            or re.search(r"[a-zA-Z][a-zA-Z0-9+.-]*://", raw)
        ):
            raise ValueError("只能填写 RTSP 路径与查询串")
        return value


class SourcePatch(Strict):
    host: Annotated[str, Field(min_length=7, max_length=15)] | None = None
    port: Annotated[int, Field(ge=1, le=65535)] | None = None
    path: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    username: Annotated[str, Field(max_length=128)] | None = None
    password: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    credential_action: Literal["keep", "clear"] = "keep"

    @field_validator("host")
    @classmethod
    def ipv4_only(cls, value):
        return Source.ipv4_only(value) if value is not None else value

    @model_validator(mode="after")
    def non_null(self):
        if any(getattr(self, field) is None for field in self.model_fields_set - {"password"}):
            raise ValueError("源配置字段不能为 null")
        return self

    @field_validator("path")
    @classmethod
    def path_only(cls, value):
        return Source.path_only(value) if value is not None else value


Point = tuple[Annotated[float, Field(ge=0, le=1)], Annotated[float, Field(ge=0, le=1)]]


class Polygon(Strict):
    id: Name
    points: Annotated[list[Point], Field(min_length=3, max_length=32)]

    @model_validator(mode="after")
    def check(self):
        p = self.points
        if len(set(p)) != len(p):
            raise ValueError("多边形顶点不能重复")
        area = abs(
            sum(p[i][0] * p[(i + 1) % len(p)][1] - p[(i + 1) % len(p)][0] * p[i][1] for i in range(len(p)))
        )
        if area < 1e-8:
            raise ValueError("区域面积必须大于零")

        def cross(a, b, c):
            return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

        for i in range(len(p)):
            for j in range(i + 1, len(p)):
                if j == i + 1 or (i == 0 and j == len(p) - 1):
                    continue
                a, b, c, d = p[i], p[(i + 1) % len(p)], p[j], p[(j + 1) % len(p)]
                if cross(a, b, c) * cross(a, b, d) <= 0 and cross(c, d, a) * cross(c, d, b) <= 0:
                    if max(min(a[0], b[0]), min(c[0], d[0])) <= min(max(a[0], b[0]), max(c[0], d[0])) and max(
                        min(a[1], b[1]), min(c[1], d[1])
                    ) <= min(max(a[1], b[1]), max(c[1], d[1])):
                        raise ValueError("多边形不能自交")
        return self


class Regions(Strict):
    source_aspect_ratio: Annotated[str, Field(pattern=r"^[1-9]\d{0,3}:[1-9]\d{0,3}$")] = "16:9"
    include: Annotated[list[Polygon], Field(max_length=8)] = []
    exclude: Annotated[list[Polygon], Field(max_length=8)] = []


class CameraCreate(Strict):
    name: Name
    source: Source
    schedule: Annotated[list[ScheduleRule], Field(max_length=64)]
    enabled: Literal[False] = False
    retention_minutes: Annotated[int, Field(ge=1, le=10080)] | None = None
    regions: Regions = Field(default_factory=Regions)


class CameraPatch(Strict):
    name: Name | None = None
    source: SourcePatch | None = None
    schedule: Annotated[list[ScheduleRule], Field(max_length=64)] | None = None
    enabled: bool | None = None
    retention_minutes: Annotated[int, Field(ge=1, le=10080)] | None = None
    regions: Regions | None = None
    preview_token: str | None = None

    @model_validator(mode="after")
    def non_null(self):
        if any(
            getattr(self, field) is None
            for field in self.model_fields_set - {"retention_minutes", "preview_token"}
        ):
            raise ValueError("除保留期继承外，修改字段不能为 null")
        return self


class CameraPreview(Strict):
    patch: CameraPatch
    base_revision: Annotated[int, Field(ge=1)]


class CameraControl(Strict):
    expires_at: AwareDatetime | None = None


class CameraRuntime(Strict):
    capture_state: CaptureState = "stopped"
    density_state: Density | None = None
    stop_reason: (
        Literal["disabled", "outside_schedule", "manual_override", "resource_pressure", "deleted"] | None
    ) = "disabled"
    last_frame_at: AwareDatetime | None = None
    stream_session_id: str | None = None
    control_revision: int = 0
    applied_revision: int = 0
    config_applied_revision: int = 0
    override_mode: Literal["force_on", "force_off"] | None = None
    override_expires_at: AwareDatetime | None = None
    last_error_code: str | None = None


class CameraDto(Strict):
    id: str
    name: str
    enabled: bool
    revision: int
    source_revision: int
    source: dict
    credential_configured: bool
    schedule: list[ScheduleRule]
    retention_minutes: int | None
    effective_retention_minutes: int
    regions: Regions
    regions_state: Literal["current", "needs_review"] = "current"
    last_test: dict | None = None
    runtime: CameraRuntime
    deleted_at: AwareDatetime | None = None


class CameraControlDto(Strict):
    camera_id: str
    revision: int
    control_revision: int
    applied_revision: int
    override_mode: Literal["force_on", "force_off"] | None
    override_expires_at: AwareDatetime | None
    desired_capture_state: Literal["online", "stopped"]


class PreviewDto(Strict):
    camera_id: str
    capture_state: CaptureState
    mode: Literal["hls", "snapshot", "unavailable"]
    session_id: str | None
    manifest_url: str | None
    snapshot_url: str | None
    captured_at: AwareDatetime | None
    stale_after_ms: int
    reason: str | None


class Crop(Strict):
    x: Annotated[float, Field(ge=0, le=1)]
    y: Annotated[float, Field(ge=0, le=1)]
    width: Annotated[float, Field(gt=0, le=1)]
    height: Annotated[float, Field(gt=0, le=1)]

    @model_validator(mode="after")
    def check(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("裁剪范围越界")
        return self


class EntityCreate(Strict):
    name: Name
    kind: Literal["person"] = "person"
    description: Annotated[str, Field(max_length=1000)] = ""
    camera_ids: Annotated[list[str], Field(max_length=32)] = []
    enabled: bool = False
    is_primary_subject: bool = Field(default=False, deprecated=True)


class EntityPatch(Strict):
    name: Name | None = None
    description: Annotated[str, Field(max_length=1000)] | None = None
    camera_ids: Annotated[list[str], Field(max_length=32)] | None = None
    enabled: bool | None = None
    is_primary_subject: bool | None = Field(default=None, deprecated=True)

    @model_validator(mode="after")
    def non_null(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("修改字段不能为 null")
        return self


class ReferencePatch(Strict):
    name: Name | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def non_null(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("修改字段不能为 null")
        return self


class PromoteReference(Strict):
    evidence_id: str
    crop: Crop | None = None
    keep_as_reference: Literal[True]
    name: Name | None = None
    enabled: bool = True


class ReferenceDto(Strict):
    asset_id: str
    entity_id: str
    entity_revision: int
    asset_version: int
    revision: int
    name_snapshot: str
    enabled: bool
    width: int
    height: int
    sha256: str
    created_at: AwareDatetime
    deleted_at: AwareDatetime | None
    content_url: str | None
    source_evidence_id: str | None = None


class EntityDto(Strict):
    id: str
    revision: int
    kind: Literal["person"]
    name: str
    description: str
    enabled: bool
    is_primary_subject: bool = Field(deprecated=True)
    origin: Literal["manual", "automatic"] = "manual"
    reviewed: bool = True
    merged_into_id: str | None = None
    last_seen_at: AwareDatetime | None = None
    camera_ids: list[str]
    references: list[ReferenceDto]
    reference_capacity: dict
    deleted_at: AwareDatetime | None


class ProviderWrite(Strict):
    name: Name
    base_url: Annotated[str, Field(max_length=2048)]
    model: Annotated[str, Field(min_length=1, max_length=200)]
    api_key: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    api_key_action: Literal["keep", "clear"] = "keep"
    timeout_seconds: Annotated[int, Field(ge=15, le=300)] = 120
    max_total_images: Annotated[int, Field(ge=2, le=256)] = 32
    max_request_body_mib: Annotated[int, Field(ge=1, le=64)] = 20
    max_capture_images: Annotated[int, Field(ge=1, le=100)] = 20
    output_token_limit: Annotated[int, Field(ge=128, le=65536)] = 4000
    output_token_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    response_format_mode: Literal["json_schema", "json_object", "prompt_json"] = "json_schema"
    detail_mode: Literal["auto", "low", "high", "omit"] = "auto"

    @field_validator("api_key")
    @classmethod
    def key_value(cls, value):
        if value is not None and (not value.strip() or set(value) <= set("*•…")):
            raise ValueError("不能将空值或掩码作为密钥")
        return value


class ProviderTest(Strict):
    provider_id: str
    config_revision: Annotated[int, Field(ge=1)]
    test_temporal: bool = False


class Capabilities(Strict):
    vision: bool = False
    data_url: bool = False
    structured_output_modes: list[str] = []
    usage_fields: list[str] = []
    verified_max_images: int | None = None
    verified_max_body_bytes: int | None = None
    temporal_reasoning: bool = False
    tool_calling: bool = False
    max_tool_rounds: int | None = None
    tested_at: AwareDatetime | None = None


class ProviderDto(Strict):
    id: str
    revision: int
    name: str
    base_url: str
    model: str
    api_key_configured: bool
    config: dict
    capabilities: Capabilities
    tested_config_revision: int | None
    tested_at: AwareDatetime | None
    last_test_error: str | None = None


PricingCurrency = Literal["CNY", "USD"]


class ProviderPricingConfig(Strict):
    currency: PricingCurrency
    input_price: str
    output_price: str
    cached_input_price: str | None = None
    source_note: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def validate_prices(self):
        def check_price(val: str, name: str):
            try:
                d = Decimal(val)
            except Exception:
                raise ValueError(f"{name} 必须为合法的十进制数值")
            if not d.is_finite() or d < 0:
                raise ValueError(f"{name} 不得为负数或非有限数值")
            if d.as_tuple().exponent < -6:
                raise ValueError(f"{name} 小数位不得超过 6 位")

        check_price(self.input_price, "input_price")
        check_price(self.output_price, "output_price")
        if self.cached_input_price is not None:
            check_price(self.cached_input_price, "cached_input_price")
        return self


class ProviderPricingWrite(Strict):
    provider_revision: Annotated[int, Field(ge=1)]
    pricing_revision: Annotated[int, Field(ge=1)]
    clear: bool = False
    currency: PricingCurrency | None = None
    input_price: str | None = None
    output_price: str | None = None
    cached_input_price: str | None = None
    source_note: Annotated[str, Field(min_length=1, max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_pricing_write(self):
        if not self.clear:
            if (
                not self.currency
                or self.input_price is None
                or self.output_price is None
                or not self.source_note
            ):
                raise ValueError("设置价格时必须提供币种、普通输入单价、输出单价和来源说明")

            def check_price(val: str, name: str):
                try:
                    d = Decimal(val)
                except Exception:
                    raise ValueError(f"{name} 必须为合法的十进制数值")
                if not d.is_finite() or d < 0:
                    raise ValueError(f"{name} 不得为负数或非有限数值")
                if d.as_tuple().exponent < -6:
                    raise ValueError(f"{name} 小数位不得超过 6 位")

            check_price(self.input_price, "input_price")
            check_price(self.output_price, "output_price")
            if self.cached_input_price is not None:
                check_price(self.cached_input_price, "cached_input_price")
        return self


class ProviderPricingDto(Strict):
    provider_id: str
    pricing_revision: int
    provider_revision: int
    applicable: bool
    price_configured: bool
    pricing: ProviderPricingConfig | None = None
    bound_base_url: str | None = None
    bound_model: str | None = None
    updated_at: AwareDatetime | None = None


class SettingsConfig(Strict):
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    capture_fps: Literal[1] = 1
    frame_max_long_edge: Annotated[int, Field(ge=640, le=1920)] = 1280
    frame_retention_minutes: Annotated[int, Field(ge=1, le=10080)] = 60
    cleanup_interval_seconds: Annotated[int, Field(ge=10, le=60)] = 60
    sparse_sample_offsets_seconds: Annotated[
        list[Annotated[int, Field(ge=0, le=59)]], Field(min_length=4, max_length=60)
    ] = [0, 20, 40, 59]
    sparse_submit_interval_minutes: Annotated[int, Field(ge=1, le=10)] = 5
    pre_trigger_seconds: Annotated[int, Field(ge=30, le=300)] = 60
    stable_before_settling_seconds: Annotated[int, Field(ge=30, le=300)] = 60
    post_trigger_seconds: Annotated[int, Field(ge=10, le=120)] = 30
    merge_gap_seconds: Annotated[int, Field(ge=0, le=60)] = 30
    max_inference_gap_seconds: Annotated[int, Field(ge=1, le=30)] = 30
    motion_pixel_delta: Annotated[int, Field(ge=5, le=80)] = 15
    motion_area_ratio: Annotated[float, Field(ge=0.001, le=0.1)] = 0.005
    motion_single_frame_ratio: Annotated[float, Field(ge=0.001, le=0.5)] = 0.03
    ai_concurrency: Annotated[int, Field(ge=1, le=4)] = 2
    max_capture_images_per_request: Annotated[int, Field(ge=1, le=100)] = 20
    max_total_images_per_request: Annotated[int, Field(ge=2, le=256)] = 32
    max_request_body_mib: Annotated[int, Field(ge=1, le=64)] = 20
    ai_timeout_seconds: Annotated[int, Field(ge=15, le=300)] = 120
    ai_retry_count: Annotated[int, Field(ge=0, le=3)] = 3
    hourly_summary_delay_minutes: Annotated[int, Field(ge=1, le=60)] = 5
    daily_summary_local_time: TimeOfDay = "00:10"
    source_stale_seconds: Annotated[int, Field(ge=5, le=30)] = 10
    media_quota_gb: Annotated[float, Field(gt=0, le=1000)] = 4
    reference_quota_gb: Annotated[float, Field(gt=0, le=100)] = 0.2
    disk_pressure_free_gb: Annotated[float, Field(gt=0)] = 3
    disk_stop_free_gb: Annotated[float, Field(ge=0.2)] = 1
    detailed_audit_days: Annotated[int, Field(ge=1, le=30)] = 7
    vision_provider_id: str | None = None
    summary_provider_id: str | None = None
    primary_subject_id: str | None = Field(default=None, deprecated=True)
    auto_discovery_enabled: bool = True
    activity_labels: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=64)]], Field(max_length=100)
    ] = []

    def minimum_retention_seconds(self) -> int:
        return max(
            self.pre_trigger_seconds,
            60,
            self.sparse_submit_interval_minutes * 60
            + (1 + self.ai_retry_count) * self.ai_timeout_seconds
            + sum([30, 120, 300][: self.ai_retry_count])
            + 120,
        )

    @model_validator(mode="after")
    def check(self):
        offsets = self.sparse_sample_offsets_seconds
        if (
            offsets != sorted(set(offsets))
            or max(b - a for a, b in zip(offsets, offsets[1:])) > self.max_inference_gap_seconds
        ):
            raise ValueError("抽样点必须升序去重且间隔不超过 max_inference_gap_seconds")
        if self.motion_single_frame_ratio < self.motion_area_ratio:
            raise ValueError("单帧变化阈值不得小于连续帧阈值")
        if self.disk_pressure_free_gb <= self.disk_stop_free_gb:
            raise ValueError("磁盘压力阈值必须大于停止阈值")
        if self.frame_retention_minutes * 60 < self.minimum_retention_seconds():
            raise ValueError(
                f"RETENTION_TOO_SHORT: 当前配置至少需要 {(self.minimum_retention_seconds() + 59) // 60} 分钟"
            )
        return self


class SettingsPreview(Strict):
    patch: dict
    base_revision: Annotated[int, Field(ge=1)]


class SettingsUpdate(Strict):
    patch: dict
    preview_token: str | None = None


class SettingsDto(Strict):
    revision: int
    config: SettingsConfig
    effective_limits: dict
    defaults: SettingsConfig
    validation_hints: dict


class WarningDto(Strict):
    code: str
    message: str
    affected_resource_ids: list[str] = []


class ImpactPreviewDto(Strict):
    preview_token: str
    expires_at: AwareDatetime
    affected_camera_ids: list[str]
    will_expire_frame_count: int
    will_expire_bytes: int
    estimated_media_bytes: dict
    requires_reconnect: bool
    requires_roi_review: bool
    warnings: list[WarningDto]
    blocking_errors: list[dict]


class AnalysisRequest(Strict):
    mode: Literal["visual", "records_only"]
    camera_ids: Annotated[list[str], Field(min_length=1, max_length=32)]
    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime
    reference_policy: Literal["current"] = "current"
    allow_partial: bool = False
    subject_id: str | None = None

    @model_validator(mode="after")
    def check(self):
        duration = timestamp(self.to) - timestamp(self.from_)
        if duration <= 0 or duration > (3600000 if self.mode == "visual" else 86400000):
            raise ValueError("重分析范围超限或起止顺序错误")
        if self.mode == "visual" and len(set(self.camera_ids)) != 1:
            raise ValueError("视觉分析一次只能选择一路摄像头")
        return self


class AnalysisCreate(AnalysisRequest):
    preview_token: str


class RegenerateSummary(Strict):
    summary_id: str
    source_revision: Annotated[int, Field(ge=1)]
    reference_policy: Literal["current"] = "current"


class CorrectionInterval(Strict):
    subject_id: str | None
    activity: Activity
    labels: Annotated[list[str], Field(max_length=20)] = []
    object_ids: Annotated[list[str], Field(max_length=32)] = []
    start_at: AwareDatetime
    end_at: AwareDatetime


class EventCorrection(Strict):
    base_source_revision: Annotated[int, Field(ge=1)]
    intervals: Annotated[list[CorrectionInterval], Field(max_length=120)]
    reason: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def check(self):
        previous = None
        for item in self.intervals:
            a, b = timestamp(item.start_at), timestamp(item.end_at)
            if b <= a or (previous is not None and a < previous):
                raise ValueError("修订区间必须排序、互不重叠且长度为正")
            previous = b
        return self


class Coverage(Strict):
    planned_ms: int = 0
    captured_ms: int = 0
    classified_ms: int = 0
    unknown_ms: int = 0
    unanalysed_ms: int = 0
    gap_ms: int = 0
    conflict_ms: int = 0
    inferred_ms: int = 0
    unanalysed_pending_ms: int = 0
    unanalysed_failed_ms: int = 0
    unanalysed_expired_ms: int = 0


class ActivityDuration(Strict):
    activity: Activity
    duration_ms: int
    estimated_ms: int = 0


class Progress(Strict):
    completed_units: int = 0
    total_units: int | None = None
    failed_units: int = 0
    expired_units: int = 0
    unit: Literal["batch", "minute", "slice", "review", "step"] = "step"


class JobDto(Strict):
    id: str
    kind: str
    job_state: JobState
    queue_reason: QueueReason | None
    phase: str
    created_at: AwareDatetime
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    progress: Progress
    attempts: int
    cancel_requested: bool
    deadline_at: AwareDatetime | None
    error: dict | None
    result_links: list[dict]


class EvidenceDto(Strict):
    id: str
    camera_id: str
    captured_at: AwareDatetime
    expires_at: AwareDatetime
    state: EvidenceState
    width: int | None
    height: int | None
    content_url: str | None
    reason: str | None


class MinuteEvidenceSlotDto(Strict):
    slot_at: AwareDatetime
    slot_state: Literal["available", "expired", "evicted", "missing", "not_captured", "outside_schedule"]
    captured_at: AwareDatetime | None
    evidence_id: str | None
    evidence: EvidenceDto | None
    reason: str | None


class Boundary(Strict):
    start_lower: AwareDatetime | None = None
    start_upper: AwareDatetime | None = None
    end_lower: AwareDatetime | None = None
    end_upper: AwareDatetime | None = None


class SegmentDto(Strict):
    id: str
    subject_id: str | None
    identity_status: Identity
    activity: Activity
    labels: list[str]
    object_ids: list[str]
    start_at: AwareDatetime
    end_at: AwareDatetime
    duration_ms: int
    boundary_range: Boundary
    observation: str
    evidence_ids: list[str]
    origin: Literal["ai", "manual"]
    support: Literal["sparse", "dense", "mixed"]
    inferred: bool = False


class MinuteDto(Strict):
    id: str
    camera_id: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    revision: int
    analysis_state: AnalysisState
    analysis_mode: Literal["sparse", "dense", "mixed"] | None
    evidence_state: EvidenceState
    coverage: Coverage
    description: str
    segments: list[SegmentDto]
    evidence_samples: list[EvidenceDto]
    reference_set_id: str | None
    model_version: str | None
    has_manual_revision: bool
    pending_job_id: str | None
    sampling: dict
    versions: list[dict] = []


class EventSummaryDto(Strict):
    id: str
    revision: int
    source_revision: int
    subject_id: str | None
    identity_status: Identity
    camera_ids: list[str]
    activity: Activity
    labels: list[str]
    object_ids: list[str]
    start_at: AwareDatetime
    end_at: AwareDatetime
    duration_ms: int
    evidence_state: EvidenceState
    source_state: Literal["current", "stale"]
    has_manual_revision: bool


class EventDto(EventSummaryDto):
    segments: list[SegmentDto]
    interruptions: list[dict]
    minute_refs: list[dict]
    replacement_event_ids: list[str]


class SummaryDto(Strict):
    id: str
    revision: int
    type: Literal["hour", "day"]
    local_date: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    scope: dict
    summary_state: Literal["pending", "provisional", "complete", "failed"]
    source_state: Literal["current", "stale"]
    completeness: Literal["full", "with_gaps"]
    coverage: Coverage
    activities: list[ActivityDuration]
    narrative: str
    highlights: list[dict]
    source_refs: list[dict]
    generated_at: AwareDatetime | None
    reference_set_id: str | None
    pending_job_id: str | None
    statistics_source_revision: int
    narrative_source_fingerprint: str | None


class Observation(Strict):
    subject_id: str | None
    identity_status: Identity
    activity: Activity
    labels: Annotated[list[Annotated[str, Field(max_length=64)]], Field(max_length=20)]
    object_ids: Annotated[list[str], Field(max_length=32)]
    start_evidence_id: str
    end_evidence_id: str
    supporting_evidence_ids: Annotated[list[str], Field(min_length=1, max_length=100)]
    observation: Annotated[str, Field(max_length=500)]
    needs_dense_review: bool
    subject_candidate_key: str | None = None
    object_candidate_keys: list[str] = Field(default_factory=list, max_length=32)


class VisualMinute(Strict):
    minute_start: AwareDatetime
    observations: Annotated[list[Observation], Field(max_length=120)]
    unresolved_evidence_ids: Annotated[list[str], Field(max_length=100)]
    review_reasons: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=20)]


class EntityCandidate(Strict):
    key: Annotated[str, Field(min_length=1, max_length=100)]
    kind: Literal["person"]
    name: Name
    description: Annotated[str, Field(max_length=1000)] = ""
    evidence_id: str
    crop: Crop
    matched_entity_id: str | None = None


class VisualOutput(Strict):
    schema_version: Literal["1.0"]
    minutes: Annotated[list[VisualMinute], Field(max_length=5)]
    # 候选独立校验，坏候选不能导致有效的活动观察失败。
    entity_candidates: list[EntityCandidate] = Field(default_factory=list, max_length=100)

    @field_validator("entity_candidates", mode="before")
    @classmethod
    def valid_candidates(cls, value):
        from pydantic import ValidationError

        result = []
        for raw in value[:100] if isinstance(value, list) else []:
            try:
                result.append(EntityCandidate.model_validate(raw))
            except (ValidationError, TypeError):
                continue
        return result


class EntityOccurrenceDto(Strict):
    id: str
    entity_id: str
    camera_id: str
    captured_at: AwareDatetime
    evidence_id: str
    crop: Crop
    manual: bool


class EntityMerge(Strict):
    target_entity_id: str
    target_revision: Annotated[int, Field(ge=1)]


class EntitySplit(Strict):
    name: Name
    occurrence_ids: Annotated[list[str], Field(min_length=1, max_length=500)]


class SourceRef(Strict):
    kind: Literal["minute", "event", "hour"]
    id: str
    revision: int
    start_at: AwareDatetime
    end_at: AwareDatetime


class Highlight(Strict):
    text: Annotated[str, Field(max_length=500)]
    source_refs: Annotated[list[SourceRef], Field(max_length=20)]


class SummaryOutput(Strict):
    narrative: Annotated[str, Field(max_length=10000)]
    highlights: Annotated[list[Highlight], Field(max_length=10)]
    source_refs: Annotated[list[SourceRef], Field(max_length=500)]


class ScopeDto(Strict):
    camera_ids: list[str]
    subject_id: str | None


class OverviewDto(Strict):
    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime
    scope: ScopeDto
    coverage: Coverage
    activities: list[ActivityDuration]
    duration_is_estimated: bool
    projection_revision: int
    latest_events: list[EventSummaryDto]
    warnings: list[WarningDto]


class TimelineBucket(Strict):
    start_at: AwareDatetime
    end_at: AwareDatetime
    coverage: Coverage
    activities: list[ActivityDuration]


class TemporalReviewDto(Strict):
    state: Literal["queued", "running", "partial", "complete", "failed", "expired"]
    reviewed_from: AwareDatetime | None = None
    reviewed_to: AwareDatetime | None = None
    checkpoint_revision: int | None = None
    mutable_tail: dict | None = None
    source_slice_ids: list[str] = []
    unresolved_ranges: list[dict] = []
    conflicts: list[dict] = []
    next_review: dict | None = None


class PublishedTimelineDto(Strict):
    schema_version: str = "published-timeline-1"
    review_run_id: str | None = None
    revision: int
    reviewed_from: AwareDatetime
    reviewed_to: AwareDatetime
    frozen_to: AwareDatetime | None = None
    segments: list[dict] = []
    unresolved_ranges: list[dict] = []
    conflicts: list[dict] = []
    next_review: dict | None = None


class TimelineDto(Strict):
    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime
    scope: ScopeDto
    bucket_seconds: Literal[60, 300, 900]
    projection_revision: int
    buckets: list[TimelineBucket]
    event_refs: list[dict]
    truncated: bool
    temporal_review: TemporalReviewDto | None = None
    published_timeline: PublishedTimelineDto | None = None


class AvailableRange(Strict):
    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime


class AnalysisPreviewDto(Strict):
    preview_token: str
    expires_at: AwareDatetime
    available_ranges: list[AvailableRange]
    missing_ranges: list[AvailableRange]
    available_frame_count: int
    earliest_expires_at: AwareDatetime | None
    reference_set_id: str
    estimated_batches: int
    warnings: list[WarningDto]


class SettingsUpdateDto(Strict):
    settings: SettingsDto
    apply_job: JobDto | None


class EventUpdateDto(Strict):
    event: EventDto
    projection_revision: int
    affected_summary_ids: list[str]
    jobs: list[JobDto]


class TokenTotal(Strict):
    value: int | None
    known_count: int
    unknown_count: int


class UsageCostSummary(Strict):
    estimated_cny_subtotal: str | None
    by_currency: dict[str, str]
    converted_requests: int
    unpriced_requests: int
    unconverted_requests: int
    reasons: list[str]
    applicable_price_revisions: list[int]
    exchange_rate_dates: list[str]
    completeness: Literal["full", "partial", "none"]


class UsageTotal(Strict):
    requests: int
    succeeded: int
    failed: int
    unknown_usage_requests: int
    capture_images: int
    reference_images: int
    input_tokens: TokenTotal
    output_tokens: TokenTotal
    cached_tokens: TokenTotal
    cost: str | None
    currency: str | None
    cost_estimated: bool
    cost_summary: UsageCostSummary


class UsageBucket(UsageTotal):
    key: str


class ExchangeRateDto(Strict):
    rate: str | None
    from_currency: str = "USD"
    to_currency: str = "CNY"
    rate_date: str | None
    fetched_at: AwareDatetime | None
    source: str | None
    is_valid: bool
    status: Literal["valid", "expired", "missing"]
    has_usd_provider: bool = False


class ExchangeRateRefreshDto(Strict):
    job_id: str
    status: Literal["queued", "already_running"]


class UsageDto(Strict):
    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime
    group_by: Literal["hour", "day", "provider", "purpose"]
    buckets: list[UsageBucket]
    totals: UsageTotal


class StatusDto(Strict):
    server_time: AwareDatetime
    api_version: str
    db_schema_version: str
    worker_heartbeat_at: AwareDatetime | None
    worker_state: Literal["online", "stale", "offline"]
    cameras: list[dict]
    queue: dict
    storage: dict
    providers: list[dict]
    warnings: list[WarningDto]
