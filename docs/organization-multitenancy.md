# 公共资源与组织租户

平台以现有 `tenant_id` UUID 作为组织主键，避免迁移业务表和
`data/users/<tenant_id>` 目录。自然人账号、组织和成员关系分别存放在
`users`、`organizations` 与 `organization_memberships`。

## 权限边界

- 平台角色控制 `/api/*` 兼容管理接口与 `/api/v2/platform/*`。
- Owner、Admin、Member 控制 `/api/v2/orgs/{organization_id}/*`。
- 每个组织接口都根据登录账号验证 URL 中的组织，不接受请求体覆盖组织编号。
- Owner 才能转移所有权；Owner 不能在未转移所有权前退出组织。
- 公共资源只能由平台管理员发布，组织成员可以共建组织资源和覆盖公共资源。
- 插件包、本机脚本、工具引擎等可信代码只能由平台发布；组织只能启停或覆盖。
- 组织 MCP 只允许远程 HTTPS Streamable HTTP，资源 JSON 禁止保存密钥字段。

## 资源解析

`scoped_resources` 保存公共和组织资源，
`organization_resource_overrides` 保存公共资源的组织级覆盖。有效配置顺序为：

1. 组织自有资源；
2. 公共资源的组织覆盖；
3. 已发布的公共资源；
4. 首次启动时从 `config/*.json` 写入的公共种子。

字段覆盖只保存差异。列表字段必须选择 `inherit`、`replace` 或 `disable`，
恢复默认时删除覆盖记录，因此公共资源升级后未覆盖字段会继续继承。

## 成员私有数据

Web 对话使用 `web_conversations`，按 `organization_id + user_id` 校验所有权。
消息渠道认领账号后，组织共享知识、文件和工具 workspace 仍落在组织 UUID；
对话、记忆、SOUL、待办和个人集成使用
`member-personal:<organization_id> + user_id` 对应的内部存储主体。内部主体不会在
平台租户列表中展示。

组织服务凭据与成员个人凭据只在 `credential_metadata` 保存归属、资源引用和
外置密钥引用；密文值写入权限为 `0600` 的独立凭据存储，任何列表、详情、
日志和审计响应都不会返回明文。Owner/Admin 可维护组织服务凭据，成员只能
维护并查看自己的个人凭据；成员退出与组织注销会同步清理对应的外置密钥。

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

- `/api/v2/me`：账号、成员关系和活动组织；
- `/api/v2/catalog/*`：可见公共目录；
- `/api/v2/orgs/{organization_id}/*`：资源、成员、对话、知识、文件、凭据和审计；
- `/api/v2/platform/*`：组织管理、公共资源发布和平台审计。

租户控制台入口为 `/app`；平台后台兼容入口为 `/admin`。平台管理员可在
`/users#organizations` 查看所有组织与自动迁移的存量个人空间，并完成组织
生命周期管理；组织成员继续在 `/app` 管理其所属组织。
