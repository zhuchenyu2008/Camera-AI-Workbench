# 后端

Python 3.11，FastAPI 单 API 进程＋独立 worker，SQLite WAL。时区为北京时间，持久数据时间统一为 UTC 毫秒。接口基线见 [后端设计](../docs/04-后端详细设计与接口契约.md)，验收进展见 [逐项清单](../docs/05-后端实现与验收清单.md)。

在 `backend` 目录安装、运行：

```powershell
python -m venv ../.venv
../.venv/Scripts/python -m pip install -e '.[dev]'
../.venv/Scripts/python -m app.cli export-openapi
../.venv/Scripts/python -m pytest
```

完整依赖锁包含运行和验收工具，干净环境可执行 `python -m pip install -r requirements.lock`。当前数据库迁移为 `0006`。生成同源 OpenAPI、TypeScript、样例和64项路由审计用 `python -m scripts.build_contracts`；CI 用 `--check` 拒绝产物过期。TypeScript 编译命令见逐项清单。

Linux 部署使用 Python 虚拟环境。配置 `CAW_DATA_DIR`、`CAW_DATABASE_URL`、`CAW_MASTER_KEY_FILE`、`CAW_ALLOWED_ORIGIN`、`CAW_RTSP_ALLOWED_PORTS`、`CAW_PROVIDER_ALLOWED_HOSTS`；域名与端口由部署人员填写，主密钥必须放在仓库外。

```sh
python -m app.cli init-key --path /etc/camera-ai-workbench/master.key
python -m app.cli migrate
python -m app.cli create-admin
python -m app.cli check-config
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
python -m app.worker_main
```

登录接口要求 `Origin` 与 `CAW_ALLOWED_ORIGIN` 完全一致。登录后的 `csrf_token` 用于所有写请求；浏览器使用安全、HttpOnly、SameSite=Strict 的会话 Cookie。完整 OpenAPI 也需要登录。CLI 导出不需要运行服务或读取主密钥。

本机开发只需设置 `CAW_LOCAL_DEV=1`，Origin 会自动固定为 `http://127.0.0.1:5173`。此模式下无需配置任何来源、RTSP 主机／端口或模型服务白名单，可连接任意 IPv4 摄像头和 HTTP／HTTPS 模型端点；开发启动器仅绑定 `127.0.0.1`。此模式会放宽网络目标限制，只应在本机开发时启用，不能用于部署。

正式部署不设置 `CAW_LOCAL_DEV`：RTSP 主机与端口必须显式配置；AI 地址必须是部署白名单内的 HTTPS 主机，确有本机兼容服务时用 `CAW_PROVIDER_LOCAL_ENDPOINTS=主机:端口` 单独允许。出站连接会再次检查白名单与 DNS 解析结果、直接连接已验证的 IP，保留 TLS SNI，不跟随重定向，不读取系统代理。前端不能更改这些部署边界。

参考图片会重新解码、应用 EXIF 方向、去除元数据、保存独立 JPEG。数据库备份用 `python -m app.cli backup --destination <新快照文件>`；现场图不备份，主密钥与参考图独立受保护保存。恢复应先停止两个进程，再执行迁移、资产扫描与清理，确认 TTL 后开放 API；不要直接复制运行中的 SQLite 文件。


升级前停止API和worker并备份，再执行`migrate`、`repair-activities`，最后统一重启。`/health/live`应显示api_version=2.0、database_schema=0006。本机启动脚本自动执行版本升级前备份、迁移与幂等历史修复。历史修复只更新确定性统计和时间线，旧汇总文案标记来源变化，不新建历史模型调用。

事件列表使用轻量EventSummaryDto，详情仍使用EventDto；活动名称开放为字符串。自动建档是独立辅助任务，按最新参考目录匹配，受并发与参考配额约束，不阻断活动提交。接口与行为详见后端设计第19节。
