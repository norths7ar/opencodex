# OpenCodex Lite 架构

## 产品与实现范围

Lite 是 Windows 本机 AI 聚合网关。Python 后端提供协议入口、管理 API、网页资源和进程管理；React 控制台使用 Node.js 与 pnpm 构建。Service Desk 仅提供网页入口。

当前选定的上游是 OpenAI API、ChatGPT 登录、DeepSeek API、MiMo API、OpenCode Zen 和 OpenCode Go；自动接入客户端为 Codex 和 DSH。API 允许独立渠道，订阅按渠道单账号管理。通用协议能力服务于这些连接，不以模型所属品牌决定是否加载厂商认证。

`backend` 是 Python 顶层包，CLI 与 Windows 服务均从 `backend.runtime` 启动。旧 `backend/ocx`、TS `backend/src` 和 npm `backend/bin` 已移除；原实现通过 Git 历史参考（本轮调整前提交 `8c04a0cd4`）。所选能力按职责迁入，前端契约位于 `frontend/src/contracts`。

## 责任与依赖

| 模块 | 所有权 |
| --- | --- |
| app | 依赖组装、启动顺序、资源关闭和后台任务 |
| api | HTTP 输入输出、管理会话、类型化请求与响应 |
| configuration | 配置结构、版本转换和持久化；凭据记录与公开响应分开 |
| providers / auth | 服务定义、用户渠道、端点声明、认证和账号生命周期 |
| catalog | 发现快照、元数据、用户覆盖、生效视图和后台刷新 |
| gateway | 路由和执行流程、逻辑请求、实际发送、重试及取消 |
| protocols | 协议编解码、流式事件转换和原生直通 |
| integrations | Codex／DSH 配置投影、应用、状态与恢复 |
| policies | 模型控制、压缩、辅助调用和工作流限制 |
| telemetry | 实际发送记录、用量、定价和诊断 |
| runtime | 全局 CLI、Windows 服务、进程所有权和运行状态 |

管理 API 与协议入口调用应用服务，应用服务依赖明确的配置、目录、认证、传输和存储接口。协议转换不管理账号或客户端文件。内部辅助调用进入同一个执行器，复用取消和结算，无需绕回本机 HTTP。前端通过管理契约访问后端，不导入后端实现。

## 配置与契约

新配置版本为 2，区分渠道 ID、服务 ID、协议和认证方式。渠道 ID 与模型原始 ID 保持稳定；显示名称、别名和 Codex 选择器编码不改变实际身份。

协议选择保留显式单模型覆盖优先于渠道默认的语义。服务定义提供缺省端点与已确认的服务规则；用户显式覆盖优先，服务声明只适用于匹配的连接。模型发现与生成端点分别声明，避免从 Messages 路径推导模型目录地址。

发现快照、元数据补充和用户覆盖分别保存。生效值保留全局默认、渠道覆盖、单模型覆盖的层次；空集合、显式关闭与继承分别解释。成功获取完整目录替换发现名单，未知新模型不依赖静态名单准入。普通读取使用持久化快照，更新由每日后台刷新、连接变更和显式刷新触发。

公开管理响应通过独立 DTO 构造。凭据密文、自定义认证头和迁移剩余数据不成为响应字段。凭据编辑区分保持、替换、环境变量引用和清除；登录状态未确认与明确缺少凭据是不同状态。

配置升级先生成独立候选结果，保留原始输入。已识别字段转换到新模型；尚未映射的字段保留在迁移剩余结果中。剩余字段清零只证明转换覆盖完整，实际激活仍需对应执行器、客户端适配与管理 API 完成接线。

## 行为承接清单

| 行为 | 历史参考实现 | 当前所有者与核对要点 |
| --- | --- | --- |
| 独立 API 渠道与协议选择 | ocx/config.py、upstream_request.py、management.py | configuration / providers；不重建渠道，不改变凭据和模型身份；旧 responses 标志只在 adapter 缺失时解释 |
| 单账号 OAuth | ocx/oauth.py、subscription_channels.py | auth；刷新串行化、账号替换、退出和续聊发行身份保持一致 |
| 目录及模型覆盖 | ocx/models.py、model_policy.py、model_management.py | catalog；完整结果替换、失败保留、用户覆盖及未知模型可用 |
| 原生与跨协议调用 | ocx/gateway.py、protocols.py、protocol_streams.py | gateway / protocols；工具 ID、媒体、推理、事件顺序和结束原因 |
| 重试与 fallback | ocx/request_attempt.py、fallback.py | gateway；每次发送独立记录，输出开始后的重试边界保持 |
| 用量与价格 | ocx/usage_store.py、usage_pricing.py、spend_calls.py | telemetry；未报告与零值区分，按实际发送时间定价，订阅不套 API 价格 |
| 客户端接入与恢复 | ocx/clients.py、client_api.py、client_catalog.py、dsh_client.py | integrations；保留原配置、自定义目录指令、无关编辑和操作日志 |
| 子代理 | ocx/subagents.py、subagent_client.py、subagent_surface.py | integrations / policies；保留候选排序和通用消息处理，删除指导注入及强制工具版本控制；恢复逻辑仅撤销网关拥有的旧修改 |
| 压缩、搜索与视觉辅助 | ocx/compaction.py、search_gateway.py、auxiliary_calls.py | policies / gateway；内部调用的父任务归属、续聊、取消与结算 |
| CLI 和服务 | ocx/cli.py、service_cli.py、startup.py、runtime.py | runtime；start 前台、service 后台、移除先停止、无窗口启动 |
| 控制台 | frontend/src/App.tsx 及保留页面 | App / pages / components / contracts；统一状态、布局、帮助和编辑入口 |

Git 历史中的原版 `backend/src` 用于核对选中功能的语义；已确认的 Lite 改进优先，例如模型目录更新、可编辑上游协议、单账号和按需渠道。迁移中偶然引入的行为不自动成为保留要求。

## 实施阶段

1. 配置与契约：建立顶层包、版本化配置转换、管理 DTO、请求上下文和行为映射。
2. 渠道与目录：实现服务定义、端点解析、认证引用和统一目录视图。
3. 请求执行：替换 Gateway 协调层，接通流式、重试、fallback、续聊和结算。
4. 客户端与策略：整理 Codex、DSH 和子代理等增强能力，落实专项行为选择。
5. 前端：使用新契约与 Lite 路由，迁出全部旧后端导入。
6. 退役：移除旧后端、npm/Bun 运行入口和退出功能的专属资源，统一构建。
7. 验证与切换：隔离业务验证、获授权的真实调用、日常运行切换及统一文档整理。

这些阶段构成一次完整替换。验收同时检查旧源码移除后的构建运行，以及所选业务链的保存、生效、失败和恢复行为。

## 配置与契约工具

在仓库环境中执行 `python -m backend.configuration <旧配置路径>` 可预览转换摘要；加 `--output <新目录>` 会生成配置、凭据记录、未映射字段和转换报告。输出目录必须尚不存在，生成结果不会自动启用。退出码 0 表示字段转换完整，1 表示存在未映射字段，2 表示转换失败。

`python -m backend.api.schema --output <文件路径>` 导出管理契约的 JSON Schema。`ChannelView` 用于列表摘要，`ChannelDetail` 用于读取完整可编辑设置，`SaveChannel` 将配置和凭据操作分别提交。

## 当前实现与验证

当前记录描述已落地的模块替换，整体架构仍需代码审计。前端基础组件和样式归属尚未统一，旧样式、桥接覆盖与新组件并存；下一阶段按独立应用的目标整理责任边界，并复核前后端数据契约。具体已知缺口见 `MIGRATION.md` 的当前阶段与待办。

日常实例已切换到版本 2 配置和新模块入口；配置与私有凭据记录在同一 SQLite 事务内提交，首次激活保留旧 `config.json`。服务任务沿用原自动启动状态，启动参数更新为 `backend.runtime`。

生成请求由 Dispatcher 选择渠道，Executor 负责单个目标和 OAuth 刷新重试，PhysicalAttempt 持有每次实际发送及其结算。内部搜索、视觉和压缩沿同一执行链记账，逻辑请求与实际发送分别记录；HTTP 适配层负责请求解压、断连和响应生命周期。模型目录成功刷新后按生效差异同步已启用的客户端。

隔离验证覆盖配置转换、凭据保持、模型目录、协议分派、fallback、续聊、压缩、搜索辅助、取消竞态、用量归属、客户端恢复与无窗口进程启停。浏览器已核对七个主入口、协议编辑保存、模型筛选跳转和统一编辑框。实际目录／额度读取经用户授权执行；本轮未发送真实生成请求。原测试文件保持原样，历史测试中的旧入口不作为新实现的验收依据。

### 子代理的有效决策

保留候选模型排序，职责限于目录投影。Codex 候选数量可以由其配置调整，本轮沿用现有数量。排序操作不附带提示词或工具版本修改。

删除指导文字生成、developer 消息注入、自定义指导模板、主动委派提示，以及 v1/base/v2 选择器和强制版本改写的专用代码与设置。工具接口版本由 Codex 自身配置决定。旧功能通过 Git 历史参考，不保留隐藏的运行开关。

保留实际请求中工具格式的识别和通用协议处理，包括可读任务转换、原生目的地密文保留与不可读任务反馈。配置升级明确丢弃已退出的开关；客户端恢复仅依据既有操作日志撤销网关拥有且未被用户修改的字段。当前 Codex 不参与实施验证，恢复流程用隔离目录验证。
