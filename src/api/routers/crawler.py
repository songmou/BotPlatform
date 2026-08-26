"""Organization-scoped crawler management and run inspection APIs."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from src.api.deps import get_organization_store, get_plugin_manager, get_principal
from src.core.plugins.base import PluginError
from src.core.services.authorization import AuthorizationError, AuthorizationService


router = APIRouter(prefix="/api/v2", tags=["crawler"])


def _context(request: Request, principal: Any, organization_id: str, minimum_role: Optional[str] = None):
    try:
        return AuthorizationService(get_organization_store(request)).organization_context(
            principal,
            organization_id,
            minimum_role=minimum_role,
            request_id=request.headers.get("x-request-id", ""),
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _plugin(request: Request):
    manager = get_plugin_manager(request)
    plugin = manager.get("web_crawler") if manager is not None else None
    if plugin is None:
        detail = "资料抓取插件未启用，请在插件中心启用并完整重启服务"
        if manager is not None and manager.errors.get("web_crawler"):
            detail = "资料抓取插件不可用：{}".format(manager.errors["web_crawler"])
        raise HTTPException(status_code=503, detail=detail)
    return plugin


def _error(exc: Exception) -> HTTPException:
    text = str(exc)
    if "不存在" in text:
        return HTTPException(status_code=404, detail=text)
    if "UNIQUE constraint" in text or "已存在" in text:
        return HTTPException(status_code=409, detail="同一组织已存在同名抓取源")
    return HTTPException(status_code=400, detail=text)


@router.get("/orgs/{organization_id}/crawl-sources")
def list_sources(
    organization_id: str, request: Request, principal=Depends(get_principal)
):
    _context(request, principal, organization_id)
    return {"items": _plugin(request).store.list_sources(organization_id)}


@router.post("/orgs/{organization_id}/crawl-sources", status_code=201)
def create_source(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id, "admin")
    try:
        return _plugin(request).store.create_source(organization_id, body, context.user_id)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/orgs/{organization_id}/crawl-sources/{source_id}")
def get_source(
    organization_id: str, source_id: str, request: Request,
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    try:
        return _plugin(request).store.get_source(organization_id, source_id)
    except PluginError as exc:
        raise _error(exc) from exc


@router.put("/orgs/{organization_id}/crawl-sources/{source_id}")
def update_source(
    organization_id: str, source_id: str, request: Request,
    body: Dict[str, Any] = Body(...), principal=Depends(get_principal),
):
    _context(request, principal, organization_id, "admin")
    try:
        return _plugin(request).store.update_source(organization_id, source_id, body)
    except Exception as exc:
        raise _error(exc) from exc


@router.delete("/orgs/{organization_id}/crawl-sources/{source_id}")
def delete_source(
    organization_id: str, source_id: str, request: Request,
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id, "admin")
    try:
        _plugin(request).delete_source(organization_id, source_id)
        return {"deleted": True}
    except PluginError as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/crawl-sources/{source_id}/runs", status_code=202)
def run_source(
    organization_id: str, source_id: str, request: Request,
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    try:
        return _plugin(request).enqueue_run(organization_id, source_id)
    except PluginError as exc:
        raise _error(exc) from exc


@router.get("/orgs/{organization_id}/crawl-runs")
def list_runs(
    organization_id: str, request: Request, source_id: str = "",
    limit: int = Query(default=100, ge=1, le=500), principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    return {"items": _plugin(request).store.list_runs(organization_id, source_id, limit)}


@router.get("/orgs/{organization_id}/crawl-runs/{run_id}")
def get_run(
    organization_id: str, run_id: str, request: Request,
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    try:
        return _plugin(request).store.get_run_detail(organization_id, run_id)
    except PluginError as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/crawl-runs/{run_id}/cancel")
def cancel_run(
    organization_id: str, run_id: str, request: Request,
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    try:
        return _plugin(request).store.cancel_run(organization_id, run_id)
    except PluginError as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/crawl-runs/{run_id}/retry", status_code=202)
def retry_run(
    organization_id: str, run_id: str, request: Request,
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    try:
        return _plugin(request).retry_run(organization_id, run_id)
    except PluginError as exc:
        raise _error(exc) from exc


@router.get("/orgs/{organization_id}/crawl-pages")
def list_pages(
    organization_id: str, request: Request, source_id: str = "",
    limit: int = Query(default=100, ge=1, le=500), principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    return {"items": _plugin(request).store.list_pages(organization_id, source_id, limit)}


@router.get("/orgs/{organization_id}/crawl-pages/{page_id}")
def get_page(
    organization_id: str, page_id: str, request: Request,
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    try:
        return _plugin(request).store.get_page(organization_id, page_id)
    except PluginError as exc:
        raise _error(exc) from exc


@router.get("/orgs/{organization_id}/crawl-pages/{page_id}/diff")
def page_diff(
    organization_id: str, page_id: str, request: Request,
    older: str = Query(min_length=1), newer: str = Query(min_length=1),
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    plugin = _plugin(request)
    plugin.store.get_page(organization_id, page_id)
    try:
        return {"diff": plugin.snapshot_diff(organization_id, older, newer)}
    except PluginError as exc:
        raise _error(exc) from exc


@router.get("/orgs/{organization_id}/crawl-records")
def query_records(
    organization_id: str, request: Request, source_id: str = "", template_name: str = "",
    limit: int = Query(default=100, ge=1, le=500), principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    return {"items": _plugin(request).store.query_records(
        organization_id, source_id, template_name, limit
    )}
