from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import subprocess
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import Config
from app.repositories.db import Database


def main():
    parser = argparse.ArgumentParser(description="摄像头 AI 工作台运维工具（北京时间）")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("check-config")
    commands.add_parser("repair-activities")
    admin = commands.add_parser("create-admin")
    admin.add_argument("--username", default="admin")
    key = commands.add_parser("init-key")
    key.add_argument("--path", type=Path, required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--destination", type=Path, required=True)
    contract = commands.add_parser("export-openapi")
    contract.add_argument("--destination", type=Path, default=Path("contracts/openapi.json"))
    args = parser.parse_args()
    if args.command == "init-key":
        target = args.path.resolve()
        repository = Path(__file__).resolve().parents[2]
        if target.is_relative_to(repository):
            parser.error("主密钥必须保存在仓库外")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(Fernet.generate_key() + b"\n")
        print("主密钥已创建；请独立保管，不要提交到 Git")
        return
    if args.command == "export-openapi":
        from app.main import create_app

        schema = create_app().openapi()
        args.destination.parent.mkdir(parents=True, exist_ok=True)
        args.destination.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("OpenAPI 已导出")
        return
    config = Config.from_env()
    config.prepare()
    if args.command == "migrate":
        db = Database(config.database_url)
        try:
            db.migrate()
        finally:
            db.close()
        print(f"数据库迁移完成，SQLite {sqlite3.sqlite_version}")
        return
    from app.services.workbench import Workbench

    w = Workbench(config)
    try:
        if args.command == "create-admin":
            password = getpass.getpass("管理员密码（至少12字）：")
            if password != getpass.getpass("再次输入密码："):
                parser.error("两次密码不一致")
            w.auth.create_admin(args.username, password)
            print("唯一管理员已创建")
        elif args.command == "repair-activities":
            from app.activities.repair import repair_history

            print(json.dumps(repair_history(w), ensure_ascii=False))
        elif args.command == "check-config":
            if config.master_key_file.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
                parser.error("主密钥必须在仓库外")
            if os.name != "nt" and config.master_key_file.stat().st_mode & 0o077:
                parser.error("主密钥权限应为 0600 或更严格")
            for capability in ("-encoders", "-muxers", "-filters"):
                result = subprocess.run(
                    [config.ffmpeg, "-hide_banner", capability], capture_output=True, timeout=15, check=True
                )
                text = result.stdout.decode(errors="replace")
                required = {"-encoders": "mjpeg", "-muxers": "hls", "-filters": "select"}[capability]
                if required not in text:
                    parser.error("FFmpeg 缺少所需能力")
            if not config.rtsp_allowed_ports or not config.rtsp_allowed_hosts:
                parser.error("缺少 RTSP 主机或端口白名单")
            probe = config.data_dir / "tmp" / "write-check.part"
            probe.write_bytes(b"check")
            probe.unlink()
            print(
                json.dumps(
                    {
                        "schema": "0006",
                        "sqlite": sqlite3.sqlite_version,
                        "timezone": "Asia/Shanghai",
                        "master_key": "readable",
                        "data_dir": "writable",
                        "ffmpeg": "verified",
                        "rtsp_allowlist": "configured",
                        "provider_allowlist": "configured"
                        if config.provider_allowed_hosts or config.provider_local_endpoints
                        else "empty",
                        "free_bytes": w.disk_free(),
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "backup":
            with w.db.read() as conn:
                w.settings_config(conn)
            # 一个一致性数据库快照；现场图不备份。轮转由部署作业显式安排。
            if w.db.path.stat().st_size * 2 > 1_000_000_000:
                parser.error("数据库及一份备份超过 1 GB 规划，请先扩容或导出")
            w.db.backup(args.destination)
            if os.name != "nt":
                args.destination.chmod(0o600)
            print("一致性数据库快照已完成；主密钥与参考图按独立备份策略保存")
    finally:
        w.db.close()


if __name__ == "__main__":
    main()
