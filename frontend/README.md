# OpenCodex Lite frontend

## Python runtime console

Use Node.js 26.13.1 and pnpm 12.21.0. TypeScript is pinned to 6.0.3, within typescript-eslint 8.72.2's supported range (`>=4.8.4 <6.1.0`). From `frontend/`:

```powershell
pnpm install --frozen-lockfile
pnpm run build:runtime
```

The build writes `frontend/dist-runtime/`, which the Lite backend serves at `http://127.0.0.1:10100/` by default. Use the [root README](../README.md) for backend commands. The modern console manages API providers, model selection, gateway access and Codex routing. Supported subscription channels expose browser and device authorization through the gateway's own account management. Provider keys are write-only; completion checks and client configuration changes run through explicit actions.

For frontend development, run `pnpm run dev:runtime` in `frontend/` and open `http://127.0.0.1:5173/`. Vite forwards requests to the Python backend at `http://127.0.0.1:10100`. If you start the backend on a different port, set `OPENCODEX_PROXY_TARGET` to its loopback URL before starting Vite.

## Code quality

Run `pnpm lint` for ESLint, `pnpm lint:fix` for automatic lint fixes, and `pnpm format` / `pnpm format:check` for Prettier. Versions and base rules follow the workspace defaults, with React, browser/Node and local translation checks added in `eslint.config.mjs`. Underscore-prefixed unused bindings are intentional placeholders; lifecycle and entry-point exceptions are documented locally. Tests and generated output are excluded from these commands. Root-level commands also check and format the Lite Python backend with Ruff.

## Build output

`src/` contains the single supported dashboard. `pnpm build` produces `dist/`; `pnpm build:runtime` produces `dist-runtime/` for Lite. Both builds use the same UI. Deferred client integrations are hidden according to the backend capability response. Migration scope and verification requirements are recorded in [MIGRATION.md](../MIGRATION.md).
