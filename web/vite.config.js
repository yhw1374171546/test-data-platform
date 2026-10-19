import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 开发时用 vite dev server（5173），把 /v1 代理到后端 8000 —— 这样浏览器里是同源，
// 不需要给后端开 CORS。构建产物直接由 FastAPI 挂在 /ui 下，也不存在跨域。
export default defineConfig({
  // 构建产物挂在后端的 /ui 下，所以资源路径也必须是 /ui/assets/...
  // （默认的 "/" 会让浏览器去请求 /assets/... 然后 404，页面一片空白 —— 这个坑很典型）
  base: "/ui/",
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/v1": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1500,
  },
});
