# Workflow 编排

BotPlatform 提供平台工作流模板和组织独立工作流。平台模板只用于分发；组织复制指定发布版本后获得完全独立的草稿与版本线，不跟随模板更新。组织成员可以编辑、试运行、发布、停用和回滚，Owner/Admin 负责 API、Webhook 与 HTTPS 连接凭据。

## 工作台

- 平台模板：`/platform/workflows`
- 组织工作流：`/organization/workflows?organization_id=<uuid>`
- 组织工作台包含工作流、运行记录和审批待办，编辑器使用原生 SVG/DOM，不需要前端构建工具。
- 草稿通过 `draft_revision` 乐观锁串行自动保存；冲突返回 HTTP 409，编辑器显示冲突并提供确认重新加载。发布版本不可变，回滚会在资源预检后根据历史快照创建一个新的发布版本。

## DSL v1

定义由 `inputs`、`outputs`、`triggers`、`nodes`、`edges` 和 `settings` 组成。变量只支持以下路径：

```text
{{input.field}}
{{nodes.node_id.output.field}}
{{item.field}}
{{trigger.field}}
```

服务端拒绝代码表达式、任意图循环、无效端口、下游节点引用、明文密钥和请求头。输入输出默认值在保存时校验类型，Switch 分支键和匹配值必须唯一，五段 cron 由统一调度解析器校验。工作流最多 500 步，For Each 最多 100 项，子工作流发布或试运行时固定依赖版本、拒绝跨工作流递归且最大深度为 5。

## 执行与恢复

组织 API 只创建 `queued` 运行。后台 Worker 使用 SQLite 租约领取运行，逐节点追加节点记录和事件。默认全局并发为 4、单组织并发为 2。运行状态包括：

```text
queued -> running -> succeeded | failed | timed_out | canceled
                    -> waiting -> queued
                    -> needs_attention -> queued | failed
```

审批、补充输入和延迟以 `workflow_waits` 持久化，进程重启后可恢复。只读和显式重试节点可以按错误策略最多重试三次。工具、脚本、非只读 HTTPS 和消息通知若在进程中断后无法确认外部结果，会进入 `needs_attention`，由 Owner/Admin 选择 `retry`、`skip` 或 `terminate`。同一节点的外部操作键固定为 `<run_id>:<node_id>`。

试运行在入队时固化当前草稿和子工作流版本映射；排队期间继续编辑不会改变本次执行内容。组织运行详情保留输入、输出、错误、节点明细和事件并对常见密钥字段脱敏；公开运行状态只返回状态、最终输出/错误和时间字段，不暴露内部状态或节点日志。

## 触发器和令牌

- 手动和试运行使用组织接口。
- API Bearer Token 只显示一次，数据库仅保存 SHA-256 哈希；工作流必须声明 `api` 触发器。
- 每个 Webhook 触发器单独签发和轮换密钥。
- 定时触发器使用经过语义校验的五段 cron 和平台统一时区，与现有 `organization_schedules` 并存。
- API 与 Webhook 接受 `Idempotency-Key`，唯一范围为工作流、触发器和键。

外部接口：

```text
POST /api/workflows/v1/{workflow_id}/run
GET  /api/workflows/v1/runs/{run_id}
POST /api/workflows/v1/hooks/{trigger_id}
```

## 安全模型

- 工作流 DSL 只引用平台发布的工具、插件、MCP、脚本、只读数据源和组织资源，不提供代码节点或数据库写入节点。
- 高风险节点没有显式审批时，运行器自动创建分配给 Owner/Admin 的安全审批门。
- HTTPS 仅允许公网 HTTPS 地址，禁用重定向并拒绝回环、私网、链路本地和非公网解析结果。
- HTTPS 凭据只保存到组织凭据服务；DSL 只保存 `credential_id`。
- 节点输入、输出、等待内容和事件会脱敏并限制大小；组织运行顶层输入也按常见密钥名脱敏，不记录模型思维链。
- 数据源节点发布、回滚和试运行前使用数据源方言执行 AST 只读及授权表预检，但不会建立数据库连接或执行 SQL。
- 所有组织接口从 URL 和登录成员关系确定组织，请求体中的组织编号不会被采用。

## 数据库升级

数据库格式版本为 v2。首次打开 v1 数据库时，平台先通过 SQLite Backup API 在数据库同目录的 `backups/` 创建带时间戳的私有备份，再在单事务中创建 Workflow 表并验证版本、关键表和外键。迁移失败会回滚并拒绝启动，原数据库和备份均保留。

配置仍不支持热重载；修改平台配置后需要完全停止旧进程再启动。Workflow 草稿、发布和运行数据本身写入 SQLite，不要求修改配置文件。
