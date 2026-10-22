from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment and optional ``.env``.

    The design document uses both ``APP_*`` variables and a few legacy, short
    variables (for example ``API_BIND``).  The aliases below intentionally
    accept both forms so the Docker Compose file and local development use the
    same settings object.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        populate_by_name=True,
        extra="ignore",
    )

    app_env: str = Field(
        default="development",
        validation_alias=AliasChoices("APP_ENV", "ENVIRONMENT"),
    )
    data_dir: Path = Field(
        default=Path("./data"),
        validation_alias=AliasChoices("APP_DATA_DIR", "DATA_DIR"),
    )
    db_url: str = Field(
        default="sqlite:///./data/app.sqlite3",
        validation_alias=AliasChoices("APP_DB_URL", "DATABASE_URL"),
    )
    timezone: str = Field(
        default="Asia/Shanghai",
        validation_alias=AliasChoices("APP_TIMEZONE", "TIMEZONE"),
    )

    # Secrets are deliberately optional at import time.  Production startup
    # fails when a sensitive operation needs a missing master key/password;
    # this keeps ``--help`` and migrations usable without exposing secrets.
    master_key_file: Path = Field(
        default=Path("./data/master.key"),
        validation_alias=AliasChoices(
            "APP_MASTER_KEY_FILE",
            "APP_ENCRYPTION_KEY_FILE",
            "MASTER_KEY_FILE",
            "ENCRYPTION_KEY_FILE",
        ),
    )
    master_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("APP_MASTER_KEY", "MASTER_KEY"),
    )
    bootstrap_password_file: Path = Field(
        default=Path("./data/bootstrap_password"),
        validation_alias=AliasChoices(
            "APP_BOOTSTRAP_PASSWORD_FILE", "BOOTSTRAP_PASSWORD_FILE"
        ),
    )
    bootstrap_password: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("APP_BOOTSTRAP_PASSWORD", "BOOTSTRAP_PASSWORD"),
    )

    api_bind: str = Field(
        default="0.0.0.0:8000",
        validation_alias=AliasChoices("API_BIND", "APP_API_BIND"),
    )
    allowed_origin: str = Field(
        default="http://localhost:5173",
        validation_alias=AliasChoices("APP_ALLOWED_ORIGIN", "ALLOWED_ORIGIN"),
    )
    session_ttl_seconds: int = Field(
        default=86_400,
        validation_alias=AliasChoices("APP_SESSION_TTL_SECONDS", "SESSION_TTL_SECONDS"),
    )
    session_cookie_name: str = Field(
        default="caw_session",
        validation_alias=AliasChoices("APP_SESSION_COOKIE_NAME", "SESSION_COOKIE_NAME"),
    )
    csrf_cookie_name: str = Field(
        default="caw_csrf",
        validation_alias=AliasChoices("APP_CSRF_COOKIE_NAME", "CSRF_COOKIE_NAME"),
    )
    csrf_header_name: str = Field(
        default="X-CSRF-Token",
        validation_alias=AliasChoices("APP_CSRF_HEADER_NAME", "CSRF_HEADER_NAME"),
    )
    cookie_secure: bool = Field(
        default=False,
        validation_alias=AliasChoices("APP_COOKIE_SECURE", "COOKIE_SECURE"),
    )
    cookie_same_site: str = Field(
        default="lax",
        validation_alias=AliasChoices("APP_COOKIE_SAMESITE", "COOKIE_SAMESITE"),
    )

    sqlite_busy_timeout_ms: int = Field(
        default=5_000,
        validation_alias=AliasChoices(
            "APP_DB_BUSY_TIMEOUT_MS",
            "APP_SQLITE_BUSY_TIMEOUT_MS",
            "DB_BUSY_TIMEOUT_MS",
        ),
    )
    sqlite_wal_autocheckpoint: int = Field(
        default=1_000,
        validation_alias=AliasChoices(
            "APP_SQLITE_WAL_AUTOCHECKPOINT", "SQLITE_WAL_AUTOCHECKPOINT"
        ),
    )
    db_echo: bool = Field(
        default=False,
        validation_alias=AliasChoices("APP_DB_ECHO", "DB_ECHO"),
    )

    capture_default_interval_ms: int = Field(
        default=1_000,
        validation_alias=AliasChoices(
            "CAPTURE_DEFAULT_INTERVAL_MS", "APP_CAPTURE_DEFAULT_INTERVAL_MS"
        ),
    )
    worker_concurrency: int = Field(
        default=2,
        validation_alias=AliasChoices("WORKER_CONCURRENCY", "APP_WORKER_CONCURRENCY"),
    )
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("LOG_LEVEL", "APP_LOG_LEVEL"),
    )

    provider_allowed_hosts: str = Field(
        default="",
        validation_alias=AliasChoices(
            "APP_PROVIDER_ALLOWED_HOSTS", "PROVIDER_ALLOWED_HOSTS"
        ),
    )
    provider_local_endpoints: str = Field(
        default="",
        validation_alias=AliasChoices(
            "APP_PROVIDER_LOCAL_ENDPOINTS", "PROVIDER_LOCAL_ENDPOINTS"
        ),
    )
    rtsp_allowed_hosts: str = Field(
        default="",
        validation_alias=AliasChoices("APP_RTSP_ALLOWED_HOSTS", "RTSP_ALLOWED_HOSTS"),
    )
    allow_insecure_local_ai: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "APP_ALLOW_INSECURE_LOCAL_AI", "ALLOW_INSECURE_LOCAL_AI"
        ),
    )

    @field_validator("app_env", mode="before")
    @classmethod
    def _normalize_environment(cls, value: Any) -> str:
        return str(value).strip().lower()

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo

        ZoneInfo(value)
        return value

    @field_validator(
        "session_ttl_seconds",
        "sqlite_busy_timeout_ms",
        "sqlite_wal_autocheckpoint",
        "capture_default_interval_ms",
        "worker_concurrency",
    )
    @classmethod
    def _validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("该配置必须为正整数")
        return value

    @field_validator("cookie_same_site")
    @classmethod
    def _validate_same_site(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"lax", "strict", "none"}:
            raise ValueError("COOKIE_SAMESITE 必须是 lax、strict 或 none")
        return value

    @property
    def environment(self) -> str:
        return self.app_env

    @property
    def secure_cookies(self) -> bool:
        return self.cookie_secure

    @property
    def master_key_file_path(self) -> Path:
        return self.master_key_file

    @property
    def is_production(self) -> bool:
        return self.app_env in {"production", "prod"}

    @property
    def encryption_key_file(self) -> Path:
        """兼容设计文档中的 ``APP_ENCRYPTION_KEY_FILE`` 命名。"""

        return self.master_key_file

    @property
    def database_url(self) -> str:
        return self.db_url

    @property
    def provider_allowed_hosts_list(self) -> list[str]:
        return _split_csv(self.provider_allowed_hosts)

    @property
    def provider_local_endpoints_list(self) -> list[str]:
        return _split_csv(self.provider_local_endpoints)

    @property
    def rtsp_allowed_hosts_list(self) -> list[str]:
        return _split_csv(self.rtsp_allowed_hosts)

    @property
    def api_host_port(self) -> tuple[str, int]:
        value = self.api_bind.strip()
        if value.startswith("[") and "]" in value:
            host, _, port = value.rpartition(":")
            host = host[1:-1]
        else:
            host, separator, port = value.rpartition(":")
            if not separator:
                host, port = value, "8000"
        try:
            parsed_port = int(port)
        except ValueError as exc:
            raise ValueError(f"API_BIND 端口无效: {self.api_bind!r}") from exc
        if not 1 <= parsed_port <= 65_535:
            raise ValueError("API_BIND 端口必须在 1 到 65535 之间")
        return host or "0.0.0.0", parsed_port

    def ensure_runtime_directories(self) -> None:
        """Create only application data directories; never remove user data."""

        for relative in ("db", "frames", "thumbs", "backups", "tmp"):
            (self.data_dir / relative).mkdir(parents=True, exist_ok=True)

    def read_bootstrap_password(self) -> str | None:
        if self.bootstrap_password is not None:
            value = self.bootstrap_password.get_secret_value().strip()
            return value or None
        path = self.bootstrap_password_file
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            return value or None
        return None


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    # The Compose sample historically names this APP_MASTER_KEY_FILE.  If a
    # caller only provides APP_ENCRYPTION_KEY_FILE, pydantic's aliases above
    # already select it.  This fallback also handles a process changing env
    # after Settings was instantiated in a long-lived test runner.
    encryption_file = os.getenv("APP_ENCRYPTION_KEY_FILE")
    if encryption_file and "APP_MASTER_KEY_FILE" not in os.environ:
        settings.master_key_file = Path(encryption_file)
    return settings
