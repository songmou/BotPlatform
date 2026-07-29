"""Administrator APIs for external scripts, runs, and tenant automation."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.api.deps import (
    get_registry,
    get_script_registry,
    get_script_schedule_service,
    get_script_service,
    require_permission,
)
from src.core.storage.tenants import TenantStoreError


router = APIRouter(tags=["scripts"])


def _services(request: Request):
    registry = get_script_registry(request)
    scripts = get_script_service(request)
    schedules = get_script_schedule_service(request)
    if registry is None or scripts is None or schedules is None:
        raise HTTPException(status_code=503, detail="脚本服务不可用")
    return registry, scripts, schedules


def _refresh_schedules(schedules) -> None:
    schedules.reload_scheduler()


def _tenant(request: Request, tenant_id: str):
    try:
        return get_registry(request).get(tenant_id)
    except TenantStoreError as exc:
        raise HTTPException(status_code=404, detail="租户不存在") from exc


@router.get("/api/scripts")
def list_scripts(
    request: Request,
    principal=Depends(require_permission("scripts.read")),
):
    registry, scripts, _ = _services(request)
    return {
        "allowed_roots": registry.allowed_roots,
        "scripts": scripts.list_scripts(),
        "external_entries": registry.list_entries(),
    }


@router.put("/api/scripts/roots")
def update_script_roots(
    body: Dict[str, Any],
    request: Request,
    principal=Depends(require_permission("scripts.manage")),
):
    registry, scripts, schedules = _services(request)
    roots = body.get("allowed_roots")
    if not isinstance(roots, list):
        raise HTTPException(status_code=400, detail="allowed_roots 必须是数组")
    try:
        result = registry.configure_roots(roots)
        scripts.reload_external_definitions()
        _refresh_schedules(schedules)
        return {"allowed_roots": result}
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/scripts", status_code=201)
def create_script(
    body: Dict[str, Any],
    request: Request,
    principal=Depends(require_permission("scripts.manage")),
):
    registry, scripts, schedules = _services(request)
    try:
        definition = registry.create(body)
        scripts.reload_external_definitions()
        _refresh_schedules(schedules)
        return next(
            item for item in scripts.list_scripts() if item["id"] == definition.id
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/scripts/{script_id}")
def update_script(
    script_id: str,
    body: Dict[str, Any],
    request: Request,
    principal=Depends(require_permission("scripts.manage")),
):
    registry, scripts, schedules = _services(request)
    try:
        registry.update(script_id, body)
        scripts.reload_external_definitions()
        _refresh_schedules(schedules)
        return next(item for item in scripts.list_scripts() if item["id"] == script_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/scripts/{script_id}/trust-current")
def trust_current_script(
    script_id: str,
    request: Request,
    principal=Depends(require_permission("scripts.manage")),
):
    registry, scripts, schedules = _services(request)
    try:
        registry.trust_current(script_id)
        scripts.reload_external_definitions()
        _refresh_schedules(schedules)
        return next(item for item in scripts.list_scripts() if item["id"] == script_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/scripts/{script_id}")
def delete_script(
    script_id: str,
    request: Request,
    principal=Depends(require_permission("scripts.manage")),
):
    registry, scripts, schedules = _services(request)
    referenced = [
        item
        for item in schedules.store.list()
        if item.script_id == script_id
    ]
    if referenced:
        raise HTTPException(status_code=409, detail="脚本仍被租户定时计划引用，不能删除")
    try:
        registry.delete(script_id)
        scripts.reload_external_definitions()
        return {"status": "ok"}
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/scripts/{script_id}/runs", status_code=202)
def run_script(
    script_id: str,
    body: Dict[str, Any],
    request: Request,
    principal=Depends(require_permission("scripts.execute")),
):
    _, scripts, _ = _services(request)
    tenant_id = body.get("tenant_id")
    if not isinstance(tenant_id, str):
        raise HTTPException(status_code=400, detail="tenant_id 必须是字符串")
    tenant = _tenant(request, tenant_id)
    try:
        return scripts.submit(
            tenant,
            script_id,
            body.get("parameters", {}),
            trigger="web",
            recipient=scripts.recipient_store.load(tenant_id),
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/script-runs")
def list_script_runs(
    request: Request,
    tenant_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    principal=Depends(require_permission("scripts.read")),
):
    _, scripts, _ = _services(request)
    tenant = _tenant(request, tenant_id)
    return scripts.list_runs(tenant, limit=limit)


@router.get("/api/script-runs/{run_id}")
def get_script_run(
    run_id: str,
    request: Request,
    tenant_id: str,
    principal=Depends(require_permission("scripts.read")),
):
    _, scripts, _ = _services(request)
    tenant = _tenant(request, tenant_id)
    try:
        return scripts.get_run(tenant, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/script-runs/{run_id}/cancel")
def cancel_script_run(
    run_id: str,
    body: Dict[str, Any],
    request: Request,
    principal=Depends(require_permission("scripts.execute")),
):
    _, scripts, _ = _services(request)
    tenant_id = body.get("tenant_id")
    if not isinstance(tenant_id, str):
        raise HTTPException(status_code=400, detail="tenant_id 必须是字符串")
    tenant = _tenant(request, tenant_id)
    try:
        return scripts.cancel_run(tenant, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/tenants/{tenant_id}/script-schedules")
def list_script_schedules(
    tenant_id: str,
    request: Request,
    principal=Depends(require_permission("schedules.manage")),
):
    _, _, schedules = _services(request)
    tenant = _tenant(request, tenant_id)
    return schedules.list_for_tenant(tenant)


@router.post("/api/tenants/{tenant_id}/script-schedules", status_code=201)
def create_script_schedule(
    tenant_id: str,
    body: Dict[str, Any],
    request: Request,
    principal=Depends(require_permission("schedules.manage")),
):
    _, _, schedules = _services(request)
    tenant = _tenant(request, tenant_id)
    payload = dict(body)
    payload["action"] = "create"
    try:
        return schedules.manage(
            tenant, payload, authorized_by="web:{}".format(principal.user.username)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/tenants/{tenant_id}/script-schedules/{schedule_id}")
def update_script_schedule(
    tenant_id: str,
    schedule_id: str,
    body: Dict[str, Any],
    request: Request,
    principal=Depends(require_permission("schedules.manage")),
):
    _, _, schedules = _services(request)
    tenant = _tenant(request, tenant_id)
    payload = dict(body)
    payload["schedule_id"] = schedule_id
    payload.setdefault("action", "update")
    try:
        return schedules.manage(
            tenant, payload, authorized_by="web:{}".format(principal.user.username)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/tenants/{tenant_id}/script-schedules/{schedule_id}")
def delete_script_schedule(
    tenant_id: str,
    schedule_id: str,
    request: Request,
    principal=Depends(require_permission("schedules.manage")),
):
    _, _, schedules = _services(request)
    tenant = _tenant(request, tenant_id)
    try:
        return schedules.manage(
            tenant,
            {"action": "delete", "schedule_id": schedule_id},
            authorized_by="web:{}".format(principal.user.username),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
