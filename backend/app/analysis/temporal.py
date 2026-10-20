"""时间片观察和上级时间线裁决的结构化契约。

这部分故意不依赖分钟表。时间片结果先作为任务结果保存，后续投影器可以在
不改变模型契约的情况下写入 TemporalSlice/Checkpoint 持久对象。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import select

from app.errors import AppError
from app.repositories import schema as s
from app.schemas import Activity, Identity, Strict
from app.timeutils import timestamp

TemporalCertainty = Literal["direct", "inferred", "uncertain"]
PublishedCertainty = Literal["direct", "inferred", "mixed", "uncertain"]


class TemporalRange(Strict):
    start_at: AwareDatetime
    end_at: AwareDatetime

    @model_validator(mode="after")
    def ordered(self):
        if self.end_at <= self.start_at:
            raise ValueError("时间范围必须为正长度")
        return self


class SliceCoverage(Strict):
    mode: Literal["dense", "sparse", "mixed"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=256)
    missing_ranges: list[TemporalRange] = Field(default_factory=list, max_length=128)


class LocalObservationItem(Strict):
    subject_id: str | None = None
    identity_status: Identity = "unknown"
    activity: Activity
    start_at: AwareDatetime
    end_at: AwareDatetime
    certainty: TemporalCertainty
    evidence_ids: list[str] = Field(min_length=1, max_length=256)
    boundary_evidence_ids: list[str] = Field(default_factory=list, max_length=256)
    observation: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_at <= self.start_at:
            raise ValueError("观察时间范围必须为正长度")
        return self


class TemporalTransition(Strict):
    at: AwareDatetime
    question: str = Field(min_length=1, max_length=500)


class TemporalReviewRequest(Strict):
    from_at: AwareDatetime = Field(alias="from")
    to_at: AwareDatetime = Field(alias="to")
    question: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def ordered(self):
        if self.to_at <= self.from_at:
            raise ValueError("复核请求时间范围必须为正长度")
        return self


class LocalObservation(Strict):
    schema_version: Literal["local-observation-1"] = "local-observation-1"
    slice_id: str = Field(min_length=1, max_length=200)
    slice_start: AwareDatetime
    slice_end: AwareDatetime
    coverage: SliceCoverage
    observations: list[LocalObservationItem] = Field(default_factory=list, max_length=128)
    unresolved_ranges: list[TemporalRange] = Field(default_factory=list, max_length=128)
    transition_candidates: list[TemporalTransition] = Field(default_factory=list, max_length=128)
    review_requests: list[TemporalReviewRequest] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def within_slice(self):
        if self.slice_end <= self.slice_start:
            raise ValueError("时间片必须为正长度")
        start, end = self.slice_start, self.slice_end
        ranges = [*self.observations, *self.unresolved_ranges, *self.review_requests]
        for item in ranges:
            lo = item.start_at if hasattr(item, "start_at") else item.from_at
            hi = item.end_at if hasattr(item, "end_at") else item.to_at
            if lo < start or hi > end:
                raise ValueError("模型输出越过时间片边界")
        for item in self.transition_candidates:
            if item.at < start or item.at >= end:
                raise ValueError("切换候选越过时间片边界")
        return self


class PublishedSegment(Strict):
    subject_id: str | None = None
    identity_status: Identity = "unknown"
    activity: Activity
    start_at: AwareDatetime
    end_at: AwareDatetime
    certainty: PublishedCertainty
    evidence_ids: list[str] = Field(min_length=1, max_length=512)
    source_slice_ids: list[str] = Field(min_length=1, max_length=256)
    boundary_range: dict[str, AwareDatetime] | None = None
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_at <= self.start_at:
            raise ValueError("发布时间段必须为正长度")
        if self.boundary_range:
            required = {"start_lower", "start_upper"}
            if set(self.boundary_range) - required:
                raise ValueError("边界范围字段无效")
            if self.boundary_range["start_upper"] < self.boundary_range["start_lower"]:
                raise ValueError("边界范围顺序无效")
        return self


class PublishedTimeline(Strict):
    schema_version: Literal["published-timeline-1"] = "published-timeline-1"
    review_run_id: str = Field(min_length=1, max_length=200)
    reviewed_from: AwareDatetime
    reviewed_to: AwareDatetime
    segments: list[PublishedSegment] = Field(default_factory=list, max_length=256)
    unresolved_ranges: list[TemporalRange] = Field(default_factory=list, max_length=256)
    conflicts: list[str] = Field(default_factory=list, max_length=256)
    next_review: dict[str, str] | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.reviewed_to <= self.reviewed_from:
            raise ValueError("复核范围必须为正长度")
        for segment in [*self.segments, *self.unresolved_ranges]:
            if segment.start_at < self.reviewed_from or segment.end_at > self.reviewed_to:
                raise ValueError("发布时间段越过复核范围")
        return self


class TemporalToolRequest(Strict):
    schema_version: Literal["temporal-review-1"] = "temporal-review-1"
    action: Literal["tool", "final", "partial"]
    tool_name: str | None = Field(default=None, max_length=80)
    arguments: dict = Field(default_factory=dict)
    timeline: PublishedTimeline | None = None
    partial_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def action_shape(self):
        if self.action == "tool" and not self.tool_name:
            raise ValueError("tool action 缺少工具名")
        if self.action == "final" and self.timeline is None:
            raise ValueError("final action 缺少时间线")
        if self.action == "partial" and not self.partial_reason:
            raise ValueError("partial action 缺少原因")
        if self.action != "tool" and self.arguments:
            raise ValueError("非工具 action 不应携带参数")
        return self


def _error(message: str, details: dict | None = None) -> AppError:
    return AppError(422, "INVALID_TEMPORAL_OUTPUT", message, details=details or {})


def _ms(value: datetime) -> int:
    return timestamp(value)


def validate_local_observation(
    output: LocalObservation,
    *,
    slice_id: str,
    start_ms: int,
    end_ms: int,
    evidence_ids: list[str],
    hard_boundaries: list[dict] | None = None,
) -> LocalObservation:
    """校验模型输出只能引用当前片段的现场证据。"""
    if output.slice_id != slice_id or _ms(output.slice_start) != start_ms or _ms(output.slice_end) != end_ms:
        raise _error("时间片标识或边界与任务快照不一致")
    allowed = set(evidence_ids)
    referenced = set(output.coverage.evidence_ids)
    if referenced != allowed:
        raise _error("覆盖证据与任务快照不一致")
    expected_boundaries = {
        (timestamp(item["start_at"]), timestamp(item["end_at"])) for item in (hard_boundaries or [])
    }
    reported_boundaries = {
        (_ms(item.start_at), _ms(item.end_at)) for item in output.coverage.missing_ranges
    }
    if reported_boundaries != expected_boundaries:
        raise _error("模型覆盖状态与采集快照不一致")
    for item in output.observations:
        if not set(item.evidence_ids) <= allowed or not set(item.boundary_evidence_ids) <= allowed:
            raise _error("观察引用了任务之外的证据")
        if any(
            _ms(item.start_at) < _ms(boundary.end_at) and _ms(item.end_at) > _ms(boundary.start_at)
            for boundary in output.coverage.missing_ranges
        ):
            raise _error("观察时间段覆盖未采集或计划外的硬边界")
    return output


def validate_published_timeline(
    output: PublishedTimeline,
    *,
    review_run_id: str,
    start_ms: int,
    end_ms: int,
    evidence_ids: list[str],
    slice_ids: list[str],
    hard_boundaries: list[dict] | None = None,
) -> PublishedTimeline:
    if output.review_run_id != review_run_id:
        raise _error("复核运行标识与任务快照不一致")
    if _ms(output.reviewed_from) < start_ms or _ms(output.reviewed_to) > end_ms:
        raise _error("发布时间线越过任务授权范围")
    allowed_evidence, allowed_slices = set(evidence_ids), set(slice_ids)
    boundaries = [
        (timestamp(item["from"]), timestamp(item["to"]))
        for item in (hard_boundaries or [])
    ]
    for segment in output.segments:
        if not set(segment.evidence_ids) <= allowed_evidence:
            raise _error("时间线引用了任务之外的证据")
        if not set(segment.source_slice_ids) <= allowed_slices:
            raise _error("时间线引用了任务之外的时间片")
        if any(_ms(segment.start_at) < end and _ms(segment.end_at) > start for start, end in boundaries):
            raise _error("发布时间段跨越未采集或计划外的硬边界")
    return output


def validate_tool_request(request: TemporalToolRequest, *, allowed_tools: set[str]) -> None:
    if request.action != "tool":
        return
    if request.tool_name not in allowed_tools:
        raise AppError(422, "TEMPORAL_TOOL_DENIED", "上级模型请求了未授权工具")
    if len(request.arguments) > 32:
        raise AppError(422, "TEMPORAL_TOOL_DENIED", "工具参数过多")


class TemporalToolRunner:
    """上级模型的只读工具适配器。

    工具输入来自任务快照，任何时间范围和 evidence ID 都必须落在快照授权
    范围内。返回值是可序列化摘要，图片由执行器按 view_frames 单独加入请求。
    """

    ALLOWED_TOOLS = {
        "get_frame_manifest",
        "view_frames",
        "get_capture_coverage",
        "get_local_observations",
        "get_checkpoint",
        "get_cross_camera_context",
        "get_identity_context",
        "get_manual_annotations",
        "get_frame_quality",
        "request_reobserve",
    }

    def __init__(self, workbench, payload: dict):
        self.w = workbench
        self.payload = payload
        self.allowed_evidence = set(payload.get("evidence_ids", []))
        self.allowed_slices = set(payload.get("slice_ids", []))
        self.start_ms = int(payload["review_start_ms"])
        self.end_ms = int(payload["review_end_ms"])

    def _range(self, arguments: dict) -> tuple[int, int]:
        try:
            start = timestamp(arguments["from"]) if "from" in arguments else self.start_ms
            end = timestamp(arguments["to"]) if "to" in arguments else self.end_ms
        except (KeyError, TypeError, ValueError):
            raise AppError(422, "TEMPORAL_TOOL_DENIED", "工具时间范围无效") from None
        if start < self.start_ms or end > self.end_ms or end <= start:
            raise AppError(422, "TEMPORAL_TOOL_DENIED", "工具时间范围越过授权范围")
        return start, end

    def _evidence(self, arguments: dict) -> list[str]:
        values = arguments.get("evidence_ids", [])
        if not isinstance(values, list) or not values or len(values) > 64:
            raise AppError(422, "TEMPORAL_TOOL_DENIED", "工具证据列表无效")
        if not all(isinstance(value, str) for value in values) or not set(values) <= self.allowed_evidence:
            raise AppError(422, "TEMPORAL_TOOL_DENIED", "工具请求了未授权证据")
        return list(dict.fromkeys(values))

    def run(self, name: str, arguments: dict) -> dict:
        if name not in self.ALLOWED_TOOLS:
            raise AppError(422, "TEMPORAL_TOOL_DENIED", "上级模型请求了未授权工具")
        if not isinstance(arguments, dict):
            raise AppError(422, "TEMPORAL_TOOL_DENIED", "工具参数必须为对象")
        if name == "view_frames":
            ids = self._evidence(arguments)
            return {"evidence_ids": ids, "requires_images": True}
        if name in {"get_frame_manifest", "get_frame_quality"}:
            start, end = self._range(arguments)
            ids = set(arguments.get("evidence_ids", self.allowed_evidence))
            if not ids <= self.allowed_evidence:
                raise AppError(422, "TEMPORAL_TOOL_DENIED", "工具请求了未授权证据")
            with self.w.db.read() as conn:
                rows = [
                    self.w.assets.metadata(conn, evidence_id, check_file=False)
                    for evidence_id in self.allowed_evidence
                ]
            rows = [
                row
                for row in rows
                if row["id"] in ids
                and start <= timestamp(row["captured_at"]) < end
            ]
            if name == "get_frame_quality":
                return {"frames": [{"evidence_id": row["id"], "state": row["state"], "reason": row["reason"]} for row in rows]}
            return {"frames": rows}
        if name == "get_local_observations":
            start, end = self._range(arguments)
            observations = self.payload.get("local_observations", [])
            return {
                "observations": [
                    item
                    for item in observations
                    if item.get("slice_id") in self.allowed_slices
                    and timestamp(item["slice_end"]) > start
                    and timestamp(item["slice_start"]) < end
                ]
            }
        if name == "get_checkpoint":
            self._range(arguments)
            return {"checkpoint": self.payload.get("checkpoint")}
        if name == "get_capture_coverage":
            self._range(arguments)
            return {"coverage": self.payload.get("coverage", {})}
        if name == "get_cross_camera_context":
            self._range(arguments)
            return {"context": self.payload.get("cross_camera_context", [])}
        if name == "get_identity_context":
            self._range(arguments)
            return {"context": self.payload.get("identity_context", [])}
        if name == "get_manual_annotations":
            self._range(arguments)
            return {"annotations": self.payload.get("manual_annotations", [])}
        if name == "request_reobserve":
            start, end = self._range(arguments)
            density = arguments.get("density", "dense")
            if density not in {"dense", "sparse"}:
                raise AppError(422, "TEMPORAL_TOOL_DENIED", "重新观察密度无效")
            camera_id = self.payload.get("camera_id")
            provider_id = self.payload.get("provider_id")
            if not camera_id or not provider_id:
                raise AppError(422, "TEMPORAL_TOOL_DENIED", "复核任务缺少重新观察所需配置")
            with self.w.db.write() as conn:
                rows = (
                    conn.execute(
                        select(s.frames.c.id, s.frames.c.expires_at_ms)
                        .where(
                            s.frames.c.camera_id == camera_id,
                            s.frames.c.id.in_(self.allowed_evidence),
                            s.frames.c.captured_at_ms >= start,
                            s.frames.c.captured_at_ms < end,
                            s.frames.c.state == "available",
                            s.frames.c.expires_at_ms > self.w.now(),
                        )
                        .order_by(s.frames.c.captured_at_ms, s.frames.c.id)
                    )
                    .mappings()
                    .all()
                )
                evidence_ids = [row["id"] for row in rows]
                if not evidence_ids:
                    return {"accepted": False, "reason": "no_available_evidence"}
                from app.services.analysis import Analysis

                task = Analysis(self.w).plan_temporal_slice(
                    conn,
                    camera_id,
                    start,
                    end,
                    provider_id,
                    reference_set_id=self.payload.get("reference_set_id"),
                    dense=density == "dense",
                    evidence_ids=evidence_ids,
                    source_revision=self.payload.get("source_revision"),
                    reobserve_question=str(arguments.get("question", ""))[:500],
                    task_kind="temporal_reobserve",
                )
            return {
                "accepted": True,
                "job_id": task["id"],
                "request": {
                    "from": start,
                    "to": end,
                    "question": str(arguments.get("question", ""))[:500],
                    "density": arguments.get("density", "dense"),
                },
            }
        raise AppError(422, "TEMPORAL_TOOL_DENIED", "工具不可用")
