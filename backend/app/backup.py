"""Consistent SQLite online backups and optional data-volume bundles.

SQLite databases are copied with SQLite's online backup API, so committed WAL
content is included consistently. No API routes or ORM models are defined here.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from .timeutils import utc_now_ms, utc_rfc3339


class BackupError(RuntimeError):
    """Backup input, output, or integrity validation failed."""


@dataclass(frozen=True, slots=True)
class SQLiteBackupResult:
    source: str
    destination: Path
    size_bytes: int
    integrity: str
    created_at_ms: int

    def __fspath__(self) -> str:
        return os.fspath(self.destination)


@dataclass(frozen=True, slots=True)
class BackupBundleResult:
    destination: Path
    size_bytes: int
    database_integrity: str
    included_images: bool
    image_file_count: int
    config_file_count: int
    created_at_ms: int

    def __fspath__(self) -> str:
        return os.fspath(self.destination)


def _is_sqlite_engine(value: Any) -> bool:
    dialect = getattr(value, "dialect", None)
    return getattr(dialect, "name", None) == "sqlite" and callable(getattr(value, "raw_connection", None))


def _source_path(source: str | os.PathLike[str] | Any) -> Path:
    if isinstance(source, os.PathLike):
        return Path(source).expanduser().resolve()
    if not isinstance(source, str):
        raise BackupError("SQLite source must be a file path, URL, Connection, or Engine")
    value = source
    if value.startswith("sqlite://"):
        try:
            from sqlalchemy.engine import make_url

            parsed = make_url(value)
            if parsed.get_backend_name() != "sqlite" or parsed.database in {None, ":memory:"}:
                raise BackupError("Only file-backed SQLite URLs can be backed up by path")
            path_value = parsed.database
        except BackupError:
            raise
        except Exception as exc:
            raise BackupError("Invalid SQLite URL") from exc
    elif value.startswith("sqlite:"):
        raise BackupError("Invalid SQLite URL")
    else:
        path_value = value
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise BackupError("SQLite source file does not exist")
    return path


def _connection_source_path(connection: sqlite3.Connection) -> Path | None:
    """Return the main database path for a live SQLite connection, if file-backed."""

    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        name = row[1] if len(row) > 1 else None
        path = row[2] if len(row) > 2 else None
        if name == "main" and path:
            return Path(str(path)).expanduser().resolve()
    return None


def _known_source_path(source: Any) -> Path | None:
    """Resolve a file-backed source when its path is available without opening it."""

    if isinstance(source, sqlite3.Connection):
        return _connection_source_path(source)
    if isinstance(source, (str, os.PathLike)):
        return _source_path(source)
    if _is_sqlite_engine(source):
        url = getattr(source, "url", None)
        if url is not None:
            try:
                from sqlalchemy.engine import make_url

                parsed = make_url(str(url))
            except ImportError:
                return _source_path(str(url))
            if parsed.database in {None, "", ":memory:"}:
                return None
            return _source_path(str(url))
    return None


def _paths_alias(first: Path, second: Path) -> bool:
    """Detect both resolved-path equality and existing hard-link aliases."""

    if first == second:
        return True
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


@contextmanager
def _open_source(source: Any) -> Iterator[tuple[sqlite3.Connection, str, Any | None]]:
    """Yield (sqlite connection, safe source label, cleanup callback)."""

    if isinstance(source, sqlite3.Connection):
        yield source, "sqlite-connection", None
        return
    if _is_sqlite_engine(source):
        raw = source.raw_connection()
        connection = getattr(raw, "driver_connection", None) or getattr(raw, "connection", None)
        if not isinstance(connection, sqlite3.Connection):
            raw.close()
            raise BackupError("SQLAlchemy engine did not provide a SQLite connection")
        try:
            yield connection, "sqlite-engine", raw.close
        finally:
            raw.close()
        return
    path = _source_path(source)
    uri = path.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=30.0)
    except sqlite3.Error as exc:
        raise BackupError(f"Unable to open SQLite source ({type(exc).__name__})") from exc
    try:
        yield connection, "sqlite-file", connection.close
    finally:
        connection.close()


def _integrity_check(connection: sqlite3.Connection) -> str:
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite integrity check failed ({type(exc).__name__})") from exc
    result = str(row[0]) if row else "no_result"
    if result.lower() != "ok":
        raise BackupError(f"SQLite backup failed integrity check: {result[:300]}")
    return result


def backup_sqlite(
    source: str | os.PathLike[str] | sqlite3.Connection | Any,
    destination: str | os.PathLike[str],
    *,
    pages: int = 256,
    sleep: float = 0.05,
    progress: Callable[[int, int, int], None] | None = None,
    overwrite: bool = False,
) -> SQLiteBackupResult:
    """Create a consistent standalone SQLite snapshot using ``Connection.backup``.

    The snapshot is first written to an exclusive temporary file in the destination
    directory, checked with ``PRAGMA integrity_check``, fsynced, and atomically
    renamed. Existing output is protected unless ``overwrite=True`` is explicit.
    """

    if isinstance(pages, bool) or not isinstance(pages, int) or pages <= 0:
        raise ValueError("pages must be a positive integer")
    if isinstance(sleep, bool) or not isinstance(sleep, (int, float)) or not math.isfinite(float(sleep)) or sleep < 0:
        raise ValueError("sleep must be a non-negative finite number")
    target = Path(destination).expanduser().resolve()
    source_path = _known_source_path(source)
    if source_path is not None and _paths_alias(source_path, target):
        raise BackupError("Source and destination must be different files")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.is_dir():
        raise BackupError("Destination must be a file")
    if target.exists() and not overwrite:
        raise BackupError("Destination already exists; pass overwrite=True to replace it")

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    created_at = utc_now_ms()
    source_label = "sqlite-connection"
    try:
        with _open_source(source) as (source_connection, source_label, _cleanup):
            destination_connection = sqlite3.connect(temporary, timeout=30.0)
            try:
                source_connection.backup(
                    destination_connection,
                    pages=pages,
                    progress=progress,
                    sleep=sleep,
                )
                destination_connection.commit()
                integrity = _integrity_check(destination_connection)
            finally:
                destination_connection.close()

        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        if target.exists() and not overwrite:
            raise BackupError("Destination was created concurrently")
        os.replace(temporary, target)
        _fsync_directory(target.parent)
        return SQLiteBackupResult(
            source=source_label,
            destination=target,
            size_bytes=target.stat().st_size,
            integrity=integrity,
            created_at_ms=created_at,
        )
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f"SQLite backup failed ({type(exc).__name__})") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def verify_sqlite_backup(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Run an integrity check and return non-sensitive database metadata."""

    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise BackupError("Backup file does not exist")
    uri = candidate.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=10.0)
        try:
            integrity = _integrity_check(connection)
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            table_count = int(
                connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            )
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise BackupError(f"Unable to verify SQLite backup ({type(exc).__name__})") from exc
    return {
        "ok": True,
        "integrity": integrity,
        "journal_mode": journal_mode,
        "table_count": table_count,
        "page_count": page_count,
        "size_bytes": candidate.stat().st_size,
    }


def _copy_image_tree(root: Path, staging: Path, archive_prefix: str) -> tuple[list[tuple[Path, str]], int, int]:
    if not root.exists():
        return [], 0, 0
    resolved_root = root.resolve()
    result: list[tuple[Path, str]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.name.endswith(".part"):
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(resolved_root)
        except ValueError:
            continue
        archive_name = (PurePosixPath(archive_prefix) / PurePosixPath(relative.as_posix())).as_posix()
        result.append((path, archive_name))
        total_bytes += path.stat().st_size
    return result, len(result), total_bytes


def create_backup_bundle(
    database: str | os.PathLike[str] | sqlite3.Connection | Any,
    destination: str | os.PathLike[str],
    *,
    data_dir: str | os.PathLike[str] | None = None,
    include_images: bool = False,
    config_paths: tuple[str | os.PathLike[str], ...] | list[str | os.PathLike[str]] = (),
    overwrite: bool = False,
    progress: Callable[[int, int, int], None] | None = None,
) -> BackupBundleResult:
    """Create a protected ZIP bundle with a SQLite snapshot and optional volumes.

    ``config_paths`` is explicit by design: the function never crawls a user's
    machine for environment files or secrets. Configuration files are copied under
    numbered archive names, and the output is created with owner-only permissions
    where the platform supports them.
    """

    target = Path(destination).expanduser().resolve()
    source_path = _known_source_path(database)
    if source_path is not None and _paths_alias(source_path, target):
        raise BackupError("Database source and bundle destination must be different files")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.is_dir():
        raise BackupError("Destination must be a file")
    if target.exists() and not overwrite:
        raise BackupError("Destination already exists; pass overwrite=True to replace it")

    config_files: list[tuple[Path, str]] = []
    for index, raw_path in enumerate(config_paths):
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise BackupError("A requested configuration file does not exist")
        if _paths_alias(path, target):
            raise BackupError("Configuration source and bundle destination must be different files")
        config_files.append((path, f"config/{index:03d}-{path.name}"))

    image_files: list[tuple[Path, str]] = []
    image_count = 0
    image_bytes = 0
    if include_images:
        if data_dir is None:
            raise BackupError("data_dir is required when include_images=True")
        data_root = Path(data_dir).expanduser().resolve()
        for volume_name in ("frames", "thumbs"):
            files, count, size = _copy_image_tree(data_root / volume_name, target.parent, f"images/{volume_name}")
            image_files.extend(files)
            image_count += count
            image_bytes += size
        if any(_paths_alias(source_path.resolve(), target) for source_path, _ in image_files):
            raise BackupError("Image source and bundle destination must be different files")

    created_at = utc_now_ms()
    with tempfile.TemporaryDirectory(prefix=".caw-backup-", dir=target.parent) as temp_name:
        staging = Path(temp_name)
        database_snapshot = staging / "app.sqlite3"
        db_result = backup_sqlite(database, database_snapshot, progress=progress)
        manifest = {
            "format": "camera-ai-workbench-backup",
            "format_version": 1,
            "created_at": utc_rfc3339(created_at),
            "created_at_ms": created_at,
            "database": "database/app.sqlite3",
            "database_integrity": db_result.integrity,
            "configuration_files": [archive_name for _, archive_name in config_files],
            "images_included": bool(include_images),
            "image_file_count": image_count,
            "image_bytes": image_bytes,
            "contains_configuration_secrets": bool(config_files),
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_archive = staging / "bundle.zip"
        try:
            with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.write(database_snapshot, "database/app.sqlite3")
                archive.write(manifest_path, "manifest.json")
                for source_path, archive_name in config_files:
                    archive.write(source_path, archive_name)
                for source_path, archive_name in image_files:
                    archive.write(source_path, archive_name)
            _verify_bundle_file(temporary_archive, verify_database=True)
            with temporary_archive.open("rb+") as handle:
                os.fsync(handle.fileno())
            if target.exists() and not overwrite:
                raise BackupError("Destination was created concurrently")
            try:
                os.chmod(temporary_archive, 0o600)
            except OSError:
                pass
            os.replace(temporary_archive, target)
            _fsync_directory(target.parent)
        except BackupError:
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            raise BackupError(f"Backup bundle creation failed ({type(exc).__name__})") from exc

    return BackupBundleResult(
        destination=target,
        size_bytes=target.stat().st_size,
        database_integrity=db_result.integrity,
        included_images=bool(include_images),
        image_file_count=image_count,
        config_file_count=len(config_files),
        created_at_ms=created_at,
    )


def _safe_archive_member(name: str) -> bool:
    if not name or "\\" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _verify_bundle_file(
    archive_path: Path,
    *,
    verify_database: bool,
    max_uncompressed_bytes: int = 100 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            names: set[str] = set()
            total_size = 0
            for info in infos:
                if not _safe_archive_member(info.filename):
                    raise BackupError("Backup bundle contains an unsafe archive path")
                if info.filename in names:
                    raise BackupError("Backup bundle contains duplicate entries")
                names.add(info.filename)
                total_size += info.file_size
                if total_size > max_uncompressed_bytes:
                    raise BackupError("Backup bundle exceeds verification size limit")
            corrupt = archive.testzip()
            if corrupt:
                raise BackupError("Backup bundle checksum validation failed")
            if "manifest.json" not in names or "database/app.sqlite3" not in names:
                raise BackupError("Backup bundle is missing its manifest or database")
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > 1_000_000:
                raise BackupError("Backup manifest is too large")
            manifest_raw = archive.read("manifest.json")
            if len(manifest_raw) > 1_000_000:
                raise BackupError("Backup manifest is too large")
            manifest = json.loads(manifest_raw.decode("utf-8"))
            if manifest.get("format") != "camera-ai-workbench-backup" or manifest.get("format_version") != 1:
                raise BackupError("Unsupported backup bundle format")
            database_integrity = None
            if verify_database:
                with tempfile.TemporaryDirectory(prefix="caw-verify-") as temp_name:
                    database_path = Path(temp_name) / "app.sqlite3"
                    with archive.open("database/app.sqlite3", "r") as source, database_path.open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    database_integrity = verify_sqlite_backup(database_path)["integrity"]
            return {
                "ok": True,
                "format": manifest["format"],
                "format_version": manifest["format_version"],
                "created_at_ms": manifest.get("created_at_ms"),
                "images_included": bool(manifest.get("images_included", False)),
                "image_file_count": int(manifest.get("image_file_count", 0)),
                "database_integrity": database_integrity or manifest.get("database_integrity"),
                "entry_count": len(infos),
                "uncompressed_bytes": total_size,
            }
    except BackupError:
        raise
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError) as exc:
        raise BackupError(f"Backup bundle verification failed ({type(exc).__name__})") from exc


def verify_backup_bundle(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Validate archive safety, checksums, manifest version, and SQLite integrity."""

    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise BackupError("Backup bundle does not exist")
    return _verify_bundle_file(candidate, verify_database=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


# Descriptive aliases used by CLI and application integrations.
create_sqlite_backup = backup_sqlite
backup_database = backup_sqlite
create_backup = create_backup_bundle
verify_bundle = verify_backup_bundle


__all__ = [
    "BackupBundleResult",
    "BackupError",
    "SQLiteBackupResult",
    "backup_database",
    "backup_sqlite",
    "create_backup",
    "create_backup_bundle",
    "create_sqlite_backup",
    "verify_backup_bundle",
    "verify_bundle",
    "verify_sqlite_backup",
]
