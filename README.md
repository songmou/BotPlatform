# BotPlatform：微信 iLink 多模型机器人

BotPlatform 是一个运行在自己电脑上的微信 AI 机器人。它通过微信 iLink 接收私聊消息，默认调用 DeepSeek V4 Flash，并提供多租户会话、可审批的本机工具、知识库、长期记忆、待办、定时任务以及可选插件。

默认安装只需要 Python 和 DeepSeek API Key。Ollama、图片理解、向量检索、浏览器自动化和 Codex 开发任务均为可选能力，不配置也能正常使用文字聊天。

## 5 分钟快速开始

### 1. 准备环境

- macOS 或 Windows
- Python 3.10 或更高版本
- 一个有效的 DeepSeek API Key

macOS/Linux：

```bash
git clone <你的仓库地址>
cd BotPlatform
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Windows PowerShell：

```powershell
git clone <你的仓库地址>
cd BotPlatform
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 配置 DeepSeek Key

macOS/Linux：

```bash
mkdir -p data/system
printf '%s\n' 'DEEPSEEK_API_KEY=替换为你的密钥' > data/system/model.env
chmod 600 data/system/model.env
```

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force data\system | Out-Null
Set-Content data\system\model.env 'DEEPSEEK_API_KEY=替换为你的密钥'
```

密钥文件位于被 Git 忽略的 `data/` 目录。不要把 Key 写进 `config/models.json`、README、Issue 或提交记录。

### 3. 检查配置

```bash
./start.sh check-config
```

Windows：

```powershell
.\start.ps1 check-config
```

检查结果分为：

- `可用`：已经具备的能力；
- `需配置`：可选能力不可用，但不影响核心文字聊天；
- `错误`：必须修复，程序会指出要修改的 `config` 文件或密钥文件。

检查不会向 DeepSeek 发送请求，也不会产生模型费用。

### 4. 启动并扫码

```bash
./start.sh
```

Windows：

```powershell
.\start.ps1
```

首次启动会显示微信二维码。扫码并在手机确认后，登录凭据保存在 `data/system/credentials.json`；后续启动会直接复用。现在向机器人发送一条私聊文字即可开始使用。

## 默认模型与切换

默认配置位于 [`config/app.json`](config/app.json) 和 [`config/models.json`](config/models.json)：

- `auto`：使用全局默认档案，开箱为 `deepseek-v4-flash`；
- `flash`：使用 DeepSeek V4 Flash；
- `pro`：使用 DeepSeek V4 Pro 思考模式；
- `local`：使用用户启用的 Ollama 档案，默认不可用。

DeepSeek V4 的模型标识与接口说明见 [DeepSeek API 文档](https://api-docs.deepseek.com/quick_start/pricing)。每位微信用户的模式独立保存在 SQLite 中：

```text
/model
/model auto
/model flash
/model pro
/model local
```

`active_model` 与 `fallback_model` 默认都指向 Flash，因此不会在失败时自动升级到费用更高的 Pro。需要自定义兜底时，可把 `fallback_model` 改为另一个已启用档案。

## 可选：启用 Ollama

BotPlatform 不会安装、启动或停止 Ollama。请先自行安装并启动服务，然后拉取适合机器的聊天/视觉模型：

```bash
ollama serve
ollama pull <你的模型名>
```

编辑 `config/models.json` 中的 `ollama_local`：

```json
{
  "enabled": true,
  "type": "ollama",
  "provider": "ollama",
  "base_url": "http://127.0.0.1:11434",
  "model": "替换为已安装的模型名",
  "temperature": 0.2,
  "max_tokens": 2048,
  "timeout_seconds": 30,
  "capabilities": {
    "tools": true,
    "vision": true,
    "reasoning": true
  }
}
```

能力声明必须与模型实际能力一致。启用后重新运行 `./start.sh check-config`：诊断会访问 Ollama 的模型列表，但不会自动拉取模型。

启用聊天 Ollama 后：

- `/model local` 可用；
- `auto/local` 图片请求会转到 `vision_model`；
- 成功回答后会异步提取可审核的长期记忆。

若没有启用视觉模型，图片消息会返回明确的配置提示，不影响文字消息。

也可通过 `OLLAMA_BASE_URL` 和 `OLLAMA_MODEL` 临时覆盖 Ollama 档案。

## 可选：向量知识检索

知识库默认使用 SQLite FTS5 全文检索，不需要 Ollama。要增加向量召回：

```bash
ollama pull bge-m3
```

然后把 `config/embeddings.json` 的 `enabled` 改为 `true`，确认地址、模型名和维度与实际模型一致。

向量服务不可用时，知识写入仍会成功并标记为等待向量化，搜索自动退化为 FTS5；不会切换到云端 embedding。

## 微信命令

```text
/agent                         查看当前 Agent
/model [auto|local|flash|pro]  查看或切换自己的模型模式
/tools                         查看工具、目录和审批规则
/id                            查看自己的租户编号
/schedules                     查看定时任务
/schedule on|off <任务编号>    开关自己的订阅
/knowledge                     查看私人知识库状态
/memory                        查看长期记忆
/memory confirm|forget <编号>  确认或停用记忆
/memory clear                  二次确认后停用全部记忆
/codex                         查看 Codex 确认命令
/clear                         清空短期模型上下文
/delete-data                   二次确认后删除自己的租户数据
/help                          显示帮助
```

普通文字会进入多轮对话。知识只有在用户明确要求保存或索引时才写入；长期记忆只提取稳定偏好、身份事实、长期目标和持续约束，低置信内容需要用户确认。

## 工具与审批

通用 Agent 可在当前租户的独立 `workspace/` 中读取、搜索和查询系统状态。以下操作会暂停模型并在微信中请求确认：

- 新建、写入、替换、复制、移动或移到专用废纸篓；
- 执行白名单命令；
- 写入或删除知识；
- 创建、继续或停止 Codex 任务。

回复“同意”或“确认”才会执行；回复“不同意”“拒绝”“取消”或等待超时都不会执行。工具限制在 [`config/tools.json`](config/tools.json) 中配置。

## 插件

[`config/plugins.json`](config/plugins.json) 中的插件默认关闭。

### 浏览器自动化

启用 `browser_automation` 后，模型可以在隔离的无痕会话中访问公网 HTTPS 页面。浏览器会拒绝本机、私网、云元数据地址及越界重定向。

可使用 Playwright Chromium、系统 Chrome/Edge，或通过 `BROWSER_EXECUTABLE` 指定路径。网页正文始终作为不可信资料处理。

### Codex 开发任务

启用 `codex_tasks` 前：

1. 使用当前系统用户完成 Codex 登录；
2. 向机器人发送 `/id`；
3. 将得到的租户编号加入 `admin_tenant_ids`；
4. 按需配置允许的项目路径并重启。

管理员列表为空时 Codex 工具不会开放。创建、继续和停止任务均需微信确认。

## 定时任务与待办

默认公开配置只保留通用提醒、待办提醒和归档示例，所有主动任务均为关闭状态。先修改 [`config/schedules.json`](config/schedules.json)，重启后再由每位用户通过 `/schedule on <任务编号>` 订阅。

内置 `todo_manager` 使用 SQLite 保存带稳定编号的待办，支持新增、查看、编辑、完成、恢复、提醒和归档。脚本注册位于 [`config/scripts.json`](config/scripts.json)。

机器人停机期间不会补跑普通任务。主动通知还依赖用户最近一次私聊留下的微信上下文；失效时请再次私聊机器人。

## CLI 主动通知

用户先私聊机器人并通过 `/id` 取得租户编号：

```bash
./start.sh notify --user <租户编号> --message "任务已完成"
./start.sh notify --user <租户编号> --image ./report.png
```

也可通过 `--stdin` 输入多行文本，或用 `--image-url` 发送远程图片。`notify` 不占用主机器人单实例锁。

## 数据、安全与多租户

- `data/system/botplatform.sqlite3` 保存租户、对话、设置、订阅、知识、记忆、待办和审计；
- `data/users/<tenant_uuid>/` 保存每个租户隔离的 workspace 和脚本产物；
- 登录凭据和模型 Key 位于 `data/system/`，不进入 SQLite；
- `data/`、虚拟环境、缓存、日志和系统文件已加入 `.gitignore`；
- macOS/Linux 上敏感文件使用 `0600`，数据目录使用 `0700`；
- 模型、工具和插件日志只记录必要元数据，不记录 API Key。

公开仓库前请阅读 [`SECURITY.md`](SECURITY.md)，并确认历史中没有出现过真实密钥。已泄露的 Key 必须在供应商控制台轮换，删除当前文件并不能撤销旧密钥。

## 配置文件

```text
config/
├── app.json         默认 Agent、模型模式、时区和历史轮数
├── models.json      DeepSeek、Ollama 与其他兼容模型档案
├── embeddings.json 可选 Ollama embedding
├── tools.json       工具目录、限制和命令白名单
├── plugins.json     浏览器与 Codex 插件
├── scripts.json     固定脚本注册表
├── schedules.json   定时任务
└── agents/          Agent 角色与提示词
```

远程 OpenAI-compatible 地址必须使用 HTTPS，本机回环地址可以使用 HTTP。配置修改后需要重启，不支持热加载。

## 启停与排错

```bash
./start.sh                  # 启动
./start.sh check-config     # 只诊断
./start.sh --logout         # 清除微信凭据并重新扫码
./start.sh --help           # 查看参数
```

同一 `data` 环境只允许一个主机器人进程。停止时在运行终端按 `Ctrl+C`，程序会依次停止定时任务、工具运行时、模型客户端和微信连接。

常见问题：

- `缺少 DEEPSEEK_API_KEY`：检查 `data/system/model.env` 的内容和权限；
- `Ollama 服务不可访问`：确认 Ollama 已由用户启动，地址与端口正确；
- `未找到模型`：执行 `ollama list`，修正模型名或先 `ollama pull`；
- 图片不可用：启用一个 `vision: true` 的模型档案；
- 浏览器不可用：安装 Playwright Chromium、Chrome/Edge，或配置 `BROWSER_EXECUTABLE`；
- 没收到定时消息：确认任务配置、用户订阅、时区以及最近私聊上下文。

## 开发与测试

项目使用 Python 标准库 `unittest`，测试不需要真实 DeepSeek Key、Ollama、微信账号或个人数据：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

GitHub Actions 会在 Python 3.10 和 3.12 的干净环境运行相同测试。

近期架构变化见 [`CHANGELOG.md`](CHANGELOG.md)。安全问题请按 [`SECURITY.md`](SECURITY.md) 私下报告。

## License

[MIT](LICENSE)
