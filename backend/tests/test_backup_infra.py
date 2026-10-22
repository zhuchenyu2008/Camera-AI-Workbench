import sqlite3
import zipfile

import pytest

from app.backup import (
    BackupError,
    backup_sqlite,
    create_backup_bundle,
    verify_backup_bundle,
    verify_sqlite_backup,
)


def _database(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO sample(value) VALUES ('one'), ('two')")
    connection.commit()
    connection.close()


def test_online_backup_api_produces_integral_sqlite_snapshot(tmp_path):
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "backup.sqlite3"
    _database(source)
    result = backup_sqlite(source, target)
    assert result.destination == target
    assert result.integrity == "ok"
    assert verify_sqlite_backup(target)["table_count"] == 1
    with pytest.raises(BackupError):
        backup_sqlite(source, target)


def test_bundle_can_optionally_include_images_and_verify(tmp_path):
    data = tmp_path / "data"
    (data / "frames" / "cam").mkdir(parents=True)
    (data / "thumbs").mkdir()
    (data / "frames" / "cam" / "frame.jpg").write_bytes(b"frame")
    (data / "thumbs" / "thumb.jpg").write_bytes(b"thumb")
    database = tmp_path / "source.sqlite3"
    _database(database)
    config = tmp_path / "settings.env"
    config.write_text("APP_ENV=test\n", encoding="utf-8")

    bundle_path = tmp_path / "bundle.zip"
    result = create_backup_bundle(
        database,
        bundle_path,
        data_dir=data,
        include_images=True,
        config_paths=[config],
    )
    assert result.image_file_count == 2
    verified = verify_backup_bundle(bundle_path)
    assert verified["ok"] is True
    assert verified["database_integrity"] == "ok"
    with zipfile.ZipFile(bundle_path) as archive:
        assert "images/frames/cam/frame.jpg" in archive.namelist()
        assert "images/thumbs/thumb.jpg" in archive.namelist()
        assert any(name.startswith("config/") for name in archive.namelist())


def test_bundle_without_images_does_not_include_volume_files(tmp_path):
    data = tmp_path / "data"
    (data / "frames").mkdir(parents=True)
    (data / "frames" / "frame.jpg").write_bytes(b"private-image")
    database = tmp_path / "source.sqlite3"
    _database(database)
    bundle = tmp_path / "metadata-only.zip"
    create_backup_bundle(database, bundle, data_dir=data, include_images=False)
    with zipfile.ZipFile(bundle) as archive:
        assert all(not name.startswith("images/") for name in archive.namelist())
        assert b"private-image" not in b"".join(archive.read(name) for name in archive.namelist())


def test_backup_rejects_bundle_output_collisions(tmp_path):
    database = tmp_path / "source.sqlite3"
    _database(database)
    with pytest.raises(BackupError):
        backup_sqlite(database, database, overwrite=True)
    with pytest.raises(BackupError):
        create_backup_bundle(database, database, overwrite=True)


def test_backup_bundle_refuses_to_replace_explicit_config_source(tmp_path):
    database = tmp_path / "source.sqlite3"
    _database(database)
    config = tmp_path / "settings.env"
    config.write_text("APP_ENV=test\n", encoding="utf-8")
    original = config.read_bytes()
    with pytest.raises(BackupError):
        create_backup_bundle(database, config, config_paths=[config], overwrite=True)
    assert config.read_bytes() == original


def test_backup_sqlalchemy_memory_engine_uses_online_backup_api(tmp_path):
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sample (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO sample(id) VALUES (1)"))
    target = tmp_path / "memory-snapshot.sqlite3"
    result = backup_sqlite(engine, target)
    assert result.integrity == "ok"
    assert verify_sqlite_backup(target)["table_count"] == 1
    engine.dispose()
