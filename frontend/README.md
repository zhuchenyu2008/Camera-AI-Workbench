# 前端

React + TypeScript + Vite + React Router + TanStack Query，按根目录 `DESIGN.md` 和 `docs/03-前端详细设计与交互规范.md` 实现。正式应用只使用真实同源 API，没有 Demo 数据或模拟成功开关。所有业务时间显示为北京时间。

```powershell
cd frontend
npm ci
npm run generate
npm run dev
```

开发入口 `http://127.0.0.1:5173`，`/api` 默认代理到 `http://127.0.0.1:8000`。可用 `CAW_DEV_API` 改变开发代理目标。实际服务要求 HTTPS 与安全 Cookie；请将开发入口放在配置匹配的 HTTPS 反向代理后。专用自动验收脚本使用独立临时数据库与本机测试配置，不修改正式安全设置。

```powershell
npm run typecheck
npm test
npm run build
```

构建会检查 57 项操作的生成元数据、同源 DTO 和样例是否过期，并检查首屏 JS gzip 不超过 300 KiB。HLS、ROI 和非首屏页面按需拆包。

`npm run generate` 仅从 `backend/contracts` 读取已生成 OpenAPI、TypeScript 和 fixture，不维护第二套同名 DTO。后端契约变化时先在 backend 运行 `python -m scripts.build_contracts`，再执行本命令。

部署 `dist/` 到既有 HTTPS 入口，同源 `/api/` 交给后端；所有应用路由须回退到 `index.html`。Cookie 和 CSRF 不存入 localStorage；现场图不写磁盘缓存，不注册 Service Worker。

逐项验收与证据位置见 [前端实现与验收清单](../docs/06-前端实现与验收清单.md)。
