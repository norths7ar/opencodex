import { fileURLToPath } from "node:url";
import { defineConfig, mergeConfig } from "vite";
import dashboard from "./vite.config.ts";

const proxy = {
  target: process.env.OPENCODEX_PROXY_TARGET ?? "http://127.0.0.1:10100",
  // The backend checks the browser origin against the original loopback Host.
  changeOrigin: false,
};

export default mergeConfig(
  dashboard,
  defineConfig({
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        "/healthz": proxy,
        "/opencodex-session": proxy,
        "/api": proxy,
      },
    },
    build: {
      outDir: fileURLToPath(new URL("./dist-runtime/", import.meta.url)),
      emptyOutDir: true,
    },
  }),
);
