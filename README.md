# OpenCodex Lite

个人维护的 Windows 本地模型网关，基于 OpenCodex 2.99.0。保留 API 与订阅渠道接入，订阅每渠道单账号，API 使用独立配置。

Python 后端已实现本机进程管理、独立 API 渠道配置、模型获取、Chat Completions 与原生 Responses 转发，以及 Codex CLI 配置接入和恢复。DeepSeek 已完成真实请求与 Codex CLI 接入验证。订阅 OAuth、跨协议转换及其余旧功能尚未迁移，旧 TypeScript 实现保留作参考。

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

## API 渠道与客户端

在控制台添加 DeepSeek 或自定义 OpenAI 兼容渠道，保存 API 基础地址和凭据来源，再获取模型、选择默认渠道与模型并保存。模型发现不生成回复；连接检查会明确确认后发送一个短请求，可能产生上游费用。

凭据可以引用进程环境变量或项目根目录 `.env` 中的变量，例如 `deepseek_api_key`。手动输入的 API Key 使用当前 Windows 用户的 DPAPI 加密后保存，不回显到网页。空白密钥输入表示保留原值；切换到环境变量来源会删除原来保存的密钥。`.env` 不进入 Git，也不会被复制到 Codex 配置。

本地应用使用控制台提供的网关地址和独立网关 Key，不使用上游 Key 或网页管理会话。网关提供 `GET /v1/models`、`POST /v1/chat/completions` 和 `POST /v1/responses`；模型名使用 `渠道ID/模型ID`，如 `deepseek/deepseek-flash`。不带渠道前缀时使用默认渠道；省略模型时使用保存的默认模型。

Responses 目前只直通明确启用该能力的上游，不自动转换 Chat 协议。DeepSeek 的 Responses 为无状态接口，客户端需回传完整历史。普通 JSON、工具调用字段及 SSE 事件保留上游语义；网关不自动重试生成请求。

Codex 面板会先预览目标 `config.toml`，再由用户明确应用或恢复。默认目录遵循 `CODEX_HOME`，否则为用户的 `.codex`；可指定独立目录。接入仅写入 Lite 的模型路由及本地网关 Key，不改登录、历史或其他客户端。恢复会保留无关的后续编辑；如果用户修改过被管理的路由字段，则报告冲突而不覆盖。当前承接的是 Codex CLI 路由，尚未迁移旧版模型目录同步或客户端自动重启。

## 开发

两个终端分别运行 `uv run ocx serve` 和 `pnpm --dir frontend dev:runtime`，在 `http://127.0.0.1:5173/` 开发网页。前端开发服务器默认代理到 `10101`；后端使用其他端口时，设置 `OPENCODEX_PROXY_TARGET` 后再启动前端。`serve` 是前台运行，Ctrl+C 退出。

旧后端仍可用 `pnpm legacy:dev` 调用原 Bun 入口；这不是 Python 运行链。

本 fork 不沿用上游官网、赞助推广、GitHub 自动化或发布流程。许可证及原作者归属见 LICENSE、CREDITS.md。
