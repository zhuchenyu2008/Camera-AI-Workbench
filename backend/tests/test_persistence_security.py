from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from app.config import Settings
from app.db import Base, create_engine_for_settings
from app.models import Admin, AuthSession
from app.security import (
    DecryptionError,
    csrf_is_safe,
    decrypt_secret,
    encrypt_secret,
    hash_password,
    issue_session,
    resolve_session,
    verify_password,
)


def test_sqlite_database_has_wal_fk_and_busy_timeout(tmp_path):
    db_path = tmp_path / "metadata.sqlite3"
    settings = Settings(db_url=f"sqlite:///{db_path.as_posix()}", sqlite_busy_timeout_ms=4321)
    engine = create_engine_for_settings(settings)
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() == 4321
        assert {"camera", "frame", "ai_job", "activity_segment", "session"}.issubset(
            set(inspect(engine).get_table_names())
        )
    finally:
        engine.dispose()


def test_master_key_envelope_authenticates_ciphertext():
    key = b"K" * 32
    envelope = encrypt_secret("rtsp://user:password@camera", key=key, aad="camera:1")
    assert decrypt_secret(envelope, key=key, aad="camera:1") == b"rtsp://user:password@camera"
    with pytest.raises(DecryptionError):
        decrypt_secret(envelope, key=key, aad="camera:2")
    tampered = bytearray(envelope)
    tampered[-1] ^= 1
    with pytest.raises(DecryptionError):
        decrypt_secret(tampered, key=key, aad="camera:1")


def test_argon2id_and_database_session_csrf():
    from sqlalchemy.orm import Session, sessionmaker
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, class_=Session, expire_on_commit=False)
    password_hash = hash_password("a long test password")
    assert password_hash.startswith("$argon2id$")
    assert verify_password(password_hash, "a long test password")
    assert not verify_password(password_hash, "incorrect password")

    with factory() as db:
        admin = Admin(username="admin", password_hash=password_hash)
        db.add(admin)
        db.flush()
        settings = Settings(session_ttl_seconds=60)
        token, csrf, record = issue_session(db, admin, settings=settings)
        db.commit()
        assert isinstance(record, AuthSession)
        assert resolve_session(db, token) is not None
        assert csrf_is_safe("POST", csrf, record.csrf_token_hash)
        assert not csrf_is_safe("POST", "wrong", record.csrf_token_hash)
        assert csrf_is_safe("GET", None, record.csrf_token_hash)
    engine.dispose()
