"""Build application services and manage the bot process lifecycle."""

from __future__ import annotations

import sys
from typing import Optional

from src.core.application.bot import (
    MessageBot,
    delete_credentials,
    display_qr_code,
    load_credentials,
    print_login_status,
    save_credentials,
)
from src.core.config.loader import ConfigError, load_project_config
from src.core.infrastructure.logging import (
    log_model_call,
    log_model_fallback,
    log_tool_call,
)
from src.core.integrations.embeddings import EmbeddingClient
from src.core.integrations.ilink import ILinkClient, ILinkError, SessionExpired
from src.core.messaging import (
    AuthenticationExpired,
    ChannelAddressStore,
    ChannelManager,
    MessageInboxStore,
    MessageRouter,
)
from src.core.messaging.adapters import WeChatILinkAdapter
from src.core.modeling import ModelError, ModelRouter
from src.core.modeling.factory import create_model_client
from src.core.paths import (
    CONFIG_DIR,
    DATA_DIR,
    PROJECT_ROOT,
    channel_credentials_path,
)
from src.core.plugins import PluginContext, build_plugins
from src.core.services.agent import AgentService
from src.core.services.knowledge import KnowledgeService
from src.core.services.integration import IntegrationService
from src.core.services.memory import MemoryService, ModelMemoryExtractor
from src.core.services.notification import (
    NotificationDispatcher,
    NotificationService,
    TenantRecipientStore,
)
from src.core.services.scheduler import SchedulerService
from src.core.services.script import ScriptService
from src.core.storage.tenants import (
    ConversationStore,
    ScheduleStore,
    SettingsStore,
    IntegrationStore,
    TenantRegistry,
    TenantStoreError,
)
from src.core.tooling import ToolRuntime


def run_bot(args, project_config=None) -> int:
    if project_config is None:
        try:
            project_config = load_project_config(CONFIG_DIR)
        except ConfigError as exc:
            print("配置加载失败：{}".format(exc), file=sys.stderr)
            return 1

    try:
        tenant_registry = TenantRegistry(DATA_DIR)
    except TenantStoreError as exc:
        print("租户数据加载失败：{}".format(exc), file=sys.stderr)
        return 1
    recipient_store = TenantRecipientStore(tenant_registry)
    address_store = ChannelAddressStore(tenant_registry)
    conversation_store = ConversationStore(
        tenant_registry, project_config.app.history_rounds * 2
    )
    settings_store = SettingsStore(tenant_registry)
    schedule_store = ScheduleStore(tenant_registry)
    integration_store = IntegrationStore(tenant_registry)
    integration_service = IntegrationService(integration_store)
    embedding_client = (
        EmbeddingClient(project_config.embedding)
        if project_config.embedding.enabled
        else None
    )
    knowledge_service = KnowledgeService(tenant_registry, embedding_client)
    if args.logout:
        delete_credentials()
        print("已清除微信登录凭证。")

    clients = {}
    try:
        for profile_id, profile in project_config.models.items():
            if not profile.enabled:
                continue
            clients[profile_id] = create_model_client(profile, logger=log_model_call)
        model = ModelRouter(
            clients,
            primary_profile_id=project_config.app.active_model,
            fallback_profile_id=project_config.app.fallback_model,
            local_profile_id=project_config.app.local_model,
            flash_profile_id=project_config.app.flash_model,
            pro_profile_id=project_config.app.pro_model,
            vision_profile_id=project_config.app.vision_model,
            cooldown_seconds=project_config.app.fallback_cooldown_seconds,
            fallback_logger=log_model_fallback,
        )
        memory_service = MemoryService(
            tenant_registry,
            ModelMemoryExtractor(model),
        )
    except (ModelError, ValueError) as exc:
        for client in clients.values():
            client.close()
        if embedding_client:
            embedding_client.close()
        print("模型客户端创建失败：{}".format(exc), file=sys.stderr)
        return 1
    try:
        identity = model.identity
        print(
            "正在检查模型档案 {}：{} / {}……".format(
                identity.profile_id, identity.provider, identity.configured_model
            )
        )
        model.ensure_ready()
        print(
            "模型已就绪：档案={}，提供商={}，模型={}".format(
                identity.profile_id, identity.provider, identity.configured_model
            )
        )
        if model.cooling_down:
            print(
                "默认模型暂不可用，文字请求将临时使用已配置的兜底模型；"
                "冷却后会重试默认模型。",
                file=sys.stderr,
            )
        print(
            "当前 Agent：{}（{}）".format(
                project_config.active_agent.name, project_config.active_agent.id
            )
        )
        while True:
            adapters = []
            ilink_clients = []
            scheduler: Optional[SchedulerService] = None
            script_service: Optional[ScriptService] = None
            tool_runtime: Optional[ToolRuntime] = None
            notification_dispatcher: Optional[NotificationDispatcher] = None
            channel_manager: Optional[ChannelManager] = None
            try:
                for channel_config in project_config.channels.values():
                    if not channel_config.enabled:
                        continue
                    credential_path = channel_credentials_path(channel_config.id)
                    try:
                        credentials = load_credentials(credential_path)
                    except ILinkError as exc:
                        print(str(exc), file=sys.stderr)
                        print(
                            "将清除渠道 {} 的无效凭证并重新登录。".format(
                                channel_config.id
                            )
                        )
                        delete_credentials(credential_path)
                        credentials = None
                    ilink = ILinkClient(credentials=credentials)
                    ilink_clients.append(ilink)
                    if credentials is None:
                        print("正在登录消息渠道 {}。".format(channel_config.id))
                        credentials = ilink.login(
                            display_qr_code,
                            status_changed=print_login_status,
                        )
                        save_credentials(credentials, credential_path)
                        print(
                            "渠道 {} 的凭证已保存到 {}。".format(
                                channel_config.id,
                                credential_path,
                            )
                        )
                    else:
                        print(
                            "已加载渠道 {} 的凭证，bot_id={}。".format(
                                channel_config.id,
                                credentials.bot_id,
                            )
                        )
                    adapters.append(
                        WeChatILinkAdapter(
                            ilink,
                            channel_id=channel_config.id,
                        )
                    )

                message_router = MessageRouter(adapters)
                notification_service = NotificationService(
                    credentials_loader=None,
                    recipient_store=recipient_store,
                    message_router=message_router,
                    address_store=address_store,
                )
                notification_dispatcher = NotificationDispatcher(notification_service)
                notification_dispatcher.start()
                script_service = ScriptService(
                    project_config.scripts,
                    None,
                    recipient_store,
                    PROJECT_ROOT,
                    tenant_registry,
                    integration_store,
                    notification_service=notification_service,
                    keychain_service=integration_service.keychain,
                )
                platform_plugins = build_plugins(
                    project_config.plugins,
                    context=PluginContext(
                        project_root=PROJECT_ROOT,
                        tenant_registry=tenant_registry,
                        notification_service=notification_service,
                        timezone=project_config.app.timezone,
                    ),
                ) if project_config.tools.enabled else []
                codex_tasks_plugin = next(
                    (plugin for plugin in platform_plugins if plugin.id == "codex_tasks"),
                    None,
                )
                mcp_manager = None
                if project_config.tools.enabled and project_config.mcp_servers:
                    from src.core.tooling.mcp_client import McpClientManager

                    mcp_manager = McpClientManager()
                    mcp_manager.start()
                    mcp_manager.reload(project_config.mcp_servers)
                tool_runtime = (
                    ToolRuntime(
                        project_config.tools,
                        project_config.app.timezone,
                        audit_logger=log_tool_call,
                        script_service=script_service,
                        tenant_registry=tenant_registry,
                        knowledge_service=knowledge_service,
                        plugins=platform_plugins,
                        mcp_manager=mcp_manager,
                    )
                    if project_config.tools.enabled
                    else None
                )
                if (
                    tool_runtime
                    and "run_command" in project_config.active_agent.tools
                    and not tool_runtime.command_runner.available
                ):
                    print(
                        "警告：macOS 命令沙箱不可用，run_command 已禁用；文件和系统工具仍可使用。",
                        file=sys.stderr,
                    )
                agent_service = AgentService(
                    model,
                    project_config.app,
                    project_config.agents,
                    tool_runtime=tool_runtime,
                    conversation_store=conversation_store,
                    settings_store=settings_store,
                    knowledge_service=knowledge_service,
                    memory_service=memory_service,
                    skills=project_config.skills,
                )
                scheduler = SchedulerService(
                    credentials=None,
                    tasks=project_config.schedules,
                    timezone_name=project_config.app.timezone,
                    agent_service=agent_service,
                    recipient_store=recipient_store,
                    script_service=script_service,
                    tenant_registry=tenant_registry,
                    schedule_store=schedule_store,
                    plugins=platform_plugins,
                    memory_service=memory_service,
                    notification_service=notification_service,
                )
                scheduler.start()
                print(
                    "定时任务已启动：启用 {} / 共 {} 项，时区 {}。".format(
                        scheduler.enabled_count,
                        len(project_config.schedules),
                        project_config.app.timezone,
                    )
                )
                message_bot = MessageBot(
                    None,
                    agent_service,
                    tenant_registry=tenant_registry,
                    recipient_store=recipient_store,
                    conversation_store=conversation_store,
                    schedule_store=schedule_store,
                    schedule_ids=[
                        task.id for task in project_config.schedules if task.enabled
                    ],
                    script_service=script_service,
                    knowledge_service=knowledge_service,
                    memory_service=memory_service,
                    codex_tasks_plugin=codex_tasks_plugin,
                    integration_service=integration_service,
                    notification_dispatcher=notification_dispatcher,
                    message_router=message_router,
                    address_store=address_store,
                )
                channel_manager = ChannelManager(
                    adapters,
                    MessageInboxStore(tenant_registry),
                    message_bot.handle_inbound,
                )
                print(
                    "消息服务已启动：渠道={}，正在等待私聊消息。按 Ctrl+C 退出。".format(
                        "、".join(adapter.channel_id for adapter in adapters)
                    )
                )
                channel_manager.run()
            except (SessionExpired, AuthenticationExpired):
                print("消息渠道登录已失效，将重新登录。", file=sys.stderr)
                if channel_manager is not None:
                    for status in channel_manager.statuses():
                        if status.state == "authentication_required":
                            delete_credentials(
                                channel_credentials_path(status.channel_id)
                            )
                continue
            finally:
                if channel_manager:
                    channel_manager.shutdown()
                if scheduler:
                    scheduler.shutdown()
                if script_service:
                    script_service.shutdown()
                if tool_runtime:
                    tool_runtime.close()
                if notification_dispatcher:
                    notification_dispatcher.shutdown()
                if channel_manager is None:
                    for client in ilink_clients:
                        client.close()
    except KeyboardInterrupt:
        print("\n机器人已停止。")
        return 0
    except (ILinkError, ModelError, OSError, TenantStoreError) as exc:
        print("启动失败：{}".format(exc), file=sys.stderr)
        return 1
    finally:
        model.close()
        memory_service.close()
        if embedding_client:
            embedding_client.close()
