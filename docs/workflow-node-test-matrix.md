# 工作流节点输入输出与集成测试矩阵

本文档与 `src.core.workflows.definition.NODE_CATALOG`、工作流编辑器节点说明和
`tests.test_workflows.WorkflowNodeContractTests` 使用同一契约。默认测试通过真实
DSL、SQLite、WorkflowStore 与生产节点分发器执行；模型、远程 HTTP 和通知边界使用
确定性测试实现。设置 `BOTPLATFORM_WORKFLOW_REAL_INTEGRATION=1` 后可运行显式配置的
真实依赖冒烟套件。

| 节点 | 关键配置/输入 | 输出 | 端口与重点用例 |
|---|---|---|---|
| start | 工作流输入 | 输入对象 | `default`；类型化输入、默认值类型、1 MiB 限制 |
| end | `output` | 最终输出 | 声明 outputs 时校验必填键和类型，保留额外键 |
| set_variable | `values` | 变量对象 | `default/error`；变量模板渲染 |
| template | `text` | `{text}` | 安全引用、未知输入和下游引用拒绝 |
| field_map | `mapping` | 映射对象 | `default/error` |
| merge | `values` | 合并对象 | `default/error` |
| delay | `seconds` | `{resumed_at}` | 持久化等待、过期、进程重启恢复 |
| llm | `prompt/model` | `{text}` | 模型不可用、调用失败、重试策略 |
| extract | `text/fields` | `{data,text}` | 模型 JSON 解析失败 |
| classifier | `text/categories` | `{text}` | 空分类拒绝 |
| agent | `agent_id/prompt` | `{text}` | 智能体不存在或不可用 |
| knowledge | `query/limit/category_ids` | `{items}` | 数量范围、组织隔离 |
| condition | `left/operator/right` | `{matched}` | `true/false/error`、全部比较操作符 |
| switch | `value/cases` | `{value}` | `case:key/default/error`、分支键与匹配值唯一 |
| for_each | `workflow_id/items` | `{items}` | 固定版本、最多 100 项、最大深度 5 |
| subworkflow | `workflow_id/inputs` | 子流程输出 | 发布时固定版本、递归调用拒绝 |
| tool | `tool_name/arguments` | `{data}` | 工具审批、失败、操作键与脱敏 |
| script | `script_id/parameters` | 脚本提交结果 | 审批、脚本不可用、真实子进程冒烟 |
| datasource | `datasource_id/sql/limit` | rows/row_count | 发布前 AST 只读与授权表预检、真实 PG 可选冒烟 |
| http | `method/url/body/credential_id` | status_code/body | HTTPS、SSRF、禁重定向、写操作审批 |
| notification | `message` | notification_ids/status | 试运行预览、安全审批、真实渠道可选冒烟 |
| approval | `title/assignees/ttl` | 审批响应 | `approved/rejected/error`、权限和重启恢复 |
| human_input | `title/fields/ttl` | 用户字段对象 | 前后端类型校验、过期和拒绝 |

流程级契约另覆盖：五段 cron 语义解析、草稿自动保存冲突恢复、试运行定义快照、
发布/回滚资源预检、公开运行响应最小投影，以及运行输入和节点日志脱敏。

## 测试命令

```bash
# 核心 DSL、存储、全部节点契约和 API
python -m unittest tests.test_workflows tests.test_workflows_api -v

# 浏览器画布和按钮 E2E
BOTPLATFORM_RUN_WORKFLOW_E2E=1 python -m unittest tests.test_workflows_e2e -v

# 显式配置的真实依赖冒烟
BOTPLATFORM_WORKFLOW_REAL_INTEGRATION=1 \
  python -m unittest tests.test_workflows_real_integration -v
```

真实依赖套件始终经过工作流生产节点分发器；设置总开关后，各项仍按以下变量逐项
启用，缺少资源时精确 `skip`：

| 依赖 | 显式配置 |
|---|---|
| 模型 | `BOTPLATFORM_WORKFLOW_TEST_MODEL` |
| HTTPS | `BOTPLATFORM_WORKFLOW_TEST_HTTPS_URL` |
| 脚本 | `BOTPLATFORM_WORKFLOW_TEST_SCRIPT_ID`，可选 `BOTPLATFORM_WORKFLOW_TEST_SCRIPT_PARAMETERS` |
| 数据源 | `BOTPLATFORM_WORKFLOW_TEST_DATASOURCE_ID`，可选 `..._PASSWORD`、`..._SQL` |
| 通知 | `BOTPLATFORM_WORKFLOW_TEST_NOTIFICATION_ENABLE_SEND=1`，以及专用 TOKEN、BASE_URL、BOT_ID、OWNER_ID、USER_ID、CONTEXT_TOKEN |

内置只读工具使用临时租户直接执行。通知必须额外设置发送开关且提供专用测试收件人，
仅设置总开关不会产生消息副作用。默认 CI 和全量 `unittest discover` 不需要真实密钥，
不产生费用、远程写入或消息副作用。
