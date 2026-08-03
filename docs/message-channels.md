# 消息渠道改造与接入手册

## 1. 改造目标

BotPlatform 将微信 iLink、企业微信智能机器人和飞书机器人统一为“消息渠道”。业务层只处理标准化的入站消息、出站消息和投递端点，不直接依赖任一平台 SDK。

本次改造的验收边界：

- 微信 iLink 原有私聊、图片、扫码登录和主动通知保持兼容；
- 企业微信与飞书采用 WebSocket 长连接，不要求公网回调地址；
- 每个渠道独立启动、独立报错，一个渠道异常不退出其他渠道；
- 渠道配置不包含密钥，平台凭据以 `0600` 权限独立保存；
- 私聊默认拥有原有能力；群聊仅在 `@机器人` 时响应，并禁用私有记忆、私有知识与本机工具；
- 同一用户可通过一次性绑定码把不同渠道身份绑定到同一租户；
- 配置修改后必须完整重启，系统不提供热重载。

## 2. 模块边界

| 模块 | 职责 | 主要文件 |
| --- | --- | --- |
| 消息契约 | 入站消息、出站消息、附件、端点、能力声明 | `src/core/messaging/contracts.py` |
| 渠道适配器 | 平台事件归一化、发送、附件下载、SDK 生命周期 | `src/core/messaging/adapters/` |
| Provider 注册表 | 渠道类型、凭据字段、适配器构造 | `src/core/messaging/providers.py` |
| 配置与凭据 | 校验 `channels.json`；独立保存密钥 | `configuration.py`、`credentials.py` |
| 运行管理 | 独立接收线程、持久化 inbox、状态隔离 | `manager.py`、`store.py` |
| 业务编排 | 群聊策略、Agent 路由、绑定命令、能力收缩 | `src/core/application/bot.py` |
| 管理入口 | 渠道监控页（只读）、智能体页渠道实例配置、CLI | `src/api/routers/bots.py`、`src/api/routers/agents.py`、`cli.py` |

平台适配器不得直接访问模型、租户、工具或知识库。新增渠道时只需实现消息契约并注册 Provider。

## 3. 通用配置

渠道的非敏感配置写入 `config/channels.json`：

```json
{
  "channels": [
    {
      "id": "wechat-main",
      "type": "wechat_ilink",
      "enabled": true,
      "agent_id": "general",
      "settings": {
        "group_policy": "private_only"
      }
    },
    {
      "id": "wecom-main",
      "type": "wecom_aibot",
      "enabled": true,
      "agent_id": "general",
      "settings": {
        "group_policy": "mention_only"
      }
    },
    {
      "id": "feishu-main",
      "type": "feishu",
      "enabled": true,
      "agent_id": "general",
      "settings": {
        "group_policy": "mention_only"
      }
    }
  ]
}
```

规则：

- `id` 是渠道实例编号，同一类型可以配置多个实例；
- `agent_id` 必须引用已启用的 Agent；
- `group_policy` 只允许 `private_only` 或 `mention_only`；
- `token`、`secret`、`password` 等凭据字段禁止写入此文件；
- 可以临时禁用全部渠道进入维护状态；管理面板与其他服务仍可运行。

管理面板 `/channels` 为只读监控页，按渠道类型分区展示各实例的连接状态；渠道实例的新增、修改、删除请在 `/agents` 智能体管理页的编辑弹窗中完成。渠道类型（微信 iLink、企业微信、飞书）为系统内置，不可增删。Web 与 CLI 保存配置后都会提示重启。

## 4. 微信 iLink 接入

1. 保留或创建 `wechat_ilink` 类型渠道。
2. 首次执行扫码登录：

   ```bash
   ./start.sh channel login wechat-main
   ```

3. 手机确认后，凭据写入兼容路径 `data/system/credentials.json`。
4. 检查状态并启动：

   ```bash
   ./start.sh channel status wechat-main
   ./start.sh
   ```

iLink 默认只处理私聊。原有通知端点和 `context_token` 继续使用，但协议上下文不会进入模型消息。

## 5. 企业微信接入

1. 在企业微信管理后台创建“智能机器人”，选择 API 模式和长连接，取得 Bot ID 与 Secret。
2. 在 `/channels` 添加 `wecom_aibot` 渠道；或先把上面的 `wecom-main` 配置加入 `channels.json`。
3. 通过标准输入保存凭据，避免密钥进入命令历史：

   ```bash
   printf '%s' '{"bot_id":"替换为 Bot ID","secret":"替换为 Secret"}' \
     | ./start.sh channel configure wecom-main --stdin
   ```

4. 执行格式检查：

   ```bash
   ./start.sh channel test wecom-main
   ```

5. 完整停止旧进程，再运行 `./start.sh`。日志出现渠道接收循环启动后，在企业微信私聊机器人；群聊中需要 `@机器人`。

适配器使用 `wecom-aibot-sdk==1.0.8`，连接企业微信长连接网关，支持文本、图片下载、图片上传、被动回复和主动发送。

## 6. 飞书接入

1. 在飞书开放平台创建企业自建应用，启用机器人能力。
2. 为应用开通接收与发送消息所需权限，选择长连接接收事件，发布应用并把机器人加入目标会话。
3. 在 `/channels` 添加 `feishu` 渠道，或更新 `channels.json`。
4. 通过标准输入保存 App ID 和 App Secret：

   ```bash
   printf '%s' '{"app_id":"cli_xxx","app_secret":"替换为 App Secret"}' \
     | ./start.sh channel configure feishu-main --stdin
   ```

5. 运行检查并完整重启：

   ```bash
   ./start.sh channel test feishu-main
   ./start.sh
   ```

6. 私聊机器人验证文字和图片；群聊中用 `@机器人` 验证触发和会话隔离。

适配器使用 `lark-channel-sdk==1.2.0` 的标准化消息、长连接、发送与资源下载接口，安全模式采用 `audit`，便于上线初期发现兼容问题。

## 7. 跨渠道身份绑定

不同平台的外部用户 ID 默认映射为不同租户，避免误合并数据。用户必须主动完成绑定：

1. 在已有数据的渠道私聊发送 `/bind`；
2. 机器人返回有效期 10 分钟的一次性绑定码；
3. 在新渠道私聊发送 `/bind <绑定码>`；
4. 绑定成功后，新渠道身份复用原租户、模型偏好和私聊数据。

系统只保存绑定码哈希，使用后立即失效。同一新身份 10 分钟内最多尝试 5 次。若新渠道身份已经产生独立租户数据，系统拒绝自动合并，防止覆盖或串租户。

## 8. 上线与回滚步骤

建议按以下顺序发布：

1. **准备**：备份 `data/system/botplatform.sqlite3`，安装锁定依赖，运行全量测试。
2. **兼容验证**：只启用 `wechat-main`，验证私聊、图片、通知和扫码凭据复用。
3. **单渠道灰度**：先启用一个企业微信或飞书实例，验证状态、私聊、群聊 `@`、图片和重连。
4. **身份灰度**：用测试账号走完 `/bind`，验证两个渠道的 `/id` 一致。
5. **正式启用**：增加其余实例，观察渠道状态与 inbox 重试。

回滚时先停止进程，把新增渠道的 `enabled` 改为 `false`，再完整重启。数据库 v23 新增的会话字段和绑定表可以保留，不影响仅运行 iLink；不要手工删除迁移记录。

## 9. 新增其他渠道的标准步骤

1. 在 `contracts.py` 现有契约上实现 `MessagingAdapter`；
2. 在 `adapters/` 新增平台适配器，保证凭据懒加载、异常翻译和 `close()` 幂等；
3. 在 `providers.py` 注册类型、展示名、凭据字段和构造器；
4. 在配置加载器中加入类型与非敏感 `settings` 白名单；
5. 声明并锁定 SDK 依赖；
6. 增加消息归一化、收发、附件、去重、故障隔离和密钥不回显测试；
7. 在 `/channels` 完成真实平台灰度，不把平台逻辑写入业务层。

## 10. 验收清单

- `python -m unittest discover -s tests -v` 通过；
- `channel list/status/test` 输出正确，错误消息为中文；
- `channels.json` 和 API 响应中不存在密钥；
- POSIX 下渠道凭据文件权限为 `0600`；
- 任一渠道断开时其他渠道继续处理消息；
- 非 `@` 群消息不入业务处理，群聊无法调用本机工具或读取私有上下文；
- 私聊上下文、各群聊上下文和主动通知上下文互不串用；
- `/bind` 一次性、过期、重放和限流行为符合预期；
- 修改配置后完整重启，确认旧进程已退出且端口未残留。
