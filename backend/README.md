# 后端

Python 3.11，FastAPI 单 API 进程＋独立 worker，SQLite WAL。时区为北京时间，持久数据时间统一为 UTC 毫秒。接口基线见 [后端设计](../docs/04-后端详细设计与接口契约.md)，验收进展见 [逐项清单](../docs/05-后端实现与验收清单.md)。

本机开发从仓库根目录运行 `./start-local.ps1 -Install`；它负责依赖、仓库外主密钥、数据库迁移、首次管理员、API、worker 和前端。后续用 `./start-local.ps1` 启动，`./start-local.ps1 -Stop` 停止，数据保存在 `data/local/`。完整步骤见[根 README](../README.md)。它使用真实摄像头与持久数据，与前端临时验收环境分开。

在 `backend` 目录安装、运行：

```powershell
python -m venv ../.venv
../.venv/Scripts/python -m pip install -e '.[dev]'
../.venv/Scripts/python -m app.cli export-openapi
../.venv/Scripts/python -m pytest
```

完整依赖锁包含运行和验收工具，干净环境可执行 `python -m pip install -r requirements.lock`。当前数据库迁移为 `0003`。生成同源 OpenAPI、TypeScript、样例和57项路由审计用 `python -m scripts.build_contracts`；CI 用 `--check` 拒绝产物过期。TypeScript 编译命令见逐项清单。

Linux 部署使用 Python 虚拟环境。配置 `CAW_DATA_DIR`、`CAW_DATABASE_URL`、`CAW_MASTER_KEY_FILE`、`CAW_ALLOWED_ORIGIN`、`CAW_RTSP_ALLOWED_HOSTS`、`CAW_RTSP_ALLOWED_PORTS`、`CAW_PROVIDER_ALLOWED_HOSTS`；域名与端口由部署人员填写，主密钥必须放在仓库外。

```sh
python -m app.cli init-key --path /etc/camera-ai-workbench/master.key
python -m app.cli migrate
python -m app.cli create-admin
python -m app.cli check-config
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
python -m app.worker_main
```

登录接口要求 `Origin` 与 `CAW_ALLOWED_ORIGIN` 完全一致。登录后的 `csrf_token` 用于所有写请求；正式部署使用 Secure、HttpOnly、SameSite=Strict 会话 Cookie。只有显式设置 `CAW_LOCAL_DEV=1` 时允许回环 HTTP 开发来源，并关闭该开发会话的 Secure 标记，同源与 CSRF 检查仍保留。完整 OpenAPI 也需要登录。CLI 导出不需要运行服务或读取主密钥。普通 CLI／systemd 不自动读取根目录 `.env`，须按部署方式注入环境变量。

AI 地址需要部署白名单，默认 HTTPS。确有本机兼容服务时用 `CAW_PROVIDER_LOCAL_ENDPOINTS=主机:端口` 单独允许。网络连接验证解析后的地址并直接连接已验证的 IP，保留原始 TLS SNI，不跟随重定向，不读取系统代理。RTSP 同时验证 `CAW_RTSP_ALLOWED_HOSTS` 的精确 IPv4 白名单和 `CAW_RTSP_ALLOWED_PORTS` 的端口白名单，默认仅允许本机。软路由 → VPS 公网端口模式需要明确把 VPS IPv4 加入主机白名单；无需在开发电脑增加 visitor。前端不能更改这些部署边界。

参考图片会重新解码、应用 EXIF 方向、去除元数据、保存独立 JPEG。数据库备份用 `python -m app.cli backup --destination <新快照文件>`；现场图不备份，主密钥与参考图独立受保护保存。恢复应先停止两个进程，再执行迁移、资产扫描与清理，确认 TTL 后开放 API；不要直接复制运行中的 SQLite 文件。
