# 前端

React + TypeScript + Vite + React Router + TanStack Query，按根目录 `DESIGN.md` 和 `docs/03-前端详细设计与交互规范.md` 实现。正式应用只使用真实同源 API，没有 Demo 数据或模拟成功开关。所有业务时间显示为北京时间。

需要连自己的摄像头时，在仓库根目录运行 `./start-local.ps1 -Install`；它会一起启动真实后端、worker 和本前端，配置及历史数据保存在 `data/local/`。后续使用 `./start-local.ps1`，停止使用 `./start-local.ps1 -Stop`。下方的临时浏览器验收环境只用于自动测试。

```powershell
cd frontend
npm ci
npm run generate
npm run dev
```

开发入口 `http://127.0.0.1:5173`，`/api` 默认代理到 `http://127.0.0.1:8000`。可用 `CAW_DEV_API` 改变开发代理目标。本机启动脚本通过 `CAW_LOCAL_DEV=1` 和精确的回环 Origin 启用 HTTP 开发会话；独立运行 Vite 时需要自行启动、配置后端和 worker。正式部署要求 HTTPS 与 Secure Cookie。专用自动验收脚本使用独立临时数据库与本机测试配置，不修改正式安全设置。

```powershell
npm run typecheck
npm test
npm run build
```

构建会检查 57 项操作的生成元数据、同源 DTO 和样例是否过期，并检查首屏 JS gzip 不超过 300 KiB。体积包含入口、概览路由及其静态依赖；HLS、ROI 和非首屏管理功能按需拆包。

`npm run generate` 仅从 `backend/contracts` 读取已生成 OpenAPI、TypeScript 和 fixture，不维护第二套同名 DTO。后端契约变化时先在 backend 运行 `python -m scripts.build_contracts`，再执行本命令。

部署 `dist/` 到既有 HTTPS 入口，同源 `/api/` 交给后端；所有应用路由须回退到 `index.html`。配置样例为 `deploy/nginx-frontend.conf.example`，需与现有后端代理片段合并。Cookie 和 CSRF 不存入 localStorage；现场图不写磁盘缓存，不注册 Service Worker。

## 本机浏览器验收

从仓库根目录执行 `.venv/Scripts/python frontend/scripts/test-backend.py`。它仅监听 `127.0.0.1:18180`，创建独立临时数据库，运行真实合成 RTSP 和本地模型响应，不接触已有摄像头文件。测试需要已安装的后端开发依赖与 `ffmpeg`。

另一个终端在 `frontend` 运行：

```powershell
$env:CAW_DEV_API='http://127.0.0.1:18180'
npm run build
npm run preview -- --port 5173 --strictPort
```

从仓库根目录打开 Playwright CLI 会话，然后依次运行 1–6 组。第 6 组会修改临时测试账户密码并退出；整套重跑时重新启动测试后端即可。

```powershell
npx --yes --package @playwright/cli playwright-cli -s=camera-frontend open http://127.0.0.1:5173 --headed
1..6 | ForEach-Object {
  npx --yes --package @playwright/cli playwright-cli -s=camera-frontend run-code --filename="frontend/scripts/browser-stage$_.js" |
    Tee-Object -FilePath "output/playwright/production-stage$_.log"
  if ($LASTEXITCODE -ne 0) { throw "第 $_ 组验收失败" }
}
node frontend/scripts/collect-browser-results.mjs
```

脚本使用 360／390／768／1440px 真实浏览器视口、独立会话和洛杉矶系统时区，证据和媒体请求均经过真实后端。模型输出为合成响应；手机为浏览器仿真，真实设备与外部服务验收范围见逐项清单。

逐项验收与证据位置见 [前端实现与验收清单](../docs/06-前端实现与验收清单.md)。
