# BotPlatform API 层 + WebUI 实现计划（精简版）

## 范围

首版只做 3 个功能模块：
1. **对话** — SSE 流式聊天
2. **模型管理** — 只读展示模型档案和路由状态
3. **智能体管理** — 只读展示 Agent 预设

知识库、长期记忆、定时任务、用户管理暂不实现。

## 技术选型

| 层 | 选择 |
|---|---|
| API 框架 | FastAPI + Uvicorn |
| 前端 | Jinja2 + HTMX + Pico CSS（CDN，无构建） |
| 流式对话 | SSE（Server-Sent Events） |
| 部署 | 独立进程，共享 SQLite，复用 src/ 包 |
| 认证 | 本地 token（自动生成到 data/system/web_token） |

## 部署方式

### 运行模式

Web 服务作为**独立 Python 进程**运行，与现有 bot 进程（main.py）完全解耦：

```bash
# 终端 1：启动微信 bot（已有）
python main.py

# 终端 2：启动 Web 管理面板（新增）
python web.py --port 8080
```

两个进程可以同时运行，也可以只启动 Web 服务（不依赖 bot 在线）。

### 启动命令

```bash
# 最简启动（默认 127.0.0.1:8080）
python web.py

# 指定端口
python web.py --port 9000

# 允许局域网访问（可选）
python web.py --host 0.0.0.0 --port 8080
```

### 访问方式

1. 启动后终端会打印访问地址和 token：
   ```
   Web 管理面板已启动：http://127.0.0.1:8080
   访问令牌：a3f8c2e1...（已保存到 data/system/web_token）
   ```
2. 浏览器打开 `http://127.0.0.1:8080?token=a3f8c2e1...`
3. 首次访问后 token 写入 cookie，后续无需再带参数

### 与 Bot 的关系

| 场景 | 说明 |
|---|---|
| 只启动 Web | 可以查看模型/Agent 配置、进行 AI 对话（直接调用模型 API） |
| 只启动 Bot | 微信机器人正常工作，Web 不可用 |
| 两者同时启动 | 共享同一个 SQLite 数据库（WAL 模式支持并发读写） |

### 前置条件

- Python 3.10+
- 已安装 requirements.txt 中的依赖（含新增的 fastapi、uvicorn、jinja2）
- DeepSeek API Key 已配置（对话功能需要）
- config/ 目录配置文件完整

### 停止

在 Web 服务终端按 `Ctrl+C` 即可，不影响 bot 进程。

## 目录结构

```
BotPlatform/
├── src/api/
│   ├── __init__.py
│   ├── app.py              # FastAPI 应用工厂
│   ├── auth.py             # Token 认证
│   ├── deps.py             # 依赖注入
│   ├── schemas.py          # Pydantic 模型
│   ├── sse.py              # SSE 流式工具
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── chat.py         # 对话（SSE 流式）
│   │   ├── models.py       # 模型管理
│   │   ├── agents.py       # 智能体管理
│   │   └── system.py       # 健康检查
│   ├── templates/
│   │   ├── base.html       # 布局骨架
│   │   ├── chat.html       # 对话界面
│   │   ├── models.html     # 模型管理
│   │   └── agents.html     # 智能体管理
│   └── static/
│       ├── app.css         # 自定义样式
│       └── app.js          # SSE 处理、Markdown 渲染、滚动
├── web.py                  # Web 服务启动入口
└── requirements.txt        # 追加 fastapi, uvicorn, jinja2
```

## 实现步骤

### 第一阶段：API 骨架（5 个文件）

1. **requirements.txt** — 追加 `fastapi>=0.115`, `uvicorn[standard]>=0.30`, `jinja2>=3.1`
2. **src/api/app.py** — FastAPI 实例、路由挂载、Jinja2 模板、静态文件、CORS
3. **src/api/auth.py** — token 生成/校验中间件（Bearer header 或 cookie）
4. **src/api/deps.py** — 依赖注入：ProjectConfig、ModelRouter、ConversationStore
5. **web.py** — 启动入口，解析 host/port，启动 uvicorn

### 第二阶段：API 路由（5 个文件）

6. **routers/system.py**
   - `GET /api/health` — 健康检查
   - `GET /api/status` — 模型就绪状态、活跃 Agent 信息

7. **routers/models.py**
   - `GET /api/models` — 所有档案列表
   - `GET /api/models/{profile_id}` — 单档案详情
   - `GET /api/models/status` — 路由器状态（主模型、冷却）

8. **routers/agents.py**
   - `GET /api/agents` — 所有 Agent 列表
   - `GET /api/agents/{agent_id}` — 详情
   - `GET /api/agents/active` — 当前活跃 Agent

9. **routers/chat.py**
   - `POST /api/chat` — SSE 流式对话（使用固定 web 会话身份）
   - `GET /api/chat/history` — 获取当前会话历史
   - `DELETE /api/chat/context` — 清空当前会话上下文

10. **src/api/schemas.py** — 请求/响应 Pydantic 模型

### 第三阶段：SSE 流式（2 个文件）

11. **src/api/sse.py** — StreamingResponse 封装
    - `data: {"type":"token","content":"..."}\n\n`
    - `data: {"type":"done","full_text":"..."}\n\n`
    - `data: {"type":"error","message":"..."}\n\n`

12. **模型适配器流式扩展**
    - OpenAI-compatible：`stream=True`
    - Ollama：流式 API
    - 不支持流式时降级为整条返回

### 第四阶段：前端页面（6 个文件）

13. **templates/base.html** — Pico CSS + HTMX + 导航栏 + 主题切换
14. **templates/chat.html** — 消息气泡 + 输入框 + SSE 流式追加 + Markdown 渲染
15. **templates/models.html** — 卡片展示模型档案 + 路由状态
16. **templates/agents.html** — Agent 列表 + 展开详情
17. **static/app.css** — 气泡样式、流式动画
18. **static/app.js** — SSE 连接、自动滚动、marked.js 渲染

### 第五阶段：收尾（2 个文件）

19. **启动脚本** — start.sh / start.ps1 增加 `web` 子命令
20. **测试** — API 路由 + SSE + 认证的基础测试

## 架构图

```
┌─────────────┐         ┌──────────────────┐
│  Web 浏览器  │◄─SSE──►│  FastAPI (web.py) │
└─────────────┘  HTMX   └────────┬─────────┘
                                  │ 读/写
                                  ▼
                        ┌──────────────────┐
                        │  SQLite (WAL)    │◄── 写入 ──┐
                        └──────────────────┘           │
                        ┌──────────────────┐           │
                        │  Bot (main.py)   │───────────┘
                        └──────────────────┘
```

## 关键决策

1. Web 对话独立调用模型，不经过微信 iLink
2. Web 对话使用固定的 "web" 会话身份，对话上下文存储在 SQLite 中
3. 首版不支持工具调用（避免审批复杂化）
4. 配置只读展示，修改仍需编辑 JSON + 重启
5. 默认监听 127.0.0.1，token 认证，不暴露到公网

## 依赖追加

```
fastapi>=0.115.0,<1.0
uvicorn[standard]>=0.30.0,<1.0
jinja2>=3.1.0,<4.0
```

## 文件清单（共 ~20 个）

| 阶段 | 文件数 |
|---|---|
| API 骨架 | 5 |
| API 路由 | 5 |
| SSE 流式 | 2 |
| 前端页面 | 6 |
| 收尾 | 2 |
| **合计** | **~20** |
