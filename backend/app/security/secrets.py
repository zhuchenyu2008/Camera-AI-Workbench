from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, MultiFernet

from app.config import Config
from app.errors import AppError


def digest(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: object) -> str:
    return digest(canonical(value))


class Secrets:
    def __init__(self, path: Path):
        # 第一行是当前密钥，其余行是仍可解密的历史密钥。
        self.cipher = MultiFernet(
            [Fernet(line.strip()) for line in path.read_bytes().splitlines() if line.strip()]
        )

    def encrypt(self, text: str) -> str:
        return self.cipher.encrypt(text.encode()).decode()

    def decrypt(self, text: str) -> str:
        return self.cipher.decrypt(text.encode()).decode()

    def sign(self, value: dict) -> str:
        return self.encrypt(canonical(value))

    def verify(self, token: str) -> dict:
        try:
            result = json.loads(self.decrypt(token))
            if not isinstance(result, dict):
                raise ValueError
            return result
        except Exception:
            raise AppError(409, "PREVIEW_STALE", "预检凭证无效，请重新预检") from None


def safe_path(root: Path, relpath: str) -> Path:
    base = root.resolve()
    target = (base / relpath).resolve()
    if not target.is_relative_to(base) or target == base:
        raise AppError(404, "RESOURCE_NOT_FOUND", "资源不存在")
    return target


def validate_provider_url(url: str, config: Config, resolve: bool = True) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        raise AppError(422, "VALIDATION_ERROR", "模型地址端口无效") from None
    if (
        parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or not host
    ):
        raise AppError(422, "VALIDATION_ERROR", "模型地址不能包含凭据、查询或片段")
    if parts.scheme not in ("https", "http"):
        raise AppError(422, "VALIDATION_ERROR", "模型地址协议不受支持")
    if config.development_mode:
        return url.rstrip("/")

    endpoint = f"{host}:{port or (443 if parts.scheme == 'https' else 80)}"
    if endpoint in config.provider_local_endpoints:
        pass
    elif parts.scheme != "https" or host not in config.provider_allowed_hosts:
        raise AppError(422, "VALIDATION_ERROR", "模型地址须为部署白名单内的 HTTPS 主机")
    elif resolve:
        try:
            ips = [
                ipaddress.ip_address(addr[4][0])
                for addr in socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
            ]
        except OSError:
            raise AppError(422, "VALIDATION_ERROR", "模型主机无法解析") from None
        if not ips or any(not ip.is_global for ip in ips):
            raise AppError(422, "VALIDATION_ERROR", "模型地址不能解析到私网、回环或保留地址")
    return url.rstrip("/")


def redact(value: str) -> str:
    text = re.sub(r"(?i)(rtsp|https?)://[^\s/@]+:[^\s/@]+@", r"\1://[REDACTED]@", value)
    text = re.sub(r"(?i)(bearer\s+|api[_-]?key[=:]\s*)\S+", r"\1[REDACTED]", text)
    return re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "[IMAGE]", text)


def constant_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
