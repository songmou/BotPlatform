"""Versioned platform catalog and organization tenant APIs."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from src.core.integrations.wecom_verify import (
    WeComVerifyError,
    verify_wecom_credentials,
)
from src.core.services.wechat_login import WeChatLoginManager

from src.api.deps import (
    get_admin_role_store,
    get_admin_user_store,
    get_credential_service,
    get_model_analytics_store,
    get_organization_store,
    get_organization_control_store,
    get_drive_audit_store,
    get_principal,
    get_plugin_manager,
    get_resource_store,
    require_permission,
)
from src.api.routers import chat as chat_api
from src.api.schemas import ChatHistoryItem, ChatHistoryResponse, ChatRequest
from src.api.sse import sse_error, streaming_response
from src.core.services.authorization import AuthorizationError, AuthorizationService
from src.core.services.credentials import CredentialError
from src.core.services.organization_controls import OrganizationControlError
from src.core.services.resources import ResourceError
from src.core.storage.organizations import OrganizationError
from src.core.messaging.providers import (
    build_channel_adapter,
    channel_provider,
    list_channel_providers,
)
from src.core.messaging import OutboundMessage
from src.core.messaging.errors import MessagingError
from src.core.config.loader import ChannelConfig
from src.core.services.drive import MAX_PREVIEW_BYTES, MAX_UPLOAD_BYTES
from src.core.plugins.registry import default_catalog
from src.core.tooling.definitions import DATASOURCE_TOOLS, TOOL_DEFINITIONS


router = APIRouter(prefix="/api/v2", tags=["v2"])


def _organization_context(
    request: Request,
    principal,
    organization_id: str,
    minimum_role: Optional[str] = None,
):
    service = AuthorizationService(get_organization_store(request))
    try:
        return service.organization_context(
            principal,
            organization_id,
            minimum_role=minimum_role,
            request_id=request.headers.get("x-request-id", ""),
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _resource_error(exc: Exception) -> HTTPException:
    text = str(exc)
    status = 404 if "不存在" in text else 400
    return HTTPException(status_code=status, detail=text)


def _organization_analytics_store(request: Request):
    store = get_model_analytics_store(request)
    if store is None:
        raise HTTPException(status_code=503, detail="模型分析服务未启用")
    return store


def _record_content_owner(
    request: Request,
    organization_id: str,
    resource_type: str,
    resource_key: str,
    user_id: int,
) -> None:
    with get_organization_store(request).database.transaction(
        immediate=True
    ) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO organization_content_ownership("
            "organization_id, resource_type, resource_key, creator_user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                organization_id,
                resource_type,
                resource_key,
                user_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def _require_content_manager(
    request: Request,
    context,
    resource_type: str,
    resource_key: str,
) -> None:
    if context.role in {"owner", "admin"} or context.platform_delegation:
        return
    with get_organization_store(request).database.read() as connection:
        row = connection.execute(
            "SELECT creator_user_id FROM organization_content_ownership "
            "WHERE organization_id=? AND resource_type=? AND resource_key=?",
            (context.organization_id, resource_type, resource_key),
        ).fetchone()
    # Content without an ownership record (created before tracking shipped)
    # is restricted to organization admins to keep members from deleting
    # uploads they did not create.
    if row is None or row["creator_user_id"] != context.user_id:
        raise HTTPException(status_code=403, detail="只有内容创建者或组织管理员可以执行该操作")


def _move_content_ownership(
    request: Request,
    organization_id: str,
    resource_type: str,
    old_key: str,
    new_key: Optional[str],
) -> None:
    with get_organization_store(request).database.transaction(
        immediate=True
    ) as connection:
        if new_key is None:
            connection.execute(
                "DELETE FROM organization_content_ownership WHERE organization_id=? "
                "AND resource_type=? AND (resource_key=? OR resource_key LIKE ?)",
                (organization_id, resource_type, old_key, old_key.rstrip("/") + "/%"),
            )
            return
        rows = connection.execute(
            "SELECT resource_key, creator_user_id, created_at "
            "FROM organization_content_ownership WHERE organization_id=? "
            "AND resource_type=? AND (resource_key=? OR resource_key LIKE ?)",
            (organization_id, resource_type, old_key, old_key.rstrip("/") + "/%"),
        ).fetchall()
        connection.execute(
            "DELETE FROM organization_content_ownership WHERE organization_id=? "
            "AND resource_type=? AND (resource_key=? OR resource_key LIKE ?)",
            (organization_id, resource_type, old_key, old_key.rstrip("/") + "/%"),
        )
        for row in rows:
            suffix = str(row["resource_key"])[len(old_key):]
            connection.execute(
                "INSERT OR REPLACE INTO organization_content_ownership("
                "organization_id, resource_type, resource_key, creator_user_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    organization_id,
                    resource_type,
                    new_key + suffix,
                    row["creator_user_id"],
                    row["created_at"],
                ),
            )


def _record_organization_drive(
    request: Request,
    principal,
    organization_id: str,
    action: str,
    path: str,
    *,
    target_path: Optional[str] = None,
    size_bytes: int = 0,
) -> None:
    store = get_drive_audit_store(request)
    if store is None:
        return
    store.record(
        operator="web:{}".format(principal.user.username),
        source="web",
        scope="tenant",
        tenant_id=organization_id,
        action=action,
        path=path,
        target_path=target_path,
        size_bytes=size_bytes,
        status="成功",
        error=None,
    )


def _analytics_range(
    date_from: Optional[datetime], date_to: Optional[datetime]
) -> tuple[str, str]:
    end = date_to or datetime.now(timezone.utc)
    start = date_from or (end - timedelta(days=7))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if end <= start:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")
    if end - start > timedelta(days=366):
        raise HTTPException(status_code=400, detail="单次查询时间范围不能超过 366 天")
    return start.isoformat(), end.isoformat()


@router.get("/me")
def current_user(request: Request, principal=Depends(get_principal)):
    organizations = get_organization_store(request)
    organizations.ensure_user(principal.user.user_id, principal.user.username)
    platform_allowed = principal.allows("admins.manage")
    memberships = organizations.list_for_user(principal.user.user_id)
    if platform_allowed:
        memberships = [
            {
                **item,
                "role": "platform_delegation",
                "membership_status": "delegated",
            }
            for item in organizations.list_organizations()
        ]
    for item in memberships:
        role = str(item.get("role") or "")
        item["permissions"] = {
            "collaborate": role in {
                "owner", "admin", "member", "platform_delegation"
            },
            "manage_sensitive": role in {
                "owner", "admin", "platform_delegation"
            },
            "delete_organization": role in {"owner", "platform_delegation"},
        }
    return {
        "user": {
            "user_id": principal.user.user_id,
            "username": principal.user.username,
            "platform_role": principal.role.code,
            "platform_permissions": principal.permissions,
        },
        "organizations": memberships,
    }


@router.post("/invitations/accept")
def accept_invitation(request: Request, body: Dict[str, Any] = Body(...)):
    try:
        return get_organization_store(request).accept_invitation(
            str(body.get("token") or ""),
            str(body.get("username") or ""),
            str(body.get("password") or ""),
            get_admin_user_store(request),
            get_admin_role_store(request),
        )
    except OrganizationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runtime-catalog")
def runtime_catalog(
    request: Request, _principal=Depends(get_principal)
):
    return {
        "timezone": request.app.state.config.app.timezone,
        "channel_providers": [
            {
                "type": provider.type,
                "name": provider.name,
                "credential_fields": list(provider.credential_fields),
            }
            for provider in list_channel_providers()
        ],
        "schedule_actions": ["text", "agent_prompt", "script", "plugin"],
    }


@router.get("/catalog/{resource_type}")
def list_catalog(
    resource_type: str,
    request: Request,
    _principal=Depends(get_principal),
):
    try:
        items = get_resource_store(request).list_public(resource_type)
        return {"items": [_safe_catalog_item(resource_type, item) for item in items]}
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.get("/platform/catalog/{resource_type}")
def list_platform_catalog(
    resource_type: str,
    request: Request,
    _principal=Depends(require_permission("panel.read")),
):
    try:
        # Platform CRUD is immediate.  Do not leak retained implementation
        # revisions or draft pointers into the management UI.
        return {"items": get_resource_store(request).list_public(resource_type)}
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.get("/catalog/{resource_type}/{resource_id}")
def get_catalog_resource(
    resource_type: str,
    resource_id: str,
    request: Request,
    _principal=Depends(get_principal),
):
    try:
        return _safe_catalog_item(
            resource_type,
            get_resource_store(request).get_public(resource_type, resource_id),
        )
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.get("/platform/catalog/{resource_type}/{resource_id}")
def get_platform_catalog_resource(
    resource_type: str,
    resource_id: str,
    request: Request,
    _principal=Depends(require_permission("panel.read")),
):
    try:
        return get_resource_store(request).get_public(resource_type, resource_id)
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.get("/platform/organizations")
def list_platform_organizations(
    request: Request,
    _principal=Depends(require_permission("tenants.read")),
):
    return {"items": get_organization_store(request).list_organizations()}


@router.get("/platform/organizations/{organization_id}/members")
def list_platform_organization_members(
    organization_id: str,
    request: Request,
    _principal=Depends(require_permission("admins.manage")),
):
    try:
        store = get_organization_store(request)
        with store.database.read() as connection:
            identities = connection.execute(
                "SELECT identity_id, channel_id, platform, account_id, "
                "external_user_id, created_at, last_seen_at "
                "FROM channel_identities WHERE tenant_id=? "
                "ORDER BY last_seen_at DESC",
                (organization_id,),
            ).fetchall()
        return {
            "items": store.list_members(organization_id),
            "identities": [dict(row) for row in identities],
        }
    except OrganizationError as exc:
        raise _resource_error(exc) from exc


@router.get("/platform/audit")
def list_platform_audit(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    _principal=Depends(require_permission("admins.manage")),
):
    with get_organization_store(request).database.read() as connection:
        rows = connection.execute(
            "SELECT * FROM security_audit_log "
            "ORDER BY audit_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@router.get("/platform/analytics/overview")
def platform_analytics_overview(
    request: Request,
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    profile_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
    _principal=Depends(require_permission("admins.manage")),
):
    start, end = _analytics_range(date_from, date_to)
    return _organization_analytics_store(request).overview(
        date_from=start,
        date_to=end,
        tenant_id=None,
        profile_id=profile_id,
        agent_id=agent_id,
        source=source,
        status=status,
    )


@router.post("/platform/organizations", status_code=201)
def create_platform_organization(
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(require_permission("admins.manage")),
):
    try:
        organization, invitation_token = get_organization_store(request).create(
            str(body.get("name") or ""),
            principal.user.user_id,
        )
    except OrganizationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_resource_store(request).ensure_organization_agents(
        str(organization["organization_id"]),
        request.app.state.config.app.default_agent,
    )
    return {
        "organization": organization,
        "owner_invitation_token": invitation_token,
    }


@router.put("/platform/organizations/{organization_id}")
def update_platform_organization(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    _principal=Depends(require_permission("admins.manage")),
):
    try:
        store = get_organization_store(request)
        if "status" in body:
            return store.set_status(
                organization_id, str(body.get("status") or "")
            )
        return store.update(organization_id, str(body.get("name") or ""))
    except OrganizationError as exc:
        raise _resource_error(exc) from exc


@router.delete("/orgs/{organization_id}")
def delete_organization(
    organization_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    if not principal.allows("admins.manage"):
        _organization_context(request, principal, organization_id, "owner")
    try:
        organization_store = get_organization_store(request)
        backup_path = organization_store.backup_organization(organization_id)
        get_credential_service(request).delete_all(organization_id)
        organization_store.delete_after_backup(organization_id)
    except (CredentialError, OrganizationError, OSError) as exc:
        raise HTTPException(
            status_code=400,
            detail="组织删除失败，未完成删除：{}".format(exc),
        ) from exc
    return {
        "deleted": True,
        "backup_id": backup_path.name,
    }


@router.post("/platform/organizations/{organization_id}/claim-token")
def issue_claim_token(
    organization_id: str,
    request: Request,
    _principal=Depends(require_permission("admins.manage")),
):
    try:
        token = get_organization_store(request).issue_legacy_claim(organization_id)
    except OrganizationError as exc:
        raise _resource_error(exc) from exc
    return {"claim_token": token, "expires_in_seconds": 3600}


@router.put("/platform/catalog/{resource_type}/{resource_id}")
def upsert_public_resource(
    resource_type: str,
    resource_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(require_permission("panel.write")),
):
    try:
        return get_resource_store(request).upsert_public(
            resource_type,
            resource_id,
            dict(body.get("payload") or body),
            principal.user.user_id,
        )
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.delete("/platform/catalog/{resource_type}/{resource_id}")
def delete_public_resource(
    resource_type: str,
    resource_id: str,
    request: Request,
    _principal=Depends(require_permission("panel.write")),
):
    try:
        get_resource_store(request).delete_public(resource_type, resource_id)
    except ResourceError as exc:
        raise _resource_error(exc) from exc
    return {"deleted": True}


@router.put("/platform/catalog/{resource_type}/{resource_id}/draft")
def save_platform_resource_draft(
    resource_type: str,
    resource_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(require_permission("panel.write")),
):
    raise HTTPException(status_code=410, detail="草稿功能已移除，请直接保存配置")


@router.post("/platform/catalog/{resource_type}/{resource_id}/publish")
def publish_platform_resource(
    resource_type: str,
    resource_id: str,
    request: Request,
    body: Dict[str, Any] = Body(default={}),
    principal=Depends(require_permission("panel.write")),
):
    raise HTTPException(status_code=410, detail="发布功能已移除，请直接保存配置")


@router.post(
    "/platform/catalog/{resource_type}/{resource_id}/rollback/{revision}"
)
def rollback_platform_resource(
    resource_type: str,
    resource_id: str,
    revision: int,
    request: Request,
    principal=Depends(require_permission("panel.write")),
):
    raise HTTPException(status_code=410, detail="回滚功能已移除，请直接保存配置")


@router.get("/platform/catalog/{resource_type}/{resource_id}/activation")
def platform_resource_activation(
    resource_type: str,
    resource_id: str,
    request: Request,
    _principal=Depends(require_permission("panel.read")),
):
    try:
        return get_resource_store(request).activation(resource_type, resource_id)
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.get("/orgs/{organization_id}/members")
def list_organization_members(
    organization_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    return {
        "items": get_organization_store(request).list_members(organization_id)
    }


@router.get("/orgs/{organization_id}/credentials")
def list_organization_credentials(
    organization_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    items = get_credential_service(request).list_for_user(
        organization_id,
        context.user_id,
        allow_platform_delegation=context.platform_delegation,
    )
    if context.role == "member" and not context.platform_delegation:
        items = [item for item in items if item.get("scope") == "personal"]
    return {"items": items}


@router.get("/orgs/{organization_id}/audit")
def list_organization_audit(
    organization_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    with get_organization_store(request).database.read() as connection:
        rows = connection.execute(
            "SELECT * FROM security_audit_log WHERE organization_id=? "
            "ORDER BY audit_id DESC LIMIT ?",
            (organization_id, limit),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@router.get("/orgs/{organization_id}/analytics/overview")
def organization_analytics_overview(
    organization_id: str,
    request: Request,
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    profile_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    start, end = _analytics_range(date_from, date_to)
    return _organization_analytics_store(request).overview(
        date_from=start,
        date_to=end,
        tenant_id=organization_id,
        profile_id=profile_id,
        agent_id=agent_id,
        source=source,
        status=status,
    )


@router.get("/orgs/{organization_id}/analytics/runs")
def list_organization_model_runs(
    organization_id: str,
    request: Request,
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    profile_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    start, end = _analytics_range(date_from, date_to)
    store = _organization_analytics_store(request)
    return {
        "currency": store.currency,
        "items": store.list_runs(
            limit=limit,
            offset=offset,
            date_from=start,
            date_to=end,
            tenant_id=organization_id,
            profile_id=profile_id,
            agent_id=agent_id,
            source=source,
            status=status,
        ),
        "limit": limit,
        "offset": offset,
    }


@router.get("/orgs/{organization_id}/analytics/runs/{run_id}")
def organization_model_run_detail(
    organization_id: str,
    run_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    result = _organization_analytics_store(request).run_detail(run_id)
    if result is None or str(result.get("tenant_id") or "") != organization_id:
        raise HTTPException(status_code=404, detail="模型运行记录不存在")
    return result


@router.get("/orgs/{organization_id}/analytics/tools")
def list_organization_tool_audit(
    organization_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    with get_organization_store(request).database.read() as connection:
        rows = connection.execute(
            "SELECT id, ts, tenant_id AS organization_id, user_id, session_id, "
            "agent_id, tool_name, status, duration_ms, output_bytes "
            "FROM tool_audit_log WHERE tenant_id=? "
            "ORDER BY ts DESC LIMIT ? OFFSET ?",
            (organization_id, limit, offset),
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "limit": limit,
        "offset": offset,
    }


@router.get("/orgs/{organization_id}/analytics/budget")
def get_organization_model_budget(
    organization_id: str,
    request: Request,
    period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    store = _organization_analytics_store(request)
    item = next(
        (
            value
            for value in store.list_budgets(period)
            if value["scope_type"] == "tenant"
            and value["scope_id"] == organization_id
        ),
        None,
    )
    return {"currency": store.currency, "budget": item}


@router.put("/orgs/{organization_id}/analytics/budget")
def put_organization_model_budget(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id, "admin")
    store = _organization_analytics_store(request)
    existing = next(
        (
            value
            for value in store.list_budgets()
            if value["scope_type"] == "tenant"
            and value["scope_id"] == organization_id
        ),
        None,
    )
    try:
        limit = int(body.get("monthly_limit_micros"))
        return store.save_budget(
            budget_id=(
                int(existing["budget_id"]) if existing is not None else None
            ),
            scope_type="tenant",
            scope_id=organization_id,
            monthly_limit_micros=limit,
            enabled=bool(body.get("enabled", True)),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/orgs/{organization_id}/credentials/{credential_id}")
def put_organization_credential(
    organization_id: str,
    credential_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    scope = str(body.get("scope") or "personal")
    resource_type = str(body.get("resource_type") or "")
    resource_id = str(body.get("resource_id") or "")
    try:
        if scope == "organization":
            raise CredentialError("组织凭据只能通过消息渠道页面配置")
        if scope != "personal" or resource_type != "integrations":
            raise CredentialError("个人凭据只能用于个人业务集成")
        return get_credential_service(request).put(
            organization_id,
            credential_id,
            actor_user_id=context.user_id,
            scope=scope,
            resource_type=resource_type,
            resource_id=resource_id,
            label=str(body.get("label") or ""),
            secret=body.get("secret"),
            allow_platform_delegation=context.platform_delegation,
        )
    except (CredentialError, ResourceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/orgs/{organization_id}/credentials/{credential_id}")
def delete_organization_credential(
    organization_id: str,
    credential_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    try:
        get_credential_service(request).delete(
            organization_id,
            credential_id,
            context.user_id,
            allow_platform_delegation=context.platform_delegation,
        )
    except CredentialError as exc:
        status = 404 if "不存在" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return {"deleted": True}


@router.post("/orgs/{organization_id}/invitations", status_code=201)
def create_organization_invitation(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id, "admin")
    role = str(body.get("role") or "member")
    if role == "owner":
        raise HTTPException(status_code=400, detail="请使用所有权转移功能")
    try:
        token = get_organization_store(request).create_invitation(
            organization_id,
            role,
            principal.user.user_id,
        )
    except OrganizationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"invitation_token": token, "expires_in_seconds": 72 * 3600}


@router.get("/orgs/{organization_id}/conversations")
def list_organization_conversations(
    organization_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    items = get_organization_store(request).list_conversations(
        context.user_id,
        organization_id,
        allow_delegation=context.platform_delegation,
    )
    # Channel-bound conversations are stored for audit, not shown in the
    # chat page; only web conversations are managed there.
    return [item for item in items if str(item.get("source") or "") != "channel"]


@router.post("/orgs/{organization_id}/conversations", status_code=201)
def create_organization_conversation(
    organization_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    return get_organization_store(request).create_conversation(
        context.user_id,
        organization_id,
        allow_delegation=context.platform_delegation,
    )


@router.delete(
    "/orgs/{organization_id}/conversations/{conversation_id}"
)
def delete_organization_conversation(
    organization_id: str,
    conversation_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    try:
        conversation = get_organization_store(request).get_conversation(
            context.user_id,
            conversation_id,
            allow_delegation=context.platform_delegation,
        )
        if str(conversation["organization_id"]) != organization_id:
            raise OrganizationError("对话不存在")
        get_organization_store(request).delete_conversation(
            context.user_id,
            conversation_id,
            allow_manage=context.role in {"owner", "admin"},
            allow_delegation=context.platform_delegation,
        )
    except OrganizationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": True}


@router.patch("/orgs/{organization_id}/conversations/{conversation_id}")
def update_organization_conversation(
    organization_id: str,
    conversation_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    try:
        conversation = get_organization_store(request).get_conversation(
            context.user_id,
            conversation_id,
            allow_delegation=context.platform_delegation,
        )
        if str(conversation["organization_id"]) != organization_id:
            raise OrganizationError("对话不存在")
        return get_organization_store(request).update_conversation(
            context.user_id,
            conversation_id,
            title=(str(body["title"]) if "title" in body else None),
            status=(str(body["status"]) if "status" in body else None),
            allow_manage=context.role in {"owner", "admin"},
            allow_delegation=context.platform_delegation,
        )
    except OrganizationError as exc:
        status = 404 if "不存在" in str(exc) else 403
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get(
    "/orgs/{organization_id}/conversations/{conversation_id}/history",
    response_model=ChatHistoryResponse,
)
def organization_conversation_history(
    organization_id: str,
    conversation_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    try:
        conversation = get_organization_store(request).get_conversation(
            context.user_id,
            conversation_id,
            allow_delegation=context.platform_delegation,
        )
    except OrganizationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if str(conversation["organization_id"]) != organization_id:
        raise HTTPException(status_code=404, detail="对话不存在")
    store = request.app.state.conversation_store
    messages = store.load_context(
        organization_id,
        session_key="organization:{}".format(conversation_id),
    )
    return ChatHistoryResponse(
        messages=[
            ChatHistoryItem(role=item.role, content=item.content)
            for item in messages
        ]
    )


@router.post(
    "/orgs/{organization_id}/conversations/{conversation_id}/chat"
)
def chat_in_organization_conversation(
    organization_id: str,
    conversation_id: str,
    request: Request,
    body: ChatRequest,
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    try:
        conversation = get_organization_store(request).get_conversation(
            context.user_id,
            conversation_id,
            allow_delegation=context.platform_delegation,
        )
    except OrganizationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if str(conversation["organization_id"]) != organization_id:
        raise HTTPException(status_code=404, detail="对话不存在")
    normalized = body.model_copy(update={"conversation_id": conversation_id})
    response = chat_api.chat(normalized, request, principal)
    if conversation.get("source") != "channel":
        return response

    async def relay_channel_response():
        async for chunk in response.body_iterator:
            raw = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            payload = None
            if raw.startswith("data: "):
                try:
                    payload = json.loads(raw[6:].strip())
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = None
            if isinstance(payload, dict) and payload.get("type") == "done":
                notification = getattr(
                    request.app.state, "notification_service", None
                )
                address_store = getattr(notification, "address_store", None)
                router = getattr(notification, "message_router", None)
                if notification is None or address_store is None or router is None:
                    yield sse_error("消息渠道运行进程不在线，回复未发送到原渠道")
                    continue
                endpoint = address_store.latest_endpoint_for_identity(
                    str(conversation.get("external_participant_ref") or ""),
                    str(conversation.get("channel_instance_id") or ""),
                )
                if endpoint is None:
                    yield sse_error("原渠道收件地址已失效，回复未发送")
                    continue
                try:
                    router.send(
                        endpoint,
                        OutboundMessage(text=str(payload.get("full_text") or "")),
                    )
                except Exception as exc:
                    yield sse_error("回复原渠道失败：{}".format(exc))
                    continue
            yield raw

    return streaming_response(relay_channel_response())


@router.put("/orgs/{organization_id}/members/{member_user_id}")
def update_organization_member(
    organization_id: str,
    member_user_id: int,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id, "admin")
    try:
        return get_organization_store(request).update_member_role(
            organization_id,
            member_user_id,
            str(body.get("role") or ""),
        )
    except OrganizationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/orgs/{organization_id}/ownership")
def transfer_organization_ownership(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id, "owner")
    try:
        new_owner_user_id = int(body.get("new_owner_user_id"))
        if context.platform_delegation:
            store = get_organization_store(request)
            store.membership(new_owner_user_id, organization_id)
            with store.database.read() as connection:
                existing_owner = connection.execute(
                    "SELECT user_id FROM organization_memberships "
                    "WHERE organization_id=? AND role='owner' AND status='active' "
                    "AND user_id IS NOT NULL LIMIT 1",
                    (organization_id,),
                ).fetchone()
            if existing_owner is None:
                with store.database.transaction(immediate=True) as connection:
                    connection.execute(
                        "UPDATE organization_memberships SET role='owner', updated_at=? "
                        "WHERE organization_id=? AND user_id=?",
                        (
                            datetime.now(timezone.utc).isoformat(),
                            organization_id,
                            new_owner_user_id,
                        ),
                    )
                return store.membership(new_owner_user_id, organization_id)
            return store.transfer_ownership(
                organization_id,
                int(existing_owner["user_id"]),
                new_owner_user_id,
            )
        return get_organization_store(request).transfer_ownership(
            organization_id, context.user_id, new_owner_user_id
        )
    except (OrganizationError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/orgs/{organization_id}/members/{member_user_id}")
def remove_organization_member(
    organization_id: str,
    member_user_id: int,
    request: Request,
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    if context.user_id != member_user_id:
        _organization_context(request, principal, organization_id, "admin")
    try:
        membership = get_organization_store(request).membership(
            member_user_id, organization_id
        )
        if membership["role"] == "owner":
            raise OrganizationError("组织所有者必须先转移所有权")
        get_credential_service(request).delete_personal(
            organization_id, member_user_id
        )
        controls = get_organization_control_store(request)
        with get_organization_store(request).database.read() as connection:
            rows = connection.execute(
                "SELECT c.channel_id FROM organization_channels c "
                "JOIN personal_channel_connections p "
                "ON p.channel_instance_id = c.channel_instance_id "
                "WHERE c.organization_id=? AND p.user_id=?",
                (organization_id, member_user_id),
            ).fetchall()
        for row in rows:
            try:
                controls.set_channel_enabled(
                    organization_id, str(row["channel_id"]), False, context.user_id
                )
            except OrganizationControlError:
                pass
        get_organization_store(request).remove_member(
            organization_id, member_user_id
        )
    except (CredentialError, OrganizationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"removed": True}


_FOUNDATION_TYPES = {"models", "tools", "skills", "plugins", "mcp", "scripts"}

_CAPABILITY_SAFE_FIELDS = {
    "agents": {
        "id", "name", "role", "description", "system_prompt", "capabilities", "tools",
        "plugin_tools", "skills", "mcp_servers", "datasources", "model", "greeting",
        "greeting_hints", "enabled", "temperature", "max_tokens",
    },
    "models": {
        "id", "provider", "model", "modality", "capabilities", "enabled",
        "billing_currency", "pricing", "dimensions", "type",
    },
    "tools": {"enabled"},
    "skills": {"id", "name", "description", "prompt", "enabled", "version"},
    "plugins": {
        "id", "name", "description", "enabled", "version", "credential_fields"
    },
    "mcp": {
        "id", "name", "description", "enabled", "transport", "version",
        "credential_fields",
    },
    "scripts": {
        "id", "name", "description", "enabled", "requires_approval", "sha256",
        "parameters", "artifact_types", "runtime", "version",
    },
    "channels": {"id", "name", "description", "type", "enabled"},
    "schedules": {"id", "name", "description", "enabled"},
}

_SENSITIVE_CATALOG_PARTS = {
    "secret", "token", "password", "api_key", "apikey", "headers",
    "path", "directory", "entrypoint", "command", "environment",
}


def _redact_catalog_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_catalog_value(item)
            for key, item in value.items()
            if not any(
                part in str(key).lower().replace("-", "_")
                for part in _SENSITIVE_CATALOG_PARTS
            )
        }
    if isinstance(value, list):
        return [_redact_catalog_value(item) for item in value]
    return value


def _safe_catalog_item(resource_type: str, item: Dict[str, Any]) -> Dict[str, Any]:
    allowed = _CAPABILITY_SAFE_FIELDS.get(resource_type, set())
    payload = {
        key: value
        for key, value in dict(item.get("payload") or {}).items()
        if key in allowed
    }
    return {
        "resource_type": str(item.get("resource_type") or resource_type),
        "resource_id": str(item.get("resource_id") or ""),
        "scope": "public",
        "revision": int(item.get("revision") or 1),
        "status": str(item.get("status") or "published"),
        "payload": _redact_catalog_value(payload),
    }


def _default_agent_id(request: Request, organization_id: str) -> str:
    with get_organization_store(request).database.read() as connection:
        row = connection.execute(
            "SELECT default_agent_id FROM organization_agent_settings "
            "WHERE organization_id=?",
            (organization_id,),
        ).fetchone()
    if row is not None and row["default_agent_id"]:
        return str(row["default_agent_id"])
    effective = get_resource_store(request).effective_agent_presets(organization_id)
    configured = request.app.state.config.app.default_agent
    if configured in effective:
        return configured
    return next(iter(effective), "")


def _typed_agents_with_disabled_public(
    request: Request, organization_id: str
) -> list[Dict[str, Any]]:
    return get_resource_store(request).list_organization_agents(organization_id)


def _safe_agent_template(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return the editable, non-secret portion of a public agent template."""
    payload = dict(item.get("payload") or {})
    safe = {
        key: value
        for key, value in payload.items()
        if key in _CAPABILITY_SAFE_FIELDS["agents"]
    }
    safe["id"] = str(item.get("resource_id") or safe.get("id") or "")
    return {
        "id": safe["id"],
        "revision": int(item.get("revision") or 1),
        "payload": _redact_catalog_value(safe),
    }


def _plugin_catalog(request: Request):
    manager = get_plugin_manager(request)
    catalog = getattr(manager, "catalog", None)
    return catalog or default_catalog()


def _plugin_option(request: Request, plugin_id: str, plugin) -> Dict[str, Any]:
    tools = []
    for name, definition in plugin.tools.items():
        tools.append({
            "name": str(name),
            "description": str(definition.description),
            "parameters": _redact_catalog_value(dict(definition.parameters)),
            "requires_approval": bool(definition.requires_approval),
        })
    return {
        "id": plugin_id,
        "name": str(plugin.name),
        "description": str(plugin.description),
        "version": str(plugin.version),
        "tools": tools,
    }


def _enabled_plugin_options(request: Request) -> list[Dict[str, Any]]:
    config_plugins = getattr(request.app.state.config, "plugins", {})
    catalog = _plugin_catalog(request)
    result = []
    for plugin_id, config in sorted(config_plugins.items()):
        if not bool(getattr(config, "enabled", False)):
            continue
        manifest = catalog.get(str(plugin_id))
        if manifest is None or not manifest.tools:
            continue
        result.append(_plugin_option(request, str(plugin_id), manifest))
    return result


def _knowledge_options(request: Request, organization_id: str) -> list[Dict[str, Any]]:
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        return []
    try:
        return [
            {
                "id": str(item["category_id"]),
                "name": str(item.get("name") or item["category_id"]),
                "description": str(item.get("description") or ""),
            }
            for item in service.list_categories(tenant_id=organization_id)
        ]
    except (KeyError, TypeError, ValueError):
        return []


@router.get("/orgs/{organization_id}/agent-editor-options")
def organization_agent_editor_options(
    organization_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    resources = get_resource_store(request)
    public = {
        resource_type: resources.list_public(resource_type)
        for resource_type in ("models", "skills", "mcp")
    }
    builtin = []
    for name, definition in TOOL_DEFINITIONS.items():
        # db_* tools are granted exclusively through the 数据源 tab; never let
        # them be picked à la carte from the built-in tool list.
        if name in DATASOURCE_TOOLS:
            continue
        builtin.append({
            "name": str(name),
            "description": str(definition.get("description") or ""),
        })
    config = getattr(request.app.state, "config", None)
    datasource_options = []
    for entry in (getattr(config, "datasources", None) or []):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        if not entry.get("enabled", True):
            continue
        tables = entry.get("tables") or []
        datasource_options.append({
            "id": str(entry.get("id")),
            "name": str(entry.get("name") or entry.get("id")),
            "engine": str(entry.get("engine") or ""),
            "database": str(entry.get("database") or ""),
            "description": str(entry.get("description") or ""),
            "read_only": bool(entry.get("read_only", True)),
            "table_count": len(tables) if isinstance(tables, list) else 0,
        })
    return {
        "templates": [
            _safe_agent_template(item) for item in resources.list_public("agents")
        ],
        "models": [
            _safe_catalog_item("models", item) for item in public["models"]
            if bool((item.get("payload") or {}).get("enabled", True))
        ],
        "builtin_tools": builtin,
        "datasources": datasource_options,
        "plugins": _enabled_plugin_options(request),
        "skills": [
            _safe_catalog_item("skills", item) for item in public["skills"]
            if bool((item.get("payload") or {}).get("enabled", True))
        ],
        "mcp": [
            _safe_catalog_item("mcp", item) for item in public["mcp"]
            if bool((item.get("payload") or {}).get("enabled", True))
        ],
        "knowledge": _knowledge_options(request, organization_id),
    }


@router.get("/orgs/{organization_id}/schedule-editor-options")
def organization_schedule_editor_options(
    organization_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    resources = get_resource_store(request)
    scripts = []
    for item in resources.list_public("scripts"):
        payload = dict(item.get("payload") or {})
        script_id = str(item.get("resource_id") or payload.get("id") or "")
        definition = getattr(request.app.state.config, "scripts", {}).get(script_id)
        if definition is None or not definition.enabled or definition.requires_approval:
            continue
        scripts.append({
            "id": script_id,
            "name": str(payload.get("name") or script_id),
            "description": str(payload.get("description") or ""),
            "parameters": _redact_catalog_value(payload.get("parameters") or {}),
            "revision": int(item.get("revision") or 1),
        })
    agents = []
    for item in resources.list_organization_agents(organization_id):
        payload = item.get("payload") or {}
        if bool(payload.get("enabled", True)):
            agents.append({
                "id": str(item.get("resource_id") or ""),
                "name": str(payload.get("name") or item.get("resource_id") or ""),
                "description": str(payload.get("description") or ""),
            })
    return {
        "timezone": request.app.state.config.app.timezone,
        "agents": agents,
        "scripts": scripts,
        "plugins": _enabled_plugin_options(request),
    }


@router.get("/orgs/{organization_id}/capabilities/{resource_type}")
def list_organization_capabilities(
    organization_id: str,
    resource_type: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    if resource_type not in _FOUNDATION_TYPES:
        raise HTTPException(status_code=404, detail="能力类型不存在")
    try:
        items = get_resource_store(request).list_public(resource_type)
    except ResourceError as exc:
        raise _resource_error(exc) from exc
    return {
        "items": [_safe_catalog_item(resource_type, item) for item in items],
        "read_only": True,
    }


@router.get("/orgs/{organization_id}/agents")
def list_typed_organization_agents(
    organization_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    try:
        items = _typed_agents_with_disabled_public(request, organization_id)
    except ResourceError as exc:
        raise _resource_error(exc) from exc
    return {
        "items": items,
        "default_agent_id": _default_agent_id(request, organization_id),
    }


@router.put("/orgs/{organization_id}/agents/{agent_id}")
def upsert_typed_organization_agent(
    organization_id: str,
    agent_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    payload = dict(body.get("payload") or body)
    payload["id"] = agent_id
    category_ids = list(dict.fromkeys(body.get("knowledge_category_ids") or []))
    try:
        if "knowledge_category_ids" in body:
            service = getattr(request.app.state, "knowledge_service", None)
            if service is None:
                raise ResourceError("知识库服务不可用")
            visible = {
                str(item["category_id"])
                for item in service.list_categories(tenant_id=organization_id)
            }
            if any(not isinstance(item, str) or item not in visible for item in category_ids):
                raise ResourceError("绑定列表包含当前组织不可见的知识库")
        result = get_resource_store(request).upsert_organization(
            organization_id,
            "agents",
            agent_id,
            payload,
            context.user_id,
            str(body.get("base_resource_id") or "") or None,
        )
        if "knowledge_category_ids" in body:
            with get_organization_store(request).database.transaction(immediate=True) as connection:
                connection.execute(
                    "DELETE FROM organization_agent_knowledge_categories "
                    "WHERE organization_id=? AND agent_id=?",
                    (organization_id, agent_id),
                )
                connection.executemany(
                    "INSERT INTO organization_agent_knowledge_categories("
                    "organization_id, agent_id, category_id, created_by, created_at) "
                    "VALUES (?, ?, ?, ?, datetime('now'))",
                    [(organization_id, agent_id, item, context.user_id) for item in category_ids],
                )
        return result
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.post("/orgs/{organization_id}/agents/{public_agent_id}/copy", status_code=201)
def copy_public_agent_to_organization(
    organization_id: str,
    public_agent_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    new_id = str(body.get("id") or "")
    try:
        return get_resource_store(request).copy_template(
            organization_id,
            public_agent_id,
            new_id,
            context.user_id,
            str(body.get("name") or ""),
        )
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.patch("/orgs/{organization_id}/agents/{agent_id}/status")
def set_typed_organization_agent_status(
    organization_id: str,
    agent_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    enabled = bool(body.get("enabled"))
    try:
        current = get_resource_store(request).get_effective(
            organization_id, "agents", agent_id
        )
        if not enabled:
            enabled_agents = get_resource_store(request).effective_agent_presets(
                organization_id
            )
            if agent_id in enabled_agents and len(enabled_agents) <= 1:
                raise ResourceError("组织必须至少保留一个已启用智能体")
            replacement_default = next(
                (item for item in enabled_agents if item != agent_id), ""
            )
        else:
            replacement_default = ""
        result = get_resource_store(request).set_organization_agent_enabled(
            organization_id, agent_id, enabled, context.user_id
        )
        if (
            not enabled
            and _default_agent_id(request, organization_id) == agent_id
            and replacement_default
        ):
            with get_organization_store(request).database.transaction(
                immediate=True
            ) as connection:
                connection.execute(
                    "INSERT INTO organization_agent_settings("
                    "organization_id, default_agent_id, updated_by, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(organization_id) DO UPDATE SET "
                    "default_agent_id=excluded.default_agent_id, "
                    "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (
                        organization_id,
                        replacement_default,
                        context.user_id,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        return result
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.put("/orgs/{organization_id}/agent-settings/default")
def set_default_organization_agent(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    agent_id = str(body.get("agent_id") or "")
    if agent_id not in get_resource_store(request).effective_agent_presets(
        organization_id
    ):
        raise HTTPException(status_code=400, detail="默认智能体不存在或已暂停")
    with get_organization_store(request).database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO organization_agent_settings("
            "organization_id, default_agent_id, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(organization_id) DO UPDATE SET "
            "default_agent_id=excluded.default_agent_id, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (organization_id, agent_id, context.user_id, datetime.now(timezone.utc).isoformat()),
        )
    return {"default_agent_id": agent_id}


@router.delete("/orgs/{organization_id}/agents/{agent_id}")
def delete_typed_organization_agent(
    organization_id: str,
    agent_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    try:
        current = get_resource_store(request).get_effective(
            organization_id, "agents", agent_id
        )
        if (
            bool(current.get("payload", {}).get("enabled", True))
            and len(
                get_resource_store(request).effective_agent_presets(
                    organization_id
                )
            )
            <= 1
        ):
            raise ResourceError("组织必须至少保留一个已启用智能体")
        if _default_agent_id(request, organization_id) == agent_id:
            raise ResourceError("不能删除组织默认智能体")
        get_resource_store(request).delete_organization(
            organization_id, "agents", agent_id
        )
        with get_organization_store(request).database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM organization_agent_knowledge_categories "
                "WHERE organization_id=? AND agent_id=?",
                (organization_id, agent_id),
            )
    except ResourceError as exc:
        raise _resource_error(exc) from exc
    return {"deleted": True}


def _personal_channel_ids(request: Request, organization_id: str) -> set:
    with get_organization_store(request).database.read() as connection:
        rows = connection.execute(
            "SELECT c.channel_id FROM organization_channels c "
            "JOIN personal_channel_connections p "
            "ON p.channel_instance_id = c.channel_instance_id "
            "WHERE c.organization_id=?",
            (organization_id,),
        ).fetchall()
    return {str(row["channel_id"]) for row in rows}


def _reject_personal_channel(request: Request, organization_id: str, channel_id: str) -> None:
    if channel_id in _personal_channel_ids(request, organization_id):
        raise HTTPException(
            status_code=400, detail="个人连接请在「我的连接」中管理"
        )


@router.get("/orgs/{organization_id}/channels")
def list_typed_organization_channels(
    organization_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    items = get_organization_control_store(request).list_channels(organization_id)
    personal = _personal_channel_ids(request, organization_id)
    items = [item for item in items if item["id"] not in personal]
    registry = getattr(request.app.state, "channel_statuses", None)
    for item in items:
        status = registry.get(item["channel_instance_id"]) if registry else None
        item["state"] = status.state if status else (
            "disabled" if not item["enabled"] else "pending_runtime"
        )
        item["detail"] = status.detail if status else ""
        item["bot_account_id"] = ""
        if item.get("credential_configured"):
            try:
                raw = json.loads(
                    get_credential_service(request).secret_for_resource(
                        organization_id,
                        "channels",
                        item["channel_instance_id"],
                    )
                )
                if isinstance(raw, dict):
                    item["bot_account_id"] = str(raw.get("bot_id") or "")
            except (CredentialError, ValueError, TypeError):
                pass
    return {
        "items": items,
        "providers": [
            {
                "type": provider.type,
                "name": provider.name,
                "credential_fields": list(provider.credential_fields),
                "secret_fields": list(provider.secret_fields),
            }
            for provider in list_channel_providers()
        ],
    }


@router.put("/orgs/{organization_id}/channels/{channel_id}")
def upsert_typed_organization_channel(
    organization_id: str,
    channel_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    _reject_personal_channel(request, organization_id, channel_id)
    try:
        return get_organization_control_store(request).upsert_channel(
            organization_id, channel_id, body, context.user_id
        )
    except OrganizationControlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/orgs/{organization_id}/channels/{channel_id}/status")
def set_typed_organization_channel_status(
    organization_id: str,
    channel_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    _reject_personal_channel(request, organization_id, channel_id)
    try:
        return get_organization_control_store(request).set_channel_enabled(
            organization_id, channel_id, bool(body.get("enabled")), context.user_id
        )
    except OrganizationControlError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/orgs/{organization_id}/channels/{channel_id}/credentials")
def put_typed_organization_channel_credentials(
    organization_id: str,
    channel_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id, "admin")
    _reject_personal_channel(request, organization_id, channel_id)
    try:
        channel = get_organization_control_store(request).get_channel(
            organization_id, channel_id
        )
        raw_credentials = dict(body.get("credentials") or {})
        if channel["type"] == "wecom_aibot":
            bot_id = str(raw_credentials.get("bot_id") or "").strip()
            secret = str(raw_credentials.get("secret") or "").strip()
            if not bot_id or not secret:
                raise CredentialError("请填写 Bot ID 和 Secret")
            current = None
            try:
                current = json.loads(
                    get_credential_service(request).secret_for_resource(
                        organization_id, "channels", channel["channel_instance_id"]
                    )
                )
            except CredentialError:
                pass
            if not (
                current
                and str(current.get("bot_id") or "") == bot_id
                and str(current.get("secret") or "") == secret
            ):
                verify_wecom_credentials(bot_id, secret)
        credentials = channel_provider(channel["type"]).validate_credentials(
            raw_credentials
        )
        return get_credential_service(request).put(
            organization_id,
            "channel:{}".format(channel_id),
            actor_user_id=context.user_id,
            scope="organization",
            resource_type="channels",
            resource_id=channel["channel_instance_id"],
            label="{} 渠道凭据".format(channel_id),
            secret=json.dumps(credentials, ensure_ascii=False),
            allow_platform_delegation=context.platform_delegation,
        )
    except WeComVerifyError as exc:
        raise HTTPException(
            status_code=400, detail="企业微信凭证校验失败：{}".format(exc)
        ) from exc
    except (OrganizationControlError, CredentialError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _organization_wechat_manager(
    request: Request,
    organization_id: str,
    channel: Dict[str, Any],
    actor_user_id: int,
) -> WeChatLoginManager:
    managers = request.app.state.wechat_login_managers
    manager_key = "org-channel:{}".format(channel["id"])
    with threading.Lock():
        manager = managers.get(manager_key)
        if manager is not None:
            return manager

        holder: Dict[str, Any] = {}

        def save(credentials: Any, _path: Any) -> None:
            # Stage credentials after a successful scan; they are persisted
            # only when the user confirms on the channel dialog.
            holder["pending"] = credentials.to_dict()

        def connected() -> bool:
            try:
                item = get_organization_control_store(request).get_channel(
                    organization_id, channel["id"]
                )
            except OrganizationControlError:
                return False
            return bool(item.get("credential_configured"))

        manager = WeChatLoginManager(
            channel_id=channel["channel_instance_id"],
            credentials_saver=save,
            connected_checker=connected,
        )
        manager.pending_holder = holder
        managers[manager_key] = manager
        return manager


@router.get("/orgs/{organization_id}/channels/{channel_id}/wechat/status")
def organization_channel_wechat_status(
    organization_id: str,
    channel_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    _reject_personal_channel(request, organization_id, channel_id)
    channel = get_organization_control_store(request).get_channel(
        organization_id, channel_id
    )
    if channel["type"] != "wechat_ilink":
        raise HTTPException(status_code=400, detail="该渠道不是微信渠道")
    manager = _organization_wechat_manager(
        request, organization_id, channel, principal.user.user_id
    )
    return manager.status()


@router.post("/orgs/{organization_id}/channels/{channel_id}/wechat/login")
def organization_channel_wechat_login(
    organization_id: str,
    channel_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    _reject_personal_channel(request, organization_id, channel_id)
    channel = get_organization_control_store(request).get_channel(
        organization_id, channel_id
    )
    if channel["type"] != "wechat_ilink":
        raise HTTPException(status_code=400, detail="该渠道不是微信渠道")
    manager = _organization_wechat_manager(
        request, organization_id, channel, principal.user.user_id
    )
    return manager.start()


@router.post("/orgs/{organization_id}/channels/{channel_id}/wechat/confirm")
def organization_channel_wechat_confirm(
    organization_id: str,
    channel_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    _reject_personal_channel(request, organization_id, channel_id)
    channel = get_organization_control_store(request).get_channel(
        organization_id, channel_id
    )
    if channel["type"] != "wechat_ilink":
        raise HTTPException(status_code=400, detail="该渠道不是微信渠道")
    manager = _organization_wechat_manager(
        request, organization_id, channel, principal.user.user_id
    )
    holder = getattr(manager, "pending_holder", None)
    pending = holder.get("pending") if holder else None
    if not pending:
        raise HTTPException(status_code=400, detail="尚未完成微信扫码")
    get_credential_service(request).put(
        organization_id,
        "channel:{}".format(channel_id),
        actor_user_id=principal.user.user_id,
        scope="organization",
        resource_type="channels",
        resource_id=channel["channel_instance_id"],
        label="{} 微信登录".format(channel_id),
        secret=json.dumps(pending, ensure_ascii=False),
        allow_platform_delegation=True,
    )
    get_organization_control_store(request).bump_channels_revision(organization_id)
    return {"ok": True}


@router.post("/orgs/{organization_id}/channels/{channel_id}/test")
def test_typed_organization_channel(
    organization_id: str,
    channel_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    store = get_organization_control_store(request)
    _reject_personal_channel(request, organization_id, channel_id)
    try:
        channel = store.get_channel(organization_id, channel_id)
        secret = get_credential_service(request).secret_for_resource(
            organization_id, "channels", channel["channel_instance_id"]
        )
        credentials = json.loads(secret)
        channel_provider(channel["type"]).validate_credentials(credentials)

        # 用存储的凭据构建一个临时 adapter，并向最近一个收件人真实发送测试消息。
        config = ChannelConfig(
            id=channel["channel_instance_id"], type=channel["type"], enabled=True
        )
        adapter = build_channel_adapter(config, credentials)
        try:
            address_store = getattr(
                getattr(request.app.state, "notification_service", None),
                "address_store",
                None,
            )
            endpoint = (
                address_store.latest_endpoint(
                    organization_id, channel_id=channel["channel_instance_id"]
                )
                if address_store is not None
                else None
            )
            if endpoint is None:
                return {
                    "ok": True,
                    "state": "credentials_valid",
                    "detail": "凭据格式有效，但尚未发现可发送的收件人（请先在该渠道与机器人私聊一次后再测试）",
                }
            adapter.send(
                endpoint,
                OutboundMessage(text="这是来自 BotPlatform 的渠道连通性测试消息。"),
            )
        finally:
            adapter.close()
    except (OrganizationControlError, CredentialError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MessagingError as exc:
        raise HTTPException(status_code=400, detail="发送测试消息失败：{}".format(exc)) from exc
    return {
        "ok": True,
        "state": "message_sent",
        "detail": "测试消息已通过 {} 发送至 {}".format(channel["type"], endpoint.recipient_id),
    }


@router.delete("/orgs/{organization_id}/channels/{channel_id}")
def delete_typed_organization_channel(
    organization_id: str,
    channel_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    _reject_personal_channel(request, organization_id, channel_id)
    try:
        instance_id = get_organization_control_store(request).delete_channel(
            organization_id, channel_id
        )
        try:
            get_credential_service(request).delete(
                organization_id,
                "channel:{}".format(channel_id),
                context.user_id,
                allow_platform_delegation=context.platform_delegation,
            )
        except CredentialError as exc:
            if "不存在" not in str(exc):
                raise
    except (OrganizationControlError, CredentialError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted": True, "channel_instance_id": instance_id}


@router.get("/orgs/{organization_id}/schedules")
def list_typed_organization_schedules(
    organization_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    return {
        "items": get_organization_control_store(request).list_schedules(
            organization_id
        ),
        "timezone": request.app.state.config.app.timezone,
    }


@router.put("/orgs/{organization_id}/schedules/{schedule_key}")
def upsert_typed_organization_schedule(
    organization_id: str,
    schedule_key: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    try:
        return get_organization_control_store(request).upsert_schedule(
            organization_id, schedule_key, body, context.user_id
        )
    except OrganizationControlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/orgs/{organization_id}/schedules/{schedule_key}/status")
def set_typed_organization_schedule_status(
    organization_id: str,
    schedule_key: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    try:
        return get_organization_control_store(request).set_schedule_enabled(
            organization_id, schedule_key, bool(body.get("enabled")), context.user_id
        )
    except OrganizationControlError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/orgs/{organization_id}/schedules/{schedule_key}")
def delete_typed_organization_schedule(
    organization_id: str,
    schedule_key: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    try:
        get_organization_control_store(request).delete_schedule(
            organization_id, schedule_key
        )
    except OrganizationControlError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": True}


@router.get("/orgs/{organization_id}/schedule-runs")
def list_typed_organization_schedule_runs(
    organization_id: str,
    request: Request,
    schedule_key: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    store = get_organization_control_store(request)
    # The organization always comes from the path; clients never pass a tenant.
    filters = {"schedule_key": schedule_key or None, "status": status or None}
    try:
        items = store.list_schedule_runs(
            organization_id, limit=limit, offset=offset, **filters
        )
        total = store.count_schedule_runs(organization_id, **filters)
    except OrganizationControlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/orgs/{organization_id}/script-runs/{run_id}")
def get_typed_organization_script_run(
    organization_id: str,
    run_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    """Expose the real script outcome behind a scheduled dispatch."""
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "script_service", None)
    registry = getattr(request.app.state, "registry", None)
    if service is None or registry is None:
        raise HTTPException(status_code=503, detail="脚本服务不可用")
    try:
        return service.get_run(registry.get(organization_id), run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/orgs/{organization_id}/schedules/{schedule_key}/run")
def run_typed_organization_schedule_now(
    organization_id: str,
    schedule_key: str,
    request: Request,
    principal=Depends(get_principal),
):
    """Trigger one organization schedule immediately for troubleshooting."""
    _organization_context(request, principal, organization_id, minimum_role="admin")
    store = get_organization_control_store(request)
    try:
        store.get_schedule(organization_id, schedule_key)
    except OrganizationControlError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="调度服务不可用")
    return {
        "ok": bool(scheduler.run_organization_schedule(organization_id, schedule_key))
    }


@router.get("/orgs/{organization_id}/drive/entries")
def list_organization_drive(
    organization_id: str,
    request: Request,
    path: str = Query(default=""),
    scope: str = Query(default="organization"),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    if scope not in {"organization", "public"}:
        raise HTTPException(status_code=400, detail="文件范围仅支持 organization 或 public")
    try:
        return service.list_entries(
            "public" if scope == "public" else "tenant",
            None if scope == "public" else organization_id,
            path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/orgs/{organization_id}/drive/folders")
def create_organization_drive_folder(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    if body.get("scope") == "public":
        raise HTTPException(status_code=403, detail="公共文件只读")
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    try:
        result = service.create_folder(
            "tenant",
            organization_id,
            str(body.get("path") or ""),
            str(body.get("name") or ""),
            exist_ok=bool(body.get("exist_ok")),
        )
        if result.get("created"):
            _record_content_owner(
                request,
                organization_id,
                "drive_entry",
                str(result["path"]),
                context.user_id,
            )
            _record_organization_drive(
                request, principal, organization_id, "mkdir", str(result["path"])
            )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/orgs/{organization_id}/drive/upload")
async def upload_organization_drive_file(
    organization_id: str,
    request: Request,
    path: str = Form(default=""),
    overwrite: bool = Form(default=False),
    scope: str = Form(default="organization"),
    file: UploadFile = File(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    if scope == "public":
        raise HTTPException(status_code=403, detail="公共文件只读")
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if not payload:
        raise HTTPException(status_code=400, detail="上传的文件内容为空")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="上传文件超过大小限制")
    try:
        expected_path = "/".join(
            value.strip("/")
            for value in (path, file.filename or "upload.bin")
            if value.strip("/")
        )
        if overwrite:
            _require_content_manager(
                request, context, "drive_entry", expected_path
            )
        result = service.save_file(
            "tenant",
            organization_id,
            path,
            file.filename or "upload.bin",
            payload,
            overwrite,
        )
        _record_content_owner(
            request,
            organization_id,
            "drive_entry",
            str(result["path"]),
            context.user_id,
        )
        _record_organization_drive(
            request,
            principal,
            organization_id,
            "upload",
            str(result["path"]),
            size_bytes=int(result.get("size") or len(payload)),
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/orgs/{organization_id}/drive/download")
def download_organization_drive_file(
    organization_id: str,
    request: Request,
    path: str = Query(min_length=1),
    scope: str = Query(default="organization"),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    if scope not in {"organization", "public"}:
        raise HTTPException(status_code=400, detail="文件范围仅支持 organization 或 public")
    storage_scope = "public" if scope == "public" else "tenant"
    tenant_id = None if scope == "public" else organization_id
    try:
        real_path = service.read_file(storage_scope, tenant_id, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if scope == "organization":
        _record_organization_drive(
            request,
            principal,
            organization_id,
            "download",
            path,
            size_bytes=real_path.stat().st_size,
        )
    return FileResponse(
        real_path,
        filename=real_path.name,
        media_type="application/octet-stream",
    )


@router.get("/orgs/{organization_id}/drive/preview")
def preview_organization_drive_file(
    organization_id: str,
    request: Request,
    path: str = Query(min_length=1),
    scope: str = Query(default="organization"),
    max_bytes: int = Query(default=MAX_PREVIEW_BYTES, ge=1),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    if scope not in {"organization", "public"}:
        raise HTTPException(status_code=400, detail="文件范围仅支持 organization 或 public")
    try:
        return service.read_text(
            "public" if scope == "public" else "tenant",
            None if scope == "public" else organization_id,
            path,
            max_bytes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/orgs/{organization_id}/drive/entries")
def move_organization_drive_entry(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    if body.get("scope") == "public":
        raise HTTPException(status_code=403, detail="公共文件只读")
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    try:
        old_path = str(body.get("path") or "")
        _require_content_manager(request, context, "drive_entry", old_path)
        if body.get("action") == "rename":
            result = service.rename(
                "tenant",
                organization_id,
                old_path,
                str(body.get("target") or ""),
            )
        elif body.get("action") == "move":
            result = service.move(
                "tenant",
                organization_id,
                old_path,
                str(body.get("target") or ""),
            )
        else:
            raise ValueError("action 仅支持 rename 或 move")
        _move_content_ownership(
            request, organization_id, "drive_entry", old_path, str(result["path"])
        )
        _record_organization_drive(
            request,
            principal,
            organization_id,
            str(body.get("action")),
            old_path,
            target_path=str(result["path"]),
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/orgs/{organization_id}/drive/entries")
def delete_organization_drive_entry(
    organization_id: str,
    request: Request,
    path: str = Query(min_length=1),
    recursive: bool = Query(default=False),
    scope: str = Query(default="organization"),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    if scope == "public":
        raise HTTPException(status_code=403, detail="公共文件只读")
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    try:
        _require_content_manager(request, context, "drive_entry", path)
        result = service.delete(
            "tenant", organization_id, path, recursive=recursive
        )
        _move_content_ownership(
            request, organization_id, "drive_entry", path, None
        )
        _record_organization_drive(
            request, principal, organization_id, "delete", path
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/orgs/{organization_id}/knowledge/categories")
def list_organization_knowledge_categories(
    organization_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="知识库服务不可用")
    return {
        "items": service.list_categories(tenant_id=organization_id),
        "embedding_enabled": service.embedding is not None,
    }


@router.get("/orgs/{organization_id}/knowledge/sources")
def list_organization_knowledge_sources(
    organization_id: str,
    request: Request,
    category_id: Optional[str] = Query(default=None),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="知识库服务不可用")
    if category_id:
        category = service.get_category(category_id)
        if (
            category["scope"] != "public"
            and str(category.get("tenant_id") or "") != organization_id
        ):
            raise HTTPException(status_code=404, detail="知识库不存在")
    return {"items": service.list(organization_id, category_id)}


def _organization_knowledge_source(service, organization_id: str, source_id: str):
    metadata = next(
        (
            item
            for item in service.list(organization_id)
            if str(item.get("source_id")) == source_id
        ),
        None,
    )
    if metadata is None:
        raise HTTPException(status_code=404, detail="知识来源不存在")
    try:
        preview = service.preview_source(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="知识来源不存在") from exc
    return {
        **metadata,
        **preview,
        "scope": metadata.get("category_scope"),
    }


@router.get("/orgs/{organization_id}/knowledge/sources/{source_id}")
def preview_organization_knowledge_source(
    organization_id: str,
    source_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="知识库服务不可用")
    return _organization_knowledge_source(service, organization_id, source_id)


@router.delete("/orgs/{organization_id}/knowledge/sources/{source_id}")
def delete_organization_knowledge_source(
    organization_id: str,
    source_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="知识库服务不可用")
    source = _organization_knowledge_source(
        service, organization_id, source_id
    )
    if source.get("scope") == "public":
        raise HTTPException(status_code=403, detail="公共知识内容只读")
    _require_content_manager(
        request, context, "knowledge_source", source_id
    )
    if not service.delete(organization_id, source_id):
        raise HTTPException(status_code=404, detail="知识来源不存在")
    _move_content_ownership(
        request, organization_id, "knowledge_source", source_id, None
    )
    return {"deleted": True}


@router.get("/orgs/{organization_id}/knowledge/search")
def search_organization_knowledge(
    organization_id: str,
    request: Request,
    q: str = Query(min_length=1),
    agent_id: Optional[str] = Query(default=None),
    category_ids: Optional[list[str]] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=20),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="知识库服务不可用")
    return {
        "results": service.search(
            organization_id,
            q,
            limit=limit,
            agent_id=agent_id,
            category_ids=category_ids,
        )
    }


@router.post("/orgs/{organization_id}/knowledge/text")
def add_organization_knowledge_text(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="知识库服务不可用")
    try:
        result = service.add_text(
            organization_id,
            str(body.get("name") or ""),
            str(body.get("content") or ""),
            (
                str(body["category_id"])
                if body.get("category_id")
                else None
            ),
        )
        _record_content_owner(
            request,
            organization_id,
            "knowledge_source",
            str(result["source_id"]),
            context.user_id,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/orgs/{organization_id}/agents/{agent_id}/knowledge-categories")
def get_organization_agent_knowledge(
    organization_id: str,
    agent_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    with get_organization_store(request).database.read() as connection:
        rows = connection.execute(
            "SELECT category_id FROM organization_agent_knowledge_categories "
            "WHERE organization_id=? AND agent_id=? ORDER BY category_id",
            (organization_id, agent_id),
        ).fetchall()
    return {"category_ids": [str(row["category_id"]) for row in rows]}


@router.put("/orgs/{organization_id}/agents/{agent_id}/knowledge-categories")
def set_organization_agent_knowledge(
    organization_id: str,
    agent_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    resources = get_resource_store(request)
    known_agent_ids = {
        str(item.get("resource_id"))
        for item in (
            list(resources.list_organization_agents(organization_id))
            + list(resources.list_public("agents"))
        )
    }
    if agent_id not in known_agent_ids:
        raise HTTPException(status_code=404, detail="智能体不存在")
    category_ids = list(dict.fromkeys(body.get("category_ids") or []))
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="知识库服务不可用")
    visible = {
        str(item["category_id"])
        for item in service.list_categories(tenant_id=organization_id)
    }
    if any(not isinstance(item, str) or item not in visible for item in category_ids):
        raise HTTPException(status_code=400, detail="绑定列表包含当前组织不可见的知识库")
    database = get_organization_store(request).database
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "DELETE FROM organization_agent_knowledge_categories "
            "WHERE organization_id=? AND agent_id=?",
            (organization_id, agent_id),
        )
        connection.executemany(
            "INSERT INTO organization_agent_knowledge_categories("
            "organization_id, agent_id, category_id, created_by, created_at"
            ") VALUES (?, ?, ?, ?, datetime('now'))",
            [
                (
                    organization_id,
                    agent_id,
                    category_id,
                    context.user_id,
                )
                for category_id in category_ids
            ],
        )
    return {"category_ids": category_ids}
