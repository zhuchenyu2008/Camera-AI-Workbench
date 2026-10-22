from app.health import dependencies, health_response, liveness, readiness


def test_liveness_is_independent_of_dependencies():
    report = liveness()
    assert report["status"] == "ok"
    assert report["checks"]["process"]["ok"] is True
    assert health_response(report, endpoint="live")[0] == 200


def test_readiness_requires_database_migration_disk_and_worker_configs(tmp_path):
    report = readiness(
        database=lambda: {"ok": True, "dialect": "sqlite"},
        data_dir=tmp_path,
        migrations_complete=True,
        worker_configured=True,
        scheduler_configured=True,
    )
    assert report["status"] == "ready"
    assert health_response(report, endpoint="ready")[0] == 200

    missing = readiness(
        database=lambda: False,
        data_dir=tmp_path,
        migrations_complete=True,
        worker_configured=True,
        scheduler_configured=True,
    )
    assert missing["status"] == "not_ready"
    assert health_response(missing, endpoint="ready")[0] == 503


def test_ai_and_camera_outages_are_dependency_degradation_not_readiness_failure(tmp_path):
    report = dependencies(
        database=lambda: {"ok": True},
        data_dir=tmp_path,
        cameras={"camera-1": {"state": "offline"}},
        ai={"primary": {"status": "unavailable"}},
    )
    assert report["status"] == "degraded"
    assert report["checks"]["cameras"]["camera-1"]["ok"] is False
    assert health_response(report, endpoint="dependencies")[0] == 200


def test_health_outputs_do_not_echo_database_error_details(tmp_path):
    report = readiness(
        database=lambda: {"ok": False, "detail": "sqlite:////private/password=secret"},
        data_dir=tmp_path,
        migrations_complete=True,
        worker_configured=True,
        scheduler_configured=True,
    )
    assert "private" not in str(report)
    assert "secret" not in str(report)


def test_health_functions_accept_positional_app_dependencies(tmp_path):
    report = readiness(lambda: True, tmp_path, True, True, True)
    assert report["status"] == "ready"
    report = dependencies(lambda: True, tmp_path, {"cam-1": {"status": "offline"}}, {"ai-1": {"status": "ready"}})
    assert report["status"] == "degraded"
    assert report["checks"]["cameras"]["cam-1"]["ok"] is False


def test_sqlite_connection_can_be_used_as_database_health_probe(tmp_path):
    import sqlite3

    connection = sqlite3.connect(tmp_path / "health.sqlite3")
    try:
        report = readiness(connection, tmp_path, True, True, True)
        assert report["checks"]["database"]["dialect"] == "sqlite"
    finally:
        connection.close()
