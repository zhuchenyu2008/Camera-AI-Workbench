from __future__ import annotations

from pathlib import Path

from app.storage import FrameStorage


def test_frame_storage_uses_controlled_camera_bucket(tmp_path: Path) -> None:
    storage = FrameStorage(tmp_path)
    path = storage.frame_path("camera_01", 0, "a" * 64)
    assert path.parent.parent == tmp_path / "frames" / "camera_01"
