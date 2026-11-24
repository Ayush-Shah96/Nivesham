import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],

  server: {
    host: "localhost",
    port: 5173,

    proxy: {
      "/api": {
        // FastAPI backend
        target: "http://127.0.0.1:8000",

        changeOrigin: true,
        secure: false,

        // Disable Vite/http-proxy request timeouts.
        // Long-running NSE Regulatory Desk syncs are therefore
        // not terminated by the frontend proxy.
        timeout: 0,
        proxyTimeout: 0,
      },
    },
  },
});