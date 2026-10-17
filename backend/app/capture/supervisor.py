from __future__ import annotations

import asyncio
import io
import random
import re
import subprocess
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from PIL import Image
from sqlalchemy import func, select, update

from app.capture.frames import FrameWriter
from app.detection.motion import Detector
from app.errors import AppError
from app.repositories import schema as s
from app.repositories.db import new_id, one
from app.security.secrets import safe_path


def ffmpeg_args(config, source, password, output_dir, long_edge):
    credentials = ""
    if source.get("username"):
        credentials = quote(source["username"], safe="") + ":" + quote(password or "", safe="") + "@"
    url = f"rtsp://{credentials}{source['host']}:{source['port']}{source['path']}"
    select_filter = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,1)',showinfo,scale=w='min({long_edge},iw)':h='min({long_edge},ih)':force_original_aspect_ratio=decrease"
    return [
        config.ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "info",
        "-threads",
        "1",
        "-rtsp_transport",
        "tcp",
        "-timeout",
        "10000000",
        "-i",
        url,
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        select_filter,
        "-fps_mode",
        "passthrough",
        "-threads",
        "1",
        "-c:v",
        "mjpeg",
        "-q:v",
        "3",
        "-f",
        "image2pipe",
        "pipe:1",
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "copy",
        "-f",
        "hls",
        "-hls_time",
        "2",
        "-hls_list_size",
        "6",
        "-hls_flags",
        "delete_segments+omit_endlist+temp_file",
        "-hls_segment_filename",
        str(output_dir / "seg_%06d.ts"),
        str(output_dir / "live.m3u8"),
    ]


@dataclass
class Stream:
    camera: dict
    session_id: str
    process: asyncio.subprocess.Process
    frames: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=2))
    timestamps: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=4))
    tasks: list = field(default_factory=list)
    last_real: float = field(default_factory=time.monotonic)
    sequence: int = 0
    dropped: int = 0
    codec: str | None = None
    detector: Detector | None = None
    stopped: bool = False


class Supervisor:
    def __init__(self, workbench):
        self.w = workbench
        self.writer = FrameWriter(workbench)
        self.streams: dict[str, Stream] = {}
        self.reconnects = {}
        self.retry_at = {}
        self.camera_locks = {}
        self.tick_lock = asyncio.Lock()

    async def start(self, camera):
        camera_id = camera["id"]
        sid = new_id("stream")
        output = safe_path(self.w.config.data_dir, f"hls/{sid}")
        output.mkdir(parents=True)
        with self.w.db.read() as conn:
            cfg = self.w.settings_config(conn)
        password = (
            self.w.secrets.decrypt(camera["credentials_cipher"]) if camera["credentials_cipher"] else ""
        )
        args = ffmpeg_args(
            self.w.config, camera["config_json"]["source"], password, output, cfg.frame_max_long_edge
        )
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
                **options,
            )
        except OSError:
            raise AppError(503, "FFMPEG_UNAVAILABLE", "无法启动 FFmpeg", retryable=True) from None
        stream = Stream(camera=camera, session_id=sid, process=process, detector=Detector(self.w.now()))
        now = self.w.now()
        with self.w.db.write() as conn:
            conn.execute(
                s.stream_sessions.insert().values(
                    id=sid, camera_id=camera_id, started_at_ms=now, source_revision=camera["source_revision"]
                )
            )
            conn.execute(
                update(s.camera_runtime)
                .where(s.camera_runtime.c.camera_id == camera_id)
                .values(
                    capture_state="starting",
                    density_state="dense",
                    stop_reason=None,
                    stream_session_id=sid,
                    heartbeat_at_ms=now,
                )
            )
            conn.execute(
                s.detection_events.insert().values(
                    id=new_id("det"),
                    camera_id=camera_id,
                    trigger_at_ms=now,
                    reason="stream_start",
                    score="0",
                    roi_version=camera["source_revision"],
                    desired_start_ms=now,
                    desired_end_ms=now
                    + cfg.stable_before_settling_seconds * 1000
                    + cfg.post_trigger_seconds * 1000,
                )
            )
        stream.tasks = [
            asyncio.create_task(self.read_stderr(stream)),
            asyncio.create_task(self.read_jpegs(stream)),
            asyncio.create_task(self.consume(stream)),
        ]
        self.streams[camera_id] = stream
        return stream

    async def read_stderr(self, stream):
        try:
            while line := await stream.process.stderr.readline():
                text = line.decode("utf-8", "replace")
                if "Video: h264" in text:
                    stream.codec = "h264"
                elif "Video: hevc" in text:
                    stream.codec = "hevc"
                if "showinfo" in text:
                    match = re.search(r"\bn:\s*\d+.*?pts_time:\s*([-+\d.e]+)", text)
                    if match:
                        if stream.timestamps.full():
                            # 时间戳与图像配对失衡时终止该会话，不错误地分配 PTS。
                            stream.process.terminate()
                            return
                        stream.timestamps.put_nowait(match.group(1))
        except (OSError, ValueError, asyncio.CancelledError):
            return

    async def read_jpegs(self, stream):
        buffer = bytearray()
        try:
            while data := await stream.process.stdout.read(65536):
                buffer.extend(data)
                if len(buffer) > 8 * 1024 * 1024:
                    stream.process.terminate()
                    return
                while True:
                    begin = buffer.find(b"\xff\xd8")
                    end = buffer.find(b"\xff\xd9", max(begin, 0) + 2)
                    if begin < 0 or end < 0:
                        break
                    jpeg = bytes(buffer[begin : end + 2])
                    del buffer[: end + 2]
                    try:
                        pts = await asyncio.wait_for(stream.timestamps.get(), timeout=2)
                    except TimeoutError:
                        stream.process.terminate()
                        return
                    stream.sequence += 1
                    packet = (jpeg, self.w.now(), pts, stream.sequence)
                    if stream.frames.full():
                        stream.frames.get_nowait()
                        stream.dropped += 1
                    stream.frames.put_nowait(packet)
        except (OSError, asyncio.CancelledError):
            return

    async def consume(self, stream):
        try:
            while True:
                jpeg, captured_at, pts, sequence = await stream.frames.get()
                # 不允许处理积压的旧包后冒充刚采集。
                if self.w.now() - captured_at > 2000:
                    stream.dropped += 1
                    continue
                row = await asyncio.to_thread(
                    self.writer.save, stream.camera["id"], stream.session_id, jpeg, captured_at, pts, sequence
                )
                if row:
                    stream.last_real = time.monotonic()
                    await asyncio.to_thread(self.detect, stream, jpeg, captured_at)
        except asyncio.CancelledError:
            return
        except (OSError, AppError):
            if stream.process.returncode is None:
                stream.process.terminate()

    def detect(self, stream, jpeg, now):
        camera_id = stream.camera["id"]
        with self.w.db.read() as conn:
            cfg = self.w.settings_config(conn)
            camera = one(conn, s.cameras, camera_id)
        regions = camera["config_json"]["regions"]
        with Image.open(io.BytesIO(jpeg)) as image:
            expected = tuple(map(int, regions["source_aspect_ratio"].split(":")))
            changed = abs(image.width / image.height - expected[0] / expected[1]) > 0.02
            if changed or camera["config_json"].get("regions_state") == "needs_review":
                regions = {"include": [], "exclude": []}
            result = stream.detector.feed(image, now, regions, cfg)
        with self.w.db.write() as conn:
            if changed and camera["config_json"].get("regions_state") != "needs_review":
                conn.execute(
                    update(s.cameras)
                    .where(s.cameras.c.id == camera_id)
                    .values(
                        config_json=camera["config_json"] | {"regions_state": "needs_review"},
                        revision=s.cameras.c.revision + 1,
                        updated_at_ms=now,
                    )
                )
            if result["triggered"]:
                latest = (
                    conn.execute(
                        select(s.detection_events)
                        .where(s.detection_events.c.camera_id == camera_id)
                        .order_by(s.detection_events.c.trigger_at_ms.desc())
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                end = now + (cfg.stable_before_settling_seconds + cfg.post_trigger_seconds) * 1000
                if latest and latest["desired_end_ms"] >= now - cfg.pre_trigger_seconds * 1000:
                    conn.execute(
                        update(s.detection_events)
                        .where(s.detection_events.c.id == latest["id"])
                        .values(desired_end_ms=end, score=str(result["score"]))
                    )
                else:
                    conn.execute(
                        s.detection_events.insert().values(
                            id=new_id("det"),
                            camera_id=camera_id,
                            trigger_at_ms=now,
                            reason=result["reason"],
                            score=str(result["score"]),
                            roi_version=camera["source_revision"],
                            desired_start_ms=now - cfg.pre_trigger_seconds * 1000,
                            desired_end_ms=end,
                        )
                    )
            runtime = (
                conn.execute(select(s.camera_runtime).where(s.camera_runtime.c.camera_id == camera_id))
                .mappings()
                .one()
            )
            analysis_error = None
            if result["triggered"] and result["state_changed"] and cfg.vision_provider_id:
                from app.services.analysis import Analysis

                boundary = now // 60000 * 60000
                padded_pre = ((cfg.pre_trigger_seconds + 59) // 60) * 60000
                try:
                    Analysis(self.w).backfill(
                        conn, camera_id, boundary - padded_pre + cfg.pre_trigger_seconds * 1000, boundary
                    )
                except AppError as error:
                    analysis_error = error.code
            diagnostics = runtime["diagnostics_json"] | {
                "analysis_error_code": analysis_error,
                "codec": stream.codec,
                "reconnect_count": self.reconnects.get(camera_id, 0),
                "dropped_frame_count": stream.dropped,
                "receive_sequence": stream.sequence,
                "last_source_pts": self.writer.last_pts.get(stream.session_id),
                "roi_needs_review": changed,
            }
            conn.execute(
                update(s.camera_runtime)
                .where(s.camera_runtime.c.camera_id == camera_id)
                .values(density_state=result["state"], diagnostics_json=diagnostics, heartbeat_at_ms=now)
            )

    async def stop(self, camera_id, reason):
        stream = self.streams.pop(camera_id, None)
        if not stream:
            return
        stream.stopped = True
        if stream.process.returncode is None:
            stream.process.terminate()
            try:
                await asyncio.wait_for(stream.process.wait(), timeout=5)
            except TimeoutError:
                stream.process.kill()
                await stream.process.wait()
        for task in stream.tasks:
            task.cancel()
        await asyncio.gather(*stream.tasks, return_exceptions=True)
        with self.w.db.write() as conn:
            conn.execute(
                update(s.stream_sessions)
                .where(s.stream_sessions.c.id == stream.session_id)
                .values(
                    ended_at_ms=func.max(self.w.now(), s.stream_sessions.c.started_at_ms),
                    exit_reason=reason,
                    codec=stream.codec,
                )
            )
            conn.execute(
                update(s.camera_runtime)
                .where(s.camera_runtime.c.camera_id == camera_id)
                .values(stream_session_id=None, density_state=None)
            )

    async def tick(self):
        async with self.tick_lock:
            await self._tick()

    async def _tick(self):
        now = self.w.now()
        with self.w.db.read() as conn:
            cfg = self.w.settings_config(conn)
            cameras = [dict(row) for row in conn.execute(select(s.cameras)).mappings()]
            storage = (
                conn.execute(select(s.system_state).where(s.system_state.c.id == "storage"))
                .mappings()
                .first()
            )
            paused = storage and storage["value_json"].get("paused", False)
        for camera in cameras:
            camera_id = camera["id"]
            lock = self.camera_locks.setdefault(camera_id, asyncio.Lock())
            if lock.locked():
                continue
            with self.w.db.read() as conn:
                runtime = dict(
                    conn.execute(select(s.camera_runtime).where(s.camera_runtime.c.camera_id == camera_id))
                    .mappings()
                    .one()
                )
                planned = bool(
                    self.w.ledger.planned(conn, camera_id, now // 1000 * 1000, now // 1000 * 1000 + 1000)
                )
            reason = (
                "deleted"
                if camera["deleted_at_ms"]
                else "disabled"
                if not camera["enabled"]
                else "resource_pressure"
                if paused
                else "manual_override"
                if runtime["override_mode"] == "force_off" and (runtime["override_until_ms"] or 0) > now
                else "outside_schedule"
                if not planned
                else None
            )
            stream = self.streams.get(camera_id)
            bad = stream and (
                stream.process.returncode is not None
                or time.monotonic() - stream.last_real > cfg.source_stale_seconds
                or camera["source_revision"] != stream.camera["source_revision"]
            )
            if stream and (reason or bad):
                async with lock:
                    await self.stop(camera_id, reason or "source_stale")
                if not reason:
                    count = self.reconnects.get(camera_id, 0) + 1
                    self.reconnects[camera_id] = count
                    self.retry_at[camera_id] = time.monotonic() + [5, 10, 20, 30, 60][
                        min(count - 1, 4)
                    ] * random.uniform(1, 1.1)
                stream = None
            if reason:
                with self.w.db.write() as conn:
                    conn.execute(
                        update(s.camera_runtime)
                        .where(s.camera_runtime.c.camera_id == camera_id)
                        .values(
                            capture_state="resource_paused" if paused and camera["enabled"] else "stopped",
                            stop_reason=reason,
                            density_state=None,
                            applied_revision=runtime["control_revision"],
                            config_applied_revision=camera["revision"],
                            heartbeat_at_ms=now,
                        )
                    )
                continue
            if not stream and time.monotonic() >= self.retry_at.get(camera_id, 0) and not lock.locked():
                try:
                    async with lock:
                        stream = await self.start(camera)
                except AppError:
                    self.retry_at[camera_id] = time.monotonic() + 60
            with self.w.db.write() as conn:
                conn.execute(
                    update(s.camera_runtime)
                    .where(s.camera_runtime.c.camera_id == camera_id)
                    .values(
                        applied_revision=runtime["control_revision"],
                        config_applied_revision=camera["revision"],
                        heartbeat_at_ms=now,
                        **(
                            {"capture_state": "offline", "last_error_code": "SOURCE_STALE"}
                            if stream is None
                            else {}
                        ),
                    )
                )
                slot = now // 1000 * 1000 - 1000
                self.writer.minute_slot(
                    conn,
                    camera_id,
                    slot,
                    camera["revision"],
                    reason="resource_pressure" if paused else "source_offline",
                )

    async def test(self, camera_id):
        lock = self.camera_locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            with self.w.db.read() as conn:
                camera = one(conn, s.cameras, camera_id)
                (
                    conn.execute(select(s.camera_runtime).where(s.camera_runtime.c.camera_id == camera_id))
                    .mappings()
                    .one()
                )
                latest = (
                    conn.execute(
                        select(s.frames)
                        .where(s.frames.c.camera_id == camera_id, s.frames.c.state == "available")
                        .order_by(s.frames.c.captured_at_ms.desc())
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
            if camera_id in self.streams:
                if latest and self.w.now() - latest["captured_at_ms"] < 10000:
                    return {
                        "evidence_id": latest["id"],
                        "codec": self.streams[camera_id].codec,
                        "width": latest["width"],
                        "height": latest["height"],
                    }
                raise AppError(503, "PREVIEW_UNAVAILABLE", "现有管道未取得新帧", retryable=True)
            # 离线测试单独复用同一 FFmpeg 参数，只处理首张真实图，不写分钟活动。
            sid = new_id("stream")
            directory = safe_path(self.w.config.data_dir, f"hls/{sid}")
            directory.mkdir()
            args = ffmpeg_args(
                self.w.config,
                camera["config_json"]["source"],
                self.w.secrets.decrypt(camera["credentials_cipher"]) if camera["credentials_cipher"] else "",
                directory,
                1280,
            )
            options = (
                {"creationflags": subprocess.CREATE_NO_WINDOW}
                if hasattr(subprocess, "CREATE_NO_WINDOW")
                else {}
            )
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options
            )
            stream = Stream(camera, sid, process)
            stream.tasks = [
                asyncio.create_task(self.read_stderr(stream)),
                asyncio.create_task(self.read_jpegs(stream)),
            ]
            with self.w.db.write() as conn:
                conn.execute(
                    s.stream_sessions.insert().values(
                        id=sid,
                        camera_id=camera_id,
                        started_at_ms=self.w.now(),
                        source_revision=camera["source_revision"],
                    )
                )
            try:
                jpeg, captured, pts, sequence = await asyncio.wait_for(stream.frames.get(), timeout=15)
                row = await asyncio.to_thread(
                    self.writer.save, camera_id, sid, jpeg, captured, pts, sequence, is_test=True
                )
                if row is None:
                    raise AppError(503, "PREVIEW_UNAVAILABLE", "测试未取得独立新秒槽")
                return {
                    "evidence_id": row["id"],
                    "codec": stream.codec,
                    "width": row["width"],
                    "height": row["height"],
                }
            except TimeoutError:
                raise AppError(503, "CAMERA_TEST_TIMEOUT", "15 秒内未取得测试图") from None
            finally:
                if process.returncode is None:
                    process.terminate()
                await process.wait()
                for task in stream.tasks:
                    task.cancel()
                await asyncio.gather(*stream.tasks, return_exceptions=True)
                with self.w.db.write() as conn:
                    conn.execute(
                        update(s.stream_sessions)
                        .where(s.stream_sessions.c.id == sid)
                        .values(
                            ended_at_ms=func.max(self.w.now(), s.stream_sessions.c.started_at_ms),
                            exit_reason="test_complete",
                            codec=stream.codec,
                        )
                    )

    async def close(self):
        for camera_id in list(self.streams):
            await self.stop(camera_id, "worker_shutdown")
