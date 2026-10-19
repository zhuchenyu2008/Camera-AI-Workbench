import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: process.env.CAW_DEV_API ?? "http://127.0.0.1:8000",
        changeOrigin: false,
      },
      "/health": {
        target: process.env.CAW_DEV_API ?? "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
  build: { manifest: true, sourcemap: false, target: "es2022" },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    restoreMocks: true,
    clearMocks: true,
  },
});
