# OpenCodex Lite：Python 后端迁移方案

日期：2026-11-09。状态：方案待审阅，未实施。依据为 `lite` 当前工作树（HEAD `667662903`，包含已有未提交的精简修改），不是原版 2.99.0 的功能描述。

## 1. 目标与边界

已确定：Python 后端、TypeScript 前端、Node + pnpm 构建；Windows 本机使用；订阅和 API 分两类，订阅每个平台/渠道暂时单账号，API 可有多条独立配置。项目自行管理后台进程启停，只提供网页控制台，不需要托盘或独立窗口；Service Desk 只提供入口。已决定移除的 macOS/Linux 产品支持、账号池轮换不再迁回。

迁移保留本地应用接入网关、模型路由、协议适配、订阅登录和管理界面。后端重新实现，前端尽量复用。不把“未在首批迁移”当成“已经批准删除”；下文单列的扩展功能仍待取舍。

客户端接入管理是保留功能：用户在网关添加渠道后，项目应能为所选客户端写入网关地址、provider/model 配置和模型目录，按客户端需要使配置生效，并能恢复原配置；不能降级为只给用户一份手工配置示例。

## 2. 目标结构

| 层 | 建议选型 | 职责 |
| --- | --- | --- |
| 网关与管理 API | FastAPI / Uvicorn + Pydantic | 本地模型协议入口、管理接口、认证、静态网页 |
| 出站传输 | HTTPX AsyncClient；需要 WS 的渠道单独实现 WS transport | 连接复用、流式转发、超时与取消；不强行用一个 HTTP 库替代所有特殊传输 |
| 数据 | SQLite + aiosqlite，配置与必要文件状态单独管理 | 保持业务数据与运行时状态的区别；现有 usage JSONL 可沿用，SQLite 索引可重建，不为统一格式引入完整 ORM |
| 前端 | 现有 React + TypeScript + Vite + Tailwind | 保留组件与交互，拆开与旧后端源码的依赖 |
| 开发工具 | Python 用 uv；前端确定 Node + pnpm | 前端统一依赖与锁文件，不再依赖 Bun 构建 |
| 日常运行 | Python 提供 API 与构建后的网页 | Node 仅用于开发、构建，不是常驻网关进程 |

建议新增 `backend/`，暂保留 `gui/` 名称，避免迁移时无意义地搬目录。Python 内部分为 `api`（管理）、`gateway`（入口与路由）、`protocols`（协议编解码）、`providers`（渠道行为）、`auth`（凭据与刷新）、`transport`（出站与流生命周期）、`storage`（持久化）、`runtime`（进程与Windows接入）。模块随实际功能建立，不照搬旧目录数量。

不引入 LangChain/LangGraph：这里需要可控的协议转换与转发，不需要 Agent 编排。HTTPX 支持异步连接与流式响应，但上游响应关闭、断连取消仍由网关负责，见 [HTTPX 文档](https://www.python-httpx.org/async/)。

## 3. 保留什么

“保留”分为保留源码和保留行为。TS 后端的行为即使保留，其实现通常仍要用 Python 重写。

| 现有位置 | 保留内容 | 迁移处理 |
| --- | --- | --- |
| `gui/src/components/`、`styles/`、`i18n/`、页面组件 | 现有视觉与保留功能的交互 | 原样复用为主，不顺便换 UI 框架；删除功能的页面随对应功能一起清理 |
| `gui/src/api.ts`、`api-targets.ts`、`fetch-json.ts` | 管理端请求、错误与登录状态处理 | 先保持所需接口路径、响应字段、同源会话语义，再局部适配 |
| `gui/src/protocol-api.ts` 等跨 `../../src` 的导入 | 前端需要的 DTO、显示逻辑、纯校验函数 | 类型逐步由 Python OpenAPI 导出；前端纯函数移到 `gui/src/contracts/` 或功能模块，不保留对旧后端目录的运行时依赖 |
| `src/providers/registry.ts`、`src/providers/registry/` | 渠道预设、模型能力、默认参数 | 作为数据与规则迁移；供应商预设不等于协议 adapter，不为每个品牌复制一套传输实现 |
| `src/router.ts`、模型目录与别名逻辑 | 明确选择 provider、模型、别名与默认路由 | 在 Python 建立对应模块，保持模型 ID 和选择结果；高级路由单独取舍 |
| `src/codex/inject.ts`、`inject/restore.ts`、`journal.ts`、`catalog/` 及其他客户端接入模块 | 自动配置客户端使用网关、同步可选模型、应用配置与恢复原设置 | 保留所选客户端的完整接入流程，维护用户原有配置和历史；必要的配置修复与重启不能因删除自动升级而连带删除 |
| 现有配置、SQLite schema、脱敏协议样例 | 用户配置含义、有效凭据引用、接口与事件样例 | 按需兼容或显式导入；业务历史与运行时锁库分别处理，不能把所有 SQLite 文件直接复制运行 |
| `tests/` 中保留功能的行为用例与 fixtures | 流式、工具调用、认证刷新、错误处理的参考结果 | 用例意图可复用；Bun 执行代码另迁移。新增/改动测试需在实施时取得相应授权 |

当前前端不是完全独立：`protocol-api.ts` 引用后端协议定义，`codex-quota-utils.ts` 引用配额函数，`codex-restart.ts` 引用重启契约；其他页面还引用 usage、provider 等逻辑。不能先删除 `src/` 再补编译错误。

## 4. 必须重写的后端

| 现有位置 | Python 实现要承接的内容 | 关键边界 |
| --- | --- | --- |
| `src/server/index.ts`、`index/serve-options.ts`、`lifecycle.ts` | FastAPI 启停、请求分发、连接与后台任务生命周期 | 应用关闭和客户端断连能释放上游连接；保留实际采用的监听地址、代理设置与超时含义 |
| `src/server/responses/`、`chat-completions.ts`、`claude-messages.ts` | Responses/compact、Chat Completions、Messages/count_tokens、模型列表等核心入口 | 同协议直通优先；跨协议才转换，不能把所有请求压成 Chat Completions 导致信息丢失 |
| `src/adapters/`、`src/bridge/`、`src/protocols/`、`src/responses/` | 请求编解码、流式事件、工具调用与结果、reasoning/签名等渠道语义 | 保留事件顺序、调用 ID、结束原因与用量；流式重试不能重复交付已经输出的内容 |
| `src/oauth/`、`src/codex/auth-api/`、`auth-context.ts`、`local-credentials/` | 登录、回调/设备授权、刷新、替换账号、退出、凭据保存 | 单账号仍需刷新串行化和有效凭据切换；沿用当前授权链路，不借迁移擅自换接入方式 |
| `src/config/`、`providers/api-keys.ts`、`lib/keyring-native.ts` | 配置读写、API 凭据、Windows Credential Manager 接入 | 一个 API 配置一份 endpoint/key/model 身份；现有凭据记录的名称、编码与读取方式需验证兼容 |
| `src/server/management/`、`auth-cors.ts`、`gui-static.ts` | 所保留管理 API、网页会话和静态服务 | 保留 `/opencodex-session`、`/healthz` 等前端入口；网关 key 与管理登录凭据职责不混用 |
| `src/server/request-log*`、`src/usage/`、`src/storage/` | 日志、用量、保留/清理策略 | 请求结果和用量统计口径不变；运行时所有权与锁不混入业务历史导入 |
| `src/responses/state.ts`、`src/routing/history/` | Responses 续接与历史索引 | 续接状态关系到后续轮次，不是普通 usage 日志；既有会话能否跨运行时继续需要单独验证 |
| `src/service/`、`src/cli/` 中本机使用部分 | 项目自身的 Python 启停、重启、状态查询与 Windows 后台接入 | 生命周期由项目自己的管理入口负责；网页和 Service Desk 不拥有后台进程 |

### 单账号模型的具体迁移

- 订阅按平台/渠道保存一个 active credential；重新登录替换该渠道的有效凭据，不新增轮换候选。API 保存多条独立连接配置，不建设 API key 池。
- 当前 `src/oauth/store.ts` 已按 active slot 工作，`oauth-account-routes.ts` 拒绝新增池账号；`providers/api-keys.ts` 已要求额外 key 建另一条 provider 配置。新实现继承这些当前行为。
- `src/codex/auth-api/login-flow.ts` 仍可能写旧名 `codexAccountMode: "pool"`，实际发送用 `singleCodexAccountId`。导入应解释字段语义，不根据名字恢复轮换，也不删除整条 ChatGPT 登录路径。
- 当前本机原生客户端身份与网关订阅渠道身份仍有不同用途；迁移不能把两者不加区分地覆盖到一个 token 字段。用户界面保持“渠道单账号”，内部保留必要的凭据角色。

### 前端必须一起承接的接口

保留功能先按旧管理接口兼容，不要求 Python 复刻已决定删除功能的接口。`/api/accounts/events` 是 fetch 读取的 SSE，需保留页面消费的 ready/revision、account-selection 事件与心跳/断连行为。实时音频的 WebSocket 属于独立数据面，不应误当作管理 SSE。`gui/vite.config.ts` 的开发代理保留原 Host；生产由 Python 同源托管界面。旧静态服务还负责注入会话信息和区分 HTML/哈希资源缓存，不能只裸挂静态目录。

特殊渠道另核对实际传输。例如 `src/lib/provider-tls-profile.ts` 有特定渠道 TLS profile；这类行为不能承诺由 HTTPX 普通请求自动替代。首批不使用的特殊渠道先列未迁移，而不是留下能选却不能工作的入口。

## 5. 哪些删掉，何时删

### 已确定不进入新实现

| 内容 | 处理 |
| --- | --- |
| macOS/Linux 产品构建、安装、服务后端 | 不迁移；保留已完成的精简结果 |
| 同渠道多订阅账号轮换、账号池排序、阈值切号 | 不迁移；单账号的配额查看、错误状态、重新登录仍保留 |
| API key 池轮换 | 不恢复；多 key 用独立 API 配置表示，同一个 key 的必要重试不因此删除 |
| Tauri 桌面壳、托盘、独立窗口及其专属安装更新链 | 不迁移；清理 `desktop/` 及前端原生窗口桥接；后台启停与在途请求收尾在 Python 项目中实现，不随桌面壳删除 |
| 跨机连接、Remote Workspace、远程消息 | 当前不迁移，不保留空实现；未来出现具体需求再追加。本机应用接入和本机进程通信保留 |
| Advisor | 不迁移；让主模型咨询专家模型的编排不属于此网关职责 |
| Compatibility Lab | 不迁移 `src/lab/`、批量兼容性实验及结果矩阵；保留渠道连接测试、模型获取、错误日志，以及开发时对实际客户端的兼容性验证 |
| Combo | 不迁移 `src/combos/` 及对应虚拟多目标模型、分流与智能选择界面；保留明确的 provider/model 选择 |
| 替客户端升级软件版本 | 不迁移 Codex CLI 等第三方客户端的下载安装升级功能，包括 `src/codex/cli-update-plan.ts`、`src/cli/codex-cli-update.ts` 等；自动接入配置、模型目录同步、必要的配置生效/重启与恢复保留 |
| 官网发布、OpenCodex 自更新 | 不迁移 `docs-site/` 发布链和 `src/update/` 自更新功能；保留必要本地文档，维护 Lite 源码和依赖后手动重启，不使用原发行版更新通道 |

### 替代完成后退役的技术实现

| 内容 | 删除条件 |
| --- | --- |
| 旧 `src/` TS 服务端 | 保留功能均已有 Python 对应实现，前端跨目录依赖已消除；迁移中继续作为参考，不先整目录删除 |
| Bun 运行依赖、`bun.lock`、Bun 专属启动与 standalone 构建脚本 | Python 服务及 Node 构建/检查已接管；仍保留的脚本逐项改写，不把整个 `scripts/` 当垃圾删 |
| `@napi-rs/keyring` 等旧运行时桥接 | Python 已正确读取/保存所需 Windows 凭据；功能保留，旧 binding 退役 |
| 旧后端源码结构校验、Bun 专用测试启动器、无对应产品的 CI | 所保留行为有新验证入口后清理；有效协议 fixtures 与行为用例继续使用 |
| 旧 TS/npm 发行链 | 本机安装/升级入口确定后退役；不再让 npm 安装包承担 Python 网关运行时 |

### 建议裁掉、但尚未决定的产品功能

| 候选 | 现有范围 | 建议与影响 |
| --- | --- | --- |
| 简单跨渠道 fallback | 非 Combo 的备用目标策略 | Combo 已确定删除；是否仍需要失败后切换指定备用渠道尚未决定，同目标必要重试保留 |
| 图像、音频、搜索及特殊渠道 | 各自数据面和 adapter | 逐项按实际使用确定；普通消息里的图片输入不等于独立图像生成服务，不一起砍 |
| 两套网页 GUI | modern/classic GUI | 可只维护一套，但需要先选定；与删除桌面窗口无关 |

## 6. 后台进程与网页控制台（已确定）

OpenCodex 是独立后台网关，网页是它的可视化控制台。关闭浏览器或退出 Service Desk 不停止网关。没有 Tauri、pywebview、托盘或独立窗口，也不把管理功能移入 Service Desk。

项目提供自己的生命周期命令，具体命令名在实现时确定：

- `start`：启动后台实例，已经运行时返回现有状态，不重复启动。
- `stop` / `restart`：识别本项目实例，通过受控关闭让请求完成或在明确时限后取消，再清理连接与运行记录；重启后重新检查健康状态。
- `status`：区分正在运行、未运行和启动失败，并提供端口、健康与日志位置。
- 打开控制台：在浏览器访问本地地址。进程未运行时由独立启动入口启动，不能依靠已经不可访问的网页来启动自身。

启停实现归项目自身，可以由命令行或快捷方式调用；Service Desk 仅提供入口，不承担进程监督、OAuth、渠道设置或日志管理。开机启动是否启用、是否需要故障自动拉起尚未确定，不因“后台运行”默认新增。移除桌面壳后，仍要保留本项目所需的单实例、健康检查和退出收尾；不再复刻 Tauri 专用 sidecar/IPC 协议。

旧状态通过只读导入迁移到独立目录，不与 Bun 双写。默认不重新启用历史 inactive 账号；缺失环境变量/凭据库引用显示为待恢复，不擅自转存明文。是否带走历史 usage 和进行中的 Responses 会话分别确认。若保留 Codex 配置注入，沿用 `src/codex/inject.ts`、`inject/restore.ts`、`journal.ts` 的备份/恢复语义，避免覆盖用户后续编辑。

## 7. 实施顺序与完成标准

| 阶段 | 工作 | 完成标准 |
| --- | --- | --- |
| 0. 定范围 | 确认首批渠道与待删扩展功能；以当前 Lite 工作树为参考基线 | pnpm、独立后台进程及网页控制台已确定；其余每项明确保留、删除或后续迁移，不自动提交/清理现有修改 |
| 1. 解耦前端 | 收拢跨后端 TS 依赖；Node 构建；调整版本来源；保留现有界面 | 用旧后端也能正常操作；新锁文件固定依赖，迁移工具不顺带升级 UI 框架 |
| 2. API 完整路径 | Python 服务、配置、模型列表、一个通用 API 渠道、流式/非流式与管理页面 | 本地应用可实际对话及调用工具，页面可配置并查看状态；这只是阶段成果，不叫迁移完成 |
| 3. 订阅完整路径 | ChatGPT 渠道单账号登录/刷新/退出、必要原生协议与其他已选渠道 | 真实客户端与管理页面可用；重新登录不产生轮换池，已有 API 配置仍独立 |
| 4. 补齐已承诺行为 | 其余入口/适配器、日志配额、配置导入、项目自身启停与后台运行 | 所有保留功能有对应实现；关闭浏览器/SD 后网关继续工作，未迁移功能明确列出 |
| 5. 切换与清理 | 使用独立端口和数据目录完成对照，备份并显式导入，再切换本机接入；清理旧运行链 | 无需 Bun 即可开发/运行新版本；保留版本可回退且旧数据未被新程序原地破坏 |

建议验证重点：Responses/Chat/Messages 在所选渠道的直通与转换、流式工具调用/取消/结束事件、刷新与重新登录、重启后的配置与历史、管理页面会话。现有 `src/protocols/baseline.ts` 的 18 个协议组合可用于检查覆盖缺口，不代表首阶段必须把每个渠道都做完。新增和迁移测试的具体范围在实施时确认，本次不修改测试。

## 8. 包管理器（已确定）

本项目确定采用 pnpm，与 OpenFic、Personal Tracker、JavDB 保持一致。`gui/package.json` 的构建本来就是 `tsc + vite`；需要迁移的是安装锁文件、Bun 测试/辅助脚本和版本来源，不是重写 React。

当前本机项目核对：OpenFic 前端声明 `pnpm@11.8.0`；Personal Tracker 前端、JavDB 前端和采集扩展声明 `pnpm@11.25.0`，均有 pnpm 锁文件。共三个产品、四个构建单元。Pi、learn_ts、qq-agent-plus 使用 npm 锁文件。LLBot 根目录用 Yarn，其前端虽有 pnpm 锁，CI 使用 npm，不能仅凭锁文件归类。
