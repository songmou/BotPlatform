# AGENTS.md

本文件为 AI 智能体在本仓库中工作提供指引。

BotPlatform 是一个轻量级的AI中台的项目，外加一个 FastAPI Web 管理面板。支持多租户，具备可审批的本机工具、知识库、记忆、定时任务、插件以及 MCP。

## 导入根路径
- 所有真实代码位于 `src.core.*`（bot/services/storage/tooling/…）与 `src.api.*`（Web 面板）之下。请从这些路径导入，**不要**从 `src.*` 导入。

## 入口
- `main.py` → 主入口（`src.core.application.cli`）。也可用 `python -m src`。
- `web.py` → FastAPI 管理面板，默认 `--host 127.0.0.1 --port 8080`。
- `./start.sh` / `.\start.ps1` 封装 `main.py`；`./start.sh web ...` 运行 `web.py`。

## 配置：无热重载，冻结数据类
- 修改 `config/*.json` 中的任何内容都需要完整重启进程 —— 没有热重载。
- 内置后台脚本没有集中式配置文件：每个脚本是 `src/core/jobs/<目录>/` 下的自包含文件夹，由同目录的 `script.json` 清单声明，启动时自动扫描发现（无 `script.json` 的目录被忽略，可作共享辅助包）；`config/scripts.json` 已移除。
- 改完代码后，务必确认已真正杀掉旧进程再重启（同端口上残留的旧服务是常见坑）。`web.py`/`uvicorn` 运行时不带 `--reload`。
- `ProjectConfig` 及所有配置数据类都是 `@dataclass(frozen=True)`。若要在运行时更新 `config.skills`/`config.mcp_servers`（例如 API 写入后刷新），必须调用 `config.update_skills(...)` / `config.update_mcp_servers(...)` —— 它们会先用 loader 的校验规则验证，再就地替换列表内容（引用不变，持有者自动可见）。禁止用 `object.__setattr__` 绕过冻结校验；直接赋值会抛出 `FrozenInstanceError`。

## 测试
- 使用标准库 `unittest`，**不是** pytest。运行：`python -m unittest discover -s tests -v`。
- CI 在 Python 3.10 和 3.12（ubuntu）上运行同一命令。测试无需真实 API Key、Ollama、微信或个人数据。
- 部分测试仅适用于 POSIX，在 Windows 上会失败（如 `folder/code.py` 这类路径分隔符断言，以及 `0o600`/`0o700` 权限检查）。这些 Windows 失败属于环境问题，并非回归 —— 请以 CI 目标环境的行为为准。
- **任何代码变更完成后，必须运行与改动相关的测试**（全量 `python -m unittest discover -s tests -v`，或聚焦的单模块如 `python -m unittest tests.test_web_api`），并在回复中给出结果。若测试失败，先修复再收尾；环境性失败需注明并以 CI 行为为准。

## 密钥与数据
- `DEEPSEEK_API_KEY` 存放于 `data/system/model.env`（必须是 `0600` 权限，且仅含单个密钥）。微信登录凭证为 `data/system/credentials.json`。
- MCP 请求头密钥（如 Authorization）存于 `data/system/mcp_headers.json`（由 `src/core/config/mcp_headers.py` 经 KeychainService 管理）；`config/mcp_servers.json` 中的 `headers` 永远写入空值，读取时自动合并。
- 切勿提交、记录密钥，也不要把密钥写入 `config/*.json`。`data/` 已在 gitignore 中。

## 约定
- 面向用户的字符串以及错误/`HTTPException` 消息一律用中文书写。文档字符串（docstring）和代码注释用英文。
- 远程 OpenAI 兼容/模型 URL 必须是 HTTPS；仅本机回环（loopback）可用 HTTP。

## 架构说明
- 内置工具是「名称→字典」的定义，分发到 `_tool_{name}` 方法（而非类）。一个智能体可用的工具 = 内置 + 插件 + MCP。
- MCP：异步的 `mcp` SDK 通过一个后台事件循环线程桥接到同步运行时；每个服务器连接由一个专属的生命周期任务持有（anyio 要求 transport 的 cancel scope 在同一任务中开启和关闭）。MCP 工具名采用 `{server_id}__{tool_name}` 命名空间。
- Skill 是注入到系统提示词中的提示片段；MCP 服务器则是真正的工具来源。
