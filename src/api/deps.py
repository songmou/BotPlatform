"""Dependency injection for API route handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from src.core.config.loader import ProjectConfig
    from src.core.modeling.router import ModelRouter
    from src.core.services.auth import AdminAuthService, AuthPrincipal
    from src.core.storage.admin_users import AdminRoleStore, AdminUserStore
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


def get_plugin_manager(request: Request):
    return getattr(request.app.state, "plugin_manager", None)


def get_scheduler(request: Request):
    return getattr(request.app.state, "scheduler", None)


def get_tool_audit_store(request: Request):
    return getattr(request.app.state, "tool_audit_store", None)


def get_mcp_call_log_store(request: Request):
    return getattr(request.app.state, "mcp_call_log_store", None)


def get_model_analytics_store(request: Request):
    return getattr(request.app.state, "model_analytics_store", None)


def get_script_service(request: Request):
    return getattr(request.app.state, "script_service", None)


def get_script_registry(request: Request):
    return getattr(request.app.state, "script_registry", None)


def get_settings_store(request: Request):
    store = getattr(request.app.state, "settings_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="租户设置服务不可用")
    return store


def get_env_resolver(request: Request):
    resolver = getattr(request.app.state, "env_resolver", None)
    if resolver is None:
        raise HTTPException(status_code=503, detail="环境变量解析服务不可用")
    return resolver


def get_drive_service(request: Request):
    return getattr(request.app.state, "drive_service", None)


def get_drive_audit_store(request: Request):
    return getattr(request.app.state, "drive_audit_store", None)


def get_organization_store(request: Request):
    service = getattr(request.app.state, "organization_store", None)
    if service is None:
        raise HTTPException(status_code=503, detail="组织服务不可用")
    return service


def get_resource_store(request: Request):
    service = getattr(request.app.state, "resource_store", None)
    if service is None:
        raise HTTPException(status_code=503, detail="资源目录服务不可用")
    return service


def get_organization_control_store(request: Request):
    service = getattr(request.app.state, "organization_control_store", None)
    if service is None:
        raise HTTPException(status_code=503, detail="组织运行配置服务不可用")
    return service


def get_credential_service(request: Request):
    service = getattr(request.app.state, "credential_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="凭据服务不可用")
    return service


def get_admin_auth(request: Request) -> "AdminAuthService":
    return request.app.state.admin_auth


def get_admin_user_store(request: Request) -> "AdminUserStore":
    return request.app.state.admin_user_store


def get_admin_role_store(request: Request) -> "AdminRoleStore":
    return request.app.state.admin_role_store


def get_principal(request: Request) -> "AuthPrincipal":
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="未登录")
    return principal


def require_permission(permission: str):
    def dependency(request: Request) -> "AuthPrincipal":
        principal = get_principal(request)
        if not principal.allows(permission):
            raise HTTPException(status_code=403, detail="没有权限执行该操作")
        return principal

    return dependency
