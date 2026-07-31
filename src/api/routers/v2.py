"""Versioned platform catalog and organization tenant APIs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from src.api.deps import (
    get_admin_role_store,
    get_admin_user_store,
    get_credential_service,
    get_model_analytics_store,
    get_organization_store,
    get_principal,
    get_resource_store,
    require_permission,
)
from src.api.routers import chat as chat_api
from src.api.schemas import ChatHistoryItem, ChatHistoryResponse, ChatRequest
from src.core.services.authorization import AuthorizationError, AuthorizationService
from src.core.services.credentials import CredentialError
from src.core.services.resources import ResourceError
from src.core.storage.organizations import OrganizationError


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
    memberships = organizations.list_for_user(principal.user.user_id)
    return {
        "user": {
            "user_id": principal.user.user_id,
            "username": principal.user.username,
            "platform_role": principal.role.code,
            "platform_permissions": principal.permissions,
        },
        "organizations": memberships,
        "active_organization_id": organizations.active_organization(
            principal.user.user_id
        ),
    }


@router.put("/me/active-organization")
def set_active_organization(
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    organization_id = str(body.get("organization_id") or "")
    try:
        get_organization_store(request).set_active_organization(
            principal.user.user_id, organization_id
        )
    except OrganizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"active_organization_id": organization_id}


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


@router.get("/catalog/{resource_type}")
def list_catalog(
    resource_type: str,
    request: Request,
    _principal=Depends(get_principal),
):
    try:
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
        return {
            "items": get_organization_store(request).list_members(
                organization_id
            )
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
            dict(body.get("payload") or {}),
            principal.user.user_id,
            str(body.get("status") or "published"),
        )
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
    return {
        "items": get_credential_service(request).list_for_user(
            organization_id, context.user_id
        )
    }


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
    try:
        return get_credential_service(request).put(
            organization_id,
            credential_id,
            actor_user_id=context.user_id,
            scope=str(body.get("scope") or "personal"),
            resource_type=str(body.get("resource_type") or ""),
            resource_id=str(body.get("resource_id") or ""),
            label=str(body.get("label") or ""),
            secret=body.get("secret"),
        )
    except CredentialError as exc:
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
            organization_id, credential_id, context.user_id
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
    return get_organization_store(request).list_conversations(
        context.user_id, organization_id
    )


@router.post("/orgs/{organization_id}/conversations", status_code=201)
def create_organization_conversation(
    organization_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    return get_organization_store(request).create_conversation(
        context.user_id, organization_id
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
            context.user_id, conversation_id
        )
        if str(conversation["organization_id"]) != organization_id:
            raise OrganizationError("对话不存在")
        get_organization_store(request).delete_conversation(
            context.user_id, conversation_id
        )
    except OrganizationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": True}


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
            context.user_id, conversation_id
        )
    except OrganizationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if str(conversation["organization_id"]) != organization_id:
        raise HTTPException(status_code=404, detail="对话不存在")
    store = request.app.state.conversation_store
    messages = store.load_context(
        organization_id,
        session_key="web:{}:{}".format(context.user_id, conversation_id),
        user_id=context.user_id,
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
            context.user_id, conversation_id
        )
    except OrganizationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if str(conversation["organization_id"]) != organization_id:
        raise HTTPException(status_code=404, detail="对话不存在")
    normalized = body.model_copy(update={"conversation_id": conversation_id})
    return chat_api.chat(normalized, request, principal)


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
        get_organization_store(request).remove_member(
            organization_id, member_user_id
        )
    except (CredentialError, OrganizationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"removed": True}


@router.get("/orgs/{organization_id}/resources/{resource_type}")
def list_organization_resources(
    organization_id: str,
    resource_type: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    try:
        return {
            "items": get_resource_store(request).list_effective(
                organization_id, resource_type
            )
        }
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.get("/orgs/{organization_id}/resources/{resource_type}/{resource_id}")
def get_organization_resource(
    organization_id: str,
    resource_type: str,
    resource_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    try:
        return get_resource_store(request).get_effective(
            organization_id, resource_type, resource_id
        )
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.put("/orgs/{organization_id}/resources/{resource_type}/{resource_id}")
def upsert_organization_resource(
    organization_id: str,
    resource_type: str,
    resource_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    try:
        return get_resource_store(request).upsert_organization(
            organization_id,
            resource_type,
            resource_id,
            dict(body.get("payload") or {}),
            context.user_id,
            (
                str(body["base_resource_id"])
                if body.get("base_resource_id")
                else None
            ),
        )
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.delete("/orgs/{organization_id}/resources/{resource_type}/{resource_id}")
def delete_organization_resource(
    organization_id: str,
    resource_type: str,
    resource_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    try:
        get_resource_store(request).delete_organization(
            organization_id, resource_type, resource_id
        )
    except ResourceError as exc:
        raise _resource_error(exc) from exc
    return {"deleted": True}


@router.put(
    "/orgs/{organization_id}/resources/{resource_type}/{resource_id}/override"
)
def set_organization_override(
    organization_id: str,
    resource_type: str,
    resource_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization_context(request, principal, organization_id)
    try:
        return get_resource_store(request).set_override(
            organization_id,
            resource_type,
            resource_id,
            context.user_id,
            enabled=bool(body.get("enabled", True)),
            patch=dict(body.get("patch") or {}),
            list_modes=dict(body.get("list_modes") or {}),
        )
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.delete(
    "/orgs/{organization_id}/resources/{resource_type}/{resource_id}/override"
)
def reset_organization_override(
    organization_id: str,
    resource_type: str,
    resource_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    try:
        return get_resource_store(request).reset_override(
            organization_id, resource_type, resource_id
        )
    except ResourceError as exc:
        raise _resource_error(exc) from exc


@router.get("/orgs/{organization_id}/drive/entries")
def list_organization_drive(
    organization_id: str,
    request: Request,
    path: str = Query(default=""),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    try:
        return service.list_entries("tenant", organization_id, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/orgs/{organization_id}/drive/folders")
def create_organization_drive_folder(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    try:
        return service.create_folder(
            "tenant",
            organization_id,
            str(body.get("path") or ""),
            str(body.get("name") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/orgs/{organization_id}/drive/upload")
async def upload_organization_drive_file(
    organization_id: str,
    request: Request,
    path: str = Form(default=""),
    overwrite: bool = Form(default=False),
    file: UploadFile = File(...),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    payload = await file.read()
    try:
        return service.save_file(
            "tenant",
            organization_id,
            path,
            file.filename or "upload.bin",
            payload,
            overwrite,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/orgs/{organization_id}/drive/download")
def download_organization_drive_file(
    organization_id: str,
    request: Request,
    path: str = Query(min_length=1),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    try:
        real_path = service.read_file("tenant", organization_id, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    try:
        return service.read_text("tenant", organization_id, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/orgs/{organization_id}/drive/entries")
def move_organization_drive_entry(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    try:
        if body.get("action") == "rename":
            return service.rename(
                "tenant",
                organization_id,
                str(body.get("path") or ""),
                str(body.get("target") or ""),
            )
        if body.get("action") == "move":
            return service.move(
                "tenant",
                organization_id,
                str(body.get("path") or ""),
                str(body.get("target") or ""),
            )
        raise ValueError("action 仅支持 rename 或 move")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/orgs/{organization_id}/drive/entries")
def delete_organization_drive_entry(
    organization_id: str,
    request: Request,
    path: str = Query(min_length=1),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "drive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    try:
        return service.delete("tenant", organization_id, path)
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
    return {"items": service.list_categories(tenant_id=organization_id)}


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
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="知识库服务不可用")
    source = _organization_knowledge_source(
        service, organization_id, source_id
    )
    if source.get("scope") == "public":
        raise HTTPException(status_code=403, detail="公共知识内容只读")
    if not service.delete(organization_id, source_id):
        raise HTTPException(status_code=404, detail="知识来源不存在")
    return {"deleted": True}


@router.get("/orgs/{organization_id}/knowledge/search")
def search_organization_knowledge(
    organization_id: str,
    request: Request,
    q: str = Query(min_length=1),
    agent_id: Optional[str] = Query(default=None),
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
            limit=20,
            agent_id=agent_id,
        )
    }


@router.post("/orgs/{organization_id}/knowledge/text")
def add_organization_knowledge_text(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    _organization_context(request, principal, organization_id)
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="知识库服务不可用")
    try:
        return service.add_text(
            organization_id,
            str(body.get("name") or ""),
            str(body.get("content") or ""),
            (
                str(body["category_id"])
                if body.get("category_id")
                else None
            ),
        )
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
