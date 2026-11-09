import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const proxy = {
  target: process.env.OPENCODEX_PROXY_TARGET ?? 'http://127.0.0.1:10101',
  // The backend checks the browser origin against the original loopback Host.
  changeOrigin: false,
}

export default defineConfig({
  root: fileURLToPath(new URL('./runtime/', import.meta.url)),
  publicDir: false,
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/healthz': proxy,
      '/opencodex-session': proxy,
      '/api': proxy,
    },
  },
  build: {
    outDir: fileURLToPath(new URL('./dist-runtime/', import.meta.url)),
    emptyOutDir: true,
  },
})
