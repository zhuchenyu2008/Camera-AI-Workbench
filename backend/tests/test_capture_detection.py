from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.capture import CameraConfig, CaptureFrame, FakeCameraSource, FrameWriter, OpenCVCameraSource
from app.detection import ChangeEventStateMachine, DetectorConfig, EventState, SignalDetector
from app.storage import FrameStorage, StorageError


def image(value: int, *, size: tuple[int, int] = (32, 24)) -> np.ndarray:
    return np.full((size[1], size[0], 3), value, dtype=np.uint8)


def test_fake_source_imports_without_camera_and_reports_health() -> None:
    source = FakeCameraSource(CameraConfig("cam-1"), [image(1)], timestamps_ms=[1000])
    source.start()
    frame = source.read_frame()
    assert frame is not None and frame.captured_at_ms == 1000
    assert source.health().connected is True
    assert source.health().frame_count == 1
    source.stop()
    assert source.health().state == "stopped"


def test_frame_writer_writes_atomic_jpeg_and_hashes(tmp_path: Path) -> None:
    storage = FrameStorage(tmp_path)
    writer = FrameWriter(storage, jpeg_quality=90, max_width=16, max_height=16)
    result = writer.write(CaptureFrame("cam-1", 1_700_000_000_000, image(128, size=(32, 24))))
    assert result.path.exists()
    assert result.path.read_bytes()[:2] == b"\xff\xd8"
    assert len(result.sha256) == 64
    assert len(result.perceptual_hash) == 16
    assert result.width <= 16 and result.height <= 16
    assert not list(tmp_path.rglob("*.part"))


def test_storage_rejects_path_traversal(tmp_path: Path) -> None:
    storage = FrameStorage(tmp_path)
    with pytest.raises(StorageError):
        storage.frame_path("../secret", 1, "0" * 64)
    with pytest.raises(StorageError):
        storage.frame_path("cam", 1, "not-a-sha")
    with pytest.raises(StorageError):
        storage.delete_frame(tmp_path / "outside.jpg")


def test_detector_emits_duplicate_diff_and_gap_signals() -> None:
    detector = SignalDetector(DetectorConfig(smoothing_window=1, expected_interval_ms=1000, change_threshold=0.05))
    first = detector.process(CaptureFrame("cam", 1000, image(0)), sha256="a" * 64)
    second = detector.process(CaptureFrame("cam", 2000, image(0)), sha256="a" * 64)
    changed = detector.process(CaptureFrame("cam", 4000, image(255)), sha256="b" * 64)
    assert first.duplicate is False
    assert second.duplicate is True
    assert changed.frame_gap is True and changed.score > 0


def test_event_state_machine_requires_consecutive_changes_and_keeps_prebuffer() -> None:
    config = DetectorConfig(
        smoothing_window=1, change_threshold=0.005, duplicate_pixel_threshold=0.0001,
        hash_distance_threshold=0, trigger_consecutive=2, stable_consecutive=2,
        prebuffer_seconds=30, post_roll_seconds=2, max_event_seconds=20, expected_interval_ms=1000,
    )
    machine = ChangeEventStateMachine(config, event_id_factory=lambda camera, timestamp: "evt-1")
    detector = SignalDetector(config)
    frames = [
        CaptureFrame("cam", 0, image(0)), CaptureFrame("cam", 1000, image(0)),
        CaptureFrame("cam", 2000, image(255)), CaptureFrame("cam", 3000, image(0)),
    ]
    for frame in frames:
        assert machine.ingest(frame, detector.process(frame)) is None
    assert machine.state is EventState.EVENT_PREBUFFER_FROZEN
    assert machine.active_event is not None
    assert len(machine.active_event.prebuffer) == 4
    transition_frame = CaptureFrame("cam", 4000, image(0))
    assert machine.ingest(transition_frame, detector.process(transition_frame)) is None
    assert machine.state is EventState.HIGH_PRECISION_CAPTURE
    closed = None
    for timestamp in (5000, 6000):
        frame = CaptureFrame("cam", timestamp, image(0))
        closed = machine.ingest(frame, detector.process(frame))
    assert closed is not None
    assert closed.event_id == "evt-1"
    assert closed.closed_reason == "stable_post_roll"
    assert machine.state is EventState.NORMAL


def test_opencv_adapter_is_lazy_and_redacts_credentials() -> None:
    class FakeCV2:
        def VideoCapture(self, url: str):
            assert url == "rtsp://user:secret@example.test/live"
            return type("Capture", (), {"isOpened": lambda self: False, "release": lambda self: None})()

    source = OpenCVCameraSource(CameraConfig("cam", "rtsp://user:secret@example.test/live"), cv2_module=FakeCV2())
    with pytest.raises(RuntimeError, match="example.test") as error:
        source.start()
    assert "secret" not in str(error.value)
    assert "user" not in str(error.value)
    assert source.health().safe_source == "rtsp://example.test/live"
