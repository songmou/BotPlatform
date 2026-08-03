# BotPlatform：轻量级的AI中台的项目

BotPlatform 是一个轻量级的AI中台的项目。当前内置微信 iLink、企业微信智能机器人和飞书长连接适配器，统一消息层已经把平台协议与会话、模型、工具、通知和租户数据解耦，可继续接入钉钉和 QQ。默认调用 DeepSeek V4 Flash，并提供多租户会话、可审批的本机工具、知识库、长期记忆、待办、定时任务以及可选插件。

当前本地配置仍以 DeepSeek 为默认文字模型，同时启用 Ollama 图片理解、浏览器自动化和个人后台任务。Codex 开发任务作为可选额外插件提供；向量检索保持关闭，不需要安装 `bge-m3`。

核心代码位于 `src/core/*` 与 `src/api/*` 两个包：`src/core/application/` 负责入口与消息编排，`src/core/services/` 承载业务服务，`src/core/storage/` 管理 SQLite 与租户数据，`src/core/integrations/` 对接微信、图片和向量能力，`src/core/infrastructure/` 提供诊断、日志与单实例运行支持，模型、工具、插件分别位于同名子包，可独立执行的后台任务集中在 `src/core/jobs/`；`src/api/` 则是 FastAPI Web 管理面板（路由、鉴权、模板与静态资源）。根目录 `main.py` 启动微信机器人（也可用 `python -m src`），`web.py` 启动 Web 管理面板。请统一从 `src.core.*` / `src.api.*` 导入。

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

消息渠道配置位于 [`config/channels.json`](config/channels.json)。每个启用渠道拥有独立接收线程，消息会先进入持久化 inbox，再由核心串行处理。企业微信与飞书支持私聊及群聊 `@机器人`；群聊默认禁用私有上下文和本机工具。完整架构、平台配置、灰度与回滚流程见[消息渠道改造与接入手册](docs/message-channels.md)。可使用以下命令管理渠道：

```bash
./start.sh channel list
./start.sh channel status
./start.sh channel login wechat-main
printf '%s' '{"bot_id":"...","secret":"..."}' | ./start.sh channel configure wecom-main --stdin
printf '%s' '{"app_id":"...","app_secret":"..."}' | ./start.sh channel configure feishu-main --stdin
./start.sh channel test wecom-main
./start.sh channel logout wechat-main
```

`notify --user <租户编号>` 默认发送到用户最后活跃的私聊渠道，也可以通过 `--channel <渠道编号>` 指定渠道。在原渠道私聊发送 `/bind` 获取一次性绑定码，再在新渠道发送 `/bind <绑定码>`，可显式绑定同一租户。

## 默认模型与切换

默认配置位于 [`config/app.json`](config/app.json) 和 [`config/models.json`](config/models.json)：

- `auto`：使用全局默认档案，开箱为 `deepseek-v4-flash`；
- `flash`：使用 DeepSeek V4 Flash；
- `pro`：使用 DeepSeek V4 Pro 思考模式；
- `local`：使用本机 Ollama 的 `gemma4:e4b`，也负责微信图片理解和本地长期记忆提取。

DeepSeek V4 的模型标识与接口说明见 [DeepSeek API 文档](https://api-docs.deepseek.com/quick_start/pricing)。每位微信用户的模式独立保存在 SQLite 中：

```text
/model
/model auto
/model flash
/model pro
/model local
```

`active_model` 与 `fallback_model` 默认都指向 Flash，因此不会在失败时自动升级到费用更高的 Pro。需要自定义兜底时，可把 `fallback_model` 改为另一个已启用档案。

## 本地 Ollama

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
  "model": "gemma4:e4b",
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

然后把 `config/models.json` 中 `bge_m3_local` 档案的 `enabled` 改为 `true`，确认地址、模型名和维度与实际模型一致，并保持 `config/app.json` 的 `embedding_model` 指向该档案。

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
/soul                          查看当前长期用户画像
/soul rebuild                  从有效记忆重建 SOUL.md
/integration setup <ctsehr|ctsoa|autogen>  安全配置业务集成
/integration status [编号]     查看集成状态
/integration delete <编号>     删除集成凭据
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
- 清单声明为强制审批的插件操作。

回复“同意”或“确认”才会执行；回复“不同意”“拒绝”“取消”或等待超时都不会执行。工具限制在 [`config/tools.json`](config/tools.json) 中配置。

### 本地 OCR（可选）

`ocr_extract_text` 可识别租户 workspace 内的图片和最多 10 页 PDF；微信图片会在模型调用前自动 OCR。OCR 完全在本机执行，Paddle 依赖和模型需由管理员显式准备：

```bash
.venv/bin/python -m pip install -r requirements-ocr.txt
.venv/bin/python -m src.core.integrations.paddle_ocr prepare
.venv/bin/python -m src.core.integrations.paddle_ocr check
```

模型保存在 `data/system/ocr_models`，不会提交仓库。缺少依赖或模型时机器人仍可启动，OCR 工具显示为不可用；修改 `config/tools.json` 中的 `ocr` 配置后需要完整重启。

## 插件

插件由 `src/core/plugins/bundled/<id>/plugin.json` 或
`data/system/plugins/<id>/plugin.json` 清单发现。清单是工具定义、设置
Schema、依赖、图标、提示和后台任务的唯一元数据来源；禁用插件及依赖
缺失的插件不会导入入口代码。插件安装、更新、启停、设置及智能体绑定
均需完整重启生效。

管理员可在 `/plugins` 插件中心从可信本地目录安装插件。插件与主进程
同权限运行，不是安全沙箱；系统不会联网、运行安装脚本或自动执行
`pip`。外部插件包存放在 `data/system/plugins/`，移除时进入可恢复目录，
插件数据默认保留，可另行通过插件 ID 二次确认清除。

智能体配置中的内置工具写在 `tools`，插件工具写在
`plugin_tools: {plugin_id: [tool_name]}`。安装并启用插件并不会自动把工具
授予智能体。

### 浏览器自动化

启用 `browser_automation` 后，模型可以在隔离的无痕会话中访问公网 HTTPS 页面。浏览器会拒绝本机、私网、云元数据地址及越界重定向。

可使用 Playwright Chromium、系统 Chrome/Edge，或通过 `BROWSER_EXECUTABLE` 指定路径。备份恢复的文生图脚本直接复用同一套隔离浏览器能力；网页正文始终作为不可信资料处理。

### Codex 开发任务

`codex_tasks` 是一个内置但可选的额外插件，只提供五个工具：列出、查看、
创建、继续和中止任务。启用前：

1. 使用当前系统用户完成 Codex 登录；
2. 向机器人发送 `/id`；
3. 将得到的租户编号加入 `allowed_tenant_ids`；
4. 配置 `projects`、`default_project` 和并发上限；
5. 在目标智能体的 `plugin_tools.codex_tasks` 中显式选择工具并重启。

创建、继续和中止由平台强制审批。插件固定使用 `workspace-write` 和无需
提权的策略；若任务仍请求额外审批、权限或用户输入，会被安全拒绝并
标记失败。插件只管理自身创建、按租户隔离的任务，不轮询外部 Codex
任务、不安装 Hook，也不发送阶段或完成通知。`openai-codex` 是插件可选
依赖，缺失时只影响该插件。

## 定时任务与待办

当前本地时间表启用文生图（08:00、14:00）、OA 考勤（08:50、18:00）、待办提醒（09:00、18:00）、每月 1 日 09:05 归档，以及每 10 分钟执行的离线窗口检查。普通早安和晚间总结保持关闭；各用户是否接收仍由已有订阅决定。

后台任务实现位于 [`src/core/jobs/`](src/core/jobs/)；每个脚本由同目录的 `script.json` 自行声明。`autogen_monitor` 负责带参考图的连载文生图，`ctsehr_check` 负责 EHR 考勤与待审批，`ctsoa_check` 负责 CTS OA 待办查询。CTS OA 首次登录会返回验证码图片；在 5 分钟内再次运行并传入 `validate_code` 后完成登录，随后复用受限权限会话直到失效。AutoGen 使用与主进程相同的默认语言模型生成中文故事提示词，并固定用上一场的第 1 张候选图续写下一场；目标网站未提供参考图上传控件时会停止，不会降级为随机文生图。传入 `reset_story=true` 可在本次成功后开始新的随机故事。它们由脚本服务以独立子进程运行；私人待办则由 `src/core/plugins/todo.py` 插件提供，不属于后台任务注册表。业务账号与密码通过 `/integration` 配置，密码只保存在权限为 `0600` 的受限凭据文件中，不进入日志、聊天历史或模型上下文。

机器人停机期间不会补跑尚未触发的普通任务；已经生成的主动通知会先持久化到 SQLite Outbox，并在机器人恢复后按用户、按原顺序补发。网络、凭证或微信服务临时异常会持续退避重试；微信上下文失效时通知会等待用户再次私聊刷新上下文。图片会在入队时复制到租户私有缓存，送达后自动删除。投递语义为至少一次：若微信已经接收、进程却在记录成功前异常退出，极端情况下可能重复一条。

## CLI 主动通知

用户先私聊机器人并通过 `/id` 取得租户编号：

```bash
./start.sh notify --user <租户编号> --message "任务已完成"
./start.sh notify --user <租户编号> --image ./report.png
```

也可通过 `--stdin` 输入多行文本，或用 `--image-url` 发送远程图片。`notify` 不占用主机器人单实例锁；能够即时投递时显示“已发送”，暂时无法投递但已经安全落盘时显示“已保存，等待补发”并返回成功。

## Web 管理面板

除了微信机器人，项目还提供一个 FastAPI Web 管理面板（`web.py`），用于在浏览器中配置与运维。

启动：

```bash
./start.sh web                          # 默认 127.0.0.1:8080
./start.sh web --host 0.0.0.0 --port 8080   # 供局域网访问
```

Windows 或不使用包装脚本时：

```powershell
python web.py --host 0.0.0.0 --port 8080
```

- **登录与账号**：面板采用「账号 + 密码」登录并以会话 Cookie 维持状态（无共享 token）。首次启动若无管理员，会自动创建账号 `admin`，初始密码打印在启动日志，并写入 `data/system/admin_initial_password`（权限 `0600`），请登录后尽快修改。会话密钥保存在 `data/system/session_secret`。
- **访问地址**：浏览器打开 `http://<host>:<port>/login` 登录。

左侧导航对应各功能模块：

- **平台管理**：平台概览、模型服务、智能体模板、工具/Skill/MCP、插件与运维脚本使用独立菜单。编辑先保存草稿，发布后才进入平台目录；
- **组织工作台**：组织概览与对话、独立智能体、知识和文件、渠道与任务、成员设置、组织分析和审计使用独立菜单；
- **组织选择**：没有全站“当前组织”。组织页面在页头选择组织，并把 `organization_id` 写入 URL；平台页面不读取组织上下文；
- **智能体边界**：平台发布智能体模板和底层能力。组织可以新建智能体或复制某个模板版本，复制后独立维护，不跟随模板升级；
- **组织运营**：普通成员可维护协作资源，Owner/Admin 管理渠道凭据、成员、预算与生命周期；不提供组织级模型、插件或 MCP 覆盖和凭据；
- **平台治理**：仅平台管理员可见，包含组织管理、管理员与角色、聚合分析和平台审计。管理员进入组织页面时显示代管提示并记录组织审计；
- **文档说明**：面板功能与使用指南。

账号按角色拥有不同权限，菜单由服务端按权限渲染。平台目录以 SQLite 为唯一事实来源；`config/*.json` 只在数据库中尚无对应资源时导入。聊天模型、智能体模板、Skill、MCP 与工具策略可受控热切换；嵌入/重排模型、插件包和脚本代码变更会显示“等待重启”，完整重启前仍使用旧运行版本。

## 数据、安全与多租户

- `data/system/botplatform.sqlite3` 保存租户、对话、设置、订阅、知识、记忆、待办和审计；也保存 Web 面板的管理员账号、角色与会话（密码为 PBKDF2 哈希，绝不明文存储）；
- `data/users/<tenant_uuid>/` 保存每个租户隔离的 `SOUL.md`、workspace 和后台任务产物；
- `SOUL.md` 是由 SQLite 有效记忆自动生成的紧凑画像，禁止手工编辑；每日扫描遗漏记忆，每周使用当前系统默认模型尝试压缩；
- 登录凭据、模型 Key、面板会话密钥（`session_secret`）与初始管理员密码（`admin_initial_password`）位于 `data/system/`，不进入 SQLite；
- `data/`、虚拟环境、缓存、日志和系统文件已加入 `.gitignore`；
- macOS/Linux 上敏感文件使用 `0600`，数据目录使用 `0700`；
- 模型、工具和插件日志只记录必要元数据，不记录 API Key。

公开仓库前请阅读 [`SECURITY.md`](SECURITY.md)，并确认历史中没有出现过真实密钥。已泄露的 Key 必须在供应商控制台轮换，删除当前文件并不能撤销旧密钥。

## 配置文件

```text
config/
├── app.json         默认 Agent、模型模式、时区和历史轮数
├── models.json      DeepSeek、Ollama 与其他兼容模型档案（含 embedding 档案）
├── channels.json    历史平台渠道种子（启动时幂等迁移到默认组织）
├── tools.json       工具目录、限制和命令白名单
├── plugins.json     插件启用状态与设置
├── skills.json      技能定义
├── mcp_servers.json MCP 服务器配置
├── schedules.json   定时任务
└── agents/          Agent 角色与提示词
```

内置后台脚本采用自包含文件夹：每个脚本位于 `src/core/jobs/<目录>/`，由同目录的 `script.json` 清单声明（id、参数、超时等），启动时自动扫描发现；新增脚本只需放入文件夹，删除脚本即删除文件夹。

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
