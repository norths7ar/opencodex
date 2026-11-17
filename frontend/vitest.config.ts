import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  cacheDir: "../.tmp/20261117-vitest-cache",
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  test: {
    include: ["tests/**/*.test.{ts,tsx}"],
    environment: "happy-dom",
    environmentOptions: { happyDOM: { url: "http://localhost/" } },
    setupFiles: ["./tests/setup.ts"],
    restoreMocks: true,
  },
});
