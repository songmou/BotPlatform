"""Build application services and manage the bot process lifecycle."""

from __future__ import annotations

import sys
from typing import Optional

from src.application.bot import (
    WeChatBot,
    delete_credentials,
    display_qr_code,
    load_credentials,
    print_login_status,
    save_credentials,
)
from src.config.loader import ConfigError, load_project_config
from src.infrastructure.logging import (
    log_model_call,
    log_model_fallback,
    log_tool_call,
)
from src.integrations.embeddings import EmbeddingClient
from src.integrations.ilink import ILinkClient, ILinkError, SessionExpired
from src.modeling import ModelError, ModelRouter
from src.modeling.factory import create_model_client
from src.paths import CONFIG_DIR, CREDENTIALS_PATH, DATA_DIR, PROJECT_ROOT
from src.plugins import PluginContext, build_plugins
from src.services.agent import AgentService
from src.services.knowledge import KnowledgeService
from src.services.memory import MemoryService, OllamaMemoryExtractor
from src.services.notification import NotificationService, TenantRecipientStore
from src.services.scheduler import SchedulerService
from src.services.script import ScriptService
from src.storage.tenants import (
    ConversationStore,
    ScheduleStore,
    SettingsStore,
    TenantRegistry,
    TenantStoreError,
)
from src.tooling import ToolRuntime


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
    notification_service = NotificationService(
        credentials_loader=load_credentials,
        recipient_store=recipient_store,
    )
    conversation_store = ConversationStore(
        tenant_registry, project_config.app.history_rounds * 2
    )
    settings_store = SettingsStore(tenant_registry)
    schedule_store = ScheduleStore(tenant_registry)
    embedding_client = (
        EmbeddingClient(project_config.embedding)
        if project_config.embedding.enabled
        else None
    )
    knowledge_service = KnowledgeService(tenant_registry, embedding_client)
    local_memory_profile = next(
        (
            profile
            for profile in project_config.models.values()
            if profile.enabled and profile.type == "ollama"
        ),
        None,
    )
    memory_service = MemoryService(
        tenant_registry,
        OllamaMemoryExtractor(local_memory_profile) if local_memory_profile else None,
    )

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
    except (ModelError, ValueError) as exc:
        for client in clients.values():
            client.close()
        memory_service.close()
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
            try:
                credentials = load_credentials()
            except ILinkError as exc:
                print(str(exc), file=sys.stderr)
                print("将删除无效凭证并重新扫码。")
                delete_credentials()
                credentials = None

            ilink = ILinkClient(credentials=credentials)
            scheduler: Optional[SchedulerService] = None
            script_service: Optional[ScriptService] = None
            tool_runtime: Optional[ToolRuntime] = None
            try:
                if credentials is None:
                    credentials = ilink.login(display_qr_code, status_changed=print_login_status)
                    save_credentials(credentials)
                    print("微信凭证已保存到 {}。".format(CREDENTIALS_PATH))
                else:
                    print("已加载保存的微信凭证，bot_id={}。".format(credentials.bot_id))

                script_service = ScriptService(
                    project_config.scripts,
                    credentials,
                    recipient_store,
                    PROJECT_ROOT,
                    tenant_registry,
                )
                platform_plugins = build_plugins(
                    project_config.plugins,
                    context=PluginContext(
                        project_root=PROJECT_ROOT,
                        tenant_registry=tenant_registry,
                        notification_service=notification_service,
                    ),
                ) if project_config.tools.enabled else []
                codex_tasks_plugin = next(
                    (plugin for plugin in platform_plugins if plugin.id == "codex_tasks"),
                    None,
                )
                tool_runtime = (
                    ToolRuntime(
                        project_config.tools,
                        project_config.app.timezone,
                        audit_logger=log_tool_call,
                        script_service=script_service,
                        tenant_registry=tenant_registry,
                        knowledge_service=knowledge_service,
                        plugins=platform_plugins,
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
                )
                scheduler = SchedulerService(
                    credentials=credentials,
                    tasks=project_config.schedules,
                    timezone_name=project_config.app.timezone,
                    agent_service=agent_service,
                    recipient_store=recipient_store,
                    script_service=script_service,
                    tenant_registry=tenant_registry,
                    schedule_store=schedule_store,
                )
                scheduler.start()
                print(
                    "定时任务已启动：启用 {} / 共 {} 项，时区 {}。".format(
                        scheduler.enabled_count,
                        len(project_config.schedules),
                        project_config.app.timezone,
                    )
                )
                WeChatBot(
                    ilink,
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
                ).run()
            except SessionExpired:
                print("微信登录已失效，将重新扫码。", file=sys.stderr)
                delete_credentials()
                continue
            finally:
                if scheduler:
                    scheduler.shutdown()
                if script_service:
                    script_service.shutdown()
                if tool_runtime:
                    tool_runtime.close()
                ilink.close()
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
