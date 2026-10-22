"""Command line entry points for API, worker, scheduler, capture and backups.

This file is intentionally an infrastructure dispatcher: it does not declare API
routes or database models. Worker/capture implementations are loaded lazily so those
components can evolve independently of the CLI contract.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import os
import sys
from typing import Any, Callable, Sequence

from .backup import create_backup_bundle, verify_backup_bundle
from .config import get_settings
from .observability import configure_logging, sanitize_string
from .security import load_master_key


class CLIError(RuntimeError):
    """A safe, user-displayable command-line configuration error."""


_RUNNER_CANDIDATES: dict[str, tuple[tuple[str, str], ...]] = {
    "worker": (
        ("app.worker_main", "run_worker"),
        ("app.worker_main", "worker_main"),
        ("app.worker", "run_worker"),
        ("app.worker", "main"),
        ("app.worker_main", "main"),
    ),
    "scheduler": (
        ("app.worker_main", "run_scheduler"),
        ("app.worker_main", "scheduler_main"),
        ("app.scheduler", "run_scheduler"),
        ("app.scheduler", "main"),
        ("app.worker_main", "main"),
    ),
    "capture": (
        ("app.worker_main", "run_capture"),
        ("app.worker_main", "capture_main"),
        ("app.capture.manager", "run_capture"),
        ("app.capture.manager", "main"),
        ("app.capture.runner", "run_capture"),
        ("app.capture.runner", "main"),
        ("app.capture", "run_capture"),
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="camera-ai-workbench",
        description="Camera AI Workbench backend service commands",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="start the FastAPI application")
    serve_parser.add_argument("--host", default=None, help="bind host (defaults to API_BIND)")
    serve_parser.add_argument("--port", type=int, default=None, help="bind port (defaults to API_BIND)")
    serve_parser.add_argument("--reload", action="store_true", help="enable development auto-reload")

    for mode, help_text in (
        ("worker", "run the persistent AI job worker"),
        ("scheduler", "run the window and retention scheduler"),
        ("capture", "run the camera capture service"),
    ):
        mode_parser = subparsers.add_parser(mode, help=help_text)
        mode_parser.add_argument(
            "runner_args",
            nargs=argparse.REMAINDER,
            help="arguments passed to the lazily loaded service runner",
        )

    backup_parser = subparsers.add_parser("backup", help="create a consistent SQLite backup bundle")
    backup_parser.add_argument("--database", default=None, help="SQLite path/URL (defaults to APP_DB_URL)")
    backup_parser.add_argument("--data-dir", default=None, help="application data root (defaults to APP_DATA_DIR)")
    backup_parser.add_argument("--output", required=True, help="destination .zip file")
    backup_parser.add_argument("--include-images", action="store_true", help="include frames and thumbnails")
    backup_parser.add_argument("--config", action="append", default=[], help="explicit config file to include; repeatable")
    backup_parser.add_argument("--overwrite", action="store_true", help="replace the output file if it exists")

    init_parser = subparsers.add_parser("init", help="create runtime directories and the local master key")
    init_parser.add_argument("--with-password", action="store_true", help="create a random bootstrap password file when absent")
    subparsers.add_parser("migrate", help="create/apply the current database schema")
    subparsers.add_parser("check-config", help="validate configuration and storage paths")

    verify_parser = subparsers.add_parser("verify-backup", help="verify a backup bundle and its SQLite snapshot")
    verify_parser.add_argument("path", help="backup .zip file")
    return parser


def _api_bind() -> tuple[str, int]:
    value = os.getenv("API_BIND", os.getenv("APP_API_BIND", "0.0.0.0:8000")).strip()
    if value.startswith("[") and "]" in value:
        host, separator, port_text = value[1:].partition("]:")
        if not separator:
            raise CLIError("API_BIND IPv6 格式无效")
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator:
            host, port_text = value, "8000"
    try:
        port = int(port_text)
    except ValueError as exc:
        raise CLIError("API_BIND 端口无效") from exc
    if not 1 <= port <= 65_535:
        raise CLIError("API_BIND 端口必须在 1 到 65535 之间")
    return host or "0.0.0.0", port


def run_serve(*, host: str | None = None, port: int | None = None, reload: bool = False) -> int:
    """Start Uvicorn against the app's existing ``app.main:app`` export."""

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise CLIError("缺少 uvicorn，请安装后端依赖") from exc
    configured_host, configured_port = _api_bind()
    resolved_port = configured_port if port is None else port
    if not 1 <= resolved_port <= 65_535:
        raise CLIError("端口必须在 1 到 65535 之间")
    uvicorn.run(
        "app.main:app",
        host=host or configured_host,
        port=resolved_port,
        reload=reload,
        log_config=None,
    )
    return 0


def _load_entrypoint(value: str) -> Callable[..., Any]:
    module_name, separator, attribute_name = value.partition(":")
    if not separator or not module_name or not attribute_name:
        raise CLIError("runner entrypoint 必须使用 module:function 格式")
    try:
        module = importlib.import_module(module_name)
        runner = getattr(module, attribute_name)
    except (ImportError, AttributeError) as exc:
        raise CLIError(f"无法加载服务 runner: {sanitize_string(value)}") from exc
    if not callable(runner):
        raise CLIError(f"服务 runner 不可调用: {sanitize_string(value)}")
    return runner


def _find_runner(mode: str) -> tuple[Callable[..., Any], str]:
    override = os.getenv(f"APP_{mode.upper()}_ENTRYPOINT", "").strip()
    if override:
        return _load_entrypoint(override), override

    import_errors: list[str] = []
    for module_name, attribute_name in _RUNNER_CANDIDATES[mode]:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # Only ignore absence of the candidate module; surface a missing
            # dependency imported by an existing service as a real startup error.
            if exc.name == module_name or module_name.startswith(f"{exc.name}."):
                import_errors.append(module_name)
                continue
            raise CLIError(f"服务模块依赖未安装: {sanitize_string(str(exc))}") from exc
        runner = getattr(module, attribute_name, None)
        if callable(runner):
            return runner, f"{module_name}:{attribute_name}"
    tried = ", ".join(dict.fromkeys(import_errors)) or "configured entrypoints"
    raise CLIError(
        f"{mode} 命令已注册，但未找到运行实现。请提供 {mode} 服务模块或设置 "
        f"APP_{mode.upper()}_ENTRYPOINT=module:function（已检查: {tried}）"
    )


def _invoke_runner(runner: Callable[..., Any], mode: str, runner_args: Sequence[str]) -> Any:
    """Invoke mode-specific runners or a generic ``main(argv=None)`` function."""

    try:
        signature = inspect.signature(runner)
        parameters = list(signature.parameters.values())
    except (TypeError, ValueError):
        parameters = []

    has_varargs = any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters)
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if not parameters or (not positional and not has_varargs):
        # Legacy service ``main()`` functions read sys.argv directly.
        original_argv = sys.argv
        sys.argv = [original_argv[0], mode, *runner_args]
        try:
            return runner()
        finally:
            sys.argv = original_argv
    first_name = positional[0].name if positional else "argv"
    if first_name in {"mode", "command", "service"}:
        return runner(mode, *runner_args)
    return runner([mode, *runner_args])


def run_service(mode: str, runner_args: Sequence[str] = ()) -> int:
    """Dispatch a background service without importing routes or ORM models."""

    if mode not in _RUNNER_CANDIDATES:
        raise CLIError(f"不支持的服务模式: {mode}")
    runner, _ = _find_runner(mode)
    configure_logging(service=mode, level=os.getenv("LOG_LEVEL", "INFO"))
    result = _invoke_runner(runner, mode, runner_args)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    if result is None:
        return 0
    if isinstance(result, bool):
        return int(result)
    if isinstance(result, int):
        return result
    raise CLIError(f"{mode} runner must return an integer exit code or None")


def _run_init(with_password: bool = False) -> int:
    settings = get_settings()
    settings.ensure_runtime_directories()
    load_master_key(settings, create=True)
    if with_password and settings.bootstrap_password is None and not settings.bootstrap_password_file.exists():
        import secrets

        settings.bootstrap_password_file.write_text(secrets.token_urlsafe(18), encoding="utf-8")
        try:
            settings.bootstrap_password_file.chmod(0o600)
        except OSError:
            pass
    print(f"initialized: {settings.data_dir}")
    return 0


def _run_migrate() -> int:
    from app.main import AppConfig, prepare_database
    config = AppConfig.from_env()
    prepare_database(config)
    print("database migrated")
    return 0


def _run_check_config() -> int:
    settings = get_settings()
    settings.ensure_runtime_directories()
    if settings.is_production:
        load_master_key(settings, create=False)
    print(f"config ok: env={settings.app_env} data_dir={settings.data_dir}")
    return 0

def _run_backup(args: argparse.Namespace) -> int:
    database = args.database or os.getenv("APP_DB_URL", "sqlite:///./data/app.sqlite3")
    data_dir = args.data_dir or os.getenv("APP_DATA_DIR", "./data")
    result = create_backup_bundle(
        database,
        args.output,
        data_dir=data_dir,
        include_images=args.include_images,
        config_paths=args.config,
        overwrite=args.overwrite,
    )
    print(
        f"backup created: {result.destination} "
        f"({result.size_bytes} bytes, integrity={result.database_integrity}, "
        f"images={result.image_file_count})"
    )
    return 0


def _run_verify_backup(path: str) -> int:
    result = verify_backup_bundle(path)
    print(
        f"backup valid: integrity={result['database_integrity']} "
        f"entries={result['entry_count']} images={result['image_file_count']}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command
    try:
        if command == "serve":
            configure_logging(service="api", level=os.getenv("LOG_LEVEL", "INFO"))
            return run_serve(host=args.host, port=args.port, reload=args.reload)
        if command in {"worker", "scheduler", "capture"}:
            return run_service(command, args.runner_args)
        if command == "init":
            return _run_init(args.with_password)
        if command == "migrate":
            return _run_migrate()
        if command == "check-config":
            return _run_check_config()
        if command == "backup":
            return _run_backup(args)
        if command == "verify-backup":
            return _run_verify_backup(args.path)
        parser.error(f"unknown command: {command}")
    except (CLIError, OSError, ValueError, RuntimeError) as exc:
        print(f"error: {sanitize_string(str(exc))}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by command-line tests
    raise SystemExit(main())


__all__ = ["CLIError", "build_parser", "main", "run_serve", "run_service"]
