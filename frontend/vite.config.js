import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发模式：npm run dev（默认 5173），/api 代理到本地 Python 服务；
// 生产模式：npm run build 产出 dist/，由 server.py 直接静态服务。
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
  build: { outDir: "dist" },
});
