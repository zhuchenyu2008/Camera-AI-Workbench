"""Camera/time-scoped L2 evidence tools backed by short read-only DB sessions."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.ai_tools import ALLOWED_TOOLS
from app.models import ActivitySegment, CameraGap, Frame


class OrmL2ToolService:
    """Authorized, bounded frame/context retrieval for one persisted L2 job."""

    def __init__(
        self,
        *,
        camera_id: str,
        start_ms: int,
        end_ms: int,
        data_dir: str | Path,
        session_factory,
        initially_authorized_frame_ids: set[str] | frozenset[str],
        candidate_frame_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.start_ms = int(start_ms)
        self.end_ms = int(end_ms)
        self.data_dir = Path(data_dir).resolve()
        self.frame_root = (self.data_dir / "frames").resolve()
        self.session_factory = session_factory
        self.authorized_frame_ids = set(initially_authorized_frame_ids)
        self.delivered_frame_ids = set(initially_authorized_frame_ids)
        self.candidate_frame_ids = None if candidate_frame_ids is None else set(candidate_frame_ids)

    def authorize(self, frame_ids: set[str]) -> None:
        self.authorized_frame_ids.update(frame_ids)
        self.delivered_frame_ids.update(frame_ids)

    def handlers(self) -> dict[str, Any]:
        return {
            "get_frames": self.get_frames,
            "get_keyframes": self.get_keyframes,
            "compare_frames": self.compare_frames,
            "crop_frame": self.crop_frame,
            "get_local_signals": self.get_local_signals,
            "get_adjacent_activity": self.get_adjacent_activity,
            "request_high_precision": self.request_high_precision,
        }

    def _session(self) -> Session:
        return self.session_factory()

    def _within(self, start_ms: int, end_ms: int) -> tuple[int, int]:
        start, end = int(start_ms), int(end_ms)
        if start < self.start_ms or end > self.end_ms or end <= start:
            raise ValueError("工具时间超出任务授权窗口")
        return start, end

    def _read_image(self, frame: Frame) -> bytes:
        path = (self.data_dir / frame.relative_path).resolve()
        if self.frame_root not in path.parents or not path.is_file():
            raise ValueError("授权帧文件不可用")
        return path.read_bytes()

    def _query_frames(self, start_ms: int, end_ms: int, limit: int, *, keyframes: bool = False) -> list[Frame]:
        start, end = self._within(start_ms, end_ms)
        db = self._session()
        try:
            query = select(Frame).where(
                Frame.camera_id == self.camera_id,
                Frame.captured_at_ms >= start,
                Frame.captured_at_ms < end,
                Frame.state == "active",
            )
            if self.candidate_frame_ids is not None:
                if not self.candidate_frame_ids:
                    return []
                query = query.where(Frame.id.in_(self.candidate_frame_ids))
            if self.delivered_frame_ids:
                query = query.where(Frame.id.not_in(self.delivered_frame_ids))
            if keyframes:
                query = query.order_by(Frame.diff_score.desc(), Frame.captured_at_ms, Frame.id)
            else:
                query = query.order_by(Frame.captured_at_ms, Frame.id)
            rows = list(db.scalars(query.limit(min(30, max(1, int(limit))))))
            return rows
        finally:
            db.close()

    def _frame_result(self, frames: list[Frame], summary: str) -> dict[str, Any]:
        ids = [frame.id for frame in frames]
        images = [self._read_image(frame) for frame in frames]
        return {
            "summary": summary,
            "frame_ids": ids,
            "captured_at_ms": {frame.id: frame.captured_at_ms for frame in frames},
            "signals": {
                frame.id: {
                    "diff_score": frame.diff_score,
                    "event": frame.event_id is not None,
                    "local_signal": frame.local_signal_json or {},
                }
                for frame in frames
            },
            "images": images,
            "image_frame_ids": ids,
            "_authorized_frame_ids": ids,
        }

    def get_frames(self, start_ms: int, end_ms: int, limit: int) -> dict[str, Any]:
        frames = self._query_frames(start_ms, end_ms, limit)
        return self._frame_result(frames, f"按时间顺序返回 {len(frames)} 张授权缓存帧。")

    def get_keyframes(self, start_ms: int, end_ms: int, limit: int) -> dict[str, Any]:
        frames = self._query_frames(start_ms, end_ms, limit, keyframes=True)
        frames.sort(key=lambda frame: (frame.captured_at_ms, frame.id))
        return self._frame_result(frames, f"按局部变化强度选取 {len(frames)} 张授权关键帧。")

    def request_high_precision(self, start_ms: int, end_ms: int, limit: int) -> dict[str, Any]:
        start, end = self._within(start_ms, end_ms)
        if end - start > 300_000:
            raise ValueError("高精度补证最多覆盖五分钟")
        frames = self._query_frames(start, end, limit, keyframes=False)
        return self._frame_result(frames, f"从五分钟内缓存中返回 {len(frames)} 张高精度补证帧。")

    def compare_frames(self, frame_a: str, frame_b: str) -> dict[str, Any]:
        left = self._get_authorized_frame(str(frame_a))
        right = self._get_authorized_frame(str(frame_b))
        left_bytes, right_bytes = self._read_image(left), self._read_image(right)
        with Image.open(BytesIO(left_bytes)) as left_image, Image.open(BytesIO(right_bytes)):
            a = left_image.convert("RGB")
            b = right_image.convert("RGB").resize(a.size)
            difference = ImageStat.Stat(ImageChops.difference(a, b)).mean
            mean_difference = sum(difference) / (3 * 255)
        return {
            "frame_a": left.id,
            "frame_b": right.id,
            "captured_at_ms": {left.id: left.captured_at_ms, right.id: right.captured_at_ms},
            "sha256_equal": left.sha256 == right.sha256,
            "pixel_difference_ratio": round(float(mean_difference), 6),
            "local_diff_scores": {left.id: left.diff_score, right.id: right.diff_score},
        }

    def crop_frame(self, frame_id: str, region: dict[str, float]) -> dict[str, Any]:
        frame = self._get_authorized_frame(frame_id)
        raw = self._read_image(frame)
        with Image.open(BytesIO(raw)) as source:
            image = source.convert("RGB")
            width, height = image.size
            left = min(width - 1, int(float(region["x"]) * width))
            top = min(height - 1, int(float(region["y"]) * height))
            right = max(left + 1, min(width, int((float(region["x"]) + float(region["width"])) * width)))
            bottom = max(top + 1, min(height, int((float(region["y"]) + float(region["height"])) * height)))
            cropped = image.crop((left, top, right, bottom))
            output = BytesIO()
            cropped.save(output, format="JPEG", quality=85, optimize=True)
        return {
            "frame_id": frame.id,
            "region": region,
            "images": [output.getvalue()],
            "image_frame_ids": [frame.id],
        }

    def get_local_signals(self, start_ms: int, end_ms: int) -> dict[str, Any]:
        start, end = self._within(start_ms, end_ms)
        db = self._session()
        try:
            query = select(Frame).where(
                Frame.camera_id == self.camera_id,
                Frame.captured_at_ms >= start,
                Frame.captured_at_ms < end,
                Frame.state == "active",
            )
            if self.candidate_frame_ids is not None:
                if not self.candidate_frame_ids:
                    query = query.where(False)
                else:
                    query = query.where(Frame.id.in_(self.candidate_frame_ids))
            frames = list(db.scalars(query.order_by(Frame.captured_at_ms).limit(600)))
            gaps = list(db.scalars(select(CameraGap).where(
                CameraGap.camera_id == self.camera_id,
                CameraGap.start_at_ms < end,
                or_(CameraGap.end_at_ms.is_(None), CameraGap.end_at_ms > start),
            ).order_by(CameraGap.start_at_ms).limit(100)))
            scores = [float(frame.diff_score or 0) for frame in frames]
            return {
                "frame_count": len(frames),
                "event_frame_count": sum(frame.event_id is not None for frame in frames),
                "mean_diff_score": sum(scores) / len(scores) if scores else 0,
                "max_diff_score": max(scores, default=0),
                "frozen_count": sum(bool((frame.local_signal_json or {}).get("frozen")) for frame in frames),
                "frame_gap_count": sum(bool((frame.local_signal_json or {}).get("frame_gap")) for frame in frames),
                "gaps": [{"start_ms": max(start, gap.start_at_ms), "end_ms": min(end, gap.end_at_ms or end), "reason": gap.reason} for gap in gaps],
            }
        finally:
            db.close()

    def get_adjacent_activity(self, start_ms: int, end_ms: int) -> dict[str, Any]:
        start, end = self._within(start_ms, end_ms)
        db = self._session()
        try:
            before = list(db.scalars(select(ActivitySegment).where(
                ActivitySegment.camera_id == self.camera_id,
                ActivitySegment.end_at_ms <= start,
            ).order_by(ActivitySegment.end_at_ms.desc()).limit(3)))
            after = list(db.scalars(select(ActivitySegment).where(
                ActivitySegment.camera_id == self.camera_id,
                ActivitySegment.start_at_ms >= end,
            ).order_by(ActivitySegment.start_at_ms).limit(3)))
            def dto(segment: ActivitySegment) -> dict[str, Any]:
                return {
                    "start_ms": segment.start_at_ms,
                    "end_ms": segment.end_at_ms,
                    "activity": segment.canonical_activity.name if segment.canonical_activity else None,
                    "description": segment.display_description,
                    "category": segment.category,
                    "state": segment.state,
                    "locked": segment.locked,
                }
            return {"before": [dto(item) for item in reversed(before)], "after": [dto(item) for item in after]}
        finally:
            db.close()

    def _get_authorized_frame(self, frame_id: str) -> Frame:
        if frame_id not in self.authorized_frame_ids:
            raise ValueError("帧不在任务授权范围")
        db = self._session()
        try:
            frame = db.scalar(select(Frame).where(
                Frame.id == frame_id,
                Frame.camera_id == self.camera_id,
                Frame.state == "active",
                Frame.captured_at_ms >= self.start_ms,
                Frame.captured_at_ms < self.end_ms,
            ))
            if frame is None:
                raise ValueError("授权帧不存在或已过期")
            return frame
        finally:
            db.close()


__all__ = ["OrmL2ToolService"]