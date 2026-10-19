from __future__ import annotations

import contextlib
import secrets
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import OperationalError

from app.errors import AppError, not_found
from app.repositories import schema as s
from app.schemas import SettingsConfig
from app.timeutils import now_ms

SCHEMA_VERSION = "0006"


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(16)}"


def one(conn: Connection, table, resource_id: str, *, deleted=False) -> dict:
    row = conn.execute(select(table).where(table.c.id == resource_id)).mappings().first()
    if row is None or (not deleted and "deleted_at_ms" in row and row["deleted_at_ms"] is not None):
        raise not_found()
    return dict(row)


class Database:
    def __init__(self, url: str):
        parsed = make_url(url)
        if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
            raise ValueError("运行环境必须使用文件 SQLite")
        Path(parsed.database).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(parsed.database).resolve()
        self.engine = create_engine(
            url, connect_args={"check_same_thread": False, "timeout": 5}, pool_size=4, max_overflow=0
        )

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")

    @contextlib.contextmanager
    def read(self):
        with self.engine.connect() as conn:
            yield conn

    @contextlib.contextmanager
    def write(self):
        with self.engine.connect() as conn:
            try:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except OperationalError as exc:
                conn.rollback()
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise AppError(503, "DB_BUSY", "数据库繁忙，请稍后重试", retryable=True) from None
                raise
            except BaseException:
                conn.rollback()
                raise

    def migrate(self):
        from alembic import command
        from alembic.config import Config as AlembicConfig

        cfg = AlembicConfig()
        cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "migrations"))
        cfg.set_main_option(
            "sqlalchemy.url", self.engine.url.render_as_string(hide_password=False).replace("%", "%%")
        )
        with self.engine.connect() as conn:
            mode = conn.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()
            if mode != "wal":
                raise RuntimeError("SQLite WAL 启用失败")
            if sqlite3.sqlite_version_info < (3, 35, 0):
                raise RuntimeError("SQLite 版本至少为 3.35")
        command.upgrade(cfg, "head")
        with self.write() as conn:
            if conn.execute(select(s.settings.c.id)).first() is None:
                stamp = now_ms()
                conn.execute(
                    s.settings.insert().values(
                        id="singleton",
                        revision=1,
                        config_json=SettingsConfig().model_dump(),
                        created_at_ms=stamp,
                        updated_at_ms=stamp,
                    )
                )
            if (
                conn.execute(select(s.system_state.c.id).where(s.system_state.c.id == "projection")).first()
                is None
            ):
                conn.execute(
                    s.system_state.insert().values(
                        id="projection", value_json={"revision": 1}, updated_at_ms=now_ms()
                    )
                )

    def check_schema(self):
        with self.engine.connect() as conn:
            if conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() != SCHEMA_VERSION:
                raise RuntimeError("数据库迁移版本不一致，请先执行 migrate")

    def backup(self, destination: Path):
        destination = destination.resolve()
        if destination == self.path or destination.exists():
            raise ValueError("备份目标必须为新的独立文件")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as source, sqlite3.connect(destination) as target:
            source.backup(target)

    def close(self):
        self.engine.dispose()
