from __future__ import annotations

import asyncio
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

import numpy as np


def utc_now_ms() -> int:
    return int(time.time() * 1000)


def redact_url(value: str) -> str:
    """返回不含 userinfo/query 的来源地址，供健康状态和日志使用。"""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        netloc = f"{host}:{parsed.port}" if parsed.port else host
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except (TypeError, ValueError):
        return "<redacted>"


def safe_error_message(message: str) -> str:
    message = re.sub(r"(?i)(rtsp://)([^/@\s]+)@", r"\1<redacted>@", str(message))
    message = re.sub(r"(?i)(password|passwd|api[_-]?key|token)\s*[=:]\s*[^\s,;]+", r"\1=<redacted>", message)
    return message[:500]


@dataclass(frozen=True, slots=True)
class CameraConfig:
    camera_id: str
    url: str | None = None
    interval_ms: int = 1000
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 5.0
    reconnect_initial_s: float = 1.0
    reconnect_max_s: float = 30.0
    jpeg_quality: int = 85
    max_width: int | None = 1920
    max_height: int | None = 1080
    source_options: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.camera_id):
            raise ValueError("camera_id 只能包含字母、数字、下划线和连字符")
        if self.interval_ms <= 0 or self.connect_timeout_s <= 0 or self.read_timeout_s <= 0:
            raise ValueError("采集间隔和超时必须大于 0")
        if self.reconnect_initial_s <= 0 or self.reconnect_max_s < self.reconnect_initial_s:
            raise ValueError("重连退避配置无效")
        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("jpeg_quality 必须在 1 到 100 之间")
        if self.max_width is not None and self.max_width <= 0:
            raise ValueError("max_width 必须大于 0")
        if self.max_height is not None and self.max_height <= 0:
            raise ValueError("max_height 必须大于 0")
        if self.url is not None:
            parsed = urlsplit(self.url)
            if parsed.scheme.lower() not in {"rtsp", "rtsps"} or not parsed.hostname:
                raise ValueError("url 必须是带主机名的 RTSP URL")

    @property
    def safe_url(self) -> str | None:
        return redact_url(self.url) if self.url else None


@dataclass(frozen=True, slots=True)
class CaptureFrame:
    camera_id: str
    captured_at_ms: int
    frame: Any
    sequence: int | None = None
    source_timestamp_ms: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.captured_at_ms < 0:
            raise ValueError("captured_at_ms 必须是非负毫秒时间戳")
        if self.source_timestamp_ms is not None and self.source_timestamp_ms < 0:
            raise ValueError("source_timestamp_ms 必须是非负毫秒时间戳")


@dataclass(slots=True)
class CameraHealth:
    camera_id: str
    state: str = "stopped"
    connected: bool = False
    last_frame_at_ms: int | None = None
    last_success_at_ms: int | None = None
    last_error_at_ms: int | None = None
    consecutive_failures: int = 0
    reconnect_count: int = 0
    frame_count: int = 0
    gap_started_at_ms: int | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    safe_source: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id, "state": self.state, "connected": self.connected,
            "last_frame_at_ms": self.last_frame_at_ms, "last_success_at_ms": self.last_success_at_ms,
            "last_error_at_ms": self.last_error_at_ms, "consecutive_failures": self.consecutive_failures,
            "reconnect_count": self.reconnect_count, "frame_count": self.frame_count,
            "gap_started_at_ms": self.gap_started_at_ms, "last_error_code": self.last_error_code,
            "last_error_message": self.last_error_message, "safe_source": self.safe_source,
        }


class CameraSource(ABC):
    """可替换的同步摄像头来源接口。"""

    def __init__(self, config: CameraConfig):
        self.config = config
        self._health = CameraHealth(camera_id=config.camera_id, safe_source=config.safe_url)
        self._started = False

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def read_frame(self, timeout_s: float | None = None) -> CaptureFrame | None: ...

    @abstractmethod
    def stop(self) -> None: ...

    def health(self) -> CameraHealth:
        current = self._health
        return CameraHealth(
            camera_id=current.camera_id, state=current.state, connected=current.connected,
            last_frame_at_ms=current.last_frame_at_ms, last_success_at_ms=current.last_success_at_ms,
            last_error_at_ms=current.last_error_at_ms, consecutive_failures=current.consecutive_failures,
            reconnect_count=current.reconnect_count, frame_count=current.frame_count,
            gap_started_at_ms=current.gap_started_at_ms, last_error_code=current.last_error_code,
            last_error_message=current.last_error_message, safe_source=current.safe_source,
        )

    def _mark_started(self) -> None:
        self._started = True
        self._health.state = "connected"
        self._health.connected = True
        self._health.last_error_code = None
        self._health.last_error_message = None

    def _mark_frame(self, captured_at_ms: int) -> None:
        self._health.state = "connected"
        self._health.connected = True
        self._health.last_frame_at_ms = captured_at_ms
        self._health.last_success_at_ms = captured_at_ms
        self._health.consecutive_failures = 0
        self._health.gap_started_at_ms = None
        self._health.frame_count += 1

    def _mark_failure(self, code: str, message: str, now_ms: int | None = None) -> None:
        now_ms = now_ms if now_ms is not None else utc_now_ms()
        self._health.state = "degraded" if self._started else "error"
        self._health.connected = False
        self._health.last_error_at_ms = now_ms
        self._health.consecutive_failures += 1
        self._health.gap_started_at_ms = self._health.gap_started_at_ms or now_ms
        self._health.last_error_code = code
        self._health.last_error_message = safe_error_message(message)

    def _mark_stopped(self) -> None:
        self._started = False
        self._health.state = "stopped"
        self._health.connected = False


class FakeCameraSource(CameraSource):
    """测试来源：按序返回数组/None/异常，不需要摄像头。"""

    def __init__(self, config: CameraConfig, frames: Iterable[Any] = (), *, timestamps_ms: Iterable[int] | None = None, loop: bool = False):
        super().__init__(config)
        self._frames = list(frames)
        self._timestamps = list(timestamps_ms) if timestamps_ms is not None else []
        if self._timestamps and len(self._timestamps) != len(self._frames):
            raise ValueError("timestamps_ms 与 frames 数量必须一致")
        self._loop, self._index, self._sequence = loop, 0, 0

    def start(self) -> None:
        self._mark_started()

    def read_frame(self, timeout_s: float | None = None) -> CaptureFrame | None:
        if not self._started:
            raise RuntimeError("camera source 尚未 start")
        if not self._frames:
            self._mark_failure("empty_source", "fake source 没有可读取帧")
            return None
        if self._index >= len(self._frames):
            if not self._loop:
                self._mark_failure("eof", "fake source 已结束")
                return None
            self._index = 0
        item, index = self._frames[self._index], self._index
        timestamp = self._timestamps[index] if self._timestamps else utc_now_ms()
        self._index += 1
        if isinstance(item, BaseException):
            self._mark_failure("fake_error", str(item))
            raise item
        if item is None:
            self._mark_failure("read_timeout", "fake source 未返回帧")
            return None
        self._sequence += 1
        result = CaptureFrame(self.config.camera_id, timestamp, item, self._sequence)
        self._mark_frame(timestamp)
        return result

    def stop(self) -> None:
        self._mark_stopped()


class OpenCVCameraSource(CameraSource):
    """OpenCV RTSP 适配器，import 时不要求安装 cv2。"""

    def __init__(self, config: CameraConfig, cv2_module: Any | None = None):
        super().__init__(config)
        self._cv2, self._capture, self._sequence = cv2_module, None, 0

    def _load_cv2(self) -> Any:
        if self._cv2 is not None:
            return self._cv2
        try:
            import cv2  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("OpenCV 未安装；请安装可选依赖 opencv-python-headless") from exc
        self._cv2 = cv2
        return cv2

    def start(self) -> None:
        if not self.config.url:
            raise ValueError("OpenCV RTSP source 需要 url")
        cv2 = self._load_cv2()
        try:
            capture = cv2.VideoCapture(self.config.url)
            if hasattr(capture, "set") and hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
                capture.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(self.config.connect_timeout_s * 1000))
            if not capture or not capture.isOpened():
                if capture and hasattr(capture, "release"):
                    capture.release()
                self._mark_failure("connect_failed", f"无法连接 RTSP 源 {self.config.safe_url}")
                raise RuntimeError(f"无法连接 RTSP 源 {self.config.safe_url}")
            self._capture = capture
            self._mark_started()
        except RuntimeError:
            raise
        except Exception as exc:
            self._mark_failure("connect_failed", f"RTSP 连接失败: {type(exc).__name__}")
            raise RuntimeError(f"RTSP 连接失败: {self.config.safe_url}") from exc

    def read_frame(self, timeout_s: float | None = None) -> CaptureFrame | None:
        if self._capture is None or not self._started:
            raise RuntimeError("camera source 尚未 start")
        try:
            ok, image = self._capture.read()
        except Exception as exc:
            self._mark_failure("read_error", f"RTSP 读取失败: {type(exc).__name__}")
            raise RuntimeError(f"RTSP 读取失败: {self.config.safe_url}") from exc
        if not ok or image is None:
            self._mark_failure("read_timeout", "RTSP 暂时未返回帧")
            return None
        self._sequence += 1
        captured_at = utc_now_ms()
        result = CaptureFrame(self.config.camera_id, captured_at, image, self._sequence)
        self._mark_frame(captured_at)
        return result

    def stop(self) -> None:
        if self._capture is not None:
            try:
                self._capture.release()
            finally:
                self._capture = None
        self._mark_stopped()


RTSPCameraSource = OpenCVCameraSource


class FFmpegCameraSource(CameraSource):
    """FFmpeg image2pipe 适配器；stderr 丢弃，避免凭据进入日志。"""

    def __init__(self, config: CameraConfig, *, ffmpeg_bin: str = "ffmpeg", popen_factory: Any | None = None):
        super().__init__(config)
        self.ffmpeg_bin, self._popen_factory = ffmpeg_bin, popen_factory
        self._process, self._sequence = None, 0

    def start(self) -> None:
        if not self.config.url:
            raise ValueError("FFmpeg RTSP source 需要 url")
        import subprocess
        # Keep the command compatible with the FFmpeg builds commonly shipped
        # on Windows.  ``-rw_timeout`` is not available in some builds and
        # makes FFmpeg exit before it even opens the RTSP URL.  The reader's
        # bounded wait and ``stop()`` provide the connection/read timeout.
        command = [self.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin", "-rtsp_transport", "tcp",
                   "-i", self.config.url, "-an"]
        fps_value = self.config.source_options.get("fps")
        if fps_value is not None:
            try:
                fps = float(fps_value)
            except (TypeError, ValueError) as exc:
                raise ValueError("FFmpeg fps 参数无效") from exc
            if not 0.25 <= fps <= 30:
                raise ValueError("FFmpeg fps 必须在 0.25 到 30 之间")
            command.extend(["-vf", f"fps={fps:g}"])
        command.extend(["-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "-"])
        factory = self._popen_factory or subprocess.Popen
        try:
            self._process = factory(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL, bufsize=0)
            if self._process.poll() is not None:
                raise RuntimeError("ffmpeg 进程启动后立即退出")
            self._mark_started()
        except Exception as exc:
            self._process = None
            self._mark_failure("connect_failed", f"FFmpeg 连接失败: {type(exc).__name__}")
            raise RuntimeError(f"FFmpeg 连接失败: {self.config.safe_url}") from exc

    def read_frame(self, timeout_s: float | None = None) -> CaptureFrame | None:
        if self._process is None or not self._started:
            raise RuntimeError("camera source 尚未 start")
        try:
            payload = _read_jpeg_from_stream(self._process.stdout, timeout_s or self.config.read_timeout_s)
        except Exception as exc:
            self._mark_failure("read_error", f"FFmpeg 读取失败: {type(exc).__name__}")
            raise RuntimeError(f"FFmpeg 读取失败: {self.config.safe_url}") from exc
        if payload is None:
            self._mark_failure("read_timeout", "FFmpeg 暂时未返回帧")
            return None
        try:
            from io import BytesIO

            from PIL import Image
            image = np.asarray(Image.open(BytesIO(payload)).convert("RGB"))
        except Exception as exc:
            self._mark_failure("decode_error", f"JPEG 解码失败: {type(exc).__name__}")
            raise RuntimeError("FFmpeg 输出帧解码失败") from exc
        self._sequence += 1
        captured_at = utc_now_ms()
        result = CaptureFrame(self.config.camera_id, captured_at, image, self._sequence)
        self._mark_frame(captured_at)
        return result

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except Exception:
                    process.kill()
            finally:
                self._mark_stopped()
        else:
            self._mark_stopped()


def _read_jpeg_from_stream(stream: Any, timeout_s: float) -> bytes | None:
    if stream is None:
        return None
    data = bytearray()
    deadline = time.monotonic() + max(0.1, timeout_s)
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        if not _wait_for_stream_data(stream, remaining):
            return bytes(data) if data else None
        chunk = stream.read(4096)
        if not chunk:
            return bytes(data) if data else None
        data.extend(chunk)
        start = data.find(b"\xff\xd8")
        if start >= 0:
            end = data.find(b"\xff\xd9", start + 2)
            if end >= 0:
                return bytes(data[start:end + 2])
    return None


def _wait_for_stream_data(stream: Any, timeout_s: float) -> bool:
    """Wait until an FFmpeg stdout pipe can be read on POSIX and Windows."""

    if not hasattr(stream, "fileno"):
        return True
    if os.name != "nt":
        import select

        ready, _, _ = select.select([stream], [], [], max(0.0, timeout_s))
        return bool(ready)
    return _wait_for_windows_pipe(stream, timeout_s)


def _wait_for_windows_pipe(stream: Any, timeout_s: float) -> bool:
    """Poll a Windows anonymous pipe without using ``select`` on a pipe handle."""

    import ctypes
    import msvcrt
    from ctypes import wintypes

    try:
        handle = msvcrt.get_osfhandle(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return True
    if handle == -1:
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    peek_named_pipe = kernel32.PeekNamedPipe
    peek_named_pipe.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    peek_named_pipe.restype = wintypes.BOOL

    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        available = wintypes.DWORD()
        ok = peek_named_pipe(
            wintypes.HANDLE(handle),
            None,
            0,
            None,
            ctypes.byref(available),
            None,
        )
        if not ok:
            # A closed/broken pipe will make read() return EOF; let the caller
            # handle that rather than reporting a platform-specific error.
            return True
        if available.value:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


async def iter_source(source: CameraSource, stop_event: asyncio.Event | None = None) -> AsyncIterator[CaptureFrame]:
    source.start()
    stop_event = stop_event or asyncio.Event()
    try:
        while not stop_event.is_set():
            frame = await asyncio.to_thread(source.read_frame, source.config.read_timeout_s)
            if frame is not None:
                yield frame
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=source.config.interval_ms / 1000)
            except asyncio.TimeoutError:
                pass
    finally:
        source.stop()
