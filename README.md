# OpenCodex Lite

OpenCodex Lite 是基于 OpenCodex 2.99.0 的 Windows 本机模型网关。Python 后端提供协议转发和管理 API，React 控制台管理渠道、模型和客户端接入。Service Desk 只负责打开网页，关闭网页不影响网关。

## 安装与运行

从源码目录运行，使用项目锁定的 Python 3.14.10、Node.js 26.13.1 和 pnpm 12.21.0；uv 需为 0.13.8 或更新版本。使用 PowerShell 7，在仓库根目录执行：

```powershell
./scripts/update.ps1
./scripts/register-cli.ps1
```

更新脚本按锁文件同步依赖、独立构建网页并发布。首次安装只发布，不自动安装或启动服务；已有受管后台实例会在发布后重启并检查健康状态。前台实例请先停止，再更新和重新启动。

注册脚本将 `ocx` 和 `opencodex` 指向项目虚拟环境。若首次注册更新了用户 PATH，重新打开终端即可在任意目录运行。请保留源码目录和 `.venv`；移动项目后需重建虚拟环境并重新注册，已安装的服务还需运行 `ocx service repair` 更新路径。

```powershell
ocx start
```

打开 <http://127.0.0.1:10100/>。网关仅监听回环地址，首次启动默认使用 10100；可用 `ocx start --port 10120` 指定其他端口。前台进程可用 Ctrl+C 退出，也可从另一终端使用 `ocx status`、`ocx stop`。`ocx restart` 停止已有实例后在当前终端前台运行；后台服务重启使用 `ocx service restart`。无参数的 `ocx` 显示帮助。

### 后台服务

使用 `ocx service install` 或首页的“自动后台启动”启用后台运行。它通过当前用户的 Windows 计划任务管理网关，不弹出常驻窗口。用 `ocx service status` 查看状态，`ocx service restart` 重启，`ocx service remove` 停止并移除任务。命令注册和服务安装是两件事：`./scripts/register-cli.ps1 -Remove` 只撤销命令入口。

默认运行目录为 `%LOCALAPPDATA%\OpenCodexLite`。用 `--state-dir` 或 `OPENCODEX_LITE_HOME` 可选择独立目录。日常配置以该目录下的 `configuration.sqlite3` 为准；同目录中已有的 Lite `config.json` 只在首次激活时作为迁移来源，之后不再参与配置读取。

## 更新当前安装

在仓库根目录运行 `./scripts/update.ps1`，或 `pnpm update:local`。独立运行目录使用 `./scripts/update.ps1 -StateDir <目录>`。脚本依次同步锁定依赖、检查类型、构建、校验完整产物、发布网页，并在实例受后台服务管理且正在运行时重启。未运行的实例不会被启动，前台实例需先停止。自动启动偏好和当前端口保持不变。

发布先放齐新资源，再原子替换网页入口；旧资源保留至少七天，后续发布按记录清理已过期且不再被保留版本引用的文件。首次发布会登记已有资源。发布或健康检查失败会报告具体阶段；入口切换失败或新实例验证失败时恢复旧网页入口，但不会声称回滚已修改的 Python 源码或依赖。

## 渠道和模型

在“渠道”中添加连接。内置连接覆盖 OpenAI API、ChatGPT 登录、DeepSeek API、MiMo API、OpenCode Zen 和 OpenCode Go；也可配置兼容端点。每条 API 渠道可单独配置地址、凭据和上游协议，多个 API 凭据分别建渠道。网关按需要在 Chat Completions、Responses 和 Messages 间转换，上下游协议一致时优先使用原生路径。

ChatGPT 支持浏览器或设备码 OAuth，由网关单独维护一个账号和令牌，不使用本机 Codex 登录。手动输入的 API Key 和订阅凭据由当前 Windows 用户加密保护。环境变量凭据从进程环境读取，不自动加载源码仓库的 `.env`；后台服务在安装或修复时保存所引用的凭据环境变量。渠道探测用于读取目录；生成验证是另一个操作。

Models 页面从持久化快照读取模型目录，不会因打开页面而访问上游。启用发现的渠道由后台每日刷新，也可手动刷新；成功后用完整目录替换旧快照，失败时保留上次成功结果并显示状态，后台以独立退避周期重试。静态目录和用户模型配置仍可独立维护。客户端使用目录返回的模型选择器或已配置别名；普通模型保持 `渠道 ID/模型 ID` 形式，名称冲突的模型由目录提供无歧义选择器。

## 客户端接入

在“客户端”中选择 Codex 或 DSH，先预览配置变更，再应用或恢复。模型目录变化后，已接入客户端的受管内容自动同步；外部修改产生的冲突需先检查。配置保存和客户端应用分别反馈结果，一个客户端失败不会阻止另一个同步。

- **Codex**：保留内置 OpenAI 服务商身份，通过 `openai_base_url` 接入，管理入口和模型目录，不更改登录。默认使用 `CODEX_HOME` 或用户的 `.codex`，也可以指定独立目录。恢复使用操作记录撤回 Lite 管理的改动；对发生外部修改的受管设置报告冲突。
- **DSH**：接入配置管理 `llm-pi-ai.providers.opencodex`，有 Desktop profile 时使用其补丁文件，否则写入 `settings.yaml`。保留其他 provider、默认模型和无关配置。

## 日志、用量和配额

Usage & Logs 记录请求状态、渠道与模型、耗时和上游报告的 token 用量，在 `usage.sqlite3` 中保留历史。费用由匹配的价格规则估算，不是账单；未匹配价格时显示未知，ChatGPT 订阅不套用 API 单价。账号配额展示上游返回的窗口和重置时间，查询配额不发送生成请求。

## 开发

分别运行 `ocx start` 和 `pnpm --dir frontend dev:runtime`。开发网页默认位于 <http://127.0.0.1:5173/>，代理到 10100；若后端使用其他端口，在启动前端前设置 `OPENCODEX_PROXY_TARGET`。`pnpm --dir frontend build:runtime` 只生成 `frontend/build/` 候选产物，不改动正在服务的网页；`pnpm --dir frontend preview` 预览这份候选。正式网页位于 `frontend/dist-runtime/`，通过[更新当前安装](#更新当前安装)流程发布。

迁移范围与验收状态见 [MIGRATION.md](MIGRATION.md)，模块职责和数据流见[架构文档](docs/lite-architecture.md)。本项目源自 [OpenCodex](https://github.com/lidge-jun/opencodex)，保留原有版权声明，许可证见 [LICENSE](LICENSE)。
