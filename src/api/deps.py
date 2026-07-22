"""Dependency injection for API route handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from src.config.loader import ProjectConfig
    from src.modeling.router import ModelRouter
    from src.storage.tenants import ConversationStore, TenantRegistry


def get_config(request: Request) -> "ProjectConfig":
    return request.app.state.config


def get_router(request: Request) -> "ModelRouter":
    return request.app.state.model_router


def get_registry(request: Request) -> "TenantRegistry":
    return request.app.state.registry


def get_conversation_store(request: Request) -> "ConversationStore":
    return request.app.state.conversation_store
