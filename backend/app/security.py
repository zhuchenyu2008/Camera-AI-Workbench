from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from argon2 import PasswordHasher
from argon2.low_level import Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import Settings, get_settings


_MASTER_KEY_BYTES: Final[int] = 32
_ENVELOPE_VERSION: Final[bytes] = b"CAW1"
_TOKEN_BYTES: Final[int] = 32
_password_hasher = PasswordHasher(type=Type.ID)


class SecurityError(Exception):
    """Base class for security/configuration failures."""


class MasterKeyError(SecurityError):
    """Raised when the application master key is unavailable or malformed."""


class DecryptionError(SecurityError):
    """Raised when an encrypted value cannot be authenticated/decrypted."""


class InvalidSecret(SecurityError):
    """Raised for malformed or unsafe secret input."""


@dataclass(frozen=True, slots=True)
class SecretEnvelope:
    """Decoded AES-GCM envelope metadata and payload."""

    version: bytes
    nonce: bytes
    ciphertext: bytes

    def encode(self) -> bytes:
        return self.version + self.nonce + self.ciphertext


def _read_file_secret(path: Path) -> bytes | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise MasterKeyError(f"密钥路径不是普通文件: {path}")
    value = path.read_bytes()
    if len(value) != _MASTER_KEY_BYTES:
        value = value.strip()
    return value or None


def _decode_key(value: str | bytes) -> bytes:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if len(raw) == _MASTER_KEY_BYTES:
        return raw
    raw = raw.strip()
    if len(raw) == _MASTER_KEY_BYTES:
        return raw
    # Accept a standard base64/urlsafe-base64 representation for Docker
    # secrets, but never silently truncate or hash a wrong-length key.
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            decoded = decoder(raw + b"=" * (-len(raw) % 4))
        except (ValueError, binascii.Error):  # type: ignore[name-defined]
            continue
        if len(decoded) == _MASTER_KEY_BYTES:
            return decoded
    raise MasterKeyError("应用主密钥必须是 32 字节原始值或可解码为 32 字节的 Base64 值")


def load_master_key(settings: Settings | None = None, *, create: bool = False) -> bytes:
    """Load the AES-256 key from env or a protected file.

    ``create`` is intended for local ``app.cli init`` only.  Production should
    mount a Docker Secret and fail closed when it is absent.
    """

    settings = settings or get_settings()
    if settings.master_key is not None:
        return _decode_key(settings.master_key.get_secret_value())

    value = _read_file_secret(settings.master_key_file)
    if value is not None:
        return _decode_key(value)

    if not create:
        raise MasterKeyError(f"未找到应用主密钥文件: {settings.master_key_file}")
    settings.master_key_file.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(_MASTER_KEY_BYTES)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(settings.master_key_file, flags, 0o600)
    except FileExistsError:
        return load_master_key(settings, create=False)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
        handle.flush()
        os.fsync(handle.fileno())
    _best_effort_restrict_file(settings.master_key_file)
    return key


def _best_effort_restrict_file(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows and some mounted secret files do not expose POSIX modes.
        pass


def _aad_bytes(aad: str | bytes | None) -> bytes:
    if aad is None:
        return b"camera-ai-workbench"
    return aad.encode("utf-8") if isinstance(aad, str) else aad


def encrypt_secret(
    plaintext: str | bytes,
    *,
    key: bytes | str | None = None,
    aad: str | bytes | None = None,
    settings: Settings | None = None,
) -> bytes:
    """Encrypt a secret with AES-256-GCM and return a versioned envelope."""

    key_bytes = load_master_key(settings) if key is None else _decode_key(key)
    data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key_bytes).encrypt(nonce, data, _aad_bytes(aad))
    return SecretEnvelope(_ENVELOPE_VERSION, nonce, ciphertext).encode()


def decrypt_secret(
    envelope: bytes | bytearray | memoryview,
    *,
    key: bytes | str | None = None,
    aad: str | bytes | None = None,
    settings: Settings | None = None,
) -> bytes:
    """Authenticate and decrypt a value; authentication failures are generic."""

    key_bytes = load_master_key(settings) if key is None else _decode_key(key)
    raw = bytes(envelope)
    if len(raw) < len(_ENVELOPE_VERSION) + 12 + 16 or not raw.startswith(_ENVELOPE_VERSION):
        raise DecryptionError("加密值格式无效")
    nonce_start = len(_ENVELOPE_VERSION)
    nonce = raw[nonce_start : nonce_start + 12]
    ciphertext = raw[nonce_start + 12 :]
    try:
        return AESGCM(key_bytes).decrypt(nonce, ciphertext, _aad_bytes(aad))
    except (InvalidTag, ValueError) as exc:
        raise DecryptionError("加密值校验失败") from exc


def encrypt_text(value: str, **kwargs: object) -> bytes:
    return encrypt_secret(value, **kwargs)


def decrypt_text(value: bytes, **kwargs: object) -> str:
    return decrypt_secret(value, **kwargs).decode("utf-8")


def hash_password(password: str) -> str:
    _validate_password(password)
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Verify Argon2id hash without leaking whether the account exists."""

    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_password_rehash(password_hash: str) -> bool:
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, VerificationError):
        return True


def _validate_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < 12:
        raise InvalidSecret("管理员密码至少需要 12 个字符")
    if len(password) > 1024:
        raise InvalidSecret("管理员密码长度不能超过 1024 个字符")


def new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    if not token or len(token) > 512:
        raise InvalidSecret("token 格式无效")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def compare_token(token: str, token_hash: str) -> bool:
    try:
        candidate = hash_token(token)
    except InvalidSecret:
        return False
    return hmac.compare_digest(candidate, token_hash)


def new_session_tokens() -> tuple[str, str]:
    """Return raw session and CSRF tokens; only their hashes belong in the DB."""

    return new_token(), new_token()


def csrf_is_safe(method: str, supplied: str | None, expected_hash: str) -> bool:
    """Double-submit-style CSRF check against the hash stored with a session."""

    if method.upper() in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return True
    if not supplied:
        return False
    return compare_token(supplied, expected_hash)


__all__ = [
    "DecryptionError",
    "InvalidSecret",
    "MasterKeyError",
    "SecretEnvelope",
    "SecurityError",
    "compare_token",
    "csrf_is_safe",
    "decrypt_secret",
    "decrypt_text",
    "encrypt_secret",
    "encrypt_text",
    "hash_password",
    "hash_token",
    "load_master_key",
    "needs_password_rehash",
    "new_session_tokens",
    "new_token",
    "verify_password",
]

# Database-backed single-user session helpers are kept here so API routes can
# share one policy without depending on cookie middleware implementation.
def issue_session(
    db: "OrmSession",
    admin: "Admin",
    *,
    ttl_seconds: int | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    settings: Settings | None = None,
) -> tuple[str, str, "AuthSession"]:
    """Create a DB session; return raw cookie/CSRF tokens only once to caller."""

    from .models import AuthSession, utc_now_ms

    settings = settings or get_settings()
    ttl = settings.session_ttl_seconds if ttl_seconds is None else ttl_seconds
    if ttl <= 0:
        raise ValueError("session TTL 必须为正数")
    session_token, csrf_token = new_session_tokens()
    now = utc_now_ms()
    record = AuthSession(
        admin_id=admin.id,
        token_hash=hash_token(session_token),
        csrf_token_hash=hash_token(csrf_token),
        created_at_ms=now,
        last_seen_at_ms=now,
        expires_at_ms=now + ttl * 1000,
        ip_hash=hashlib.sha256(ip_address.encode("utf-8")).hexdigest() if ip_address else None,
        user_agent=user_agent[:512] if user_agent else None,
    )
    db.add(record)
    db.flush()
    return session_token, csrf_token, record


def resolve_session(db: "OrmSession", session_token: str, *, touch: bool = False) -> "AuthSession | None":
    """Resolve a raw cookie token to an active admin session, failing closed."""

    from sqlalchemy import select

    from .models import Admin, AuthSession, utc_now_ms

    try:
        token_digest = hash_token(session_token)
    except InvalidSecret:
        return None
    now = utc_now_ms()
    record = db.scalar(
        select(AuthSession).where(
            AuthSession.token_hash == token_digest,
            AuthSession.revoked_at_ms.is_(None),
            AuthSession.expires_at_ms > now,
        )
    )
    if record is None:
        return None
    admin = db.get(Admin, record.admin_id)
    if admin is None or not admin.enabled:
        return None
    if touch:
        record.last_seen_at_ms = now
    return record


def validate_session_csrf(record: "AuthSession", method: str, supplied: str | None) -> bool:
    return csrf_is_safe(method, supplied, record.csrf_token_hash)


def revoke_session(db: "OrmSession", record: "AuthSession") -> None:
    from .models import utc_now_ms

    if record.revoked_at_ms is None:
        record.revoked_at_ms = utc_now_ms()
        db.flush()


def cookie_options(*, csrf: bool = False, settings: Settings | None = None) -> dict[str, object]:
    """FastAPI ``Response.set_cookie`` options for session/CSRF cookies."""

    settings = settings or get_settings()
    options: dict[str, object] = {
        "httponly": not csrf,
        "secure": settings.cookie_secure or settings.is_production,
        "samesite": settings.cookie_same_site,
        "path": "/",
    }
    if not csrf:
        options["max_age"] = settings.session_ttl_seconds
    return options


# Type-only imports avoid importing the ORM model graph until these helpers run.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as OrmSession

    from .models import Admin, AuthSession
else:
    OrmSession = object  # type: ignore[misc,assignment]
    Admin = object  # type: ignore[misc,assignment]
    AuthSession = object  # type: ignore[misc,assignment]

__all__.extend([
    "cookie_options", "issue_session", "resolve_session", "revoke_session", "validate_session_csrf",
])
