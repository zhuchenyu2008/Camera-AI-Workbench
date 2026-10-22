"""Server-side whitelist tool executor for L2 evidence completion."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Any, Callable, Mapping

ALLOWED_TOOLS = frozenset({
    "get_frames",
    "get_keyframes",
    "compare_frames",
    "crop_frame",
    "get_local_signals",
    "get_adjacent_activity",
    "request_high_precision",
})

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "get_frames", "description": "Retrieve a bounded set of authorized frames from a time interval.", "parameters": {
        "type": "object", "properties": {"start_ms": {"type": "integer"}, "end_ms": {"type": "integer"}, "limit": {"type": "integer", "minimum": 1, "maximum": 30}},
        "required": ["start_ms", "end_ms", "limit"], "additionalProperties": False,
    }}},
    {"type": "function", "function": {"name": "get_keyframes", "description": "Retrieve the strongest change frames in a bounded interval.", "parameters": {
        "type": "object", "properties": {"start_ms": {"type": "integer"}, "end_ms": {"type": "integer"}, "limit": {"type": "integer", "minimum": 1, "maximum": 30}},
        "required": ["start_ms", "end_ms", "limit"], "additionalProperties": False,
    }}},
    {"type": "function", "function": {"name": "compare_frames", "description": "Compare two already authorized frame identifiers.", "parameters": {
        "type": "object", "properties": {"frame_a": {"type": "string"}, "frame_b": {"type": "string"}},
        "required": ["frame_a", "frame_b"], "additionalProperties": False,
    }}},
    {"type": "function", "function": {"name": "crop_frame", "description": "Inspect a relative crop from an already authorized frame.", "parameters": {
        "type": "object", "properties": {"frame_id": {"type": "string"}, "region": {"type": "object", "properties": {
            "x": {"type": "number", "minimum": 0, "maximum": 1}, "y": {"type": "number", "minimum": 0, "maximum": 1},
            "width": {"type": "number", "minimum": 0, "maximum": 1}, "height": {"type": "number", "minimum": 0, "maximum": 1},
        }, "required": ["x", "y", "width", "height"], "additionalProperties": False}},
        "required": ["frame_id", "region"], "additionalProperties": False,
    }}},
    {"type": "function", "function": {"name": "get_local_signals", "description": "Read non-semantic change and coverage signals for an authorized time range.", "parameters": {
        "type": "object", "properties": {"start_ms": {"type": "integer"}, "end_ms": {"type": "integer"}},
        "required": ["start_ms", "end_ms"], "additionalProperties": False,
    }}},
    {"type": "function", "function": {"name": "get_adjacent_activity", "description": "Read existing activity summaries adjacent to the authorized interval.", "parameters": {
        "type": "object", "properties": {"start_ms": {"type": "integer"}, "end_ms": {"type": "integer"}},
        "required": ["start_ms", "end_ms"], "additionalProperties": False,
    }}},
    {"type": "function", "function": {"name": "request_high_precision", "description": "Retrieve cached high-frequency frames for a subrange no longer than five minutes.", "parameters": {
        "type": "object", "properties": {"start_ms": {"type": "integer"}, "end_ms": {"type": "integer"}, "limit": {"type": "integer", "minimum": 1, "maximum": 30}},
        "required": ["start_ms", "end_ms", "limit"], "additionalProperties": False,
    }}},
]


class ToolAuthorizationError(ValueError):
    """A model-requested tool call exceeded its server-owned authorization."""


@dataclass(frozen=True)
class ToolContext:
    camera_id: str
    start_ms: int
    end_ms: int
    allowed_frame_ids: frozenset[str] = frozenset()
    max_calls: int = 4
    max_images: int = 30
    calls: int = 0
    images: int = 0
    max_range_ms: int | None = None


class ToolExecutor:
    """Only executes server callbacks inside a fixed camera/time/image budget."""

    def __init__(
        self,
        context: ToolContext,
        handlers: Mapping[str, Callable[..., Any]],
        on_authorize: Callable[[set[str]], None] | None = None,
    ):
        self.context = context
        self.handlers = dict(handlers)
        self.on_authorize = on_authorize

    @property
    def calls_used(self) -> int:
        """Number of attempted tool calls, including denied calls; callers cannot mutate the budget."""
        return self.context.calls

    @property
    def calls_remaining(self) -> int:
        return max(0, self.context.max_calls - self.context.calls)

    @property
    def images_used(self) -> int:
        """Initial plus tool-returned image count for this job."""
        return self.context.images

    @property
    def images_remaining(self) -> int:
        return max(0, self.context.max_images - self.context.images)

    def execute(self, name: str, arguments: Mapping[str, Any]) -> Any:
        if self.calls_remaining <= 0:
            raise ToolAuthorizationError("工具调用次数超过任务预算")
        # Count attempted calls, including denied or failed ones, so failures cannot
        # be used to evade the per-job call budget.
        self.context = replace(self.context, calls=self.context.calls + 1)
        if name not in ALLOWED_TOOLS or name not in self.handlers:
            raise ToolAuthorizationError("工具不在服务端白名单中")
        if not isinstance(arguments, Mapping):
            raise ToolAuthorizationError("工具参数必须是 JSON 对象")
        args = dict(arguments)
        self._validate(name, args)
        result = self.handlers[name](**args)
        image_count = _image_count(result)
        if self.context.images + image_count > self.context.max_images:
            raise ToolAuthorizationError("工具图片预算已用尽")
        result_ids: set[str] = set()
        if isinstance(result, Mapping):
            raw_ids = result.get("_authorized_frame_ids", ())
            if not isinstance(raw_ids, (list, tuple, set, frozenset)):
                raise ToolAuthorizationError("工具返回了无效的帧授权列表")
            if any(not isinstance(frame_id, str) or not frame_id for frame_id in raw_ids):
                raise ToolAuthorizationError("工具返回了无效的帧授权列表")
            result_ids = set(raw_ids)
        if result_ids and self.on_authorize is not None:
            self.on_authorize(result_ids)
        self.context = replace(
            self.context,
            allowed_frame_ids=self.context.allowed_frame_ids | result_ids,
            images=self.context.images + image_count,
        )
        return result

    def _validate(self, name: str, args: dict[str, Any]) -> None:
        contracts = {
            "get_frames": ({"start_ms", "end_ms", "limit"}, {"start_ms", "end_ms", "limit"}),
            "get_keyframes": ({"start_ms", "end_ms", "limit"}, {"start_ms", "end_ms", "limit"}),
            "request_high_precision": ({"start_ms", "end_ms", "limit"}, {"start_ms", "end_ms", "limit"}),
            "compare_frames": ({"frame_a", "frame_b"}, {"frame_a", "frame_b"}),
            "crop_frame": ({"frame_id", "region"}, {"frame_id", "region"}),
            "get_local_signals": ({"start_ms", "end_ms"}, {"start_ms", "end_ms"}),
            "get_adjacent_activity": ({"start_ms", "end_ms"}, {"start_ms", "end_ms"}),
        }
        required, allowed = contracts[name]
        if set(args) - allowed or required - set(args):
            raise ToolAuthorizationError("工具参数不符合白名单 schema")

        has_start = "start_ms" in args or "start" in args
        has_end = "end_ms" in args or "end" in args
        if has_start or has_end:
            start = args.get("start_ms", args.get("start"))
            end = args.get("end_ms", args.get("end"))
            if (
                not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool)
            ):
                raise ToolAuthorizationError("工具时间范围无效")
            if start < self.context.start_ms or end > self.context.end_ms or end <= start:
                raise ToolAuthorizationError("工具时间范围超出任务授权窗口")
            if self.context.max_range_ms is not None and end - start > self.context.max_range_ms:
                raise ToolAuthorizationError("工具时间范围超过任务预算")

        for key in {"frame_id", "frame_a", "frame_b"} & set(args):
            frame_id = args[key]
            if (
                not isinstance(frame_id, str) or not frame_id or len(frame_id) > 128
                or "/" in frame_id or "\\" in frame_id or ".." in frame_id
                or frame_id not in self.context.allowed_frame_ids
            ):
                raise ToolAuthorizationError("工具引用了未授权帧")

        limit = args.get("limit")
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 30:
                raise ToolAuthorizationError("工具 limit 超出预算")
            if self.context.images >= self.context.max_images or limit > self.images_remaining:
                raise ToolAuthorizationError("工具图片预算已用尽")

        if name == "crop_frame" and self.context.images >= self.context.max_images:
            raise ToolAuthorizationError("工具图片预算已用尽")
        if "region" in args:
            region = args["region"]
            keys = ("x", "y", "width", "height")
            if not isinstance(region, Mapping) or any(
                not isinstance(region.get(key), (int, float))
                or isinstance(region.get(key), bool)
                or not math.isfinite(float(region[key]))
                or not 0 <= float(region[key]) <= 1
                for key in keys
            ):
                raise ToolAuthorizationError("crop region 必须是 0 到 1 的相对坐标")
            if float(region["x"]) + float(region["width"]) > 1 or float(region["y"]) + float(region["height"]) > 1:
                raise ToolAuthorizationError("crop region 越界")


def _image_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return sum(_image_count(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_image_count(item) for item in value)
    if isinstance(value, (bytes, bytearray)):
        return 1
    return 0


__all__ = ["ALLOWED_TOOLS", "TOOL_SCHEMAS", "ToolAuthorizationError", "ToolContext", "ToolExecutor"]