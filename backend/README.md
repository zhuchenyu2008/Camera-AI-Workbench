# Camera AI Workbench backend

后端采用 FastAPI + SQLAlchemy + SQLite WAL（单实例首版），按设计文档提供 REST `/api/v1`、SSE、持久化 AI Job、采集/帧存储、三层分析和留存清理能力。

## 本地启动

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python -m app.cli init
.\.venv\Scripts\python -m app.cli migrate
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

首次启动会在 `APP_DATA_DIR` 下创建 SQLite 数据库、图片目录和本地密钥文件。生产环境必须通过 Docker Secret 或受限文件注入主密钥与初始密码；不要把 RTSP/Provider 密钥写入日志或版本库。生产模式缺少主密钥或管理员密码会拒绝启动。

Docker Compose 生产启动前至少准备以下文件（也可在 `.env` 中改成 `/run/secrets/...` 的挂载路径）：

```bash
mkdir -p data
python -m app.cli init --with-password
# 将 data/master.key 和 data/bootstrap_password 作为受限文件挂载到 Compose 的共享数据卷，或配置 APP_MASTER_KEY_FILE / APP_BOOTSTRAP_PASSWORD_FILE。
docker compose up -d --build
```

若使用 Docker Secret，请让 API、worker、scheduler、capture 四个服务都能读取同一份 `APP_MASTER_KEY_FILE`；主密钥变化会使历史 RTSP/Provider 密文无法解密。首次初始化后应移除或轮换 bootstrap password 文件。

## Worker / Scheduler

```powershell
.\.venv\Scripts\python -m app.worker_main worker
.\.venv\Scripts\python -m app.worker_main scheduler
```

API、Worker 和 Scheduler 可以使用同一个镜像；容器通过共享的数据卷保存数据库与 JPEG。采集进程可单独运行，以避免 RTSP 解码故障影响 API。采集使用 FFmpeg image2pipe，按摄像头独立任务重连；普通帧与事件高精度帧均写入 `data/frames`，缩略图写入 `data/thumbs`。

## 测试

```powershell
pytest -q
```

设计基线见 [`../docs/implementation/backend-design-v1.0.md`](../docs/implementation/backend-design-v1.0.md)。所有时间内部使用 UTC 毫秒，API 使用 UTC RFC3339，区间统一为左闭右开 `[start, end)`。
