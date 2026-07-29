# Web 管理面板完整集成测试报告

- 测试日期：2026-08-25（本机时间）
- 测试对象：BotPlatform Web 管理面板（`src/api/`，FastAPI）
- 测试方式：两阶段
  - 阶段一：进程内自动化集成测试（`unittest` + FastAPI `TestClient`），新增 9 个测试文件补齐未覆盖的 router，与现有测试一起全量运行
  - 阶段二：真实服务只读冒烟测试（`python web.py --panel-only --host 127.0.0.1 --port 8090`）

## 总体结论

| 阶段 | 用例数 | 通过 | 失败 | 结论 |
|---|---|---|---|---|
| 阶段一：全量单元/集成测试 | 522 | 522 | 0 | ✅ 全部通过 |
| 阶段二：真实服务冒烟检查 | 29 | 29 | 0 | ✅ 全部通过 |

未发现产品代码缺陷。测试过程中未修改任何产品代码；真实 `config/` 与 `data/` 未被测试污染（所有写盘路径均 patch 到临时目录，事后经文件修改时间核实）。

## 阶段一：进程内自动化集成测试

### 执行方式

```
python -m unittest discover -s tests -v
```

结果：`Ran 522 tests — OK`（约 27 秒）。

### 本次新增的测试基础设施

- `tests/_web_api_base.py`：共享基类 `WebApiTestBase`（不以 `test` 开头，不被 discovery 收集）。以临时目录构建真实 `TenantRegistry`、管理员账号存储（SQLite），创建 `root`(admin) 与 `watcher`(viewer) 双账号并分别登录，供各测试文件复用；子类通过 `app_kwargs()` 钩子注入 mock 服务。

### 新增测试文件与覆盖矩阵（共 133 个用例，全部通过）

| 测试文件 | 用例数 | 覆盖端点与场景 |
|---|---|---|
| `test_web_schedules_api.py` | 16 | `/api/schedules` CRUD；非法 ID/重复 ID/缺 cron/非法 cron（4 字段、含字母）400；非法 action（unknown、text 缺 content、agent_prompt 缺 prompt）400；引用不存在脚本 400；crons+condition 组合；持久化与 scheduler.reload 验证；未登录 401 |
| `test_web_scripts_api.py` | 18 | 服务未注入全端点 503；`GET/POST/PUT/DELETE /api/scripts`、`PUT /roots`（非数组 400、ValueError→400）；删除被定时任务引用 409；`POST /{id}/runs` 202（tenant_id 非字符串 400、未知租户 404）；`GET /api/script-runs`、`GET /{run_id}` 404、`POST /{run_id}/cancel`；租户级 script-schedules CRUD；viewer 全线 403 |
| `test_web_plugins_tools_api.py` | 12 | `GET /api/plugins` 列表、`GET/PUT /{id}`（未知 404、无配置 404、更新持久化）；`GET /api/tools`（无 runtime 时 available=False）；`PATCH /api/tools/{name}`（未知 404、状态落盘）；`GET /api/tools/audit`（无 store 返回空、非法 status 422）；未登录 401 |
| `test_web_skills_mcp_api.py` | 22 | skills CRUD：非法 ID 400、重复 409、不存在 404、`config.update_skills` 原地生效与落盘；mcp CRUD：transport 校验（stdio 需 command、sse/streamablehttp 需 url、非法值 400）、重复 409、`/{id}/tools` 与 invoke 在无 runtime 下的降级响应 |
| `test_web_knowledge_api.py` | 13 | 服务未注入 503；真实 `KnowledgeService(registry, None)`：`GET /tenants`、列表、`POST /text`（空内容 400）、`GET /search` 命中验证、`POST /upload`（txt 成功、非法后缀 400、空文件 400、落盘位置验证）、`POST /reindex`（无 embedding 400）、`DELETE`（成功/404）、租户不存在 404、viewer 读写均 403 |
| `test_web_model_analytics_api.py` | 18 | store 未注入 503；overview/timeseries/breakdown/runs/run_detail/export.csv 200 与参数透传；时间范围非法（end≤start、>366 天）400；source/status 非法 400；bucket/dimension 非法 422；feedback（404/400）；budgets CRUD（409 冲突、404）；viewer 可读不可管理 |
| `test_web_models_write_api.py` | 15 | `POST /api/models`（非法 ID 400、重复 409、model 空 400、pricing 校验：未知字段/缺必填/负数 400、合法 pricing 201）；`PUT /{id}`（200/404/非法 pricing 400）；`PUT /switch`（成功切换且 fallback 让位、未知档案 404）；`DELETE`（成功、主模型 400、404）；未登录 401 |
| `test_web_agents_write_api.py` | 11 | `GET` 列表/详情/404；`POST`（非法 ID 400、重复 409、空名 400、按 agent 落盘 `agents/{id}.json`）；`PUT`（200/404、内存与文件同步）；`DELETE`（成功、默认 agent 400、404）；未登录 401 |
| `test_web_misc_api.py` | 8 | `GET /api/bots`（无凭证/有效凭证/损坏凭证三分支）；`GET /api/auth/me`（admin 含 `*`、viewer 权限集、未登录 401）；`/schedules` `/plugins` `/docs` `/knowledge` 页面渲染 200 与未认证 302→`/login?next=...` |

存量测试（auth 登录/限流、system、models 读、agents 读、chat 流式/工具/历史、tenants、admins、roles、owns_services 生命周期等）共 389 例，全部继续通过。

### 防污染措施

所有直接读写真实配置的模块级路径常量均以 `unittest.mock.patch.object` 替换为临时目录文件：
`SCHEDULES_FILE`、`SKILLS_FILE`、`MCP_FILE`、`MODELS_FILE`、`PLUGINS_FILE`、`TOOL_STATE_FILE`、`AGENTS_DIR`、`CREDENTIALS_PATH`。测试后经 `git status` 与文件 mtime 核实，`config/`、`data/` 下的改动均早于测试运行时间，属于用户既有工作区改动，与测试无关。

### 测试过程中的修正（测试自身，非产品缺陷）

- `test_web_misc_api.py` 初版断言未认证页面重定向 Location 严格等于 `/login`，实际产品返回 `/login?next=/schedules`（带回跳参数，行为合理）。已放宽为前缀断言。

## 阶段二：真实服务冒烟测试

- 启动命令：`python web.py --panel-only --host 127.0.0.1 --port 8090`（不启动微信渠道）
- 登录凭证：`data/system/admin_initial_password`（admin 登录成功）
- 全程只读，未执行任何写操作、未发起真实模型请求；测试完毕已 kill 进程并确认 8090 端口释放

### 检查清单（29/29 通过）

| 检查项 | 结果 |
|---|---|
| `GET /api/health` → 200 | ✅ |
| `GET /login` 页面 → 200 | ✅ |
| 未认证 `GET /models` → 302 `/login?next=/models` | ✅ |
| 未认证 `GET /api/status` → 401 | ✅ |
| `POST /api/auth/login`（admin）→ 200 并获得 cookie | ✅ |
| 安全响应头 `Content-Security-Policy`（default-src 'self' …） | ✅ |
| 安全响应头 `X-Frame-Options: DENY` | ✅ |
| 只读 API ×11：`/api/status` `/api/models` `/api/agents` `/api/skills` `/api/mcp` `/api/plugins` `/api/schedules` `/api/scripts` `/api/tenants` `/api/bots` `/api/auth/me` → 全部 200 | ✅ |
| 页面 ×10：`/` `/models` `/agents` `/schedules` `/scripts` `/tools` `/plugins` `/users` `/knowledge` `/docs` → 全部 200 | ✅ |
| `POST /api/auth/logout` → 200 | ✅ |

## 发现的问题与建议

1. **（观察项，非缺陷）启动时 MCP 连接告警**：真实服务启动日志输出 `MCP 服务 mcp_list 连接失败：unhandled errors in a TaskGroup (1 sub-exception)`。面板功能不受影响（降级设计符合预期），建议核查 `config/mcp_servers.json` 中 `mcp_list` 服务的 command/url 配置是否有效，或在面板 MCP 页面禁用该条目。
2. **（观察项）`create_model` 客户端创建失败被静默吞掉**：`POST /api/models` 在 `create_model_client` 抛异常时仍返回 201（档案已保存、客户端离线，日志有 warning）。行为有意为之，但前端不易感知"已保存但离线"状态，可考虑在响应中附带 `client_ready` 字段。
3. **（观察项）未认证页面重定向携带 `next` 参数**：行为正确且体验良好，仅提示编写外部探活脚本时需以前缀匹配 `/login`。

## 结论

Web 管理面板 15 个 router 的全部端点均已获得自动化集成测试覆盖；两阶段共 551 项检查全部通过，未发现产品缺陷。
