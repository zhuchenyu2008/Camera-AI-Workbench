from __future__ import annotations

import os
import threading
import time
from typing import Any

from app.capture import CameraConfig, FFmpegCameraSource
from app.capture.sources import _read_jpeg_from_stream


def test_read_jpeg_from_pipe_waits_for_complete_frame() -> None:
    read_fd, write_fd = os.pipe()
    payload = b"prefix\xff\xd8jpeg-frame\xff\xd9suffix"

    def write_payload() -> None:
        try:
            time.sleep(0.05)
            os.write(write_fd, payload)
        finally:
            os.close(write_fd)

    writer = threading.Thread(target=write_payload, daemon=True)
    writer.start()
    try:
        with os.fdopen(read_fd, "rb", closefd=True) as stream:
            assert _read_jpeg_from_stream(stream, 1.0) == b"\xff\xd8jpeg-frame\xff\xd9"
    finally:
        writer.join(timeout=1.0)


def test_ffmpeg_command_does_not_require_rw_timeout_option() -> None:
    commands: list[list[str]] = []

    class FakeProcess:
        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float) -> None:
            return None

    def fake_popen(command: list[str], **_: Any) -> FakeProcess:
        commands.append(command)
        return FakeProcess()

    source = FFmpegCameraSource(
        CameraConfig("cam-1", "rtsp://camera.example/live"),
        popen_factory=fake_popen,
    )
    source.start()
    source.stop()

    assert commands
    assert "-rw_timeout" not in commands[0]
