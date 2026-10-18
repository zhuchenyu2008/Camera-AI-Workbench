from __future__ import annotations

import os
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    data_dir: Path
    database_url: str
    master_key_file: Path
    allowed_origin: str = "https://camera.example.com"
    rtsp_allowed_ports: tuple[int, ...] = (15554, 15555)
    rtsp_allowed_hosts: tuple[str, ...] = ("127.0.0.1",)
    provider_allowed_hosts: tuple[str, ...] = ()
    provider_local_endpoints: tuple[str, ...] = ()
    ffmpeg: str = "ffmpeg"
    secure_cookies: bool = True
    session_seconds: int = 86400
    validated_ai_concurrency: int = 2
    max_upload_bytes: int = 8 * 1024 * 1024
    clock: object = field(default=None, repr=False, compare=False)

    @classmethod
    def from_env(cls) -> Config:
        data = Path(os.environ.get("CAW_DATA_DIR", "data")).resolve()
        key = Path(os.environ.get("CAW_MASTER_KEY_FILE", "/etc/camera-ai-workbench/master.key"))
        origin = os.environ.get("CAW_ALLOWED_ORIGIN", "https://camera.example.com").rstrip("/")
        parts = urlsplit(origin)
        local_http = (
            os.environ.get("CAW_LOCAL_DEV") == "1"
            and parts.scheme == "http"
            and parts.hostname in {"127.0.0.1", "localhost"}
        )
        if (
            (parts.scheme != "https" and not local_http)
            or not parts.hostname
            or parts.path
            or parts.query
            or parts.fragment
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError("CAW_ALLOWED_ORIGIN 必须是 HTTPS 来源；本机开发仅允许显式启用的回环 HTTP 来源")
        # 同时验证端口，避免无效 Origin 在启动后才导致连接失败。
        parts.port
        if os.environ.get("CAW_TIMEZONE", "Asia/Shanghai") != "Asia/Shanghai":
            raise ValueError("首版只支持 Asia/Shanghai")
        return cls(
            data_dir=data,
            database_url=os.environ.get(
                "CAW_DATABASE_URL", f"sqlite:///{(data / 'db/workbench.sqlite3').as_posix()}"
            ),
            master_key_file=key,
            allowed_origin=origin,
            secure_cookies=not local_http,
            rtsp_allowed_ports=tuple(
                int(p) for p in os.environ.get("CAW_RTSP_ALLOWED_PORTS", "15554,15555").split(",")
            ),
            rtsp_allowed_hosts=tuple(
                str(IPv4Address(host.strip()))
                for host in os.environ.get("CAW_RTSP_ALLOWED_HOSTS", "127.0.0.1").split(",")
                if host.strip()
            ),
            provider_allowed_hosts=tuple(
                filter(None, os.environ.get("CAW_PROVIDER_ALLOWED_HOSTS", "").split(","))
            ),
            provider_local_endpoints=tuple(
                filter(None, os.environ.get("CAW_PROVIDER_LOCAL_ENDPOINTS", "").split(","))
            ),
            ffmpeg=os.environ.get("CAW_FFMPEG", "ffmpeg"),
            validated_ai_concurrency=int(os.environ.get("CAW_VALIDATED_AI_CONCURRENCY", "2")),
        )

    def prepare(self) -> None:
        for name in ("db", "frames", "references", "hls", "tmp", "logs"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)
