from __future__ import annotations

import hashlib
import math
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Deque, Mapping

import numpy as np
from PIL import Image

from .capture.sources import CaptureFrame
from .capture.writer import _to_pil_image, perceptual_hash


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    resize_width: int = 320
    resize_height: int = 180
    change_threshold: float = 0.08
    duplicate_pixel_threshold: float = 0.015
    hash_distance_threshold: int = 4
    smoothing_window: int = 3
    trigger_consecutive: int = 2
    stable_consecutive: int = 3
    freeze_after_seconds: int = 10
    prebuffer_seconds: int = 30
    post_roll_seconds: int = 60
    max_event_seconds: int = 300
    expected_interval_ms: int = 1000
    detector_version: str = "local-v1"

    def __post_init__(self) -> None:
        if self.resize_width <= 0 or self.resize_height <= 0:
            raise ValueError("缩放尺寸必须大于 0")
        if not 0 <= self.change_threshold <= 1 or not 0 <= self.duplicate_pixel_threshold <= 1:
            raise ValueError("差异阈值必须在 0 到 1 之间")
        if self.hash_distance_threshold < 0:
            raise ValueError("hash_distance_threshold 不能为负")
        if min(self.smoothing_window, self.trigger_consecutive, self.stable_consecutive) <= 0:
            raise ValueError("连续计数配置必须大于 0")
        if min(self.freeze_after_seconds, self.prebuffer_seconds, self.post_roll_seconds) < 0:
            raise ValueError("秒数配置不能为负")
        if self.max_event_seconds <= 0 or self.expected_interval_ms <= 0:
            raise ValueError("事件上限和采集间隔必须大于 0")


@dataclass(frozen=True, slots=True)
class LocalSignal:
    camera_id: str
    captured_at_ms: int
    score: float
    raw_score: float
    stable: bool
    duplicate: bool
    frozen: bool
    frame_gap: bool
    timestamp_gap_ms: int
    trigger_reason: str | None
    detector_version: str
    sha256: str | None = None
    perceptual_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id, "captured_at_ms": self.captured_at_ms,
            "score": self.score, "raw_score": self.raw_score, "stable": self.stable,
            "duplicate": self.duplicate, "frozen": self.frozen, "frame_gap": self.frame_gap,
            "timestamp_gap_ms": self.timestamp_gap_ms, "trigger_reason": self.trigger_reason,
            "detector_version": self.detector_version, "sha256": self.sha256,
            "perceptual_hash": self.perceptual_hash,
        }


class SignalDetector:
    """低分辨率灰度 MAD + perceptual hash；不做语义、人脸或 OCR。"""

    def __init__(self, config: DetectorConfig | None = None):
        self.config = config or DetectorConfig()
        self._previous_gray: np.ndarray | None = None
        self._previous_hash: str | None = None
        self._previous_sha256: str | None = None
        self._previous_timestamp_ms: int | None = None
        self._scores: deque[float] = deque(maxlen=self.config.smoothing_window)
        self._duplicate_since_ms: int | None = None
        self._last_camera_id: str | None = None

    def reset(self) -> None:
        self.__init__(self.config)

    def set_expected_interval(self, interval_ms: int) -> None:
        """Update the planned sampling cadence without discarding the signal history."""
        if interval_ms <= 0:
            raise ValueError("expected interval must be positive")
        from dataclasses import replace

        self.config = replace(self.config, expected_interval_ms=int(interval_ms))

    def process(self, frame: CaptureFrame, *, sha256: str | None = None) -> LocalSignal:
        if self._last_camera_id is not None and self._last_camera_id != frame.camera_id:
            self.reset()
        self._last_camera_id = frame.camera_id
        gray = _normalized_gray(frame.frame, self.config.resize_width, self.config.resize_height)
        current_hash = perceptual_hash(Image.fromarray(gray, mode="L"))
        if self._previous_timestamp_ms is None:
            timestamp_gap = 0
            frame_gap = False
        else:
            timestamp_gap = frame.captured_at_ms - self._previous_timestamp_ms
            frame_gap = timestamp_gap <= 0 or timestamp_gap > int(self.config.expected_interval_ms * 1.5)
        if self._previous_gray is None:
            raw_score = 0.0
            duplicate = False
        else:
            raw_score = float(np.mean(np.abs(gray.astype(np.float32) - self._previous_gray.astype(np.float32))) / 255.0)
            exact_duplicate = sha256 is not None and sha256 == self._previous_sha256
            close_hash = (
                _hex_hamming_distance(current_hash, self._previous_hash or "") <= self.config.hash_distance_threshold
                and raw_score <= self.config.duplicate_pixel_threshold
            )
            duplicate = exact_duplicate or close_hash
        if duplicate:
            if self._duplicate_since_ms is None:
                self._duplicate_since_ms = frame.captured_at_ms
        else:
            self._duplicate_since_ms = None
        frozen = bool(
            duplicate
            and self._duplicate_since_ms is not None
            and frame.captured_at_ms - self._duplicate_since_ms >= self.config.freeze_after_seconds * 1000
        )
        self._scores.append(raw_score)
        score = float(sum(self._scores) / len(self._scores))
        stable = len(self._scores) >= self.config.smoothing_window and score < self.config.change_threshold
        reasons: list[str] = []
        if score >= self.config.change_threshold:
            reasons.append("diff")
        if duplicate:
            reasons.append("duplicate")
        if frozen:
            reasons.append("freeze")
        if frame_gap:
            reasons.append("frame_gap")
        result = LocalSignal(
            camera_id=frame.camera_id, captured_at_ms=frame.captured_at_ms, score=score,
            raw_score=raw_score, stable=stable, duplicate=duplicate, frozen=frozen,
            frame_gap=frame_gap, timestamp_gap_ms=timestamp_gap,
            trigger_reason=",".join(reasons) or None, detector_version=self.config.detector_version,
            sha256=sha256, perceptual_hash=current_hash,
        )
        self._previous_gray = gray
        self._previous_hash = current_hash
        self._previous_sha256 = sha256
        self._previous_timestamp_ms = frame.captured_at_ms
        return result


def _normalized_gray(value: Any, width: int, height: int) -> np.ndarray:
    image = _to_pil_image(value).convert("L")
    image.thumbnail((width, height), Image.Resampling.BILINEAR)
    canvas = Image.new("L", (width, height), 0)
    canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
    return np.asarray(canvas, dtype=np.uint8)


def _hex_hamming_distance(first: str, second: str) -> int:
    if not first or not second or len(first) != len(second):
        return 10**9
    try:
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except ValueError:
        return 10**9


class EventState(str, Enum):
    NORMAL = "normal"
    EVENT_PREBUFFER_FROZEN = "event_prebuffer_frozen"
    HIGH_PRECISION_CAPTURE = "high_precision_capture"
    POST_ROLL = "post_roll"


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: str
    camera_id: str
    started_at_ms: int
    ended_at_ms: int | None
    state: EventState
    trigger_reasons: tuple[str, ...]
    config_snapshot: Mapping[str, Any]
    prebuffer: tuple[CaptureFrame, ...]
    frames: tuple[CaptureFrame, ...]
    closed_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "camera_id": self.camera_id,
            "started_at_ms": self.started_at_ms, "ended_at_ms": self.ended_at_ms,
            "state": self.state.value, "trigger_reasons": list(self.trigger_reasons),
            "config_snapshot": dict(self.config_snapshot), "frame_count": len(self.frames),
            "prebuffer_count": len(self.prebuffer), "closed_reason": self.closed_reason,
        }


class ChangeEventStateMachine:
    """NORMAL → 前缓存冻结 → 高精度采集 → 后滚动；活动窗口保留帧引用。"""

    def __init__(self, config: DetectorConfig | None = None, *, event_id_factory: Any | None = None):
        self.config = config or DetectorConfig()
        self._event_id_factory = event_id_factory or _event_id
        self._ring: Deque[CaptureFrame] = deque()
        self._ring_capacity = max(1, math.ceil(self.config.prebuffer_seconds * 1000 / self.config.expected_interval_ms) + 2)
        self.state = EventState.NORMAL
        self._candidate: list[CaptureFrame] = []
        self._stable_count = 0
        self._event: dict[str, Any] | None = None

    @property
    def ring_buffer(self) -> tuple[CaptureFrame, ...]:
        return tuple(self._ring)

    @property
    def active_event(self) -> EventRecord | None:
        return self._snapshot_event() if self._event is not None else None

    def ingest(self, frame: CaptureFrame, signal: LocalSignal) -> EventRecord | None:
        if frame.camera_id != signal.camera_id or frame.captured_at_ms != signal.captured_at_ms:
            raise ValueError("frame 与 signal 的摄像头或时间戳不一致")
        if self._ring and frame.captured_at_ms < self._ring[-1].captured_at_ms:
            raise ValueError("帧时间戳必须单调不减")
        self._append_ring(frame)
        is_change = signal.score >= self.config.change_threshold and not signal.frame_gap and not signal.duplicate
        if self.state is EventState.NORMAL:
            return self._ingest_normal(frame, signal, is_change)
        if self._event is None:
            raise RuntimeError("状态机事件状态不一致")
        if (
            not self._event["frames"]
            or self._event["frames"][-1].captured_at_ms != frame.captured_at_ms
        ):
            self._event["frames"].append(frame)
        self._event["last_at_ms"] = frame.captured_at_ms
        self._event["reasons"].update(_reasons(signal))
        elapsed = frame.captured_at_ms - self._event["started_at_ms"]
        if elapsed >= self.config.max_event_seconds * 1000:
            return self._close(frame.captured_at_ms, "max_duration")
        if self.state is EventState.EVENT_PREBUFFER_FROZEN:
            # 事件确认后的下一帧开始高精度采集；至少保留一个可观察的高精度阶段。
            self.state = EventState.HIGH_PRECISION_CAPTURE
            self._event["state"] = self.state
            if is_change:
                self._event["last_change_at_ms"] = frame.captured_at_ms
                self._stable_count = 0
            return None
        if is_change:
            self._event["last_change_at_ms"] = frame.captured_at_ms
            self._stable_count = 0
            self.state = EventState.HIGH_PRECISION_CAPTURE
        else:
            self._stable_count = self._stable_count + 1 if signal.stable or signal.duplicate else 0
            self.state = EventState.POST_ROLL
            elapsed_since_change = frame.captured_at_ms - self._event["last_change_at_ms"]
            if (
                self._stable_count >= self.config.stable_consecutive
                and elapsed_since_change >= self.config.post_roll_seconds * 1000
            ):
                return self._close(frame.captured_at_ms, "stable_post_roll")
        self._event["state"] = self.state
        return None

    def flush(self, ended_at_ms: int | None = None, reason: str = "shutdown") -> EventRecord | None:
        if self._event is None:
            return None
        end = ended_at_ms if ended_at_ms is not None else self._event["last_at_ms"]
        return self._close(end, reason)

    def _append_ring(self, frame: CaptureFrame) -> None:
        self._ring.append(frame)
        cutoff = frame.captured_at_ms - self.config.prebuffer_seconds * 1000
        while self._ring and (self._ring[0].captured_at_ms < cutoff or len(self._ring) > self._ring_capacity):
            self._ring.popleft()

    def _ingest_normal(self, frame: CaptureFrame, signal: LocalSignal, is_change: bool) -> EventRecord | None:
        if is_change:
            self._candidate.append(frame)
        else:
            self._candidate.clear()
        if len(self._candidate) < self.config.trigger_consecutive:
            return None
        prebuffer = tuple(self._ring)
        first = self._candidate[0]
        self._event = {
            "event_id": self._event_id_factory(frame.camera_id, first.captured_at_ms),
            "camera_id": frame.camera_id,
            "started_at_ms": first.captured_at_ms,
            "last_at_ms": frame.captured_at_ms,
            "last_change_at_ms": frame.captured_at_ms,
            "state": EventState.EVENT_PREBUFFER_FROZEN,
            "reasons": _reasons(signal) or {"diff"},
            "config_snapshot": _config_snapshot(self.config),
            "prebuffer": prebuffer,
            "frames": list(prebuffer),
        }
        self.state = EventState.EVENT_PREBUFFER_FROZEN
        self._candidate.clear()
        return None

    def _close(self, ended_at_ms: int, reason: str) -> EventRecord:
        event = self._snapshot_event(ended_at_ms=ended_at_ms, closed_reason=reason)
        self._event = None
        self.state = EventState.NORMAL
        self._candidate.clear()
        self._stable_count = 0
        return event

    def _snapshot_event(self, *, ended_at_ms: int | None = None, closed_reason: str | None = None) -> EventRecord:
        assert self._event is not None
        return EventRecord(
            event_id=self._event["event_id"], camera_id=self._event["camera_id"],
            started_at_ms=self._event["started_at_ms"], ended_at_ms=ended_at_ms,
            state=self.state, trigger_reasons=tuple(sorted(self._event["reasons"])),
            config_snapshot=dict(self._event["config_snapshot"]),
            prebuffer=tuple(self._event["prebuffer"]), frames=tuple(self._event["frames"]),
            closed_reason=closed_reason,
        )


def _reasons(signal: LocalSignal) -> set[str]:
    return {item for item in (signal.trigger_reason or "").split(",") if item}


def _config_snapshot(config: DetectorConfig) -> dict[str, Any]:
    return {name: getattr(config, name) for name in config.__dataclass_fields__}


def _event_id(camera_id: str, timestamp_ms: int) -> str:
    return hashlib.sha256(f"{camera_id}:{timestamp_ms}:{time.monotonic_ns()}".encode()).hexdigest()[:24]
