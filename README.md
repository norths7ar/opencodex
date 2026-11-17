# OpenCodex Lite

OpenCodex Lite 是基于 OpenCodex 2.99.0 的 Windows 本机模型网关。Python 后端提供协议转发和管理 API，React 控制台管理渠道、模型和客户端接入。Service Desk 只负责打开网页，关闭网页不影响网关。

Lite 面向 Windows 单机使用，自动接入客户端为 Codex 和 DSH，ChatGPT 使用单账号。它不提供原版的多账号池轮换、其他客户端专用接入、独立托盘或安装包，也不支持 macOS/Linux 产品运行。控制台中的 Routing 和 Key Limits 仍为待实现入口。

## 安装与运行

从源码目录运行，使用项目锁定的 Python 3.14.10、Node.js 26.13.1 和 pnpm 12.21.0；uv 需为 0.13.8 或更新版本。使用 PowerShell 7，在仓库根目录执行：

```powershell
uv sync --locked
./scripts/register-cli.ps1
```

注册后执行 `ocx service update`，按锁文件同步依赖、独立构建网页并发布。首次安装只发布，不自动安装或启动服务；已有受管后台实例会在发布后重启并检查健康状态。前台实例请先停止，再更新和重新启动。

注册脚本让 `ocx` 和 `opencodex` 通过项目虚拟环境的 Python 调用 CLI 模块，避免更新项目包时占用包内的命令启动器。若首次注册更新了用户 PATH，重新打开终端即可在任意目录运行。请保留源码目录和 `.venv`；移动项目后需重建虚拟环境并重新注册，已安装的服务还需运行 `ocx service repair` 更新路径。

```powershell
ocx start
```

打开 <http://127.0.0.1:10100/>。网关仅监听回环地址，首次启动默认使用 10100；可用 `ocx start --port 10120` 指定其他端口。前台进程可用 Ctrl+C 退出，也可从另一终端使用 `ocx status`、`ocx stop`。`ocx restart` 停止已有实例后在当前终端前台运行；后台服务重启使用 `ocx service restart`。无参数的 `ocx` 显示帮助。

### 后台服务

使用 `ocx service install` 或首页的“自动后台启动”启用后台运行。它通过当前用户的 Windows 计划任务在登录后启动网关，不弹出常驻窗口；不是登录前运行的系统服务。用 `ocx service status` 查看状态，`ocx service restart` 重启，`ocx service remove` 停止并移除任务。命令注册和服务安装是两件事：`./scripts/register-cli.ps1 -Remove` 只撤销命令入口。

默认运行目录为 `%LOCALAPPDATA%\OpenCodexLite`。用 `--state-dir` 或 `OPENCODEX_LITE_HOME` 可选择独立目录。日常配置以该目录下的 `configuration.sqlite3` 为准；同目录中已有的 Lite `config.json` 只在首次激活时作为迁移来源，之后不再参与配置读取。

## 更新当前安装

已有安装若仍使用指向 `ocx.exe` 的旧启动器，先在仓库根目录重新执行一次 `./scripts/register-cli.ps1`。

在任意目录运行 `ocx service update`，更新该命令所注册的源码安装（不自动拉取 Git）。独立运行目录使用 `ocx service update --state-dir <目录>`。命令依次同步锁定依赖、执行前端生产代码类型检查与构建、校验完整产物、发布网页，并在实例受后台服务管理且正在运行时重启。未运行的实例不会被启动，前台实例需先停止。自动启动偏好和当前端口保持不变。

该流程使用已安装的项目运行时。更换 Python 或更新被运行中进程占用的原生依赖时，先停止服务，在仓库目录的终端中完成 `uv sync --locked`，再更新网页并按需启动服务。

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

### 开发检查

在仓库根目录安装开发依赖并运行检查：

```powershell
uv sync --locked
pnpm --dir frontend install --frozen-lockfile
pnpm lint
pnpm format:check
pnpm typecheck
pnpm test
pnpm test:tools
```

`pnpm typecheck` 依次运行后端 Pyright 和前端 TypeScript 检查；Pyright 按 `pyproject.toml` 中的 standard 配置检查 `backend/`，前端同时检查生产代码和测试。Ruff 与 ESLint 负责 lint，Ruff 与 Prettier 负责格式。编辑器使用 Pylance 时选择项目的 `.venv` 解释器。正式更新命令不代替这些完整开发检查。

### 测试

当前正式测试位于后端 `tests` 和前端 `frontend/tests`，也可在根目录运行 `pnpm test` 串联两者。后端异步用例使用已有 AnyIO 提供的 pytest 插件和 asyncio 后端；前端使用 Vitest 与 happy-dom，`pnpm --dir frontend typecheck` 同时检查生产代码和测试。原版中仍适用的界面行为测试已迁入当前入口，依赖已退役实现的测试和旧 Bun 运行器已移除。

测试围绕正常使用、失败和恢复组织：

- 核心调用：渠道配置与持久化、工具调用与多轮续聊、fallback／取消，以及 Codex／DSH 接入同步和恢复。
- 日常管理：目录刷新与退避、模型覆盖和身份冲突、ChatGPT 登录与刷新、日志用量及费用、存储清理与恢复、服务管理和网页发布回滚。
- 扩展与首次使用：搜索、视觉辅助、上下文压缩、子代理，以及旧配置激活、导入和脱敏检查。
- 前端交互：异步请求与切页、刷新失败重试、编辑草稿与版本冲突恢复、会话续期、语言包与页面模块加载失败。

前端测试按当前页面可触发的行为取舍。共享控件保留键盘选择、取消、焦点恢复及防误操作；数据层保留超时恢复、缓存回访和旧响应竞态；额度按 Lite 实际提供的 ChatGPT 窗口验证。旧供应商场景、旧服务器兼容、重复的状态组合及精确 DOM／几何枚举不作为维护目标。

后端用例使用真实业务实现、SQLite 和客户端文件，外部上游通过可控传输替换。每次测试创建仓库根 `.tmp/20261117-lite-workflow-*` 隔离目录并在结束后清理，日常配置和账号不参与测试。共享边界阻止真实网络连接和子进程启动；前端默认阻止未模拟的 HTTP 请求。运行依赖安装命令本身仍可能访问包仓库。

这些是后端 HTTP／领域流程和前端组件测试。服务与发布测试替换操作系统任务、进程和构建边界；账号测试使用模拟授权。因此不代表真实浏览器布局、真实供应商、实际 Codex／DSH 程序、OAuth 或 Windows 计划任务已验收。流式故障用例直接驱动 ASGI 请求与写端，验证交付和清理逻辑，不代替真实 TCP 断连验收。新增用例应围绕实际使用的正常、失败和恢复过程，避免复制历史补丁清单。

独立工具测试使用 `pnpm test:tools`：Codex 排队助手以假 CLI 和隔离目录验证参数、目标选择及失败反馈，不发送真实消息，Windows 默认使用项目基线 PowerShell 7；需要单独验证其他 shell 时可设置 `CODEX_QUEUE_TEST_SHELLS`。隐私扫描测试仅验证既有文本检测函数，不扫描仓库。该扫描器尚未适配 Lite 的全部文件类型和规则，不作为完整隐私检查门槛。

模块职责、数据流和关键设计见[架构文档](docs/lite-architecture.md)。迁移和阶段验收记录保留在 Git 历史中。本项目源自 [OpenCodex](https://github.com/lidge-jun/opencodex)，保留原有版权声明，许可证见 [LICENSE](LICENSE)。
