from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest
from sqlalchemy import select, update

from app.capture.recovery import CaptureRecovery
from app.capture.supervisor import Supervisor, classify_ffmpeg_error
from app.repositories import schema as s
from app.repositories.db import one
from tests.conftest import make_camera
from tests.rtsp_source import SyntheticRTSP


async def test_B02_real_ffmpeg_rtsp_tcp_one_connection_jpeg_hls(w, clock):
    camera = make_camera(w)
    base = clock.value
    started = time.monotonic()

    async def ticker():
        while True:
            clock.value = base + int((time.monotonic() - started) * 1000)
            await asyncio.sleep(0.025)

    ticking = asyncio.create_task(ticker())
    supervisor = Supervisor(w)
    try:
        async with SyntheticRTSP(w.config.ffmpeg, seconds=10) as source:
            w.config = dataclasses.replace(w.config, rtsp_allowed_ports=(source.port,))
            with w.db.write() as conn:
                row = one(conn, s.cameras, camera)
                cfg = row["config_json"] | {
                    "source": row["config_json"]["source"] | {"port": source.port, "username": ""}
                }
                conn.execute(
                    update(s.cameras)
                    .where(s.cameras.c.id == camera)
                    .values(config_json=cfg, credentials_cipher=None)
                )
                row = one(conn, s.cameras, camera)
            await asyncio.gather(supervisor.tick(), supervisor.tick())
            stream = supervisor.streams[camera]
            messages = []
            original_readline = stream.process.stderr.readline

            async def record_line():
                line = await original_readline()
                messages.append(line)
                return line

            stream.process.stderr.readline = record_line
            for _ in range(100):
                with w.db.read() as conn:
                    frames = list(
                        conn.execute(select(s.frames).where(s.frames.c.camera_id == camera)).mappings()
                    )
                if len(frames) >= 4 and (w.config.data_dir / f"hls/{stream.session_id}/live.m3u8").exists():
                    break
                await asyncio.sleep(0.1)
            assert len(frames) >= 4, (
                f"实际 FFmpeg 未收到足够帧：{len(frames)}：{b''.join(messages).decode(errors='replace')}"
            )
            assert source.connections == 1
            assert len({f["slot_at_ms"] for f in frames}) == len(frames)
            assert len({f["source_pts"] for f in frames}) == len(frames)
            assert all((w.config.data_dir / f["relpath"]).is_file() for f in frames)
            assert (w.config.data_dir / f"hls/{stream.session_id}/live.m3u8").is_file()
            assert stream.codec == "h264"
    finally:
        await supervisor.close()
        ticking.cancel()
        await asyncio.gather(ticking, return_exceptions=True)


def test_B01_B02_recovery_keeps_gaps_without_fabricating_images(w, clock):
    camera = make_camera(w)
    start = w.now()
    clock.advance(10 * 60000)
    CaptureRecovery(w).finalize_before(recovered=True)
    with w.db.read() as conn:
        assert not conn.execute(select(s.frames)).first()
        minutes = list(conn.execute(select(s.minute_records)).mappings())
        assert len(minutes) == 10
        result = w.ledger.calculate(
            conn, start, start + 10 * 60000, {"camera_ids": [camera], "subject_id": None}
        )
        assert result["coverage"]["gap_ms"] == 10 * 60000
        assert result["coverage"]["captured_ms"] == 0
        captures = list(conn.execute(select(s.minute_capture)).mappings())
        assert all(r["captured_bitmap"] == 0 and r["planned_bitmap"] == (1 << 60) - 1 for r in captures)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("method DESCRIBE failed: 401Unauthorized", "RTSP_AUTH_FAILED"),
        ("method DESCRIBE failed: 404 Not Found", "RTSP_PATH_NOT_FOUND"),
        ("Connection refused", "RTSP_CONNECT_FAILED"),
        ("Could not find codec parameters", "RTSP_NO_VIDEO"),
    ],
)
def test_ffmpeg_error_classification_uses_safe_messages(line, expected):
    result = classify_ffmpeg_error(line)
    assert result is not None
    assert result[0] == expected
    assert "rtsp://" not in result[1]
