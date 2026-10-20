import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Vite runs on 5240. The API port follows GEXLAB_PORT (8240 by default), the same variable launch.bat reads.
export default defineConfig(({ mode }) => {
  const apiPort = loadEnv(mode, ".", "GEXLAB_").GEXLAB_PORT || "8240";
  return {
    plugins: [react()],
    define: { __API_PORT__: JSON.stringify(apiPort) },
    server: {
      port: 5240,
      strictPort: true,
      host: "127.0.0.1",
      proxy: {
        "/api": { target: `http://127.0.0.1:${apiPort}`, changeOrigin: false },
      },
    },
    preview: { port: 5241, strictPort: true, host: "127.0.0.1" },
    build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
  };
});
