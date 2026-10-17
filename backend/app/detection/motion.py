from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw

from app.errors import AppError


def region_mask(regions: dict, width: int, height: int) -> np.ndarray:
    base = Image.new("L", (width, height), 0 if regions.get("include") else 255)
    draw = ImageDraw.Draw(base)
    for polygon in regions.get("include", []):
        draw.polygon(
            [(round(x * (width - 1)), round(y * (height - 1))) for x, y in polygon["points"]], fill=255
        )
    for polygon in regions.get("exclude", []):
        draw.polygon(
            [(round(x * (width - 1)), round(y * (height - 1))) for x, y in polygon["points"]], fill=0
        )
    mask = np.asarray(base) > 0
    if not mask.any():
        raise AppError(422, "VALIDATION_ERROR", "关注区域减去忽略区域后的有效面积不能为零")
    return mask


def sparse_frames(frames: list[dict], minute_start: int, offsets: list[int]) -> list[dict]:
    selected, used = [], set()
    for offset in offsets:
        candidates = [
            f
            for f in frames
            if f["id"] not in used
            and minute_start <= f["slot_at_ms"] < minute_start + 60000
            and abs(f["slot_at_ms"] - minute_start - offset * 1000) <= 2000
        ]
        if candidates:
            frame = min(
                candidates,
                key=lambda f: (abs(f["slot_at_ms"] - minute_start - offset * 1000), f["slot_at_ms"], f["id"]),
            )
            selected.append(frame)
            used.add(frame["id"])
    return selected


@dataclass
class Detector:
    started_at: int
    state: str = "dense"
    last_motion_at: int | None = None
    settling_at: int | None = None
    previous: np.ndarray | None = None
    consecutive: int = 0

    def feed(self, image: Image.Image, now: int, regions: dict, settings) -> dict:
        image = image.convert("L")
        image.thumbnail((320, 320), Image.Resampling.BILINEAR)
        current = np.asarray(image).astype(np.int16)
        mask = region_mask(regions, image.width, image.height)
        score = 0.0
        if self.previous is not None and self.previous.shape == current.shape:
            score = float((np.abs(current - self.previous)[mask] > settings.motion_pixel_delta).mean())
        self.previous = current
        self.consecutive = self.consecutive + 1 if score >= settings.motion_area_ratio else 0
        triggered = score >= settings.motion_single_frame_ratio or self.consecutive >= 2
        before = self.state
        if triggered:
            self.last_motion_at = now
            self.settling_at = None
            self.state = "dense"
        elif (
            self.state == "dense"
            and now - (self.last_motion_at or self.started_at)
            >= settings.stable_before_settling_seconds * 1000
        ):
            self.state = "settling"
            self.settling_at = now
        elif self.state == "settling" and now - self.settling_at >= settings.post_trigger_seconds * 1000:
            self.state = "sparse"
        return dict(
            score=score,
            triggered=triggered,
            state=self.state,
            state_changed=before != self.state,
            reason="lighting_or_motion" if score > 0.5 else "motion",
        )
