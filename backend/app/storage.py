"""受控的本地帧文件存储。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class StorageError(ValueError):
    """存储路径或输入不符合安全约束。"""


def safe_camera_id(camera_id: str) -> str:
    """校验可进入文件系统路径的摄像头 ID。"""
    if not isinstance(camera_id, str) or not _CAMERA_ID_RE.fullmatch(camera_id):
        raise StorageError("非法 camera_id")
    return camera_id


@dataclass(frozen=True, slots=True)
class FramePath:
    camera_id: str
    captured_at_ms: int
    sha256: str
    path: Path


class FrameStorage:
    """在 data_dir/frames 下保存 JPEG，不接受任意用户路径。"""

    def __init__(self, data_dir: str | os.PathLike[str]):
        self.root = Path(data_dir).expanduser().resolve()
        self.frames_root = (self.root / "frames").resolve()
        self.frames_root.mkdir(parents=True, exist_ok=True)

    def frame_path(self, camera_id: str, captured_at_ms: int, sha256: str) -> Path:
        safe_camera_id(camera_id)
        if not isinstance(captured_at_ms, int) or captured_at_ms < 0:
            raise StorageError("非法 captured_at_ms")
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise StorageError("非法 sha256")
        # 按摄像头和摘要分桶，避免单目录过大并保持摄像头隔离。
        path = self.frames_root / camera_id / sha256[:2] / f"{sha256}.jpg"
        return self._checked(path)

    def thumbnail_path(self, camera_id: str, sha256: str) -> Path:
        safe_camera_id(camera_id)
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise StorageError("非法 sha256")
        root = (self.root / "thumbs").resolve()
        path = root / camera_id / sha256[:2] / f"{sha256}.jpg"
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise StorageError("缩略图路径越界") from exc
        return path

    def path_for(self, camera_id: str, captured_at_ms: int, sha256: str) -> FramePath:
        return FramePath(camera_id, captured_at_ms, sha256, self.frame_path(camera_id, captured_at_ms, sha256))

    def _checked(self, path: Path) -> Path:
        try:
            path.relative_to(self.frames_root)
        except ValueError as exc:
            raise StorageError("路径越界") from exc
        return path

    def delete_frame(self, path: str | os.PathLike[str]) -> None:
        """只允许删除 frames 根目录下的 JPEG。"""
        candidate = Path(path).expanduser().resolve()
        self._checked(candidate)
        if candidate.suffix.lower() not in {".jpg", ".jpeg"}:
            raise StorageError("只允许删除 JPEG 帧")
        candidate.unlink(missing_ok=True)

    def iter_frame_files(self, camera_id: str | None = None) -> Iterator[Path]:
        root = self.frames_root if camera_id is None else self.frames_root / safe_camera_id(camera_id)
        if not root.exists():
            return
        yield from root.rglob("*.jpg")
