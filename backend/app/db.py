from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event, text
from sqlalchemy.engine import Connection, URL, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import Settings, get_settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy 2 declarative models."""

    pass


_ENGINE: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _is_sqlite(url: str | URL) -> bool:
    return make_url(url).get_backend_name() == "sqlite"


def _sqlite_database_path(url: str | URL) -> Path | None:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or parsed.database in {None, ":memory:"}:
        return None
    return Path(parsed.database).expanduser()


def _install_sqlite_pragmas(engine: Engine, settings: Settings) -> None:
    """Install connection-level safety/performance pragmas for SQLite."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={int(settings.sqlite_busy_timeout_ms)}")
            # WAL is unavailable for an in-memory database and unnecessary for
            # SQLite's shared-memory URI.  For file databases it is the design
            # baseline and is safe to request on every new connection.
            if settings.db_url not in {"sqlite:///:memory:", "sqlite://"}:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute(
                    f"PRAGMA wal_autocheckpoint={int(settings.sqlite_wal_autocheckpoint)}"
                )
            cursor.execute("PRAGMA temp_store=MEMORY")
        finally:
            cursor.close()


def create_engine_for_settings(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    url = settings.db_url
    kwargs: dict[str, Any] = {
        "echo": settings.db_echo,
        "future": True,
        "pool_pre_ping": True,
    }
    if _is_sqlite(url):
        is_memory = make_url(url).database in {None, ":memory:"}
        kwargs["connect_args"] = {
            "check_same_thread": False,
            "timeout": settings.sqlite_busy_timeout_ms / 1000,
        }
        if is_memory:
            kwargs["poolclass"] = StaticPool
        else:
            db_path = _sqlite_database_path(url)
            if db_path is not None and str(db_path) not in {":memory:", ""}:
                db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = __import__("sqlalchemy").create_engine(url, **kwargs)
    if _is_sqlite(url):
        _install_sqlite_pragmas(engine, settings)
    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    global _ENGINE
    if settings is None:
        settings = get_settings()
    if _ENGINE is None or str(_ENGINE.url) != str(settings.db_url):
        if _ENGINE is not None:
            _ENGINE.dispose()
        _ENGINE = create_engine_for_settings(settings)
    return _ENGINE


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _SessionLocal
    engine = get_engine(settings)
    if _SessionLocal is None or _SessionLocal.kw.get("bind") is not engine:
        _SessionLocal = sessionmaker(
            bind=engine,
            class_=Session,
            autoflush=False,
            expire_on_commit=False,
            autocommit=False,
        )
    return _SessionLocal


# These aliases are intentionally available for simple integrations and tests.
engine = get_engine()
SessionLocal = get_session_factory()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a short-lived SQLAlchemy Session."""

    db = get_session_factory()()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db_session() -> Session:
    """Create a session for non-FastAPI services; caller owns its lifecycle."""

    return get_session_factory()()


def create_all() -> None:
    """Development/test helper; production uses Alembic migrations."""

    # Import models so all mappers are registered before metadata creation.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=get_engine())


def check_database() -> dict[str, Any]:
    """Return a small, non-sensitive database readiness report."""

    db = get_db_session()
    try:
        value = db.execute(text("SELECT 1")).scalar_one()
        report: dict[str, Any] = {"ok": value == 1, "dialect": db.bind.dialect.name}
        if db.bind.dialect.name == "sqlite":
            report["journal_mode"] = db.execute(text("PRAGMA journal_mode")).scalar_one()
            report["foreign_keys"] = db.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            report["busy_timeout_ms"] = db.execute(text("PRAGMA busy_timeout")).scalar_one()
        return report
    finally:
        db.close()


def dispose_engine() -> None:
    global _ENGINE, _SessionLocal, engine, SessionLocal
    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _SessionLocal = None
    # Recreate the public aliases so tests changing APP_DB_URL do not retain a
    # closed engine.  Runtime code normally never needs to call this.
    engine = get_engine()
    SessionLocal = get_session_factory()
