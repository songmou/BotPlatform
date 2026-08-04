"""FastAPI application factory for the web management panel."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.api.auth import (
    OrganizationAuditMiddleware,
    SecurityHeadersMiddleware,
    SessionAuthMiddleware,
)
from src.api.routers import (
    admins,
    agents,
    auth,
    bots,
    chat,
    content_v2,
    datasources,
    drive,
    knowledge,
    models,
    mcp,
    plugins,
    scripts,
    schedules,
    skills,
    system,
    tenants,
    model_analytics,
    v2,
    tenant_env,
)

API_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = API_DIR / "templates"
STATIC_DIR = API_DIR / "static"


def create_app(config, model_router, registry, conversation_store,
               tool_runtime=None, knowledge_service=None, plugin_context=None,
               plugin_manager=None,
               scheduler=None, tool_audit_store=None,
               model_analytics_store=None,
               admin_auth=None, admin_user_store=None, admin_role_store=None,
               script_service=None, script_registry=None,
               script_schedule_service=None,
               settings_store=None, env_resolver=None,
               drive_service=None, drive_audit_store=None,
               channel_statuses=None,
               organization_store=None, resource_store=None,
               organization_control_store=None,
               credential_service=None,
               notification_service=None,
               datasource_service=None,
               secure_cookies=False, owns_services=True) -> FastAPI:
    if organization_store is None:
        from src.core.storage.organizations import OrganizationStore

        organization_store = OrganizationStore(registry)
    unified_database = (
        admin_user_store is not None
        and getattr(getattr(organization_store, "database", None), "path", None)
        == getattr(getattr(admin_user_store, "database", None), "path", None)
    )
    if unified_database:
        organization_store.sync_users(admin_user_store)
    if resource_store is None:
        from src.core.services.resources import ScopedResourceStore

        resource_store = ScopedResourceStore(organization_store, config)
    if organization_control_store is None:
        from src.core.services.organization_controls import OrganizationControlStore

        organization_control_store = OrganizationControlStore(
            organization_store, resource_store, config
        )
    if credential_service is None:
        from src.core.integrations.keychain import KeychainService
        from src.core.services.credentials import CredentialService

        credential_root = getattr(organization_store.registry, "system_root", None)
        if not isinstance(credential_root, Path):
            credential_root = getattr(admin_auth, "system_root", None)
        if not isinstance(credential_root, Path):
            database_path = getattr(organization_store.database, "path", None)
            credential_root = (
                database_path.parent
                if isinstance(database_path, Path)
                else API_DIR
            )
        credential_service = CredentialService(
            organization_store,
            KeychainService(
                storage_path=credential_root / "organization_credentials.json"
            ),
            KeychainService(
                storage_path=credential_root / "integration_credentials.json"
            ),
        )
    if scheduler is not None:
        try:
            reload_scripts = getattr(scheduler, "reload_script_schedules", None)
            if callable(reload_scripts):
                reload_scripts()
            reload_organizations = getattr(
                scheduler, "reload_organization_schedules", None
            )
            if callable(reload_organizations):
                reload_organizations()
        except Exception:
            # Reload errors are surfaced by organization management pages;
            # startup must remain available for corrective administration.
            pass

    # When the panel shares its service graph with the bot process
    # (owns_services=False), shutdown is handled by the bot runtime and the
    # lifespan hook must not close the shared services a second time.
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if not app.state.owns_services:
            return
        if app.state.scheduler is not None:
            app.state.scheduler.shutdown()
        if app.state.script_service is not None:
            app.state.script_service.shutdown()
        if app.state.tool_runtime is not None:
            app.state.tool_runtime.close()

    app = FastAPI(
        title="BotPlatform Web",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.state.config = config
    app.state.model_router = model_router
    app.state.registry = registry
    app.state.conversation_store = conversation_store
    app.state.tool_runtime = tool_runtime
    app.state.knowledge_service = knowledge_service
    app.state.plugin_context = plugin_context
    app.state.plugin_manager = plugin_manager or getattr(
        tool_runtime, "plugin_manager", None
    )
    app.state.scheduler = scheduler
    app.state.tool_audit_store = tool_audit_store
    app.state.model_analytics_store = model_analytics_store
    app.state.admin_auth = admin_auth
    app.state.admin_user_store = admin_user_store
    app.state.admin_role_store = admin_role_store
    app.state.script_service = script_service
    app.state.script_registry = script_registry
    app.state.script_schedule_service = script_schedule_service
    app.state.settings_store = settings_store
    app.state.env_resolver = env_resolver
    app.state.drive_service = drive_service
    app.state.drive_audit_store = drive_audit_store
    app.state.datasource_service = datasource_service
    app.state.channel_statuses = channel_statuses
    app.state.organization_store = organization_store
    app.state.resource_store = resource_store
    app.state.organization_control_store = organization_control_store
    app.state.credential_service = credential_service
    app.state.notification_service = notification_service
    app.state.secure_cookies = secure_cookies
    app.state.owns_services = owns_services
    if resource_store is not None:
        def activate_platform_resource(
            resource_type, resource_id, payload, _previous
        ):
            if resource_type == "agents":
                return
            if resource_type == "skills":
                values = [
                    item["payload"]
                    for item in resource_store.list_public("skills")
                    if item["resource_id"] != resource_id
                ]
                if payload is not None:
                    values.append(payload)
                config.update_skills(values)
                return
            if resource_type == "mcp":
                from src.core.config.mcp_headers import merge_headers

                values = [
                    item["payload"]
                    for item in resource_store.list_public("mcp")
                    if item["resource_id"] != resource_id
                ]
                if payload is not None:
                    values.append(payload)
                values = merge_headers(values)
                config.update_mcp_servers(values)
                manager = getattr(tool_runtime, "mcp_manager", None)
                if manager is not None:
                    manager.reload(values)
                return
            if resource_type == "models":
                if payload is None:
                    previous = model_router.clients.pop(resource_id, None)
                    if previous is not None:
                        previous.close()
                    return
                from src.core.modeling.factory import create_model_client
                from src.core.services.resources import _model_from_payload

                profile = _model_from_payload(resource_id, payload)
                if profile.modality != "chat":
                    raise RuntimeError("向量或重排模型需要重启后生效")
                replacement = None
                if profile.enabled:
                    logger = (
                        model_analytics_store.record_model_call
                        if model_analytics_store is not None
                        else None
                    )
                    replacement = create_model_client(profile, logger=logger)
                previous = model_router.clients.get(resource_id)
                if replacement is None:
                    model_router.clients.pop(resource_id, None)
                else:
                    model_router.clients[resource_id] = replacement
                if previous is not None:
                    previous.close()
                return
            if resource_type == "tools" and tool_runtime is not None:
                from src.core.config.loader import ToolConfig

                tool_runtime.reload_config(ToolConfig(**dict(payload)))

        resource_store.set_activation_handler(activate_platform_resource)
    if drive_service is not None and knowledge_service is not None:
        attach = getattr(drive_service, "attach_knowledge_service", None)
        if callable(attach):
            attach(knowledge_service)

    app.add_middleware(SessionAuthMiddleware)
    app.add_middleware(OrganizationAuditMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    app.include_router(auth.router)
    app.include_router(system.router)
    app.include_router(content_v2.router)
    app.include_router(models.router)
    app.include_router(model_analytics.router)
    app.include_router(agents.router)
    app.include_router(chat.router)
    app.include_router(knowledge.router)
    app.include_router(drive.router)
    app.include_router(schedules.router)
    app.include_router(plugins.router)
    app.include_router(plugins.tools_router)
    app.include_router(bots.channels_router)
    app.include_router(skills.router)
    app.include_router(scripts.router)
    app.include_router(mcp.router)
    app.include_router(datasources.router)
    app.include_router(tenants.router)
    app.include_router(admins.router)
    app.include_router(tenant_env.router)
    app.include_router(v2.router)

    return app
