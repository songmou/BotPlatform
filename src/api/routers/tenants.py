"""Tenant (bot end-user) management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.api.deps import (
    get_credential_service,
    get_organization_store,
    get_registry,
    require_permission,
)
from src.api.schemas import TenantDetailOut, TenantOverviewOut
from src.core.services.credentials import CredentialError
from src.core.storage.organizations import OrganizationError
from src.core.storage.tenants import TenantStoreError

router = APIRouter(prefix="/api/tenants", tags=["tenants"])


def _overview(registry, tenant_id: str) -> dict:
    for item in registry.list_overviews():
        if item["tenant_id"] == tenant_id:
            return item
    raise HTTPException(status_code=404, detail="租户不存在")


@router.get("", response_model=list[TenantOverviewOut])
def list_tenants(
    request: Request, principal=Depends(require_permission("tenants.read"))
):
    registry = get_registry(request)
    return [TenantOverviewOut(**item) for item in registry.list_overviews()]


@router.get("/{tenant_id}", response_model=TenantDetailOut)
def get_tenant(
    tenant_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    principal=Depends(require_permission("tenants.read")),
):
    registry = get_registry(request)
    try:
        registry.get(tenant_id)
    except TenantStoreError:
        raise HTTPException(status_code=404, detail="租户不存在")
    overview = _overview(registry, tenant_id)
    with registry.database.read() as connection:
        subscriptions = [
            {"task_id": str(row["task_id"]), "enabled": bool(row["enabled"])}
            for row in connection.execute(
                "SELECT task_id, enabled FROM schedule_subscriptions "
                "WHERE tenant_id=? ORDER BY task_id",
                (tenant_id,),
            ).fetchall()
        ]
        integrations = [
            {
                "integration_id": str(row["integration_id"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in connection.execute(
                "SELECT integration_id, updated_at FROM integrations "
                "WHERE tenant_id=? ORDER BY integration_id",
                (tenant_id,),
            ).fetchall()
        ]
        events = [
            {
                "role": str(row["role"]),
                "content": str(row["content"])[:500],
                "event_type": str(row["event_type"]),
                "created_at": str(row["created_at"]),
            }
            for row in connection.execute(
                "SELECT role, content, event_type, created_at FROM conversation_events "
                "WHERE tenant_id=? ORDER BY event_id DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        ]
        events.reverse()
    return TenantDetailOut(
        **overview,
        schedule_subscriptions=subscriptions,
        integrations=integrations,
        recent_events=events,
    )


@router.delete("/{tenant_id}")
def delete_tenant(
    tenant_id: str,
    request: Request,
    principal=Depends(require_permission("tenants.delete")),
):
    registry = get_registry(request)
    try:
        context = registry.get(tenant_id)
        with registry.database.read() as connection:
            is_organization = connection.execute(
                "SELECT 1 FROM organizations WHERE organization_id=?",
                (tenant_id,),
            ).fetchone()
        if is_organization is None:
            registry.delete(context)
            return {"status": "ok"}
        organization_store = get_organization_store(request)
        backup_path = organization_store.backup_organization(tenant_id)
        get_credential_service(request).delete_all(tenant_id)
        organization_store.delete_after_backup(tenant_id)
    except TenantStoreError:
        raise HTTPException(status_code=404, detail="租户不存在")
    except (CredentialError, OrganizationError, OSError) as exc:
        raise HTTPException(
            status_code=400,
            detail="组织删除失败，未完成删除：{}".format(exc),
        ) from exc
    return {"status": "ok", "backup_id": backup_path.name}
