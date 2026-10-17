from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select, update

from app.errors import AppError
from app.repositories import schema as s
from app.security.secrets import constant_equal, digest
from app.timeutils import iso

PASSWORDS = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


class Auth:
    def __init__(self, workbench):
        self.w = workbench

    def limit(self, scope: str, maximum: int, seconds: int = 60):
        now = self.w.now()
        window = now // (seconds * 1000) * seconds * 1000
        with self.w.db.write() as conn:
            row = (
                conn.execute(
                    select(s.rate_limits).where(
                        s.rate_limits.c.scope == scope, s.rate_limits.c.window_ms == window
                    )
                )
                .mappings()
                .first()
            )
            if row and row["count"] >= maximum:
                raise AppError(
                    429,
                    "RATE_LIMITED",
                    "操作过于频繁，请稍后重试",
                    details={"retry_after_seconds": max(1, (window + seconds * 1000 - now + 999) // 1000)},
                    retryable=True,
                )
            if row:
                conn.execute(
                    update(s.rate_limits)
                    .where(s.rate_limits.c.scope == scope, s.rate_limits.c.window_ms == window)
                    .values(count=row["count"] + 1)
                )
            else:
                conn.execute(s.rate_limits.insert().values(scope=scope, window_ms=window, count=1))
            conn.execute(delete(s.rate_limits).where(s.rate_limits.c.window_ms < now - 86400000))

    def create_admin(self, username: str, password: str):
        if not 1 <= len(username) <= 64 or not 12 <= len(password) <= 256:
            raise ValueError("用户名需 1～64 字，密码需 12～256 字")
        encoded = PASSWORDS.hash(password)
        with self.w.db.write() as conn:
            if conn.execute(select(s.users.c.id)).first():
                raise ValueError("管理员已存在，不允许注册第二个账户")
            conn.execute(
                s.users.insert().values(
                    id="admin", username=username, password_hash=encoded, password_changed_at_ms=self.w.now()
                )
            )

    def login(self, username: str, password: str, ip: str) -> tuple[str, dict]:
        self.limit(f"login-ip:{digest(ip)}", 10, 300)
        self.limit(f"login-user:{digest(username)}", 10, 300)
        with self.w.db.read() as conn:
            user = conn.execute(select(s.users).where(s.users.c.username == username)).mappings().first()
        # 未找到用户时仍计算密码哈希，避免明显的账户枚举时序差异。
        good = False
        try:
            if user:
                good = PASSWORDS.verify(user["password_hash"], password)
            else:
                PASSWORDS.hash(password)
        except (VerificationError, InvalidHashError):
            pass
        if not good:
            raise AppError(401, "INVALID_CREDENTIALS", "账户或密码错误")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self.w.now()
        expiry = now + self.w.config.session_seconds * 1000
        with self.w.db.write() as conn:
            current = conn.execute(select(s.users).where(s.users.c.id == user["id"])).mappings().one()
            if current["password_hash"] != user["password_hash"]:
                raise AppError(401, "INVALID_CREDENTIALS", "账户或密码已变化")
            conn.execute(
                s.sessions.insert().values(
                    id_hash=digest(token),
                    user_id=user["id"],
                    csrf_hash=digest(csrf),
                    csrf_cipher=self.w.secrets.encrypt(csrf),
                    created_at_ms=now,
                    last_seen_at_ms=now,
                    expires_at_ms=expiry,
                )
            )
        return token, {
            "username": username,
            "display_name": username,
            "csrf_token": csrf,
            "expires_at": iso(expiry),
        }

    def authenticate(self, token: str | None, csrf: str | None = None, write=False) -> dict:
        if not token:
            raise AppError(401, "AUTH_REQUIRED", "请先登录")
        with self.w.db.read() as conn:
            row = (
                conn.execute(
                    select(s.sessions, s.users.c.username)
                    .join(s.users, s.sessions.c.user_id == s.users.c.id)
                    .where(s.sessions.c.id_hash == digest(token))
                )
                .mappings()
                .first()
            )
        if row is None or row["revoked_at_ms"] is not None or row["expires_at_ms"] <= self.w.now():
            raise AppError(401, "AUTH_REQUIRED", "会话已失效，请重新登录")
        if write and (not csrf or not constant_equal(row["csrf_hash"], digest(csrf))):
            raise AppError(403, "CSRF_INVALID", "CSRF 凭证无效")
        return dict(row)

    def session(self, row: dict) -> dict:
        return {
            "username": row["username"],
            "display_name": row["username"],
            "csrf_token": self.w.secrets.decrypt(row["csrf_cipher"]),
            "expires_at": iso(row["expires_at_ms"]),
        }

    def logout(self, session: dict):
        with self.w.db.write() as conn:
            conn.execute(
                update(s.sessions)
                .where(s.sessions.c.id_hash == session["id_hash"])
                .values(revoked_at_ms=self.w.now())
            )

    def password(self, session: dict, current: str, new: str):
        with self.w.db.read() as conn:
            user = conn.execute(select(s.users).where(s.users.c.id == session["user_id"])).mappings().one()
        try:
            PASSWORDS.verify(user["password_hash"], current)
        except (VerificationError, InvalidHashError):
            raise AppError(401, "INVALID_CREDENTIALS", "当前密码错误") from None
        encoded = PASSWORDS.hash(new)
        with self.w.db.write() as conn:
            changed = conn.execute(
                update(s.users)
                .where(s.users.c.id == session["user_id"], s.users.c.password_hash == user["password_hash"])
                .values(password_hash=encoded, password_changed_at_ms=self.w.now())
            ).rowcount
            if changed != 1:
                raise AppError(412, "REVISION_CONFLICT", "密码已由其他会话修改")
            conn.execute(
                update(s.sessions)
                .where(s.sessions.c.user_id == session["user_id"])
                .values(revoked_at_ms=self.w.now())
            )
