# OpenCodex Lite

个人维护的 Windows 本地模型网关，基于 OpenCodex 2.99.0。保留 API 与订阅渠道接入，订阅每渠道单账号，API 使用独立配置。

Python 后端目前已实现本机启动、停止、重启、状态查询和网页运行控制台。API 转发、渠道配置和订阅 OAuth 尚未迁移，旧 TypeScript 实现保留作参考；当前 Python 实例还不能作为模型网关使用。

## 目录

- `backend/ocx/`：Python 后端与进程管理入口。
- `backend/src/`、`backend/bin/`：迁移中的旧 TypeScript 后端。
- `frontend/`：React 网页控制台；当前运行入口在 `runtime/`。
- `scripts/`：项目级开发工具。
- `tests/`：现有测试。
- `MIGRATION.md`：本项目的迁移计划。

根目录的 `pyproject.toml`、`uv.lock` 管理 Python；`frontend/pnpm-lock.yaml` 管理前端依赖。旧 Bun 依赖和测试配置尚未退役。

## 本机运行

在项目根目录执行：

```powershell
uv sync
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build:runtime
uv run ocx start
uv run ocx status
uv run ocx stop
```

启动后访问 `http://127.0.0.1:10101/`。关闭终端或浏览器不会停止后台进程。再次 `start` 返回已有实例；`restart` 等待旧进程退出再重启。网页可以停止进程，停止后需用命令重新启动。

默认只监听 `127.0.0.1`，运行记录和日志位于 `%LOCALAPPDATA%\OpenCodexLite`，不读取或改写旧 Bun 配置。`--port` 指定端口，端口占用会报错而不自动换端口；`--state-dir` 或 `OPENCODEX_LITE_HOME` 可指定独立运行目录。`--json` 输出机器可读状态。`status` 区分运行、停止、失败和无响应，失败或无响应时退出码为 1。

网页构建输出到 `frontend/dist-runtime/`，与旧控制台的 `dist/` 隔离。网页未构建时，后端仍可启停，首页会显示构建命令。

## 开发

两个终端分别运行 `uv run ocx serve` 和 `pnpm --dir frontend dev:runtime`，在 `http://127.0.0.1:5173/` 开发网页。前端开发服务器默认代理到 `10101`；后端使用其他端口时，设置 `OPENCODEX_PROXY_TARGET` 后再启动前端。`serve` 是前台运行，Ctrl+C 退出。

旧后端仍可用 `pnpm legacy:dev` 调用原 Bun 入口；这不是 Python 运行链。

本 fork 不沿用上游官网、赞助推广、GitHub 自动化或发布流程。许可证及原作者归属见 LICENSE、CREDITS.md。
