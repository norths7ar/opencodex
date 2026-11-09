# OpenCodex Lite

个人维护的 Windows 本地模型网关，基于 OpenCodex 2.99.0。保留 API 与订阅渠道接入，订阅每渠道单账号，API 使用独立配置。

当前运行代码仍为 Bun + TypeScript，网页控制台为 React。Python 后端、Node + pnpm 前端构建是迁移目标，尚未完成。

## 目录

- `backend/`：后端源码、CLI 启动入口和后端 TypeScript 配置。
- `frontend/`：网页控制台。
- `scripts/`：项目级开发工具。
- `tests/`：现有测试。
- `MIGRATION.md`：本项目的迁移计划。

根目录保留共用的依赖清单、锁文件和测试配置。

## 当前开发入口

- 后端：`bun run dev:proxy`
- 网页开发：`bun run dev:gui`
- 网页构建：`bun run build:gui`

本 fork 不沿用上游官网、赞助推广、GitHub 自动化或发布流程。许可证及原作者归属见 LICENSE、CREDITS.md。
