# Camera AI Workbench 前端

## 本地开发

1. 在仓库根目录启动后端 API（默认 `http://127.0.0.1:8000`）。
2. 在本目录运行 `npm install`（首次安装）和 `npm run dev`。
3. 打开 `http://localhost:5173`。Vite 会将 `/api` 与 `/health` 请求代理至本地 API。

## 校验与构建

- `npm test`：运行 Vitest 单元测试。
- `npm run build`：执行 TypeScript 项目检查并生成生产静态资源至 `dist/`。
- `npm run preview`：预览已构建资源。

## Docker Compose

在仓库根目录执行 `docker compose up --build`，随后访问 `http://localhost:8080`。端口可通过根目录 `.env` 中的 `WEB_PORT` 覆盖。
