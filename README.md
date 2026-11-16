# OpenCodex Lite

OpenCodex Lite 是基于 OpenCodex 2.99.0 的 Windows 本机模型网关。Python 后端提供协议转发和管理 API，React 控制台管理渠道、模型和客户端接入。Service Desk 只负责打开网页，关闭网页不影响网关。

## 安装与运行

从源码目录运行，使用项目锁定的 Python 3.14.10、Node.js 26.13.1 和 pnpm 12.21.0；uv 需为 0.13.8 或更新版本。在仓库根目录执行：

```powershell
uv sync --locked
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build:runtime
./scripts/register-cli.ps1
```

注册脚本将 `ocx` 和 `opencodex` 指向项目虚拟环境。若首次注册更新了用户 PATH，重新打开终端即可在任意目录运行。请保留源码目录和 `.venv`；移动项目后需重建虚拟环境并重新注册，已安装的服务还需运行 `ocx service repair` 更新路径。

```powershell
ocx start
```

打开 <http://127.0.0.1:10100/>。网关仅监听回环地址，首次启动默认使用 10100；可用 `ocx start --port 10120` 指定其他端口。前台进程可用 Ctrl+C 退出，也可从另一终端使用 `ocx status`、`ocx stop`。无参数的 `ocx` 显示帮助。

### 后台服务

使用 `ocx service install` 或网页 Home → Startup 启用后台运行。它通过当前用户的 Windows 计划任务管理网关，不弹出常驻窗口。用 `ocx service status` 查看状态，`ocx service restart` 重启，`ocx service remove` 停止并移除任务。命令注册和服务安装是两件事：`./scripts/register-cli.ps1 -Remove` 只撤销命令入口。

默认运行目录为 `%LOCALAPPDATA%\OpenCodexLite`。用 `--state-dir` 或 `OPENCODEX_LITE_HOME` 可选择独立目录。日常配置以该目录下的 `configuration.sqlite3` 为准；同目录中已有的 Lite `config.json` 只在首次激活时作为迁移来源，之后不再参与配置读取。

## 渠道和模型

在 Providers 中添加渠道。内置连接覆盖 OpenAI API、ChatGPT 登录、DeepSeek API、MiMo API、OpenCode Zen 和 OpenCode Go；也可配置兼容端点。每条 API 渠道可单独配置地址、凭据和上游协议，多个 API 凭据分别建渠道。网关按需要在 Chat Completions、Responses 和 Messages 间转换，上下游协议一致时优先使用原生路径。

ChatGPT 支持浏览器或设备码 OAuth，由网关单独维护一个账号和令牌，不使用本机 Codex 登录。手动输入的 API Key 和订阅凭据由当前 Windows 用户加密保护。渠道探测用于读取目录；生成验证是另一个操作。

Models 页面从持久化快照读取模型目录，不会因打开页面而访问上游。启用发现的渠道由后台每日刷新，也可手动刷新；成功后用完整目录替换旧快照，失败时保留上次成功结果并显示状态。静态目录和用户模型配置仍可独立维护。客户端可以使用 `渠道 ID/模型 ID` 或已配置别名。

## 客户端接入

在 Route 中选择 Codex 或 DSH，先预览配置变更，再应用或恢复。

- **Codex**：保留内置 OpenAI 服务商身份，通过 `openai_base_url` 接入，管理入口和模型目录，不更改登录。默认使用 `CODEX_HOME` 或用户的 `.codex`，也可以指定独立目录。恢复使用操作记录撤回 Lite 管理的改动；对发生外部修改的受管设置报告冲突。
- **DSH**：接入配置管理 `llm-pi-ai.providers.opencodex`，有 Desktop profile 时使用其补丁文件，否则写入 `settings.yaml`。保留其他 provider、默认模型和无关配置。

## 日志、用量和配额

Usage & Logs 记录请求状态、渠道与模型、耗时和上游报告的 token 用量，在 `usage.sqlite3` 中保留历史。费用由匹配的价格规则估算，不是账单；未匹配价格时显示未知，ChatGPT 订阅不套用 API 单价。账号配额展示上游返回的窗口和重置时间，查询配额不发送生成请求。

## 开发

分别运行 `ocx start` 和 `pnpm --dir frontend dev:runtime`。开发网页默认位于 <http://127.0.0.1:5173/>，代理到 10100；若后端使用其他端口，在启动前端前设置 `OPENCODEX_PROXY_TARGET`。生产网页构建到 `frontend/dist-runtime/`。

迁移范围与验收状态见 [MIGRATION.md](MIGRATION.md)，模块职责和数据流见[架构文档](docs/lite-architecture.md)。许可证及原作者归属见 [LICENSE](LICENSE) 和 [CREDITS.md](CREDITS.md)。
