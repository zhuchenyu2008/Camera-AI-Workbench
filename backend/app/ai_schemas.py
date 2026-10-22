"""AI 三层结构化输出契约与安全校验。

模型的输出永远是不可信输入。这里先做纯结构校验，再由任务执行器注入任务授权上下文完成
证据 ID、窗口边界、工具参数和跨段连续性校验。所有时间区间均为左闭右开 ``[start, end)``。
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvidenceKind(StrEnum):
    direct = "direct"
    inferred = "inferred"
    unknown = "unknown"


class JobLayer(StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(UTC)


def _valid_interval(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start, end = _utc(start), _utc(end)
    if end <= start:
        raise ValueError("时间区间必须满足 end > start")
    return start, end


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TimeRange(StrictModel):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_range(self) -> "TimeRange":
        self.start, self.end = _valid_interval(self.start, self.end)
        return self


class EvidenceRef(StrictModel):
    evidence_frame_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("evidence_frame_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 128 for item in value):
            raise ValueError("证据帧 ID 不合法")
        return list(dict.fromkeys(value))


class Observation(StrictModel):
    start: datetime
    end: datetime
    description: str = Field(min_length=1, max_length=2000)
    evidence_frame_ids: list[str] = Field(min_length=1, max_length=100)
    subject_candidate_key: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_range(self) -> "Observation":
        self.start, self.end = _valid_interval(self.start, self.end)
        return self


class CandidateActivity(StrictModel):
    start: datetime
    end: datetime
    name: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    confidence: float = Field(ge=0, le=1)
    evidence_frame_ids: list[str] = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_range(self) -> "CandidateActivity":
        self.start, self.end = _valid_interval(self.start, self.end)
        return self


class Gap(StrictModel):
    start: datetime
    end: datetime
    kind: Literal["missing", "occluded", "frozen", "unknown"]
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_range(self) -> "Gap":
        self.start, self.end = _valid_interval(self.start, self.end)
        return self


class L1Response(StrictModel):
    window: TimeRange
    observations: list[Observation] = Field(default_factory=list, max_length=100)
    candidate_activities: list[CandidateActivity] = Field(default_factory=list, max_length=100)
    gaps: list[Gap] = Field(default_factory=list, max_length=100)
    needs_more_evidence: bool = False
    uncertainties: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_children(self) -> "L1Response":
        for item in [*self.observations, *self.candidate_activities, *self.gaps]:
            if item.start < self.window.start or item.end > self.window.end:
                raise ValueError("L1 子区间超出任务窗口")
        _validate_non_overlapping(self.gaps, "gaps")
        return self


class ActivityInterval(StrictModel):
    start: datetime
    end: datetime
    evidence_kind: EvidenceKind
    confidence: float = Field(ge=0, le=1)
    reason: str | None = Field(default=None, max_length=2000)
    anchor_frame_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_frame_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_kind(self) -> "ActivityInterval":
        self.start, self.end = _valid_interval(self.start, self.end)
        if self.evidence_kind is EvidenceKind.inferred:
            if not self.reason or len(self.anchor_frame_ids) < 2:
                raise ValueError("推断区间必须提供 reason 和至少两个锚点帧")
        elif self.evidence_kind is EvidenceKind.direct and not self.evidence_frame_ids:
            raise ValueError("直接观察区间必须提供证据帧")
        elif self.evidence_kind is EvidenceKind.unknown and not self.reason:
            raise ValueError("未知区间必须提供原因")
        return self


class ActivitySegmentOutput(StrictModel):
    start: datetime
    end: datetime
    canonical_activity: str | None = Field(default=None, max_length=200)
    display_description: str = Field(min_length=1, max_length=2000)
    category: str | None = Field(default=None, max_length=100)
    confidence: float = Field(ge=0, le=1)
    intervals: list[ActivityInterval] = Field(min_length=1, max_length=100)
    source_observation_ids: list[str] = Field(default_factory=list, max_length=100)
    needs_review: bool = False

    @model_validator(mode="after")
    def validate_intervals(self) -> "ActivitySegmentOutput":
        self.start, self.end = _valid_interval(self.start, self.end)
        for interval in self.intervals:
            if interval.start < self.start or interval.end > self.end:
                raise ValueError("活动证据子区间超出活动段")
        _validate_non_overlapping(self.intervals, "intervals")
        return self


class UnknownInterval(StrictModel):
    start: datetime
    end: datetime
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_range(self) -> "UnknownInterval":
        self.start, self.end = _valid_interval(self.start, self.end)
        return self


class L2Response(StrictModel):
    window: TimeRange
    segments: list[ActivitySegmentOutput] = Field(default_factory=list, max_length=100)
    unknown_intervals: list[UnknownInterval] = Field(default_factory=list, max_length=100)
    transitions: list[str] = Field(default_factory=list, max_length=100)
    tool_summary: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_segments(self) -> "L2Response":
        for segment in self.segments:
            if segment.start < self.window.start or segment.end > self.window.end:
                raise ValueError("活动段超出 L2 任务窗口")
        for interval in self.unknown_intervals:
            if interval.start < self.window.start or interval.end > self.window.end:
                raise ValueError("未知区间超出 L2 任务窗口")
        _validate_non_overlapping(self.segments, "segments")
        _validate_non_overlapping(self.unknown_intervals, "unknown_intervals")
        combined = sorted([*self.segments, *self.unknown_intervals], key=lambda item: (item.start, item.end))
        for previous, current in zip(combined, combined[1:]):
            if current.start < previous.end:
                raise ValueError("活动段与未知区间存在重叠")
        return self


class L3ActivityStat(StrictModel):
    activity: str = Field(min_length=1, max_length=200)
    duration_seconds: int = Field(ge=0)
    occurrences: int = Field(ge=0)
    inferred_seconds: int = Field(default=0, ge=0)


class L3Response(StrictModel):
    period: TimeRange
    summary_text: str = Field(min_length=1, max_length=10000)
    activity_statistics: list[L3ActivityStat] = Field(default_factory=list, max_length=500)
    unknown_seconds: int = Field(default=0, ge=0)
    inferred_seconds: int = Field(default=0, ge=0)
    source_segment_ids: list[str] = Field(default_factory=list, max_length=500)
    incomplete: bool = False


def _validate_non_overlapping(items: list[Any], label: str) -> None:
    ordered = sorted(items, key=lambda item: (item.start, item.end))
    for previous, current in zip(ordered, ordered[1:]):
        if current.start < previous.end:
            raise ValueError(f"{label} 存在重叠区间")


def validate_authorized_evidence(
    result: L1Response | L2Response,
    *,
    allowed_frame_ids: set[str],
    allowed_observation_ids: set[str] | None = None,
    allowed_window: TimeRange | None = None,
    max_inference_gap_seconds: int = 300,
) -> None:
    """把模型结果与当前 Job 的授权上下文绑定。

    该函数不信任模型提供的 frame ID；所有直接/推断证据都必须来自 Job 输入或服务端工具返回。
    """
    window = result.window
    if allowed_window is not None and (window.start < allowed_window.start or window.end > allowed_window.end):
        raise ValueError("模型结果窗口超出授权窗口")

    def check_range(start: datetime, end: datetime) -> None:
        if start < window.start or end > window.end:
            raise ValueError("模型区间超出结果窗口")

    def check_ids(ids: list[str]) -> None:
        if not set(ids).issubset(allowed_frame_ids):
            raise ValueError("模型引用了未授权证据帧")

    if isinstance(result, L1Response):
        for item in [*result.observations, *result.candidate_activities]:
            check_range(item.start, item.end)
            check_ids(item.evidence_frame_ids)
        for item in result.gaps:
            check_range(item.start, item.end)
        return

    if allowed_observation_ids is not None:
        for segment in result.segments:
            if not set(segment.source_observation_ids).issubset(allowed_observation_ids):
                raise ValueError("活动段引用了未授权观察记录")
    for segment in result.segments:
        check_range(segment.start, segment.end)
        for interval in segment.intervals:
            check_range(interval.start, interval.end)
            check_ids(interval.evidence_frame_ids)
            check_ids(interval.anchor_frame_ids)
            if interval.evidence_kind is EvidenceKind.inferred:
                if (interval.end - interval.start).total_seconds() > max_inference_gap_seconds:
                    raise ValueError("推断区间超过最大允许缺口")


def parse_layer_result(layer: str, payload: str | bytes | dict[str, Any]) -> L1Response | L2Response | L3Response:
    """严格解析 AI 返回的单一 JSON 对象。"""
    import json

    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError("AI 响应必须是 JSON 对象")
    layer = layer.upper()
    if layer == "L1":
        return L1Response.model_validate(payload)
    if layer == "L2":
        return L2Response.model_validate(payload)
    if layer == "L3":
        return L3Response.model_validate(payload)
    raise ValueError(f"未知 AI 层级: {layer}")
