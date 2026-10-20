from sqlalchemy import inspect

from app.repositories import schema as s


def test_temporal_schema_is_migrated_and_linked(w):
    expected = {
        "temporal_slices",
        "local_observation_runs",
        "temporal_review_runs",
        "temporal_checkpoints",
        "published_timelines",
    }
    with w.db.read() as conn:
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0007"
        inspector = inspect(conn)
        assert expected <= set(inspector.get_table_names())

        assert {fk["referred_table"] for fk in inspector.get_foreign_keys("temporal_slices")} >= {
            "cameras",
            "stream_sessions",
        }
        assert {fk["referred_table"] for fk in inspector.get_foreign_keys("local_observation_runs")} >= {
            "temporal_slices",
            "jobs",
            "providers",
        }
        assert {fk["referred_table"] for fk in inspector.get_foreign_keys("temporal_checkpoints")} >= {
            "cameras",
            "temporal_review_runs",
        }
        assert {fk["referred_table"] for fk in inspector.get_foreign_keys("temporal_review_runs")} >= {
            "cameras",
            "jobs",
            "temporal_checkpoints",
        }
        assert {fk["referred_table"] for fk in inspector.get_foreign_keys("published_timelines")} >= {
            "cameras",
            "temporal_checkpoints",
        }

        assert "temporal_published_timelines_current" in {
            index["name"] for index in inspector.get_indexes("published_timelines")
        }
        assert s.temporal_slices.c.end_at_ms.type.python_type is int
