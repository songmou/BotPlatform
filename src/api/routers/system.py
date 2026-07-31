"""System health, status, and page-serving endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api.deps import get_config, get_router
from src.api.schemas import HealthResponse, StatusResponse

router = APIRouter(tags=["system"])


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


@router.get("/", response_class=HTMLResponse)
def page_index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(name="chat.html", request=request, context={"active": "chat"})


@router.get("/admin")
def page_admin():
    return RedirectResponse("/", status_code=302)


@router.get("/app", response_class=HTMLResponse)
def page_tenant_app(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        name="tenant_app.html",
        request=request,
        context={},
    )


@router.get("/models", response_class=HTMLResponse)
def page_models(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(name="models.html", request=request, context={"active": "models"})


@router.get("/agents", response_class=HTMLResponse)
def page_agents(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(name="agents.html", request=request, context={"active": "agents"})


@router.get("/schedules", response_class=HTMLResponse)
def page_schedules(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(name="schedules.html", request=request, context={"active": "schedules"})


@router.get("/scripts", response_class=HTMLResponse)
def page_scripts(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        name="scripts.html", request=request, context={"active": "scripts"}
    )


@router.get("/tools", response_class=HTMLResponse)
def page_tools(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(name="tools.html", request=request, context={"active": "tools"})


@router.get("/plugins", response_class=HTMLResponse)
def page_plugins(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(name="plugins.html", request=request, context={"active": "plugins"})


@router.get("/channels", response_class=HTMLResponse)
def page_channels(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        name="channels.html", request=request, context={"active": "channels"}
    )


@router.get("/users", response_class=HTMLResponse)
def page_users(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(name="users.html", request=request, context={"active": "users"})


@router.get("/docs", response_class=HTMLResponse)
def page_docs(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(name="docs.html", request=request, context={"active": "docs"})


@router.get("/knowledge", response_class=HTMLResponse)
def page_knowledge(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        name="knowledge.html", request=request, context={"active": "knowledge"}
    )


@router.get("/drive", response_class=HTMLResponse)
def page_drive(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        name="drive.html", request=request, context={"active": "drive"}
    )
