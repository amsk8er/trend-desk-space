import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const port = Number(env.VITE_DEV_PORT || "5173");
  // Keep the dev proxy on IPv4. On some macOS/Node combinations `localhost`
  // resolves to ::1 while uvicorn listens on 127.0.0.1, leaving /api requests
  // pending even though both development services are healthy.
  const apiTarget = env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8848";
  const base = env.VITE_BASE_PATH || "/";

  return {
    base,
    plugins: [react()],
    server: {
      port,
      strictPort: true,
      proxy: { "/api": apiTarget },
    },
  };
});
