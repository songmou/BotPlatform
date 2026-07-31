"""FastAPI application factory for the web management panel."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.api.auth import SecurityHeadersMiddleware, SessionAuthMiddleware
from src.api.routers import (
    admins,
    agents,
    auth,
    bots,
    chat,
    drive,
    knowledge,
    models,
    mcp,
    plugins,
    publish,
    scripts,
    schedules,
    skills,
    system,
    tenants,
    model_analytics,
)

API_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = API_DIR / "templates"
STATIC_DIR = API_DIR / "static"


def create_app(config, model_router, registry, conversation_store,
               tool_runtime=None, knowledge_service=None, plugin_context=None,
               scheduler=None, tool_audit_store=None,
               model_analytics_store=None,
               admin_auth=None, admin_user_store=None, admin_role_store=None,
               script_service=None, script_registry=None,
               script_schedule_service=None,
               drive_service=None, drive_audit_store=None,
               publish_store=None,
               secure_cookies=False, owns_services=True) -> FastAPI:
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
    app.state.scheduler = scheduler
    app.state.tool_audit_store = tool_audit_store
    app.state.model_analytics_store = model_analytics_store
    app.state.admin_auth = admin_auth
    app.state.admin_user_store = admin_user_store
    app.state.admin_role_store = admin_role_store
    app.state.script_service = script_service
    app.state.script_registry = script_registry
    app.state.script_schedule_service = script_schedule_service
    app.state.drive_service = drive_service
    app.state.drive_audit_store = drive_audit_store
    from src.core.services.publish import PublishStore

    app.state.publish_store = publish_store or PublishStore()
    app.state.secure_cookies = secure_cookies
    app.state.owns_services = owns_services
    if drive_service is not None and knowledge_service is not None:
        attach = getattr(drive_service, "attach_knowledge_service", None)
        if callable(attach):
            attach(knowledge_service)

    app.add_middleware(SessionAuthMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    app.include_router(auth.router)
    app.include_router(system.router)
    app.include_router(models.router)
    app.include_router(model_analytics.router)
    app.include_router(agents.router)
    app.include_router(chat.router)
    app.include_router(knowledge.router)
    app.include_router(drive.router)
    app.include_router(schedules.router)
    app.include_router(plugins.router)
    app.include_router(plugins.tools_router)
    app.include_router(publish.router)
    app.include_router(bots.bots_router)
    app.include_router(skills.router)
    app.include_router(scripts.router)
    app.include_router(mcp.router)
    app.include_router(tenants.router)
    app.include_router(admins.router)

    return app
