import { defineConfig, loadEnv } from "vite";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";

// The API trusts an X-Tenant-Id header and has no CORS setup, so the dev server proxies
// these paths to it instead of the browser calling it cross-origin.
// The target comes from VITE_API_URL (frontend/.env or the shell), default port 8001.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const api = env.VITE_API_URL || "http://127.0.0.1:8001";
  return {
    plugins: [react(), tailwindcss()],
    server: {
      port: 5173,
      proxy: {
        "/api": { target: api, changeOrigin: true, rewrite: (p) => p.replace(/^\/api/, "") },
      },
    },
  };
});
