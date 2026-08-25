"""Per-tenant organization environment variables and resolution views.

Scripts and plugins declare the environment variable *names* they need
(``env_allowlist``). The actual values are supplied either by the platform
(global ``scripts.env``) or per organization (this store). This module is the
only place where organization values are written; the script/plugin popups
only display the resolved bindings.

Values are intentionally never returned in plaintext; the management UI sends
new values via ``PUT`` and reads masked values via ``GET``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.api.deps import (
    get_env_resolver,
    get_registry,
    get_settings_store,
    require_permission,
)
from src.core.plugins.registry import default_catalog
from src.core.services.env_resolver import validate_env_name
from src.core.storage.tenants import TenantStoreError


router = APIRouter(prefix="/api/tenants", tags=["tenant-env"])


def _tenant(request: Request, tenant_id: str):
    try:
        return get_registry(request).get(tenant_id)
    except TenantStoreError as exc:
        raise HTTPException(status_code=404, detail="租户不存在") from exc


def _allowlist_for(
    request: Request, script_id: Optional[str], plugin_id: Optional[str]
) -> List[str]:
    if script_id:
        service = request.app.state.script_service
        if service is None:
            raise HTTPException(status_code=503, detail="脚本服务不可用")
        definition = service.definitions.get(script_id)
        if definition is None:
            raise HTTPException(status_code=404, detail="脚本不存在")
        return list(definition.env_allowlist)
    if plugin_id:
        manifest = default_catalog().get(plugin_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="插件不存在")
        return list(manifest.env_allowlist)
    return []


@router.get("/{tenant_id}/env")
def list_tenant_env(
    tenant_id: str,
    request: Request,
    principal=Depends(require_permission("tenants.manage")),
):
    _tenant(request, tenant_id)
    store = get_settings_store(request)
    values = store.env(tenant_id)
    return {
        "variables": [
            {"name": name, "masked": _mask(values[name])}
            for name in sorted(values)
        ]
    }


@router.put("/{tenant_id}/env/{name}", status_code=200)
def set_tenant_env(
    tenant_id: str,
    name: str,
    body: Dict[str, Any],
    request: Request,
    principal=Depends(require_permission("tenants.manage")),
):
    _tenant(request, tenant_id)
    try:
        var_name = validate_env_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "value" not in body or not isinstance(body["value"], str):
        raise HTTPException(status_code=400, detail="value 必须是字符串")
    store = get_settings_store(request)
    values = store.env(tenant_id)
    values[var_name] = body["value"]
    store.set_env(tenant_id, values)
    return {"name": var_name, "masked": _mask(body["value"]), "defined": True}


@router.delete("/{tenant_id}/env/{name}", status_code=200)
def delete_tenant_env(
    tenant_id: str,
    name: str,
    request: Request,
    principal=Depends(require_permission("tenants.manage")),
):
    _tenant(request, tenant_id)
    try:
        var_name = validate_env_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store = get_settings_store(request)
    values = store.env(tenant_id)
    if var_name not in values:
        raise HTTPException(status_code=404, detail="环境变量不存在")
    values.pop(var_name, None)
    store.set_env(tenant_id, values)
    return {"name": var_name, "deleted": True}


@router.get("/{tenant_id}/env/resolve")
def resolve_env(
    tenant_id: str,
    request: Request,
    script_id: Optional[str] = Query(None),
    plugin_id: Optional[str] = Query(None),
    principal=Depends(require_permission("tenants.manage")),
):
    _tenant(request, tenant_id)
    names = _allowlist_for(request, script_id, plugin_id)
    resolver = get_env_resolver(request)
    return {"names": names, "bindings": resolver.describe(tenant_id, names)}


@router.get("/env/global/resolve")
def resolve_global_env(
    request: Request,
    script_id: Optional[str] = Query(None),
    plugin_id: Optional[str] = Query(None),
    principal=Depends(require_permission("tenants.manage")),
):
    names = _allowlist_for(request, script_id, plugin_id)
    resolver = get_env_resolver(request)
    return {"names": names, "bindings": resolver.global_describe(names)}


def _mask(value: str) -> str:
    from src.core.services.env_resolver import mask

    return mask(value)
