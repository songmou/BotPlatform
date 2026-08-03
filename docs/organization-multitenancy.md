# 公共资源与组织租户

平台以现有 `tenant_id` UUID 作为组织主键，避免迁移业务表和
`data/users/<tenant_id>` 目录。自然人账号、组织和成员关系分别存放在
`users`、`organizations` 与 `organization_memberships`。

## 权限边界

- 平台角色控制 `/api/*` 兼容管理接口与 `/api/v2/platform/*`。
- Owner、Admin、Member 控制 `/api/v2/orgs/{organization_id}/*`。
- 每个组织接口都根据登录账号验证 URL 中的组织，不接受请求体覆盖组织编号。
- Owner 才能转移所有权；Owner 不能在未转移所有权前退出组织。
- 公共资源只能由平台角色按权限发布。组织所有成员可以共建智能体、知识、文件、
  非密钥渠道配置和定时任务；Owner/Admin 维护组织服务凭据、成员、预算与生命周期。
- 插件包、本机脚本、工具引擎与 MCP 等能力只能由平台发布，组织不能覆盖或单独启停。
- 组织可以新建智能体，或复制指定版本的平台智能体模板；复制后完全独立，不跟随模板升级。
- 资源 JSON 禁止保存密钥字段；模型 Key、MCP 请求头和插件密钥继续保存在受限密钥存储。

## 资源解析

`platform_resources` 保存资源指针，`platform_resource_versions` 保存不可变版本。
资源区分草稿、已发布和实际运行版本，并记录激活状态与失败原因。数据库是平台
配置的唯一事实来源；`config/*.json` 只在首次启动且数据库不存在对应资源时导入，
后续文件变化不会覆盖数据库版本。

组织资源不再使用通用覆盖模型。`organization_agents` 只保存组织自己的智能体及
模板来源 ID/版本。模型、工具、Skill、插件工具和 MCP 始终引用当前已激活的平台
目录；新引用不能选择弃用项，已有引用在迁移完成前可以继续使用弃用项。

聊天模型、智能体模板、Skill、MCP 和工具策略发布后可热应用。热应用失败时运行时
继续使用旧快照。嵌入/重排模型、插件包和脚本代码变更进入“等待重启”，重启前组织
仍只看到旧运行版本。

## 成员私有数据

Web 对话使用 `web_conversations`，按 `organization_id + user_id` 校验所有权。
消息渠道认领账号后，组织共享知识、文件和工具 workspace 仍落在组织 UUID；
对话、记忆、SOUL、待办和个人集成使用
`member-personal:<organization_id> + user_id` 对应的内部存储主体。内部主体不会在
平台租户列表中展示。

消息渠道凭据与成员个人业务集成凭据只在 `credential_metadata` 保存归属、资源引用和
外置密钥引用；密文值写入权限为 `0600` 的独立凭据存储，任何列表、详情、
日志和审计响应都不会返回明文。Owner/Admin 可维护渠道凭据，成员只能维护并
查看自己的个人业务集成凭据；成员退出与组织注销会同步清理对应的外置密钥。
历史组织级模型、插件与 MCP 凭据只保留只读迁移元数据，不参与运行。

渠道命令：

- `/claim`：为旧单人租户签发一次性认领码；
- `/bind`：把另一个渠道身份关联到同一自然人账号；
- `/org list`：列出账号加入的组织；
- `/org use <组织编号或唯一前缀>`：切换活动组织。

## 迁移与删除

- 启动时把旧 tenant 幂等登记为保留原 UUID 的未认领单人组织；进程运行期间
  新创建的渠道 tenant 也会立即登记，不需要等待下次重启。
- `data/system/web_conversations.json` 会幂等复制到首位平台管理员的调试空间；
  新会话只写 SQLite。
- 成员退出会删除其当前组织内的私有存储，不删除组织共享资源。
- 组织删除前会在
  `data/system/organization_backups/<organization_id>-<timestamp>` 生成 SQLite、
  共享目录、成员私有目录和清单快照；确认备份完成后，依次清理外置凭据和
  组织数据。V2 组织接口、旧平台租户删除接口和机器人整租户删除均遵守该
  顺序。备份仅包含凭据引用元数据，不复制密钥明文；组织凭据和历史集成
  凭据文件中的对应密钥都会在备份成功后清理。

## 审计

所有修改型 `/api/v2/*` 请求都会记录请求编号、操作者、组织、HTTP 动作、
资源路由和结果状态，不记录请求体。平台管理员可通过
`/api/v2/platform/audit` 查看聚合审计，组织成员可通过
`/api/v2/orgs/{organization_id}/audit` 查看本组织审计。旧 `/api/*` 管理
接口返回弃用响应头，租户账号不能通过兼容接口绕过 V2 组织权限。

组织模型调用汇总、运行详情、工具审计与预算分别位于
`/api/v2/orgs/{organization_id}/analytics/*`。服务端固定使用 URL 中且已验证
成员关系的组织编号过滤；运行详情对跨组织编号返回不存在。组织成员可以查看
组织统计，只有 Owner/Admin 可以维护组织模型预算。

## V2 接口

- `/api/v2/me`：账号、平台权限和组织成员关系列表，不返回当前或所选组织；
- `/api/v2/catalog/*`：可见公共目录；
- `/api/v2/platform/knowledge/*`、`/api/v2/platform/drive/*`：平台公共知识与公共文件管理；
- `/api/v2/orgs/{organization_id}/agents|channels|schedules|knowledge|drive|members|analytics|audit`：类型化组织能力；
- `/api/v2/platform/catalog/{type}/{id}/draft|publish|rollback|activation`：平台目录版本与激活；
- `/api/v2/platform/*`：组织管理和平台审计。

统一控制台首页 `/` 对平台管理员进入 `/platform`，组织账号进入
`/organization/overview`。平台页面位于 `/platform/*`，不读取任何组织上下文；
组织页面位于 `/organization/<module>?organization_id=<uuid>`。只有一个组织时前端
自动补全 URL；多个组织且 URL 未指定时显示选择状态，不会静默选择首项。无权访问
URL 中的组织直接返回 403。旧页面地址只进行 308 跳转。

知识库与文件管理采用双入口：`/platform/knowledge`、`/platform/drive` 管理公共内容，
`/organization/knowledge`、`/organization/drive` 管理 URL 指定组织的共享内容。
组织成员可在组织页面浏览公共知识和公共文件，但公共内容始终只读；平台管理员需要
代管具体组织时也必须通过带 `organization_id` 的组织页面进入并留下代管审计。
