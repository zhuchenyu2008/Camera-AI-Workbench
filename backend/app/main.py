"""FastAPI 应用入口。

路由层与 ORM/业务层解耦：应用提供一个标准的 ``Session`` 依赖和一个可替换的
repository 插槽。主进程可以把自己的 repository 传给 ``create_app``，而不需要
修改 API DTO 或路由签名。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException


LOG = logging.getLogger("camera_ai_workbench")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


@dataclass(frozen=True, slots=True)
class AppConfig:
    data_dir: Path
    database_url: str
    allowed_origins: tuple[str, ...]
    environment: str = "development"
    timezone: str = "Asia/Shanghai"
    session_ttl_seconds: int = 86_400
    secure_cookies: bool = False
    auto_migrate: bool = True
    auth_disabled: bool = False
    bootstrap_password_file: Path | None = None
    bootstrap_password: str | None = None
    master_key_file: Path | None = None
    master_key: str | None = None
    max_request_bytes: int = 1 * 1024 * 1024
    max_upload_bytes: int = 8 * 1024 * 1024
    resource_root: Path | None = None
    api_version: str = "1.0.0"
    provider_allowed_hosts: tuple[str, ...] = ()
    provider_local_endpoints: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "AppConfig":
        environment = os.getenv("APP_ENV", os.getenv("CAW_ENV", "development")).lower()
        data_dir = Path(os.getenv("APP_DATA_DIR", os.getenv("CAW_DATA_DIR", "./data"))).expanduser()
        if not data_dir.is_absolute():
            data_dir = (Path.cwd() / data_dir).resolve()
        database_url = os.getenv("APP_DB_URL", os.getenv("CAW_DATABASE_URL", ""))
        if not database_url:
            database_url = "sqlite:///" + (data_dir / "db" / "app.sqlite3").as_posix()

        origins_value = os.getenv("APP_ALLOWED_ORIGIN", os.getenv("CAW_ALLOWED_ORIGIN", "http://localhost:5173"))
        origins = tuple(item.strip().rstrip("/") for item in origins_value.split(",") if item.strip())
        for origin in origins:
            parts = urlsplit(origin)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path not in {"", "/"}:
                raise ValueError("APP_ALLOWED_ORIGIN 必须是 HTTP(S) origin，可用逗号分隔多个来源")
            if parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError("APP_ALLOWED_ORIGIN 不得包含凭据、query 或 fragment")

        secure_default = environment == "production" or all(o.startswith("https://") for o in origins)
        secure_cookies = _env_bool("APP_SECURE_COOKIES", secure_default)
        auth_disabled = _env_bool("APP_AUTH_DISABLED", False)
        if auth_disabled and environment == "production":
            raise ValueError("生产环境禁止 APP_AUTH_DISABLED=1")

        password_file = os.getenv(
            "APP_BOOTSTRAP_PASSWORD_FILE", os.getenv("CAW_BOOTSTRAP_PASSWORD_FILE", "")
        ).strip()
        key_file = os.getenv(
            "APP_MASTER_KEY_FILE", os.getenv("CAW_MASTER_KEY_FILE", str(data_dir / "master.key"))
        ).strip()
        resource_root_value = os.getenv("APP_RESOURCE_ROOT", "")
        resource_root = Path(resource_root_value).expanduser() if resource_root_value else Path(__file__).parent / "resources"
        return cls(
            data_dir=data_dir,
            database_url=database_url,
            allowed_origins=origins,
            environment=environment,
            timezone=os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
            session_ttl_seconds=int(
                os.getenv("APP_SESSION_TTL_SECONDS", os.getenv("CAW_SESSION_TTL_SECONDS", "86400"))
            ),
            secure_cookies=secure_cookies,
            auto_migrate=_env_bool("APP_AUTO_MIGRATE", True),
            auth_disabled=auth_disabled,
            bootstrap_password_file=Path(password_file).expanduser() if password_file else None,
            bootstrap_password=os.getenv("APP_BOOTSTRAP_PASSWORD") or os.getenv("CAW_BOOTSTRAP_PASSWORD"),
            master_key_file=Path(key_file).expanduser() if key_file else None,
            master_key=os.getenv("APP_MASTER_KEY") or os.getenv("APP_ENCRYPTION_KEY"),
            max_request_bytes=int(os.getenv("APP_MAX_REQUEST_BYTES", str(1 * 1024 * 1024))),
            max_upload_bytes=int(os.getenv("APP_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024))),
            resource_root=resource_root,
            api_version=os.getenv("APP_VERSION", "1.0.0"),
            provider_allowed_hosts=tuple(
                item.strip() for item in os.getenv("APP_PROVIDER_ALLOWED_HOSTS", "").split(",") if item.strip()
            ),
            provider_local_endpoints=tuple(
                item.strip() for item in os.getenv("APP_PROVIDER_LOCAL_ENDPOINTS", "").split(",") if item.strip()
            ),
        )

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in ("db", "frames", "thumbs", "tmp", "logs", "backups"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)
        if self.master_key_file:
            self.master_key_file.parent.mkdir(parents=True, exist_ok=True)


# 兼容旧代码中的命名。
Config = AppConfig


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# API 自有的极小持久化层仅用于会话、幂等缓存和没有业务 repository 时的资源适配。
# 主 agent 的 ORM metadata 可以在启动时一并 create_all / Alembic migrate，二者不互相覆盖。
API_METADATA = MetaData()
api_resources = Table(
    "api_resources",
    API_METADATA,
    Column("id", String(128), primary_key=True),
    Column("kind", String(64), nullable=False, index=True),
    Column("payload_json", JSON, nullable=False),
    Column("version", Integer, nullable=False, default=1),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
    Column("deleted_at_ms", Integer, nullable=True),
)
api_idempotency = Table(
    "api_idempotency",
    API_METADATA,
    Column("scope", String(255), primary_key=True),
    Column("key", String(255), primary_key=True),
    Column("request_hash", String(64), nullable=False),
    Column("status_code", Integer, nullable=False),
    Column("response_json", JSON, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    Column("expires_at_ms", Integer, nullable=False),
)
auth_users = Table(
    "api_auth_users",
    API_METADATA,
    Column("id", String(64), primary_key=True),
    Column("username", String(128), nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    Column("updated_at_ms", Integer, nullable=False),
)
auth_sessions = Table(
    "api_auth_sessions",
    API_METADATA,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", String(64), nullable=False, index=True),
    Column("csrf_hash", String(64), nullable=False),
    Column("csrf_cipher", Text, nullable=False),
    Column("created_at_ms", Integer, nullable=False),
    Column("expires_at_ms", Integer, nullable=False, index=True),
    Column("revoked_at_ms", Integer, nullable=True),
)


def now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _sha256(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _load_master_key_for_config(config: AppConfig) -> bytes:
    """Load the encryption key; only development may generate a local key."""
    import base64

    production = config.environment in {"production", "prod"}
    if config.master_key:
        raw = config.master_key.encode("utf-8")
        if len(raw) == 32:
            return raw
        try:
            decoded = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
        except Exception as exc:
            raise RuntimeError("APP_MASTER_KEY 不是有效的 32 字节或 Base64 密钥") from exc
        if len(decoded) != 32:
            raise RuntimeError("APP_MASTER_KEY 必须是 32 字节原始值或 Base64 密钥")
        return decoded
    path = config.master_key_file
    if path is None:
        if production:
            raise RuntimeError("生产环境必须通过 APP_MASTER_KEY_FILE 配置应用主密钥")
        return secrets.token_bytes(32)
    if path.exists():
        raw = path.read_bytes().strip()
        if len(raw) == 32:
            return raw
        try:
            decoded = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
        except Exception as exc:
            raise RuntimeError("APP_MASTER_KEY_FILE 不是有效的 32 字节密钥") from exc
        if len(decoded) != 32:
            raise RuntimeError("APP_MASTER_KEY_FILE 必须是 32 字节密钥")
        return decoded
    if production:
        raise RuntimeError(f"生产环境主密钥文件不存在: {path}")
    raw = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return raw


def _load_or_create_fernet(config: AppConfig) -> Fernet:
    """Derive the session Fernet key from the application AES-256 key."""
    import base64
    return Fernet(base64.urlsafe_b64encode(_load_master_key_for_config(config)))


class AuthManager:
    """单用户 cookie session + CSRF；不把密码或 CSRF 明文写入数据库。"""

    cookie_name = "caw_session"

    def __init__(self, config: AppConfig, fernet: Fernet):
        self.config = config
        self.fernet = fernet
        self.cookie_name = os.getenv("APP_SESSION_COOKIE_NAME", "caw_session")
        try:
            from argon2 import PasswordHasher
            from argon2.exceptions import InvalidHashError, VerificationError

            self._password_hasher = PasswordHasher()
            self._verification_errors = (VerificationError, InvalidHashError)
        except ImportError:  # pragma: no cover - 依赖由 pyproject 固定
            self._password_hasher = None
            self._verification_errors = (Exception,)

    def _hash_password(self, password: str) -> str:
        if self._password_hasher:
            return self._password_hasher.hash(password)
        return "sha256$" + _sha256(password)

    def _verify_password(self, encoded: str, password: str) -> bool:
        if encoded.startswith("sha256$"):
            return secrets.compare_digest(encoded, "sha256$" + _sha256(password))
        try:
            return bool(self._password_hasher.verify(encoded, password))
        except self._verification_errors:
            return False

    def ensure_bootstrap(self, db: Session) -> None:
        if db.execute(select(auth_users.c.id).limit(1)).first():
            return
        password = self.config.bootstrap_password
        if not password and self.config.bootstrap_password_file and self.config.bootstrap_password_file.exists():
            password = self.config.bootstrap_password_file.read_text(encoding="utf-8").strip()
        if not password:
            if self.config.environment in {"production", "prod"}:
                raise RuntimeError(
                    "生产环境尚未初始化管理员；请配置 APP_BOOTSTRAP_PASSWORD_FILE 或 APP_BOOTSTRAP_PASSWORD"
                )
            return
        if len(password) < 12:
            raise RuntimeError("初始管理员密码至少需要 12 个字符")
        stamp = now_ms()
        db.execute(
            auth_users.insert().values(
                id="admin",
                username=os.getenv("APP_BOOTSTRAP_USERNAME", "admin"),
                password_hash=self._hash_password(password),
                created_at_ms=stamp,
                updated_at_ms=stamp,
            )
        )
        db.commit()

    def login(self, db: Session, username: str, password: str) -> tuple[str, dict[str, Any]]:
        user = db.execute(select(auth_users).where(auth_users.c.username == username)).mappings().first()
        if not user or not self._verify_password(user["password_hash"], password):
            raise APIError(401, "INVALID_CREDENTIALS", "账户或密码错误")
        token = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        stamp = now_ms()
        expires = stamp + self.config.session_ttl_seconds * 1000
        db.execute(
            auth_sessions.insert().values(
                token_hash=_sha256(token),
                user_id=user["id"],
                csrf_hash=_sha256(csrf),
                csrf_cipher=self.fernet.encrypt(csrf.encode()).decode(),
                created_at_ms=stamp,
                expires_at_ms=expires,
            )
        )
        db.commit()
        return token, {
            "username": user["username"],
            "display_name": user["username"],
            "csrf_token": csrf,
            "expires_at": _iso_from_ms(expires),
        }

    def authenticate(self, db: Session, token: str | None) -> dict[str, Any]:
        if not token:
            raise APIError(401, "AUTH_REQUIRED", "请先登录")
        row = db.execute(
            select(auth_sessions, auth_users.c.username)
            .join(auth_users, auth_sessions.c.user_id == auth_users.c.id)
            .where(auth_sessions.c.token_hash == _sha256(token))
        ).mappings().first()
        if not row or row["revoked_at_ms"] is not None or row["expires_at_ms"] <= now_ms():
            raise APIError(401, "AUTH_REQUIRED", "会话已失效，请重新登录")
        return dict(row)

    def validate_csrf(self, session: dict[str, Any], csrf: str | None) -> None:
        if not csrf or not secrets.compare_digest(session["csrf_hash"], _sha256(csrf)):
            raise APIError(403, "CSRF_INVALID", "CSRF 凭证无效")

    def session_dto(self, session: dict[str, Any]) -> dict[str, Any]:
        try:
            csrf = self.fernet.decrypt(session["csrf_cipher"].encode()).decode()
        except (InvalidToken, KeyError) as exc:
            raise APIError(401, "SESSION_INVALID", "会话凭证无法解密，请重新登录") from exc
        return {
            "username": session["username"],
            "display_name": session["username"],
            "csrf_token": csrf,
            "expires_at": _iso_from_ms(session["expires_at_ms"]),
        }

    def logout(self, db: Session, session: dict[str, Any]) -> None:
        db.execute(
            auth_sessions.update()
            .where(auth_sessions.c.token_hash == session["token_hash"])
            .values(revoked_at_ms=now_ms())
        )
        db.commit()

    def change_password(self, db: Session, session: dict[str, Any], current: str, new: str) -> None:
        user = db.execute(select(auth_users).where(auth_users.c.id == session["user_id"])).mappings().first()
        if not user or not self._verify_password(user["password_hash"], current):
            raise APIError(401, "INVALID_CREDENTIALS", "当前密码错误")
        db.execute(
            auth_users.update()
            .where(auth_users.c.id == user["id"])
            .values(password_hash=self._hash_password(new), updated_at_ms=now_ms())
        )
        db.execute(
            auth_sessions.update()
            .where(auth_sessions.c.user_id == user["id"])
            .values(revoked_at_ms=now_ms())
        )
        db.commit()


class APIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable


# 兼容历史代码的错误名。
AppError = APIError


def _iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _import_external_metadata() -> list[MetaData]:
    """尽量发现主 agent 提供的 SQLAlchemy metadata，不把其设为硬依赖。"""

    metadata_list: list[MetaData] = []
    module_names = (
        "app.models",
        "app.models.base",
        "app.db.models",
        "app.repositories.schema",
        "app.repositories.schema_v1",
        "app.repositories.schema_v7",
    )
    for name in module_names:
        try:
            module = __import__(name, fromlist=["*"])
        except (ImportError, ModuleNotFoundError):
            continue
        candidates: list[Any] = []
        if isinstance(getattr(module, "metadata", None), MetaData):
            candidates.append(module.metadata)
        base = getattr(module, "Base", None)
        if base is not None and isinstance(getattr(base, "metadata", None), MetaData):
            candidates.append(base.metadata)
        for item in candidates:
            if item is not API_METADATA and item not in metadata_list:
                metadata_list.append(item)
    return metadata_list


def _make_engine(config: AppConfig) -> Engine:
    connect_args: dict[str, Any] = {}
    if config.database_url.startswith("sqlite"):
        try:
            busy_timeout_ms = max(1, int(os.getenv("APP_DB_BUSY_TIMEOUT_MS", "5000")))
        except ValueError as exc:
            raise ValueError("APP_DB_BUSY_TIMEOUT_MS 必须是正整数") from exc
        connect_args = {"check_same_thread": False, "timeout": busy_timeout_ms / 1000}
    engine = create_engine(config.database_url, connect_args=connect_args, future=True)
    if config.database_url.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            cursor.execute("PRAGMA journal_mode=WAL")
            try:
                wal_autocheckpoint = max(1, int(os.getenv("APP_SQLITE_WAL_AUTOCHECKPOINT", "1000")))
            except ValueError as exc:
                cursor.close()
                raise ValueError("APP_SQLITE_WAL_AUTOCHECKPOINT 必须是正整数") from exc
            cursor.execute(f"PRAGMA wal_autocheckpoint={wal_autocheckpoint}")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.close()
    return engine


def _run_migrations(config: AppConfig, engine: Engine, *, strict: bool = False) -> None:
    """启动时优先执行 Alembic；无 migrations 时退化为 metadata.create_all。"""

    root = Path(__file__).resolve().parents[1]
    migrations = root / "migrations"
    version_files = list((migrations / "versions").glob("*.py")) if (migrations / "versions").exists() else []
    if config.auto_migrate and version_files:
        try:
            from alembic import command
            from alembic.config import Config as AlembicConfig

            alembic_config = AlembicConfig()
            alembic_config.set_main_option("script_location", str(migrations))
            alembic_config.set_main_option("sqlalchemy.url", config.database_url.replace("%", "%%"))
            command.upgrade(alembic_config, "head")
            return
        except Exception:
            if strict:
                raise
            LOG.exception("Alembic 迁移失败，继续使用 metadata.create_all")
    # 这一步只创建不存在的表，不删除或重写已有模型表。
    for metadata in [*_import_external_metadata(), API_METADATA]:
        metadata.create_all(engine)


def prepare_database(config: AppConfig, engine: Engine | None = None) -> Engine:
    config.prepare()
    engine = engine or _make_engine(config)
    _run_migrations(config, engine, strict=config.environment in {"production", "prod"})
    return engine


def get_db(request: Request):
    """FastAPI Session 依赖，可在测试和集成时用 app.dependency_overrides 替换。"""
    factory = request.app.state.SessionLocal
    db: Session = factory()
    try:
        yield db
        if db.in_transaction():
            db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _extract_token(request: Request) -> tuple[str | None, bool]:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None, False
    return request.cookies.get(AuthManager.cookie_name), True


def _request_id(request: Request) -> str:
    value = request.headers.get("X-Request-ID", "")
    return value if _REQUEST_ID.fullmatch(value) else "req_" + uuid.uuid4().hex


def _error_payload(request: Request, error: APIError) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "request_id": getattr(request.state, "request_id", ""),
            "details": error.details,
            "retryable": error.retryable,
        }
    }


def create_app(
    config: AppConfig | None = None,
    *,
    settings: AppConfig | None = None,
    repository: Any | None = None,
    engine: Engine | None = None,
    session_factory: Any | None = None,
) -> FastAPI:
    """创建应用。

    ``repository`` 可由主进程传入，路由会优先调用它的领域方法；缺少领域能力
    时返回明确的 501，而不是构造假摄像头、假 AI 结果或假任务状态。
    """

    config = config or settings or AppConfig.from_env()
    config.prepare()
    engine = prepare_database(config, engine)
    SessionLocal = session_factory or sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    master_key = _load_master_key_for_config(config)
    fernet = Fernet(__import__("base64").urlsafe_b64encode(master_key))
    auth = AuthManager(config, fernet)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        # 重跑一次迁移/建表，覆盖 CLI 先启动 API、或外部 volume 刚挂载的场景。
        _run_migrations(config, engine, strict=config.environment in {"production", "prod"})
        API_METADATA.create_all(engine, checkfirst=True)
        with SessionLocal() as db:
            auth.ensure_bootstrap(db)
            if config.environment in {"production", "prod"} and not db.execute(select(auth_users.c.id).limit(1)).first():
                raise RuntimeError("生产环境必须先初始化管理员账户，拒绝以空密码启动")
        application.state.migrations_ready = True
        startup = getattr(application.state.repository, "startup", None)
        if startup:
            result = startup()
            if hasattr(result, "__await__"):
                await result
        try:
            yield
        finally:
            shutdown = getattr(application.state.repository, "shutdown", None)
            if shutdown:
                result = shutdown()
                if hasattr(result, "__await__"):
                    await result
            if hasattr(application.state, "outbox"):
                application.state.outbox.close()
            if session_factory is None:
                engine.dispose()

    application = FastAPI(
        title="摄像头 AI 总结工作台 API",
        version=config.api_version,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    application.state.config = config
    application.state.engine = engine
    application.state.SessionLocal = SessionLocal
    application.state.repository = repository
    application.state.auth = auth
    application.state.fernet = fernet
    application.state.master_key = master_key
    application.state.api_metadata = API_METADATA
    from app.events import InMemoryOutbox, SQLiteOutbox
    application.state.outbox = (InMemoryOutbox() if ":memory:" in config.database_url else SQLiteOutbox(config.database_url))
    application.state.api_resources = api_resources
    application.state.api_idempotency = api_idempotency
    application.state.resource_root = (config.resource_root or Path(__file__).parent / "resources").resolve()
    application.state.migrations_ready = False
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.allowed_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.middleware("http")
    async def boundary(request: Request, call_next):
        request.state.request_id = _request_id(request)
        request.state.started_at_ms = now_ms()
        try:
            # 浏览器写请求只允许显式配置的 Origin；无 Origin 的服务端/CLI 调用不受影响。
            origin = request.headers.get("Origin")
            if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin.rstrip("/") not in config.allowed_origins:
                raise APIError(403, "ORIGIN_DENIED", "请求来源不受允许")

            content_length = request.headers.get("Content-Length")
            if content_length:
                try:
                    length = int(content_length)
                except ValueError:
                    raise APIError(400, "VALIDATION_ERROR", "Content-Length 不合法") from None
                limit = config.max_upload_bytes if "multipart/form-data" in request.headers.get("Content-Type", "") else config.max_request_bytes
                if length < 0 or length > limit:
                    raise APIError(413, "REQUEST_TOO_LARGE", "请求超过大小上限", {"max_bytes": limit})
            response = await call_next(request)
        except APIError as error:
            response = JSONResponse(_error_payload(request, error), status_code=error.status_code)
        except Exception:
            LOG.exception("未处理的请求异常 request_id=%s", request.state.request_id)
            response = JSONResponse(
                _error_payload(request, APIError(500, "INTERNAL_ERROR", "请求处理失败")), status_code=500
            )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/") and response.media_type != "text/event-stream":
            response.headers.setdefault("Cache-Control", "private, no-store")
        if origin and origin.rstrip("/") in config.allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        return response

    @application.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError):
        return JSONResponse(_error_payload(request, exc), status_code=exc.status_code)

    @application.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        fields: list[dict[str, Any]] = []
        for item in exc.errors():
            location = [str(part) for part in item.get("loc", []) if part not in {"body", "query", "path"}]
            fields.append({"field": ".".join(location), "message": item.get("msg", "字段无效"), "type": item.get("type")})
        error = APIError(422, "VALIDATION_ERROR", "请求参数校验失败", {"field_errors": fields})
        return JSONResponse(_error_payload(request, error), status_code=422)

    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        error = APIError(exc.status_code, "HTTP_ERROR", str(exc.detail))
        return JSONResponse(_error_payload(request, error), status_code=exc.status_code)

    @application.exception_handler(Exception)
    async def fallback_error_handler(request: Request, _exc: Exception):
        LOG.exception("未处理的应用异常 request_id=%s", request.state.request_id)
        error = APIError(500, "INTERNAL_ERROR", "请求处理失败")
        return JSONResponse(_error_payload(request, error), status_code=500)

    from app.api import health_router, legacy_router, router

    application.include_router(router, prefix="/api/v1")
    application.include_router(health_router)
    application.include_router(legacy_router)
    return application


# 导出默认 app，方便 uvicorn app.main:app 以及 Docker 健康检查。
try:
    app = create_app()
except Exception as exc:  # pragma: no cover - 开发环境允许 CLI/help 在数据卷尚未准备好时导入
    if os.getenv("APP_ENV", "development").strip().lower() in {"production", "prod"}:
        raise
    LOG.warning("默认 app 初始化延迟: %s", exc)
    app = FastAPI(title="摄像头 AI 总结工作台 API", version="1.0.0")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
