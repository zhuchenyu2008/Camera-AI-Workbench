# Linux 部署与回退

执行前填入实际域名、AI 主机白名单、frp visitor 端口与 FFmpeg 路径。模板不含真实凭据，不创建或覆盖现有 frp、证书和 Nginx 配置。

1. 安装 Python 3.11、FFmpeg、独立服务用户 `camera-workbench`。程序放在 `/opt/camera-workbench`，数据目录 `/var/lib/camera-ai-workbench` 归服务用户所有，权限 0700。
2. 在仓库根目录创建 `.venv`，安装 `backend/requirements.lock`。进入 `backend` 执行 CLI。主密钥用 `init-key` 创建在 `/etc/camera-ai-workbench/master.key`，由服务用户读取，权限 0600。环境文件同样只向服务用户开放；填写 API 的真实 HTTPS Origin。
3. 以服务用户执行 `python -m app.cli migrate`、`python -m app.cli create-admin`、`python -m app.cli check-config`。管理员密码由隐藏终端输入，密钥和密码不放进命令参数或 Git。
4. 将两个 systemd 单元安装到服务目录；合并 Nginx 的 location 片段到已有 HTTPS server，先运行 `nginx -t`。不对外开放 8000、15554 或媒体目录。
5. 启动 worker、API，登录后检查 `/api/status` 的心跳。配置、测试模型，再添加和测试一路摄像头、参考档案，完成预检后启用。新配置在下一完整秒槽生效，实际确认看 control_revision / applied_revision。
6. 使用目标 VPS 完成 `docs/05` 列明的实测项。systemd 内存限制为初始保护值，不能当作实际容量保证。额外机位须逐路取得五分钟样本并重新评估；AI 并发超过 2 需先验证，再由部署配置修改 `CAW_VALIDATED_AI_CONCURRENCY`。

升级前记录 Git commit，停止 API 和 worker，执行一次带明确目标文件名的一致性备份。`backup` 使用 SQLite Backup API，不能直接拷贝正在写入的主数据库文件。数据库加一份备份受 1 GB 规划检查；备份文件需要覆盖／轮换时由运维明确安排，不无限新增快照。现场图不备份；参考图和主密钥独立受保护保存。

回退时停止两个进程，切回已记录的代码及其迁移版本，恢复对应的一致性数据库快照和必要参考图。不要在运行进程上替换 SQLite，也不要将较新的数据库直接交给不兼容的旧代码。执行 `migrate`、`check-config` 后，先启动 worker 完成过期资产清理、失效租约恢复和缺口重建，再启动 API。资产访问始终按当前时间检查 TTL，旧备份不能让过期图片重新可读。

应用 API / worker 各自使用 15 个、每个最多 10 MB 的轮转日志，总规划为 300 MB，时间带北京时间偏移。日志仅记录安全代码和 ID，不记录请求图片、Base64、密码、密钥或第三方响应正文。系统 journal 的总额度由现有服务器运维策略控制。
