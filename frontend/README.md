# OpenCodex Lite frontend

## Python runtime console

Use Node.js 24 and pnpm 11.25.0. From `frontend/`:

```powershell
pnpm install --frozen-lockfile
pnpm run build:runtime
```

The build writes `frontend/dist-runtime/`, which the Python backend serves at `http://127.0.0.1:10101/` by default. Use the [root README](../README.md) for backend commands. The console shows the runtime status and can stop it; provider APIs and OAuth are not part of this stage.

For frontend development, run `pnpm run dev:runtime` in `frontend/` and open `http://127.0.0.1:5173/`. Vite forwards requests to the Python backend at `http://127.0.0.1:10101`. If you start the backend on a different port, set `OPENCODEX_PROXY_TARGET` to its loopback URL before starting Vite.

## Legacy dashboard reference

The original modern and classic dashboards remain as migration references. They are not connected to the Python runtime yet. The old backend entry is `pnpm legacy:dev` from the repository root; the old frontend source remains under `src/` and `classic/`. Its build output is `dist/`, separate from the Python console's `dist-runtime/`.
