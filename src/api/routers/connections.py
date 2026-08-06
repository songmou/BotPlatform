"""Personal messaging channel connection endpoints."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from src.api.deps import get_principal
from src.core.integrations.wecom_verify import (
    WeComVerifyError,
    verify_wecom_credentials,
)
from src.core.services.connections import (
    PersonalConnectionError,
    PersonalConnectionService,
)
from src.core.services.wechat_login import WeChatLoginManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/connections", tags=["connections"])

_MANAGER_LOCK = threading.Lock()


def _service(request: Request) -> PersonalConnectionService:
    state = request.app.state
    service = getattr(state, "personal_connection_service", None)
    if service is None:
        service = PersonalConnectionService(
            state.organization_store,
            state.organization_control_store,
            state.credential_service,
        )
        state.personal_connection_service = service
    return service


def _managers(request: Request) -> Dict[str, WeChatLoginManager]:
    managers = getattr(request.app.state, "wechat_login_managers", None)
    if managers is None:
        managers = {}
        request.app.state.wechat_login_managers = managers
    return managers


def _error(exc: PersonalConnectionError) -> HTTPException:
    if str(exc) == "连接不存在":
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _runtime_state(request: Request, channel_instance_id: str) -> Dict[str, str]:
    registry = getattr(request.app.state, "channel_statuses", None)
    status = registry.get(channel_instance_id) if registry else None
    return {
        "state": status.state if status else "",
        "detail": status.detail if status else "",
    }


def _get_login_manager(
    request: Request, connection_id: str, detail: Dict[str, Any]
) -> WeChatLoginManager:
    managers = _managers(request)
    with _MANAGER_LOCK:
        manager = managers.get(connection_id)
        if manager is not None:
            return manager
        _service(request)
        organization_id = detail["organization_id"]
        channel_id = detail["channel"]["id"]
        holder: Dict[str, Any] = {}

        def save(credentials: Any, _path: Any) -> None:
            # Stage credentials after a successful scan; they are persisted
            # only when the user confirms on the connection page.
            holder["pending"] = credentials.to_dict()

        def connected() -> bool:
            try:
                channel = request.app.state.organization_control_store.get_channel(
                    organization_id, channel_id
                )
            except Exception:  # noqa: BLE001 - poll loop must not fail
                return False
            return bool(channel.get("credential_configured"))

        manager = WeChatLoginManager(
            channel_id=detail["channel_instance_id"],
            credentials_saver=save,
            connected_checker=connected,
        )
        manager.pending_holder = holder
        managers[connection_id] = manager
        return manager


@router.get("")
def list_connections(request: Request, principal=Depends(get_principal)):
    service = _service(request)
    user_id = principal.user.user_id
    managers = _managers(request)
    items = []
    for item in service.list_for_user(user_id):
        item = dict(item)
        item.update(_runtime_state(request, item["channel_instance_id"]))
        if not item.get("state"):
            item["state"] = (
                "disabled" if not item["enabled"] else "pending_runtime"
            )
        if item["platform"] == "wechat":
            manager = managers.get(item["connection_id"])
            item["login"] = manager.status() if manager else None
        items.append(item)
    return {"items": items}


@router.get("/options")
def connection_options(request: Request, principal=Depends(get_principal)):
    store = request.app.state.organization_store
    if principal.allows("admins.manage"):
        organizations = [
            {
                "organization_id": str(item["organization_id"]),
                "name": str(item.get("name") or item["organization_id"]),
                "role": "platform_admin",
                "membership_status": "active",
                "status": "active",
            }
            for item in store.list_organizations()
            if item.get("status") == "active"
        ]
    else:
        organizations = [
            {
                "organization_id": str(item["organization_id"]),
                "name": str(item.get("name") or item["organization_id"]),
                "role": str(item.get("role") or "member"),
                "membership_status": str(item.get("membership_status") or "active"),
                "status": str(item.get("status") or "active"),
            }
            for item in store.list_for_user(principal.user.user_id)
        ]
    return {
        "organizations": [
            item
            for item in organizations
            if item.get("membership_status") == "active"
            and item.get("status") == "active"
        ]
    }


@router.post("", status_code=201)
def create_connection(
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    service = _service(request)
    platform = str(body.get("platform") or "").strip()
    organization_id = str(body.get("organization_id") or "").strip()
    agent_id = str(body.get("agent_id") or "").strip()
    if not organization_id or not agent_id:
        raise HTTPException(status_code=400, detail="请选择归属组织和智能体")
    try:
        created = service.create(
            user_id=principal.user.user_id,
            organization_id=organization_id,
            platform=platform,
            agent_id=agent_id,
            allow_delegation=principal.allows("admins.manage"),
        )
    except PersonalConnectionError as exc:
        raise _error(exc) from exc
    if platform == "wecom":
        bot_id = str(body.get("bot_id") or "").strip()
        secret = str(body.get("secret") or "").strip()
        if not bot_id or not secret:
            raise HTTPException(status_code=400, detail="请填写 Bot ID 和 Secret")
        try:
            verify_wecom_credentials(bot_id, secret)
        except WeComVerifyError as exc:
            service.delete(created["connection_id"], principal.user.user_id)
            raise HTTPException(
                status_code=400, detail="企业微信凭证校验失败：{}".format(exc)
            ) from exc
        try:
            created = service.put_wecom_credentials(
                created["connection_id"],
                principal.user.user_id,
                bot_id,
                secret,
                allow_delegation=principal.allows("admins.manage"),
            )
        except PersonalConnectionError as exc:
            raise _error(exc) from exc
    return created


@router.patch("/{connection_id}/status")
def set_connection_status(
    connection_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    service = _service(request)
    try:
        return service.set_enabled(
            connection_id, principal.user.user_id, bool(body.get("enabled"))
        )
    except PersonalConnectionError as exc:
        raise _error(exc) from exc


@router.put("/{connection_id}/agent")
def set_connection_agent(
    connection_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    agent_id = str(body.get("agent_id") or "").strip()
    if not agent_id:
        raise HTTPException(status_code=400, detail="请选择智能体")
    service = _service(request)
    try:
        detail = service.change_agent(connection_id, principal.user.user_id, agent_id)
    except PersonalConnectionError as exc:
        raise _error(exc) from exc
    conversation_store = getattr(request.app.state, "conversation_store", None)
    if conversation_store is not None:
        try:
            conversation_store.clear_contexts_for_bots(
                ["organization-channel:{}".format(detail["channel_instance_id"])]
            )
        except Exception:  # noqa: BLE001 - cleanup must not fail the rebind
            logger.exception("清理连接会话上下文失败")
    return detail


@router.delete("/{connection_id}")
def delete_connection(
    connection_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    service = _service(request)
    try:
        service.delete(connection_id, principal.user.user_id)
    except PersonalConnectionError as exc:
        raise _error(exc) from exc
    with _MANAGER_LOCK:
        _managers(request).pop(connection_id, None)
    return {"deleted": True}


@router.get("/{connection_id}/wechat/status")
def wechat_status(
    connection_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    service = _service(request)
    try:
        detail = service.get(connection_id, principal.user.user_id)
    except PersonalConnectionError as exc:
        raise _error(exc) from exc
    if detail["platform"] != "wechat":
        raise HTTPException(status_code=400, detail="该连接不是微信")
    manager = _get_login_manager(request, connection_id, detail)
    return manager.status()


@router.post("/{connection_id}/wechat/login")
def wechat_login(
    connection_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    service = _service(request)
    try:
        detail = service.get(connection_id, principal.user.user_id)
    except PersonalConnectionError as exc:
        raise _error(exc) from exc
    if detail["platform"] != "wechat":
        raise HTTPException(status_code=400, detail="该连接不是微信")
    manager = _get_login_manager(request, connection_id, detail)
    return manager.start()


@router.post("/{connection_id}/wechat/confirm")
def confirm_wechat_connection(
    connection_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    service = _service(request)
    try:
        detail = service.get(connection_id, principal.user.user_id)
    except PersonalConnectionError as exc:
        raise _error(exc) from exc
    if detail["platform"] != "wechat":
        raise HTTPException(status_code=400, detail="该连接不是微信")
    manager = _get_login_manager(request, connection_id, detail)
    holder = getattr(manager, "pending_holder", None)
    pending = holder.get("pending") if holder else None
    if not pending:
        raise HTTPException(status_code=400, detail="尚未完成微信扫码")
    service.save_wechat_credentials(
        connection_id,
        pending,
        allow_delegation=principal.allows("admins.manage"),
    )
    return {"ok": True}


@router.put("/{connection_id}/wecom/credentials")
def set_wecom_credentials(
    connection_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    service = _service(request)
    try:
        row = service._require_owner(connection_id, principal.user.user_id)
    except PersonalConnectionError as exc:
        raise _error(exc) from exc
    bot_id = str(body.get("bot_id") or "").strip()
    secret = str(body.get("secret") or "").strip()
    if not bot_id or not secret:
        raise HTTPException(status_code=400, detail="请填写 Bot ID 和 Secret")
    current = service.current_wecom_secret(row)
    # A credential handshake kicks the live long connection; skip verification
    # when the saved credentials are unchanged.
    if not (current and current["bot_id"] == bot_id and current["secret"] == secret):
        try:
            verify_wecom_credentials(bot_id, secret)
        except WeComVerifyError as exc:
            raise HTTPException(
                status_code=400, detail="企业微信凭证校验失败：{}".format(exc)
            ) from exc
    try:
        return service.put_wecom_credentials(
            connection_id,
            principal.user.user_id,
            bot_id,
            secret,
            allow_delegation=principal.allows("admins.manage"),
        )
    except PersonalConnectionError as exc:
        raise _error(exc) from exc
