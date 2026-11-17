# OpenCodex Lite frontend

The React console uses Node.js 26.13.1 and pnpm 12.21.0. Install the locked dependencies with `pnpm install --frozen-lockfile` from this directory.

## Development and preview

Run `pnpm dev` with the Python gateway running. The development console listens on `http://127.0.0.1:5173` and proxies to port 10100. Set `OPENCODEX_PROXY_TARGET` to use another loopback gateway address.

Both `pnpm build` and `pnpm build:runtime` check types and create a complete candidate in `frontend/build/`. `pnpm preview` serves that candidate using the same Vite configuration. Building does not replace the console currently served by the gateway.

From any directory, `ocx service update` synchronizes locked dependencies, builds and publishes the candidate to `frontend/dist-runtime/`, then restarts an already-running managed service and checks it. The publisher retains recent hashed assets for open browser tabs. See the [root README](../README.md) for installation, independent state directories, retention and failure behavior.

## Code quality

Use `pnpm lint`, `pnpm typecheck`, and `pnpm format:check`; `pnpm lint:fix` and `pnpm format` apply corrections. Local translation rules cover the app shell, pages, components and feature modules. English defines translation keys; other dictionaries fall back at the shared catalog boundary. New English, Simplified Chinese and Traditional Chinese copy belongs in those dictionaries, not page-local language branches.

Run `pnpm test` for the Lite suite in `tests`, using Vitest and happy-dom. It covers current component interactions and data behavior with simulated HTTP responses; it does not replace browser layout or live gateway acceptance. Applicable upstream tests have been migrated into this suite, while tests for retired features and the old Bun test tooling have been removed. Lint, formatting and type checks include the current tests.

The [architecture document](../docs/lite-architecture.md) defines component and module responsibilities. Theme values live in `src/design/theme.css`; shared controls own their interaction behavior. Upstream design decisions under `design-system/decisions` are historical records, not current instructions.
