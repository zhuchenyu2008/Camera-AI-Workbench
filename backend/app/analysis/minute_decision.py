"""受限的分钟决策工具和确定性回退。

视觉请求只负责观察。这个模块给需要复核的分钟提供一个很小的、只读的
工具面，并把最终动作限制为固定枚举；时长仍由 Ledger 计算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import select

from app.errors import AppError
from app.repositories import schema as s
from app.schemas import Strict
from app.timeutils import iso, timestamp

MAX_TOOL_CALLS = 2

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_minute_packet",
            "description": "读取当前机位当前分钟的证据槽位、分析状态和活动锚点。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_adjacent_minutes",
            "description": "读取当前分钟前后各一个分钟的活动结果。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_activity_catalog",
            "description": "读取当前机位已有活动名称目录。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_range_statistics",
            "description": "读取当前分钟附近限定范围的确定性覆盖统计。",
            "parameters": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                },
                "required": ["from", "to"],
                "additionalProperties": False,
            },
        },
    },
]


class MinuteDecision(Strict):
    action: Literal["infer_full_minute", "keep_observed", "keep_conflict"]
    reason: str = Field(default="", max_length=500)


@dataclass
class MinuteToolRegistry:
    """Read-only tool registry scoped to one camera and one minute."""

    workbench: Any
    conn: Any
    camera_id: str
    minute_start: int
    max_calls: int = MAX_TOOL_CALLS

    def __post_init__(self):
        self.calls = 0
        self.audit: list[dict] = []

    def _guard(self, name: str, args: dict) -> None:
        allowed = {
            "get_minute_packet": set(),
            "get_adjacent_minutes": set(),
            "get_activity_catalog": set(),
            "get_range_statistics": {"from", "to"},
        }
        if name not in allowed:
            self.audit.append({"name": name, "arguments": args, "error": "MINUTE_TOOL_DENIED"})
            raise AppError(422, "MINUTE_TOOL_DENIED", "分钟决策工具不在只读白名单")
        if self.calls >= self.max_calls:
            self.audit.append({"name": name, "arguments": args, "error": "MINUTE_TOOL_LIMIT"})
            raise AppError(422, "MINUTE_TOOL_LIMIT", "分钟决策最多调用两个工具")
        if not isinstance(args, dict):
            self.audit.append({"name": name, "arguments": args, "error": "MINUTE_TOOL_INVALID_ARGUMENTS"})
            raise AppError(422, "MINUTE_TOOL_INVALID_ARGUMENTS", "工具参数必须为对象")
        if set(args) - allowed[name]:
            self.audit.append({"name": name, "arguments": args, "error": "MINUTE_TOOL_INVALID_ARGUMENTS"})
            raise AppError(422, "MINUTE_TOOL_INVALID_ARGUMENTS", "工具参数包含未允许字段")
        self.calls += 1

    def call(self, name: str, args: dict | None = None) -> dict:
        args = args or {}
        self._guard(name, args)
        try:
            if name == "get_minute_packet":
                result = self.minute_packet()
            elif name == "get_adjacent_minutes":
                result = self.adjacent_minutes()
            elif name == "get_activity_catalog":
                from app.services.analysis import Analysis

                result = {"activities": Analysis(self.workbench).activity_catalog(self.conn, self.camera_id)}
            else:
                result = self.range_statistics(args)
        except AppError as exc:
            if not self.audit or self.audit[-1].get("name") != name or "error" not in self.audit[-1]:
                self.audit.append({"name": name, "arguments": args, "error": exc.code})
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError(422, "MINUTE_TOOL_INVALID_ARGUMENTS", "工具参数无效") from exc
        self.audit.append({"name": name, "arguments": args, "result": result})
        return result

    def _minute_row(self, start: int) -> dict | None:
        return (
            self.conn.execute(
                select(s.minute_records, s.minute_revisions.c.result_json)
                .outerjoin(
                    s.minute_revisions,
                    (s.minute_revisions.c.minute_id == s.minute_records.c.id)
                    & (s.minute_revisions.c.revision == s.minute_records.c.current_revision),
                )
                .where(s.minute_records.c.camera_id == self.camera_id, s.minute_records.c.start_at_ms == start)
            )
            .mappings()
            .first()
        )

    def minute_packet(self) -> dict:
        row = self._minute_row(self.minute_start)
        if row is None:
            raise AppError(422, "MINUTE_TOOL_SCOPE", "分钟不属于当前机位")
        frames = self.conn.execute(
            select(s.frames.c.id, s.frames.c.captured_at_ms, s.frames.c.expires_at_ms)
            .where(
                s.frames.c.camera_id == self.camera_id,
                s.frames.c.captured_at_ms >= self.minute_start,
                s.frames.c.captured_at_ms < self.minute_start + 60000,
            )
            .order_by(s.frames.c.captured_at_ms, s.frames.c.id)
        ).mappings().all()
        spans = [
            {
                "activity": row["activity"],
                "start_at": iso(row["start_at_ms"]),
                "end_at": iso(row["end_at_ms"]),
                "identity_status": row["identity_status"],
                "inferred": row.get("details_json", {}).get("inference_mode") == "edge_estimated",
            }
            for row in self.workbench.ledger.spans(
                self.conn, [self.camera_id], self.minute_start, self.minute_start + 60000
            )
        ]
        result = row.get("result_json") or {}
        return {
            "camera_id": self.camera_id,
            "minute_start": iso(self.minute_start),
            "analysis_state": row["analysis_state"],
            "evidence_ids": [frame["id"] for frame in frames],
            "evidence_slots": [
                {
                    "evidence_id": frame["id"],
                    "captured_at": iso(frame["captured_at_ms"]),
                    "expires_at": iso(frame["expires_at_ms"]),
                }
                for frame in frames
            ],
            "activities": spans,
            "visual_outputs": result.get("visual_outputs", []),
        }

    def adjacent_minutes(self) -> dict:
        result = []
        for start in (self.minute_start - 60000, self.minute_start + 60000):
            row = self._minute_row(start)
            if row is None:
                continue
            result.append(
                {
                    "minute_start": iso(start),
                    "analysis_state": row["analysis_state"],
                    "description": (row.get("result_json") or {}).get("description", ""),
                    "activities": [
                        {
                            "activity": span["activity"],
                            "start_at": iso(span["start_at_ms"]),
                            "end_at": iso(span["end_at_ms"]),
                        }
                        for span in self.workbench.ledger.spans(
                            self.conn, [self.camera_id], start, start + 60000
                        )
                    ],
                }
            )
        return {"minutes": result}

    def range_statistics(self, args: dict) -> dict:
        try:
            start = timestamp(args["from"])
            end = timestamp(args["to"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError(422, "MINUTE_TOOL_INVALID_ARGUMENTS", "统计范围必须是有效时间") from exc
        lower, upper = self.minute_start - 60000, self.minute_start + 120000
        if start < lower or end > upper or end <= start:
            raise AppError(422, "MINUTE_TOOL_SCOPE", "统计范围超出当前分钟附近限制")
        return self.workbench.ledger.calculate(
            self.conn,
            start,
            end,
            {"camera_ids": [self.camera_id], "subject_id": None},
        )


def needs_minute_decision(spans: list[dict], review: bool) -> bool:
    if review:
        return True
    return any(
        span.get("details_json", {}).get("inference_mode") == "edge_estimated"
        or span.get("identity_status") in {"unknown", "possible"}
        for span in spans
    )
