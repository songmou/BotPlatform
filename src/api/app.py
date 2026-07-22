"""FastAPI application factory for the web management panel."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.api.auth import TokenAuthMiddleware, load_or_create_token
from src.api.routers import agents, chat, models, system

API_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = API_DIR / "templates"
STATIC_DIR = API_DIR / "static"


def create_app(config, model_router, registry, conversation_store,
               tool_runtime=None, knowledge_service=None) -> FastAPI:
    app = FastAPI(title="BotPlatform Web", docs_url=None, redoc_url=None)

    app.state.config = config
    app.state.model_router = model_router
    app.state.registry = registry
    app.state.conversation_store = conversation_store
    app.state.tool_runtime = tool_runtime
    app.state.knowledge_service = knowledge_service
    app.state.web_token = load_or_create_token()

    app.add_middleware(TokenAuthMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    app.include_router(system.router)
    app.include_router(models.router)
    app.include_router(agents.router)
    app.include_router(chat.router)

    return app
