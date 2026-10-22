from __future__ import annotations

import hashlib
import math
import os
import secrets
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ..storage import FrameStorage
from .sources import CaptureFrame


@dataclass(frozen=True, slots=True)
class FrameWriteResult:
    frame_id: str
    camera_id: str
    captured_at_ms: int
    path: Path
    sha256: str
    perceptual_hash: str
    byte_size: int
    width: int
    height: int
    thumbnail_path: Path | None = None


class FrameWriter:
    """JPEG 原子落盘：先写同目录隐藏 .part，fsync 后 os.replace。"""

    def __init__(self, storage: FrameStorage, *, jpeg_quality: int = 85,
                 max_width: int | None = 1920, max_height: int | None = 1080):
        if not 1 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality 必须在 1 到 100 之间")
        self.storage = storage
        self.jpeg_quality, self.max_width, self.max_height = jpeg_quality, max_width, max_height

    def write(self, captured: CaptureFrame) -> FrameWriteResult:
        image = _resize_to_fit(_to_pil_image(captured.frame), self.max_width, self.max_height).convert("RGB")
        encoded = BytesIO()
        image.save(encoded, format="JPEG", quality=self.jpeg_quality, optimize=True)
        payload = encoded.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        target = self.storage.frame_path(captured.camera_id, captured.captured_at_ms, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(target, payload)
        thumbnail = image.copy()
        thumbnail.thumbnail((320, 180), Image.Resampling.BILINEAR)
        thumb_buffer = BytesIO()
        thumbnail.save(thumb_buffer, format="JPEG", quality=75, optimize=True)
        thumbnail_path = self.storage.thumbnail_path(captured.camera_id, digest)
        thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(thumbnail_path, thumb_buffer.getvalue())
        return FrameWriteResult(
            frame_id=secrets.token_urlsafe(16), camera_id=captured.camera_id,
            captured_at_ms=captured.captured_at_ms, path=target, sha256=digest,
            perceptual_hash=perceptual_hash(image), byte_size=len(payload),
            width=image.width, height=image.height, thumbnail_path=thumbnail_path,
        )

    @staticmethod
    def _write_atomic(target: Path, payload: bytes) -> None:
        temp_path = target.with_name(f".{target.name}.{secrets.token_hex(8)}.part")
        try:
            with temp_path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists():
                # 内容摘要相同则幂等保留原文件。
                temp_path.unlink(missing_ok=True)
            else:
                os.replace(temp_path, target)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise


def _to_pil_image(value: Any) -> Image.Image:
    if isinstance(value, Image.Image):
        return value.copy()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return Image.open(BytesIO(bytes(value))).copy()
    array = np.asarray(value)
    if array.ndim == 2:
        return Image.fromarray(array.astype(np.uint8), mode="L")
    if array.ndim == 3 and array.shape[2] in (3, 4):
        return Image.fromarray(array.astype(np.uint8), mode="RGB" if array.shape[2] == 3 else "RGBA")
    raise TypeError("frame 必须是 Pillow Image、JPEG bytes 或 HxW[×C] 数组")


def _resize_to_fit(image: Image.Image, max_width: int | None, max_height: int | None) -> Image.Image:
    if not max_width and not max_height:
        return image
    width, height = image.size
    ratios = [1.0]
    if max_width:
        ratios.append(max_width / width)
    if max_height:
        ratios.append(max_height / height)
    ratio = min(ratios)
    if ratio >= 1:
        return image
    return image.resize((max(1, round(width * ratio)), max(1, round(height * ratio))), Image.Resampling.LANCZOS)


def perceptual_hash(image: Image.Image | Any, *, hash_size: int = 8) -> str:
    """DCT perceptual hash，输出 64-bit 十六进制指纹。"""
    if hash_size <= 0:
        raise ValueError("hash_size 必须大于 0")
    sample_size = hash_size * 4
    pil = _to_pil_image(image).convert("L").resize((sample_size, sample_size), Image.Resampling.BILINEAR)
    pixels = np.asarray(pil, dtype=np.float64)
    axis = np.arange(sample_size, dtype=np.float64)
    frequencies = np.arange(hash_size, dtype=np.float64)[:, None]
    basis = np.cos((np.pi / (2 * sample_size)) * (2 * axis + 1) * frequencies)
    basis[0] *= math.sqrt(1 / sample_size)
    basis[1:] *= math.sqrt(2 / sample_size)
    low = basis @ pixels @ basis.T
    low = low[:hash_size, :hash_size]
    median = float(np.median(low.reshape(-1)[1:]))
    bits = low >= median
    value = 0
    for bit in bits.reshape(-1):
        value = (value << 1) | int(bool(bit))
    return f"{value:0{(hash_size * hash_size + 3) // 4}x}"
