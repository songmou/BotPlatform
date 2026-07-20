# Changelog

本项目按功能阶段记录主要变化。

## Unreleased

- 默认文字模型改为 DeepSeek V4 Flash，保留 V4 Pro 手动模式。
- 模型档案增加 `enabled`，Ollama、图片理解、向量检索和自动记忆提取改为可选能力。
- 新增 `check-config` 启动诊断，区分必需错误和可选能力警告。
- Ollama 生命周期改由用户管理，BotPlatform 不再自动运行 `ollama serve`。
- 移除个人业务集成及其专属凭据、任务和依赖；公开配置不再包含真实租户或内部地址。
- 浏览器和 Codex 插件默认关闭，全部主动定时任务默认关闭。
- 增加 MIT 许可证、安全策略和 GitHub Actions 测试。

## 2026-07 Platform consolidation

- 将租户、会话、设置、订阅、待办、知识、记忆和审计统一迁移到 SQLite，启用 WAL、外键和事务。
- 增加租户隔离的 workspace、文件工具、命令沙箱和微信审批恢复流程。
- 增加 FTS5/向量混合知识检索，以及可查看、确认和遗忘的长期记忆。
- 增加固定脚本注册、后台运行记录、定时任务和主动通知 CLI。
- 增加插件框架、隔离浏览器自动化和 Codex 后台开发任务管理。
- 增加模型协议契约、Ollama 与 OpenAI-compatible 适配器、调用观测和按用户模型模式。
