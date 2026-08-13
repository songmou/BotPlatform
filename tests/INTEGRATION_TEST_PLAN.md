# 智能体工具集成测试计划（Integration Testing）

覆盖 BotPlatform「智能体 → 各工具」链路的集成测试。范式：用脚本化假模型
`FakeToolOllama` 驱动**真实** `AgentService.chat()` 工具循环，断言工具结果
（tool-role 消息）真实回灌、审批流正确、以及真实副作用（写盘 / 写库 / MCP 协议往返）。

运行方式：

```bash
# 核心交互套件（Tools/Plugins/MCP 真实 stdio/Skill/Scripts/Knowledge/Drive/Schedule）
./.venv/bin/python -m unittest tests.test_agent_interaction -v

# 数据库真实 PG 集成（需要本机 Postgres 或 Docker）
export BOTPLATFORM_TEST_PG_DSN="postgresql://user:pass@127.0.0.1:5432/db"
./.venv/bin/python -m unittest tests.test_agent_interaction_db -v
# 或：启动 Docker Desktop 后直接运行（自动拉起 postgres:16-alpine）

# 全量
./.venv/bin/python -m unittest discover -s tests -v
```

---

## 关键问题 → 预期结果 矩阵

### A. 内置 Tools（只读，天然真实）

| 关键问题 | 预期结果 |
|---|---|
| `get_current_time` 是否回灌带 `iso` 字段的时间消息？ | `tool_msg.role=="tool"` 且内容含 `iso`、`tool_call_id=="call-1"` |
| `list_directory` / `read_text_file` 是否真实读取临时目录？ | 返回内容包含真实文件名/文件内容 |
| `get_system_info` 是否暴露主机信息但**不泄露环境变量**？ | 含 `hostname`、含 `default_working_directory`；内容**不含** `environ` |
| 未知工具是否安全拒绝？ | 返回「未授权工具」并提示模型「该工具不可用」 |

### A2. 内置 Tools 全集（真实读写 / 真实 OS / 真实子进程）

| 关键问题 | 预期结果 |
|---|---|
| `get_disk_usage` 是否拿到真实磁盘 `free`？ | tool 消息含 `free` |
| `list_processes` 在受限环境（macOS 沙箱无 `/bin/ps` 权限）是否优雅降级？ | 该用例 `skip`（环境限制，非缺陷） |
| `find_files` / `search_text` 是否真搜到文件与命中行？ | 返回真实文件名与命中字符串（如 `ABC123`） |
| `get_path_info` 是否识别文件类型？ | 内容含 `"type": "file"` |
| `create_directory` / `replace_text` / `copy_path` / `move_path` / `move_to_trash` 是否走审批流且副作用真实？ | 调用即返回 `ApprovalRequired`；同意后目录/文件真实被创建、替换、复制、移动、移入废纸篓 |
| `run_command`(python profile) 是否执行真实子进程？ | 在可用 sandbox 环境断言真实 stdout（如 `RC:2`）；sandbox 不可用时 `skip` |
| 多工具轮次后模型是否能综合？ | 多轮 tool 消息均被收集，最终 `FinalAnswer` |
| 被禁用工具是否返回禁用消息？ | 返回「工具已被禁用」 |

### B. 插件 Plugins

| 关键问题 | 预期结果 |
|---|---|
| `todo_manage` 是否走 `direct_response` 不过模型二次改写？ | 模型只被调用 1 次，结果含「已新增待办」且不含错误改写文本 |
| 插件目录是否发现内置插件？ | `todo`、`web_research` 被声明 |

### C. MCP（离线命名空间 + 真实 stdio 协议）

| 关键问题 | 预期结果 |
|---|---|
| 离线 namespace 分发（`mcp_list__ping`）是否正确回灌？ | tool 消息含 `pong`；`calls==[("mcp_list__ping", {})]` |
| 真实本地 stdio MCP（`local_echo`）是否能握手并调用？ | `resolve_tool_names` 展开 `local_echo__*`；`echo`/`add` 经 chat 返回真实结果（`echo:hello` / `5`） |
| 真实 MCP 工具抛错是否如实反馈？ | `call_tool("local_echo__boom")` 抛出 `RuntimeError` |
| 远程 `mcp_list` 不可达时是否跳过而非崩溃？ | 该用例 `skip` |

### D. Skill

| 关键问题 | 预期结果 |
|---|---|
| 启用的 Skill 是否注入系统提示词？ | `build_system_prompt` 含 `# Skill: 运维脚本自动化` |
| 禁用的 Skill 是否不注入？ | 提示词**不含**该 Skill |
| Skill 是否经 chat 到达模型上下文？ | 系统消息内确实含该 Skill 文本 |

### E. 脚本 Scripts（真实子进程）

| 关键问题 | 预期结果 |
|---|---|
| `run_script` 是否触发真实子进程并把结果回灌？ | 返回 `run_id`；脚本真实执行，`status=="success"`、`summary=="脚本执行成功"` |
| `list_scripts` 是否列出脚本？ | 内容含脚本名 |
| `requires_approval` 脚本是否走审批流且真实执行？ | `ApprovalRequired` → 同意后真实跑通 |

### F. 数据库网关（纯函数，只读校验）

| 关键问题 | 预期结果 |
|---|---|
| 只读 `SELECT` 是否放行并限制行数？ | `compile_readonly` 返回用到的表 + 正 `limit` |
| `INSERT` 是否被拒？ | 抛 `DataSourceError` |
| 未授权表是否被拒？ | 抛 `DataSourceError` |

### G. 知识库 Knowledge（真实 Service + SQLite）

| 关键问题 | 预期结果 |
|---|---|
| `knowledge_add_text` 是否走审批且真实入库？ | `ApprovalRequired` → 同意后 `list` 真实含该来源 |
| `knowledge_list` / `knowledge_search` 是否真实检索？ | 经 chat 返回真实来源名 / 命中内容 |
| `knowledge_delete` 是否走审批且真实删除？ | `ApprovalRequired` → 同意后 `list` 不再含该来源 |

### H. 网盘 Drive（真实 Service + 文件系统）

| 关键问题 | 预期结果 |
|---|---|
| `drive_save_file` → `drive_list_files` → `drive_read_file` 是否真实往返？ | 保存/列出/读取内容一致（`网盘内容123`） |
| `drive_delete_file` 是否走审批且真实删除？ | `ApprovalRequired` → 同意后文件被删 |

### I. 定时任务 Schedule（真实 Organization 存储 + 角色门控）

| 关键问题 | 预期结果 |
|---|---|
| `list_script_schedules` 经 chat 是否返回真实列表？ | tool 消息含 `schedules` 键 |
| 所有者是否能创建定时任务？ | `manage(create)` 成功，`list_for_tenant` 含该任务 |
| 普通成员是否被拒绝？ | `manage(create)` 抛 `ValueError`（仅 owner/admin 可改） |

### J. 数据库真实 PG（真实连接，断言真实副作用）

| 关键问题 | 预期结果 |
|---|---|
| `db_list_tables` 经 chat 是否列出真实表？ | 返回含 `customers` |
| `db_describe_table` 是否返回真实列结构？ | 含 `id`/`name`/`city` |
| `db_query`（只读）是否返回真实行？ | 返回 `Alice`/`Carol`（Shanghai 客户） |
| `db_execute`（审批写）是否真实修改行？ | `ApprovalRequired` → 同意后**真实回查** `id=2` 的城市变为 `Shanghai` |
| 只读源执行 `UPDATE` 是否被拒？ | 抛 `DataSourceError` |
| 查询未授权表 / 非 SELECT 是否被拒？ | 抛 `DataSourceError` |

---

## 集成测试中发现的真实缺陷（已修复）

1. **`src/core/datasource/drivers.py` · `fetch_columns`**
   原写法 `pg_get_expr(pg_node_tree(c.column_default), 0)` 在真实 PG 上报
   `cannot accept a value of type pg_node_tree`。`information_schema.columns.column_default`
   本身已是可读的默认值文本，改为直接取 `c.column_default`。

2. **`src/core/datasource/pool.py` · `ConnectionPool.put`**
   `query()` 等调用 `begin_readonly`（`BEGIN READ ONLY`）后**未结束事务**便归还连接，
   导致被复用的连接仍处只读事务，`execute_write` 的 `UPDATE` 报
   `cannot execute UPDATE in a read-only transaction`。修复：归还连接时
   `rollback()` 任何未结束事务，覆盖全部 4 处 `begin_readonly` 调用方。

## 环境降级策略

| 依赖 | 不可用时行为 |
|---|---|
| `list_processes`（`/bin/ps` 权限） | 该用例 `skip` |
| `run_command`（sandbox-exec 无法应用策略） | 该用例 `skip` |
| 远程 `mcp_list` 端点 | 该用例 `skip` |
| 真实 Postgres（Docker 不可用且无 `BOTPLATFORM_TEST_PG_DSN`） | 整个 DB 集成套件 `skip` |

当前本机已装 Postgres 16（brew），可用 `BOTPLATFORM_TEST_PG_DSN` 指向本机实例跑通真实 DB 用例。
