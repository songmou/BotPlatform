"""Dependency injection for API route handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from src.core.config.loader import ProjectConfig
    from src.core.modeling.router import ModelRouter
    from src.core.storage.tenants import ConversationStore, TenantRegistry


def get_config(request: Request) -> "ProjectConfig":
    return request.app.state.config


def get_router(request: Request) -> "ModelRouter":
    return request.app.state.model_router


def get_registry(request: Request) -> "TenantRegistry":
    return request.app.state.registry


def get_conversation_store(request: Request) -> "ConversationStore":
    return request.app.state.conversation_store


def get_tool_runtime(request: Request):
    return request.app.state.tool_runtime


def get_plugin_context(request: Request):
    return request.app.state.plugin_context


def get_scheduler(request: Request):
    return getattr(request.app.state, "scheduler", None)


def get_tool_audit_store(request: Request):
    return getattr(request.app.state, "tool_audit_store", None)
