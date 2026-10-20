"""本机持久开发环境：真实 API、worker 和 Vite，不安装系统服务。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import Config
from app.repositories import schema as s
from app.repositories.db import SCHEMA_VERSION, Database
from app.services.workbench import Workbench
from app.worker_main import ProcessLock

ROOT = Path(__file__).resolve().parents[2]


def load_config() -> Config:
    env_file = ROOT / ".env"
    if not env_file.exists():
        shutil.copyfile(ROOT / ".env.example", env_file)
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name.strip().startswith("CAW_"):
            raise ValueError(".env 只支持 CAW_NAME=value 格式，不执行 Shell 表达式")
        os.environ.setdefault(name.strip(), value.strip().strip("\"'"))
    data = Path(os.environ.get("CAW_DATA_DIR", "data/local"))
    os.environ["CAW_DATA_DIR"] = str((ROOT / data).resolve())
    if not os.environ.get("CAW_MASTER_KEY_FILE"):
        user_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local/share")
        os.environ["CAW_MASTER_KEY_FILE"] = str(user_data / "Camera-AI-Workbench/master.key")
    os.environ["CAW_DEV_API"] = "http://127.0.0.1:8000"
    config = Config.from_env()
    if config.allowed_origin != "http://127.0.0.1:5173" or config.secure_cookies:
        raise ValueError("本机脚本要求 CAW_LOCAL_DEV=1、CAW_ALLOWED_ORIGIN=http://127.0.0.1:5173")
    if config.master_key_file.resolve().is_relative_to(ROOT):
        raise ValueError("主密钥必须保存在仓库外")
    config.prepare()
    return config


def initialize(config: Config, *, admin_password: str | None = None) -> None:
    if not shutil.which(config.ffmpeg):
        raise ValueError("未找到 FFmpeg，请安装后加入 PATH 或在 .env 中设置 CAW_FFMPEG")
    key = config.master_key_file.resolve()
    key.parent.mkdir(parents=True, exist_ok=True)
    if not key.exists():
        descriptor = os.open(key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(Fernet.generate_key() + b"\n")
    db = Database(config.database_url)
    try:
        if db.path.exists():
            with sqlite3.connect(db.path.as_uri() + "?mode=ro", uri=True) as conn:
                previous = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            if previous != SCHEMA_VERSION:
                backup = config.data_dir / "db" / f"pre-upgrade-{previous}-{int(time.time())}.sqlite3"
                db.backup(backup)
                print("升级前数据库快照已保存到 data 目录的 db 文件夹。", flush=True)
    finally:
        db.close()
    workbench = Workbench(config, migrate=True)
    try:
        from app.activities.repair import repair_history

        repair_history(workbench)
        with workbench.db.read() as conn:
            has_admin = conn.execute(select(s.users.c.id)).first() is not None
        if not has_admin:
            password = admin_password or getpass.getpass("首次启动，设置 admin 密码（至少 12 字符）：")
            if admin_password is None and password != getpass.getpass("再次输入密码："):
                raise ValueError("两次密码不一致")
            workbench.auth.create_admin("admin", password)
            print("管理员 admin 已创建；后续启动保留原账户和数据。", flush=True)
    finally:
        workbench.db.close()


def request_stop(config: Config) -> None:
    runtime = config.data_dir / "local-runtime.json"
    if not runtime.exists():
        print("本机工作台未在运行。")
        return
    (config.data_dir / "local-stop.request").touch()
    deadline = time.monotonic() + 30
    while runtime.exists() and time.monotonic() < deadline:
        time.sleep(0.25)
    if runtime.exists():
        raise RuntimeError("停止超时，请检查本机启动终端和 logs/local-worker.log")
    print("本机 API、worker、采集管道和前端已停止。")


def serve(config: Config) -> None:
    stop_file = config.data_dir / "local-stop.request"
    runtime = config.data_dir / "local-runtime.json"
    with ProcessLock(config.data_dir / "local-launcher.lock"):
        for port in (8000, 5173):
            with socket.socket() as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    raise RuntimeError(f"本机端口 {port} 已被占用，请先停止原进程") from None
        node = shutil.which("node")
        vite = ROOT / "frontend/node_modules/vite/bin/vite.js"
        if not node or not vite.is_file():
            raise RuntimeError("缺少 Node.js 或前端依赖，请运行 start-local.ps1 -Install")
        help_build = subprocess.run(
            [node, str(ROOT / "frontend/scripts/build-help.mjs")],
            cwd=ROOT / "frontend",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if help_build.returncode:
            raise RuntimeError("内置帮助生成失败，请检查前端依赖和 docs 目录")
        initialize(config)
        stop_file.unlink(missing_ok=True)
        stopped = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stopped.set())
        commands = {
            "api": [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
            "worker": [sys.executable, "-u", "-m", "scripts.run_local", "--worker"],
            "frontend": [node, str(vite), "--host", "127.0.0.1", "--port", "5173", "--strictPort"],
        }
        children, logs = {}, []
        try:
            for name, command in commands.items():
                log = (config.data_dir / "logs" / f"local-{name}.log").open("ab")
                logs.append(log)
                children[name] = subprocess.Popen(
                    command,
                    cwd=ROOT / ("frontend" if name == "frontend" else "backend"),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            runtime.write_text(
                json.dumps({"launcher": os.getpid(), "children": {k: v.pid for k, v in children.items()}}),
                encoding="utf-8",
            )
            opener = build_opener(ProxyHandler({}))
            ready, deadline = False, time.monotonic() + 45
            while not stopped.wait(0.25) and not stop_file.exists():
                for name, child in children.items():
                    if child.poll() is not None:
                        raise RuntimeError(f"{name} 已退出，请查看 logs/local-{name}.log")
                if not ready:
                    try:
                        for url in ("http://127.0.0.1:8000/health/live", config.allowed_origin):
                            with opener.open(url, timeout=1) as response:
                                if response.status != 200:
                                    raise URLError("服务未就绪")
                                if url.endswith("/health/live"):
                                    health = json.load(response)
                                    if (
                                        health.get("api_version") != "2.0"
                                        or health.get("database_schema") != "0007"
                                    ):
                                        raise RuntimeError(
                                            "后台版本不一致，请停止旧 API 和 worker 后统一重启"
                                        )
                    except (URLError, TimeoutError, OSError):
                        if time.monotonic() > deadline:
                            raise RuntimeError("本机服务启动超时，请检查 data/local/logs") from None
                    else:
                        ready = True
                        print(f"工作台已启动：{config.allowed_origin}", flush=True)
                        print(
                            "摄像头配置和数据会保留；Ctrl+C 或 start-local.ps1 -Stop 停止本次运行。",
                            flush=True,
                        )
        finally:
            stop_file.touch()
            # worker 先优雅停止 FFmpeg、释放数据库，再终止 API 与前端。
            worker = children.get("worker")
            if worker:
                try:
                    worker.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=5)
            for name, child in children.items():
                if name != "worker" and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
            for log in logs:
                log.close()
            runtime.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="启动／停止本机摄像头 AI 工作台")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stop", action="store_true")
    mode.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        config = load_config()
        if args.stop:
            request_stop(config)
        elif args.worker:
            from app.worker_main import main as worker_main

            worker_main(stop_file=config.data_dir / "local-stop.request")
        else:
            serve(config)
    except (ValueError, RuntimeError, OSError) as error:
        print(f"启动／停止失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
