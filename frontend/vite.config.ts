import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const port = Number(env.VITE_DEV_PORT || "5173");
  const apiTarget = env.VITE_API_PROXY_TARGET || "http://localhost:8848";

  return {
    plugins: [react()],
    server: {
      port,
      strictPort: true,
      proxy: { "/api": apiTarget },
    },
  };
});
