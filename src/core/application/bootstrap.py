"""Build application services and manage the bot process lifecycle."""

from __future__ import annotations

import signal
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from src.core.application.bot import (
    MessageBot,
    delete_credentials,
    display_qr_code,
    print_login_status,
    save_credentials,
)
from src.core.application.services import CoreServices, build_core_services
from src.core.config.loader import (
    ChannelConfig,
    ConfigError,
    ProjectConfig,
    load_project_config,
)
from src.core.infrastructure.logging import (
    log_model_call,
    log_model_fallback,
    log_tool_call,
)
from src.core.integrations.ilink import ILinkClient, ILinkError
from src.core.messaging import (
    ChannelAddressStore,
    ChannelCredentialError,
    ChannelCredentialStore,
    ChannelManager,
    ChannelStatusRegistry,
    MessageInboxStore,
    MessageRouter,
    build_channel_adapter,
)
from src.core.modeling import ModelError
from src.core.paths import (
    CONFIG_DIR,
    DATA_DIR,
    PROJECT_ROOT,
    SYSTEM_DATA_DIR,
    channel_credentials_path,
)
from src.core.plugins import PluginContext, PluginManager, build_plugin_manager
from src.core.services.agent import AgentService
from src.core.services.integration import IntegrationService
from src.core.services.memory import MemoryService, ModelMemoryExtractor
from src.core.services.notification import (
    NotificationDispatcher,
    NotificationService,
)
from src.core.services.scheduler import SchedulerService
from src.core.services.script import ScriptService
from src.core.services.script_registry import ExternalScriptRegistry
from src.core.services.env_resolver import EnvResolver
from src.core.services.organization_schedule_tool import (
    OrganizationScheduleToolService,
)
from src.core.storage.tenants import (
    SettingsStore,
    IntegrationStore,
    TenantStoreError,
)
from src.core.storage.drive_audit import DriveAuditStore
from src.core.tooling import ToolRuntime


def _install_sigterm_handler() -> None:
    """Translate SIGTERM into KeyboardInterrupt so cleanup chains run.

    Service managers (systemd, launchd, docker) stop processes with SIGTERM;
    without this handler the process dies immediately and skips the
    scheduler/script/tool shutdown performed in the run loop's ``finally``.
    Signal handlers can only be installed from the main thread.
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def _raise_interrupt(_signum, _frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _raise_interrupt)


@dataclass
class BotRuntime:
    """Stable service graph shared by the channel loop and the web panel.

    Every service here survives channel re-logins; only ILink clients,
    adapters, ``MessageBot`` and ``ChannelManager`` are rebuilt per loop
    iteration in :func:`run_channel_loop`.
    """

    project_config: ProjectConfig
    services: CoreServices
    settings_store: SettingsStore
    integration_store: IntegrationStore
    integration_service: IntegrationService
    memory_service: MemoryService
    address_store: ChannelAddressStore
    message_router: MessageRouter
    notification_service: NotificationService
    notification_dispatcher: NotificationDispatcher
    external_script_registry: ExternalScriptRegistry
    script_service: ScriptService
    plugin_context: PluginContext
    plugin_manager: PluginManager
    mcp_manager: Optional[Any]
    tool_runtime: Optional[ToolRuntime]
    agent_service: AgentService
    scheduler: SchedulerService
    channel_statuses: ChannelStatusRegistry
    _started: bool = field(default=False, repr=False)
    _closed: bool = field(default=False, repr=False)

    def start(self) -> None:
        """Start background workers (outbox dispatcher and scheduler) once."""
        if self._started:
            return
        self._started = True
        self.plugin_manager.start()
        self.notification_dispatcher.start()
        self.scheduler.start()
        print(
            "定时任务已启动：启用 {} / 共 {} 项，时区 {}。".format(
                self.scheduler.enabled_count,
                len(self.project_config.schedules),
                self.project_config.app.timezone,
            )
        )

    def shutdown(self) -> None:
        """Stop every owned service exactly once, in reverse start order."""
        if self._closed:
            return
        self._closed = True
        self.scheduler.shutdown()
        self.script_service.shutdown()
        if self.tool_runtime:
            self.tool_runtime.close()
        self.notification_dispatcher.shutdown()
        self.memory_service.close()


def build_bot_runtime(
    project_config: ProjectConfig,
    services: CoreServices,
    *,
    tool_audit_store: Optional[Any] = None,
    mcp_call_log_store: Optional[Any] = None,
    tool_states: Optional[Dict[str, Dict[str, Any]]] = None,
    drive_audit_store: Optional[Any] = None,
) -> BotRuntime:
    """Assemble the stable service graph on top of ``CoreServices``.

    ``tool_audit_store``/``tool_states`` are only supplied by the web panel
    entry point; the plain bot entry point leaves them unset. Raises
    ``ModelError``/``ValueError`` on model client failures; the caller owns
    ``services`` and must close it when this raises.
    """
    tenant_registry = services.tenant_registry
    if drive_audit_store is None:
        drive_audit_store = DriveAuditStore(tenant_registry)
    settings_store = SettingsStore(tenant_registry)
    integration_store = IntegrationStore(tenant_registry)
    integration_service = IntegrationService(integration_store)
    memory_service = MemoryService(
        tenant_registry,
        ModelMemoryExtractor(services.model_router),
    )
    try:
        address_store = ChannelAddressStore(tenant_registry)
        # The router is long-lived and initially empty; the channel loop
        # swaps adapters in via ``MessageRouter.reset`` after each login.
        message_router = MessageRouter()
        notification_service = NotificationService(
            credentials_loader=None,
            recipient_store=services.recipient_store,
            message_router=message_router,
            address_store=address_store,
            conversation_store=services.conversation_store,
        )
        notification_dispatcher = NotificationDispatcher(notification_service)
        external_script_registry = ExternalScriptRegistry(
            SYSTEM_DATA_DIR / "script_registry.json",
            SYSTEM_DATA_DIR / "scripts.env",
        )
        # Organization values override the platform-managed global env; the
        # global layer is supplied by the external registry's 0600-checked file.
        env_resolver = EnvResolver(settings_store, external_script_registry.global_values)
        script_service = ScriptService(
            project_config.scripts,
            None,
            services.recipient_store,
            PROJECT_ROOT,
            tenant_registry,
            integration_store,
            notification_service=notification_service,
            keychain_service=integration_service.keychain,
            external_registry=external_script_registry,
            env_resolver=env_resolver,
            address_store=address_store,
        )
        # Chat tools and the scheduler share the organization-scoped schedule
        # store used by the Web panel.
        organization_schedule_service = OrganizationScheduleToolService(
            services.organization_control_store,
            services.organization_store,
            project_config,
        )
        plugin_context = PluginContext(
            project_root=PROJECT_ROOT,
            tenant_registry=tenant_registry,
            notification_service=notification_service,
            timezone=project_config.app.timezone,
            data_root=DATA_DIR / "plugins",
            env_resolver=env_resolver,
            knowledge_service=services.knowledge_service,
            model_router=services.model_router,
        )
        plugin_manager = build_plugin_manager(
            project_config.plugins if project_config.tools.enabled else {},
            context=plugin_context,
        )
        mcp_manager = None
        if project_config.tools.enabled and project_config.mcp_servers:
            from src.core.tooling.mcp_client import McpClientManager

            mcp_manager = McpClientManager()
            mcp_manager.start()
            mcp_manager.reload(project_config.mcp_servers)

        datasource_service = None
        if project_config.datasources:
            from src.core.datasource import DataSourceService

            datasource_service = DataSourceService()
            datasource_service.reload(project_config.datasources)

        tool_runtime = (
            ToolRuntime(
                project_config.tools,
                project_config.app.timezone,
                audit_logger=log_tool_call,
                script_service=script_service,
                tenant_registry=tenant_registry,
                knowledge_service=services.knowledge_service,
                plugin_manager=plugin_manager,
                mcp_manager=mcp_manager,
                tool_audit_store=tool_audit_store,
                mcp_call_log_store=mcp_call_log_store,
                tool_states=tool_states,
                organization_schedule_service=organization_schedule_service,
                drive_service=services.drive_service,
                drive_audit_store=drive_audit_store,
                resource_store=services.resource_store,
                datasource_service=datasource_service,
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
            services.model_router,
            project_config.app,
            project_config.agents,
            tool_runtime=tool_runtime,
            conversation_store=services.conversation_store,
            settings_store=settings_store,
            knowledge_service=services.knowledge_service,
            memory_service=memory_service,
            model_analytics_store=services.model_analytics_store,
            skills=project_config.skills,
            resource_store=services.resource_store,
        )
        scheduler = SchedulerService(
            credentials=None,
            tasks=project_config.schedules,
            timezone_name=project_config.app.timezone,
            agent_service=agent_service,
            recipient_store=services.recipient_store,
            script_service=script_service,
            tenant_registry=tenant_registry,
            schedule_store=services.schedule_store,
            plugin_manager=plugin_manager,
            memory_service=memory_service,
            notification_service=notification_service,
            organization_control_store=services.organization_control_store,
        )
    except Exception:
        memory_service.close()
        raise

    return BotRuntime(
        project_config=project_config,
        services=services,
        settings_store=settings_store,
        integration_store=integration_store,
        integration_service=integration_service,
        memory_service=memory_service,
        address_store=address_store,
        message_router=message_router,
        notification_service=notification_service,
        notification_dispatcher=notification_dispatcher,
        external_script_registry=external_script_registry,
        script_service=script_service,
        plugin_context=plugin_context,
        plugin_manager=plugin_manager,
        mcp_manager=mcp_manager,
        tool_runtime=tool_runtime,
        agent_service=agent_service,
        scheduler=scheduler,
        channel_statuses=ChannelStatusRegistry(),
    )


def run_channel_loop(runtime: BotRuntime, project_config: ProjectConfig) -> int:
    """Build channel adapters and rebuild them when organization config changes."""
    tenant_registry = runtime.services.tenant_registry
    channel_manager: Optional[ChannelManager] = None
    credential_store = ChannelCredentialStore()
    applied_revisions: Dict[str, Dict[str, Any]] = {}

    def configured_channels():
        controls = runtime.services.organization_control_store
        credentials_service = runtime.services.credential_service
        if controls is None or credentials_service is None:
            return list(project_config.channels.values()), {}, False
        rows = []
        for organization in runtime.services.organization_store.list_organizations():
            rows.extend(controls.list_channels(str(organization["organization_id"])))
        if not rows:
            return list(project_config.channels.values()), {}, False
        configs = []
        secrets = {}
        for row in rows:
            config = ChannelConfig(
                id=row["channel_instance_id"],
                type=row["type"],
                enabled=row["enabled"],
                agent_id=row["agent_id"],
                settings=dict(row["settings"]),
            )
            configs.append(config)
            if row["credential_configured"]:
                try:
                    import json

                    secrets[config.id] = json.loads(
                        credentials_service.secret_for_resource(
                            row["organization_id"], "channels", config.id
                        )
                    )
                except Exception:
                    secrets[config.id] = None
        return configs, secrets, True

    try:
        while True:
            adapters = []
            channel_configs, organization_secrets, organization_mode = configured_channels()
            config_map = {item.id: item for item in channel_configs}
            for channel_config in channel_configs:
                if not channel_config.enabled:
                    runtime.channel_statuses.set(
                        channel_config.id, channel_config.type, "disabled"
                    )
                    continue
                try:
                    credentials = (
                        organization_secrets.get(channel_config.id)
                        if organization_mode
                        else credential_store.load(
                            channel_config.id, channel_config.type
                        )
                    )
                    if (
                        credentials is None
                        and not organization_mode
                        and channel_config.type == "wechat_ilink"
                    ):
                        credential_path = channel_credentials_path(channel_config.id)
                        print("正在登录消息渠道 {}。".format(channel_config.id))
                        with ILinkClient() as ilink:
                            ilink_credentials = ilink.login(
                                display_qr_code, status_changed=print_login_status
                            )
                        save_credentials(ilink_credentials, credential_path)
                        credentials = credential_store.load(
                            channel_config.id, channel_config.type, required=True
                        )
                    if credentials is None:
                        raise ChannelCredentialError(
                            "渠道 {} 尚未配置凭据".format(channel_config.id)
                        )
                    adapter = build_channel_adapter(
                        channel_config,
                        credentials,
                        token_resolver=runtime.address_store.latest_context_token,
                    )
                except Exception as exc:
                    runtime.channel_statuses.set(
                        channel_config.id,
                        channel_config.type,
                        "authentication_required"
                        if isinstance(exc, ChannelCredentialError)
                        else "failed",
                        str(exc),
                    )
                    print(str(exc), file=sys.stderr)
                    continue
                adapters.append(adapter)
                runtime.channel_statuses.set(
                    channel_config.id, channel_config.type, "starting"
                )

            runtime.message_router.reset(adapters)
            message_bot = MessageBot(
                runtime.agent_service,
                runtime.message_router,
                tenant_registry=tenant_registry,
                recipient_store=runtime.services.recipient_store,
                conversation_store=runtime.services.conversation_store,
                schedule_store=runtime.services.schedule_store,
                schedule_ids=[
                    task.id for task in project_config.schedules if task.enabled
                ],
                script_service=runtime.script_service,
                knowledge_service=runtime.services.knowledge_service,
                memory_service=runtime.memory_service,
                integration_service=runtime.integration_service,
                notification_dispatcher=runtime.notification_dispatcher,
                address_store=runtime.address_store,
                channel_configs=config_map,
            )
            channel_manager = ChannelManager(
                adapters,
                MessageInboxStore(tenant_registry),
                message_bot.handle_inbound,
                status_registry=runtime.channel_statuses,
            )
            channel_manager.start()
            active = "、".join(adapter.channel_id for adapter in adapters) or "无"
            print("消息服务已启动：渠道={}，正在等待消息。".format(active))
            controls = runtime.services.organization_control_store
            applied_revisions = controls.runtime_revisions() if controls else {}
            while True:
                threading.Event().wait(1.0)
                revisions = controls.runtime_revisions() if controls else {}
                if revisions != applied_revisions:
                    print("检测到组织渠道配置变化，正在应用。")
                    channel_manager.shutdown()
                    channel_manager = None
                    break
    except KeyboardInterrupt:
        print("\n机器人已停止。")
        return 0
    except (ILinkError, ModelError, OSError, TenantStoreError) as exc:
        print("启动失败：{}".format(exc), file=sys.stderr)
        return 1
    finally:
        if channel_manager is not None:
            channel_manager.shutdown()
        else:
            runtime.message_router.close()
    return 0


def run_bot(args, project_config=None) -> int:
    _install_sigterm_handler()
    if project_config is None:
        try:
            project_config = load_project_config(CONFIG_DIR)
        except ConfigError as exc:
            print("配置加载失败：{}".format(exc), file=sys.stderr)
            return 1

    if args.logout:
        delete_credentials()
        print("已清除微信登录凭证。")

    try:
        services = build_core_services(
            project_config,
            DATA_DIR,
            model_call_logger=log_model_call,
            fallback_logger=log_model_fallback,
        )
    except TenantStoreError as exc:
        print("租户数据加载失败：{}".format(exc), file=sys.stderr)
        return 1
    except (ModelError, ValueError) as exc:
        print("模型客户端创建失败：{}".format(exc), file=sys.stderr)
        return 1
    try:
        project_config = services.project_config
        runtime = build_bot_runtime(project_config, services)
    except (ModelError, ValueError) as exc:
        services.close()
        print("模型客户端创建失败：{}".format(exc), file=sys.stderr)
        return 1
    model = services.model_router
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
        runtime.start()
        return run_channel_loop(runtime, project_config)
    except KeyboardInterrupt:
        print("\n机器人已停止。")
        return 0
    except (ILinkError, ModelError, OSError, TenantStoreError) as exc:
        print("启动失败：{}".format(exc), file=sys.stderr)
        return 1
    finally:
        runtime.shutdown()
        services.close()
