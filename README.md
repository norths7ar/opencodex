# OpenCodex Lite

个人维护的 Windows 本地模型网关，基于 OpenCodex 2.99.0。保留 API 与订阅渠道接入，订阅每渠道单账号，API 使用独立配置。

当前运行代码仍为 Bun + TypeScript，网页控制台为 React。Python 后端、Node + pnpm 前端构建是迁移目标，尚未完成。

## 当前开发入口

- 后端：`bun run dev:proxy`
- 网页开发：`bun run dev:gui`
- 网页构建：`bun run build:gui`

本 fork 不沿用上游官网、赞助推广、GitHub 自动化或发布流程。许可证及原作者归属见 LICENSE、CREDITS.md。
