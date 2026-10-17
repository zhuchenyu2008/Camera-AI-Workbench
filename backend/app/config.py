from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    data_dir: Path
    database_url: str
    master_key_file: Path
    allowed_origin: str = "https://camera.example.com"
    rtsp_allowed_ports: tuple[int, ...] = (15554, 15555)
    provider_allowed_hosts: tuple[str, ...] = ()
    provider_local_endpoints: tuple[str, ...] = ()
    ffmpeg: str = "ffmpeg"
    secure_cookies: bool = True
    session_seconds: int = 86400
    max_upload_bytes: int = 8 * 1024 * 1024
    clock: object = field(default=None, repr=False, compare=False)

    @classmethod
    def from_env(cls) -> Config:
        data = Path(os.environ.get("CAW_DATA_DIR", "data")).resolve()
        key = Path(os.environ.get("CAW_MASTER_KEY_FILE", "/etc/camera-ai-workbench/master.key"))
        origin = os.environ.get("CAW_ALLOWED_ORIGIN", "https://camera.example.com").rstrip("/")
        if urlsplit(origin).scheme != "https" or urlsplit(origin).path:
            raise ValueError("CAW_ALLOWED_ORIGIN 必须是无路径的 HTTPS 来源")
        if os.environ.get("CAW_TIMEZONE", "Asia/Shanghai") != "Asia/Shanghai":
            raise ValueError("首版只支持 Asia/Shanghai")
        return cls(
            data_dir=data,
            database_url=os.environ.get(
                "CAW_DATABASE_URL", f"sqlite:///{(data / 'db/workbench.sqlite3').as_posix()}"
            ),
            master_key_file=key,
            allowed_origin=origin,
            rtsp_allowed_ports=tuple(
                int(p) for p in os.environ.get("CAW_RTSP_ALLOWED_PORTS", "15554,15555").split(",")
            ),
            provider_allowed_hosts=tuple(
                filter(None, os.environ.get("CAW_PROVIDER_ALLOWED_HOSTS", "").split(","))
            ),
            provider_local_endpoints=tuple(
                filter(None, os.environ.get("CAW_PROVIDER_LOCAL_ENDPOINTS", "").split(","))
            ),
            ffmpeg=os.environ.get("CAW_FFMPEG", "ffmpeg"),
        )

    def prepare(self) -> None:
        for name in ("db", "frames", "references", "hls", "tmp", "logs"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)
