# OpenCodex Lite

个人维护的 Windows 本地模型网关，基于 OpenCodex 2.99.0。保留 API 与订阅渠道接入，订阅每渠道单账号，API 使用独立配置。

后端正在按原 OpenCodex GUI 和业务实现迁移。当前提供独立 API 渠道配置、实时模型目录与选择、Chat Completions／Responses／Messages 转发、Codex 配置应用与恢复，以及 OpenAI/ChatGPT 单账号 OAuth 接入。网页只保留新版，暂缓功能的导航入口隐藏。请求日志、用量统计和 ChatGPT 配额已接入；其他订阅渠道及客户端仍待迁移；旧 TypeScript 实现继续作为行为基准。

## 目录

- `backend/ocx/`：Python 后端与进程管理入口。
- `backend/src/`、`backend/bin/`：迁移中的旧 TypeScript 后端。
- `frontend/`：React 新版网页控制台，入口为 `src/main.tsx`。
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

启动后访问 `http://127.0.0.1:10101/`。关闭终端或浏览器不会停止后台进程。再次 `start` 返回已有实例；`restart` 等待旧进程退出再重启。进程管理由项目命令提供，不依赖浏览器或 Service Desk 存活。

默认只监听 `127.0.0.1`，运行记录和日志位于 `%LOCALAPPDATA%\OpenCodexLite`，不读取或改写旧 Bun 配置。`--port` 指定端口，端口占用会报错而不自动换端口；`--state-dir` 或 `OPENCODEX_LITE_HOME` 可指定独立运行目录。`--json` 输出机器可读状态。`status` 区分运行、停止、失败和无响应，失败或无响应时退出码为 1。

网页构建输出到 `frontend/dist-runtime/`，与旧控制台的 `dist/` 隔离。网页未构建时，后端仍可启停，首页会显示构建命令。

### 自动后台运行

在 Home → Startup 启用自动启动，或执行 `uv run ocx startup install`。项目为当前 Windows 用户注册计划任务，在登录、重新连接或解锁后启动隐藏的监督进程；监督进程复用正在运行的网关，并在意外退出后重试。凭据仍由当前用户解密，任务不在用户登录前运行。

`uv run ocx startup status` 查看注册与运行状态；`repair` 更新任务路径和启动环境；`disable` 暂停自动启动，`enable` 恢复，`remove` 移除任务。禁用或移除任务保留当前网关运行；`ocx stop` 明确停止网关及本轮监督，之后手动启动或下次登录可恢复已启用的自动运行。更换项目或虚拟环境位置后使用 `repair`，修复不会重新启用已禁用的任务。

自动启动保存网关所需的代理、证书和已配置凭据环境变量，使用当前用户 DPAPI 加密。相关环境改变后重新执行 `repair`。各命令支持 `--state-dir` 和 `--port`，不同数据目录的任务独立管理。

## API 渠道与客户端

在原供应商页面通过预设或自定义表单添加 API 渠道，编辑默认渠道、默认模型、凭据及协议配置。每条 API 渠道使用一份凭据，多份凭据建立独立渠道。原 JSON 配置编辑器、模型展示名和别名流程保留。连接探测读取模型目录；生成测试是另一个需要明确确认的操作。

模型发现以成功获取的完整上游目录为准，刷新会更新缓存并移除上游已不再列出的条目。静态预设补充模型信息，用户自定义模型单独保留；新模型无需等待 Lite 更新。查询失败显示失败状态并保留最后成功快照，默认模型离开目录时提示处理。

手动输入的上游 API Key 使用当前 Windows 用户的 DPAPI 加密保存，不回显到网页，也不写入 Codex 配置。开发验证可使用项目根目录 `.env` 中的测试凭据；这不替代产品中的供应商管理流程。

网关仅监听本机回环地址，沿用原 OpenCodex 的本机数据面接入方式，本地应用不需要另设网关 Key；浏览器管理操作仍使用独立会话。模型使用 `渠道ID/模型ID` 或配置的别名；Codex 目录中的模型 ID 按原规则处理内部斜杠，发生歧义时拒绝猜测。模型选择和禁用控制目录可见性，不阻止客户端明确指定该模型。

转发优先保留原生协议，只有上下游协议不同才转换。当前提供 Chat Completions、Responses、Messages 的 JSON／SSE 与工具调用桥接；转换后的 Responses 续接状态保存在独立 SQLite 中；`store:false` 只使用 24 小时内存缓存，不持久化。`responses/compact` 和 `messages/count_tokens` 目前要求相应原生上游。渠道专有传输、复杂 opaque reasoning 及其他尚未承接的协议分支仍需逐项迁移，不能把通用协议可用等同于所有预设已完整验证。

在应用接入的 Codex 页面预览目标配置，再应用、同步或恢复。默认目录遵循 `CODEX_HOME`，否则为用户 `.codex`；也可指定独立目录。接入沿用原版 `openai_base_url`，保持 Codex 的内置服务商身份，不重写登录或历史。配置与模型目录、缓存一起管理；后续供应商／模型修改会同步已启用的 Codex，页面区分保存成功与同步待处理。必要时由用户点击重启使运行中的客户端加载更新。

恢复按日志撤回本项目管理的字段，保留无关后续编辑；被管理字段发生外部修改时报告冲突。正常停机会恢复 Codex 配置与模型缓存，同时保留启用意图，下一次启动重新应用；显式关闭接入则不会重新应用。启动也会继续上次未完成的应用或恢复意图。

已有外部 `model_catalog_json` 作为只读来源参与目录合成，其原有模型条目和按模型绑定的指令保留；源文件仍由用户原有工具维护。后续同步重新读取源文件，恢复时撤回目录引用。Lite 不提供 Codex Set 的 Prompt 编辑功能。

Route → DSH 复用原配置预览和回滚流程，管理 `llm-pi-ai.providers.opencodex`。已有 Desktop profile 时写入 `$DSH_HOME/profiles/desktop/cordis.patch.yml`，否则使用 `settings.yaml`；其他 provider、默认模型和无关 YAML 设置保留。应用前显示修改计划，禁用移除受管片段，回滚中心可恢复历史快照；整份文件有后续变更时需明确确认恢复。DSH 配置独立于网关启停保留，实际客户端按原有热加载流程生效。其余客户端入口暂时隐藏。

## ChatGPT 订阅

在供应商页面选择“添加供应商 → 账号 → OpenAI / ChatGPT”，使用浏览器授权或设备码登录。浏览器授权使用本机 1455 回调端口；端口被其他登录占用时可选择设备码。网关独立保存 Windows 加密凭据，自动刷新到期令牌，不借用本机 Codex 登录。

每个渠道保存一个账号。重新登录会先获取新账号的模型目录并做轻量生成验证，成功后替换旧账号；取消或验证失败保留原账号。账号页面可重新登录和退出。订阅请求使用 ChatGPT 原生 Responses 通道，其他入口按协议转换；账号和供应商页面展示上游实际返回的配额窗口、重置时间及刷新状态。

订阅模型目录直接查询上游。ChatGPT 会根据查询中的客户端协议版本返回可用模型；渠道设置中的“模型发现协议版本”允许调整该参数，留空使用项目内置基准。模型 ID 不受静态白名单限制。

## 请求日志、用量与配额

“Usage & Logs”展示请求状态、渠道和模型、耗时、上游报告的 token 用量、缓存读取及费用估算。流式结束、上游限额、失败和客户端取消分别记录；筛选和请求详情沿用原版界面。统计按日期、模型、供应商和客户端聚合，缺少用量的请求仍计入请求数，缺少价格时显示未知。

请求元数据保存在运行目录的 `usage.sqlite3`，重启后继续累计；消息正文和凭据不写入账本。费用采用原版按供应商匹配的定价目录及用户明确覆盖，包含上下文长度和服务档位规则，是估算而非账单。ChatGPT 订阅用量不套用 API 单价。旧版历史账本的导入放在后续迁移阶段。

账本默认保留全部历史。运行目录 `config.json` 可设置 `usageLedgerMaxBytes`（至少 1 MiB），重启生效；超过上限时向约 90% 收缩，保留进行中的请求和最新完成记录，并在统计中显示历史截断情况。

ChatGPT 配额来自账号查询和已认证响应头，按上游返回的窗口展示。刷新失败保留仍有效的最近读数并提示失败；登录替换和退出会清除旧账号的配额缓存。额度查询不发送生成请求。

## 开发

两个终端分别运行 `uv run ocx serve` 和 `pnpm --dir frontend dev:runtime`，在 `http://127.0.0.1:5173/` 开发网页。前端开发服务器默认代理到 `10101`；后端使用其他端口时，设置 `OPENCODEX_PROXY_TARGET` 后再启动前端。`serve` 是前台运行，Ctrl+C 退出。

旧后端仍可用 `pnpm legacy:dev` 调用原 Bun 入口；这不是 Python 运行链。

本 fork 不沿用上游官网、赞助推广、GitHub 自动化或发布流程。许可证及原作者归属见 LICENSE、CREDITS.md。
