import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Ports are fixed by the project: Vite 5240, API 8240. Never 3000/3001/3010/7777-7780/8000/8001.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5240,
    strictPort: true,
    host: "127.0.0.1",
    proxy: {
      "/api": { target: "http://127.0.0.1:8240", changeOrigin: false },
    },
  },
  preview: { port: 5241, strictPort: true, host: "127.0.0.1" },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
});
