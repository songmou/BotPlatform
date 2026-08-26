"""System health and explicit platform/organization page routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api.deps import get_config, get_router, require_permission
from src.api.schemas import HealthResponse, StatusResponse
from src.core.storage.organizations import OrganizationError


router = APIRouter(tags=["system"])


def _page(request: Request, name: str, active: str, **context):
    principal = getattr(request.state, "principal", None)
    platform_admin = bool(
        principal is not None and principal.allows("admins.manage")
    )
    return request.app.state.templates.TemplateResponse(
        name=name,
        request=request,
        context={
            "active": active,
            "platform_admin": platform_admin,
            **context,
        },
    )


def _validate_organization_page(request: Request) -> None:
    organization_id = str(request.query_params.get("organization_id") or "")
    if not organization_id:
        return
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        store = request.app.state.organization_store
        if principal.allows("admins.manage"):
            store.get(organization_id)
        else:
            store.membership(principal.user.user_id, organization_id)
    except OrganizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _organization_page(
    request: Request,
    module: str,
    *,
    template: str = "organization_module.html",
    active: str | None = None,
    **context,
):
    _validate_organization_page(request)
    return _page(
        request,
        template,
        active or "organization-" + module,
        organization_page=True,
        module=module,
        **context,
    )


@router.get("/api/health", response_model=HealthResponse)
def health():
    return HealthResponse()


@router.get("/api/status", response_model=StatusResponse)
def status(request: Request):
    config = get_config(request)
    model_router = get_router(request)
    return StatusResponse(
        model_ready=True,
        active_model=model_router.primary_profile_id,
        cooling_down=model_router.cooling_down,
        agents_count=len(config.agents),
        default_agent=config.app.default_agent,
    )


@router.get("/")
def page_index(request: Request):
    principal = getattr(request.state, "principal", None)
    if principal is not None and principal.allows("admins.manage"):
        return RedirectResponse("/platform", status_code=302)
    return RedirectResponse("/organization/overview", status_code=302)


@router.get("/platform", response_class=HTMLResponse)
def page_platform_overview(
    request: Request, _principal=Depends(require_permission("admins.manage"))
):
    return _page(request, "platform_overview.html", "platform-overview")


@router.get("/platform/models", response_class=HTMLResponse)
def page_platform_models(
    request: Request, _principal=Depends(require_permission("panel.read"))
):
    return _page(request, "models.html", "platform-models")


@router.get("/platform/agent-templates", response_class=HTMLResponse)
def page_platform_agents(
    request: Request, _principal=Depends(require_permission("panel.read"))
):
    return _page(request, "agents.html", "platform-agents")


@router.get("/platform/workflows", response_class=HTMLResponse)
def page_platform_workflows(
    request: Request, _principal=Depends(require_permission("panel.read"))
):
    return _page(
        request,
        "workflows.html",
        "platform-workflows",
        workflow_mode="platform",
    )


@router.get("/platform/tools", response_class=HTMLResponse)
def page_platform_tools(
    request: Request, _principal=Depends(require_permission("panel.read"))
):
    return _page(request, "tools.html", "platform-tools",
                 tools_tab="builtin")


@router.get("/platform/skills", response_class=HTMLResponse)
def page_platform_skills(
    request: Request, _principal=Depends(require_permission("panel.read"))
):
    return _page(request, "tools.html", "platform-skills",
                 tools_tab="skills")


@router.get("/platform/mcp", response_class=HTMLResponse)
def page_platform_mcp(
    request: Request, _principal=Depends(require_permission("panel.read"))
):
    return _page(request, "tools.html", "platform-mcp",
                 tools_tab="mcp")


@router.get("/platform/database", response_class=HTMLResponse)
def page_platform_database(
    request: Request, _principal=Depends(require_permission("panel.read"))
):
    return _page(request, "database.html", "platform-database")


@router.get("/platform/plugins", response_class=HTMLResponse)
def page_platform_plugins(
    request: Request, _principal=Depends(require_permission("panel.read"))
):
    return _page(request, "plugins.html", "platform-plugins")


@router.get("/platform/scripts", response_class=HTMLResponse)
def page_platform_scripts(
    request: Request, _principal=Depends(require_permission("scripts.read"))
):
    return _page(request, "scripts.html", "platform-scripts")


@router.get("/platform/knowledge", response_class=HTMLResponse)
def page_platform_knowledge(
    request: Request, _principal=Depends(require_permission("knowledge.read"))
):
    return _page(
        request,
        "knowledge.html",
        "platform-knowledge",
        resource_mode="platform-public",
    )


@router.get("/platform/drive", response_class=HTMLResponse)
def page_platform_drive(
    request: Request, _principal=Depends(require_permission("drive.read"))
):
    return _page(
        request,
        "drive.html",
        "platform-drive",
        resource_mode="platform-public",
    )


@router.get("/organization/overview", response_class=HTMLResponse)
def page_organization_overview(request: Request):
    return _organization_page(request, "overview")


@router.get("/organization/chat", response_class=HTMLResponse)
def page_organization_chat(request: Request):
    return _organization_page(
        request, "chat", template="chat.html", active="organization-chat"
    )


@router.get("/organization/agents", response_class=HTMLResponse)
def page_organization_agents(request: Request):
    return _organization_page(request, "agents")


@router.get("/organization/workflows", response_class=HTMLResponse)
def page_organization_workflows(request: Request):
    return _organization_page(
        request,
        "workflows",
        template="workflows.html",
        active="organization-workflows",
        workflow_mode="organization",
    )


@router.get("/organization/knowledge", response_class=HTMLResponse)
def page_organization_knowledge(request: Request):
    return _organization_page(
        request,
        "knowledge",
        template="knowledge.html",
        resource_mode="organization",
    )


@router.get("/organization/crawler", response_class=HTMLResponse)
def page_organization_crawler(request: Request):
    return _organization_page(
        request,
        "crawler",
        template="crawler.html",
        active="organization-crawler",
        platform_timezone=request.app.state.config.app.timezone,
    )


@router.get("/organization/drive", response_class=HTMLResponse)
def page_organization_drive(request: Request):
    return _organization_page(
        request,
        "drive",
        template="drive.html",
        resource_mode="organization",
    )


@router.get("/organization/channels", response_class=HTMLResponse)
def page_organization_channels(request: Request):
    return _organization_page(request, "channels")


@router.get("/organization/schedules", response_class=HTMLResponse)
def page_organization_schedules(request: Request):
    return _organization_page(request, "schedules")


@router.get("/organization/members", response_class=HTMLResponse)
def page_organization_members(request: Request):
    return _organization_page(request, "members")


@router.get("/organization/analytics", response_class=HTMLResponse)
def page_organization_analytics(request: Request):
    return _organization_page(
        request,
        "analytics",
        template="governance.html",
        active="organization-analytics",
    )


@router.get("/organization/audit", response_class=HTMLResponse)
def page_organization_audit(request: Request):
    return _organization_page(
        request,
        "audit",
        template="governance.html",
        active="organization-audit",
    )


@router.get("/platform/organizations", response_class=HTMLResponse)
def page_platform_organizations(
    request: Request, _principal=Depends(require_permission("admins.manage"))
):
    return _page(
        request,
        "users.html",
        "platform-organizations",
        initial_user_view="organizations",
    )


@router.get("/platform/access", response_class=HTMLResponse)
def page_platform_access(
    request: Request, _principal=Depends(require_permission("admins.manage"))
):
    return _page(
        request, "users.html", "platform-access", initial_user_view="admins"
    )


@router.get("/platform/analytics", response_class=HTMLResponse)
def page_platform_analytics(
    request: Request, _principal=Depends(require_permission("admins.manage"))
):
    return _page(
        request,
        "governance.html",
        "platform-analytics",
        module="platform-analytics",
    )


@router.get("/platform/audit", response_class=HTMLResponse)
def page_platform_audit(
    request: Request, _principal=Depends(require_permission("admins.manage"))
):
    return _page(
        request,
        "governance.html",
        "platform-audit",
        module="platform-audit",
    )


@router.get("/docs", response_class=HTMLResponse)
def page_docs(request: Request):
    return _page(request, "docs.html", "docs")
