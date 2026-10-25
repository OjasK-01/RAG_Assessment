import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// With VITE_BASE_URL set (frontend/.env), the browser calls the API directly and the backend's CORS
// allow-list must include this page's origin. With it empty, /api is proxied to the backend so the
// browser only ever talks to one origin.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: { "/api": env.VITE_BASE_URL || "http://127.0.0.1:8000" },
    },
  };
});
