from __future__ import annotations

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.repositories import schema as s
from app.repositories.db import new_id
from tests.conftest import make_camera


def test_B04_database_checks_and_deferred_current_revision(w):
    camera = make_camera(w)
    minute = new_id("min")
    with w.db.write() as conn:
        conn.execute(
            s.minute_records.insert().values(
                id=minute,
                camera_id=camera,
                start_at_ms=w.now(),
                current_revision=1,
                analysis_state="complete",
                created_at_ms=w.now(),
            )
        )
        # 在同一事务中后写入版本，提交时才校验指针。
        conn.execute(
            s.minute_revisions.insert().values(
                minute_id=minute,
                revision=1,
                source_kind="ai",
                source_fingerprint="deferred",
                reference_set_id=None,
                result_json={},
                created_at_ms=w.now(),
            )
        )
    with pytest.raises(IntegrityError):
        with w.db.write() as conn:
            conn.execute(
                update(s.minute_records).where(s.minute_records.c.id == minute).values(current_revision=999)
            )
    with pytest.raises(IntegrityError):
        with w.db.write() as conn:
            conn.exec_driver_sql("UPDATE settings SET config_json = 'not-json'")
    with pytest.raises(IntegrityError):
        with w.db.write() as conn:
            conn.exec_driver_sql("UPDATE cameras SET enabled = 7")
    with w.db.read() as conn:
        assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        assert (
            conn.execute(
                select(s.minute_records.c.current_revision).where(s.minute_records.c.id == minute)
            ).scalar_one()
            == 1
        )
