import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const backend = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL(".", import.meta.url)) },
  },
  server: {
    host: "127.0.0.1",
    port: 3000,
    proxy: {
      "/api": { target: backend, changeOrigin: true, ws: true },
    },
  },
  preview: {
    port: 3000,
    proxy: {
      "/api": { target: backend, changeOrigin: true, ws: true },
    },
  },
  build: {
    // electron-builder packages `out/**`; its own output goes to `dist/`.
    outDir: "out",
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
  },
});
