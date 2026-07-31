"""Agent publish endpoints for messaging platforms."""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from src.api.deps import get_config, get_conversation_store, require_permission
from src.api.schemas import PublishAgentIn, PublishEnabledIn, WeComConfigIn
from src.core.services.publish import (
    ALL_PLATFORMS,
    PLACEHOLDER_PLATFORMS,
    PLATFORM_NAMES,
    PLATFORM_WECHAT,
    PLATFORM_WECOM,
    SUPPORTED_PLATFORMS,
    PublishError,
    PublishStore,
)
from src.core.services.wechat_login import WeChatLoginManager
from src.core.integrations.wecom_bot import WeComVerifyError, verify_wecom_credentials

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/publish", tags=["publish"])


def _get_store(request: Request) -> PublishStore:
    store = getattr(request.app.state, "publish_store", None)
    if store is None:
        store = PublishStore()
        request.app.state.publish_store = store
    return store


def _get_login_manager(request: Request) -> WeChatLoginManager:
    manager = getattr(request.app.state, "wechat_login_manager", None)
    if manager is None:
        manager = WeChatLoginManager()
        request.app.state.wechat_login_manager = manager
    return manager


def _ensure_supported(platform: str) -> None:
    if platform in PLACEHOLDER_PLATFORMS:
        raise HTTPException(status_code=400, detail="该平台暂未开放")
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=404, detail="未知的发布平台")


def _platform_bot_ids(platform: str) -> list:
    """Tenant bot_ids whose conversations belong to a publish platform.

    WeCom uses the fixed "wecom" bot_id; WeChat tenants key on the runtime
    iLink bot_id, read from the saved credentials.
    """
    if platform == PLATFORM_WECOM:
        return ["wecom"]
    if platform == PLATFORM_WECHAT:
        from src.core.application.bot import load_credentials
        from src.core.paths import channel_credentials_path

        try:
            creds = load_credentials(channel_credentials_path("wechat-main"))
        except Exception:  # noqa: BLE001 - missing/invalid credentials
            creds = None
        bot_id = getattr(creds, "bot_id", "") if creds else ""
        return [bot_id] if bot_id else []
    return []


def _clear_platform_context(request: Request, platform: str) -> None:
    """Wipe short-term context so a newly bound agent starts fresh."""
    store = get_conversation_store(request)
    if store is None:
        return
    try:
        store.clear_contexts_for_bots(_platform_bot_ids(platform))
    except Exception:  # noqa: BLE001 - clearing is best-effort
        logger.warning("清空 %s 会话上下文失败", platform, exc_info=True)


def _public_config(platform: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Never leak secrets; only report whether they are configured."""
    if platform == PLATFORM_WECOM:
        return {
            "bot_id": config.get("bot_id", ""),
            "bind_method": config.get("bind_method", "manual"),
            "configured": bool(config.get("bot_id") and config.get("secret")),
        }
    return {}


@router.get("")
def list_publish(request: Request):
    store = _get_store(request)
    config = get_config(request)
    agent_names = {aid: a.name for aid, a in config.agents.items()}
    platforms = []
    for platform in ALL_PLATFORMS:
        supported = platform in SUPPORTED_PLATFORMS
        agent = None
        if supported:
            item = store.bound_agent(platform)
            if item is not None:
                aid = item.get("agent_id")
                agent = {
                    "agent_id": aid,
                    "agent_name": agent_names.get(aid, aid),
                    "enabled": bool(item.get("enabled", True)),
                    "exists": aid in config.agents,
                }
        platforms.append(
            {
                "platform": platform,
                "name": PLATFORM_NAMES.get(platform, platform),
                "supported": supported,
                "agent": agent,
                "config": _public_config(platform, store.platform_config(platform))
                if supported
                else {},
            }
        )
    return {"platforms": platforms}


@router.put("/{platform}/agents")
def publish_agent(
    platform: str,
    body: PublishAgentIn,
    request: Request,
    principal=Depends(require_permission("publish.manage")),
):
    _ensure_supported(platform)
    config = get_config(request)
    if body.agent_id not in config.agents:
        raise HTTPException(status_code=404, detail="智能体不存在")
    store = _get_store(request)
    previous = store.bound_agent(platform)
    if platform == PLATFORM_WECOM:
        wecom_config = store.platform_config(PLATFORM_WECOM)
        if not wecom_config.get("bot_id") or not wecom_config.get("secret"):
            raise HTTPException(
                status_code=400,
                detail="请先配置并保存企业微信 Bot ID 和 Secret",
            )
    try:
        record = store.publish(platform, body.agent_id)
    except PublishError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if previous is not None and previous.get("agent_id") != body.agent_id:
        _clear_platform_context(request, platform)
    return {"status": "ok", "agent": record}


@router.put("/{platform}/agents/{agent_id}/enabled")
def set_publish_enabled(
    platform: str,
    agent_id: str,
    body: PublishEnabledIn,
    request: Request,
    principal=Depends(require_permission("publish.manage")),
):
    _ensure_supported(platform)
    if not _get_store(request).set_agent_enabled(platform, agent_id, body.enabled):
        raise HTTPException(status_code=404, detail="未找到该发布记录")
    return {"status": "ok"}


@router.delete("/{platform}/agents/{agent_id}")
def delete_publish(
    platform: str,
    agent_id: str,
    request: Request,
    principal=Depends(require_permission("publish.manage")),
):
    _ensure_supported(platform)
    if not _get_store(request).remove_agent(platform, agent_id):
        raise HTTPException(status_code=404, detail="未找到该发布记录")
    return {"status": "ok"}


@router.put("/wecom/config")
def set_wecom_config(
    body: WeComConfigIn,
    request: Request,
    principal=Depends(require_permission("publish.manage")),
):
    bot_id = body.bot_id.strip()
    secret = body.secret.strip()
    if not bot_id or not secret:
        raise HTTPException(status_code=400, detail="请填写 Bot ID 和 Secret")
    store = _get_store(request)
    existing = store.platform_config(PLATFORM_WECOM)
    # Verifying opens a second subscribe with the same Bot ID/Secret, which
    # WeCom treats as a competing connection and uses to kick the running
    # long connection. Skip it when the credentials are unchanged so that
    # re-saving does not disrupt a healthy connection.
    if bot_id == existing.get("bot_id") and secret == existing.get("secret"):
        return {"status": "ok"}
    try:
        verify_wecom_credentials(bot_id, secret)
    except WeComVerifyError as exc:
        raise HTTPException(
            status_code=400,
            detail="企业微信凭证校验失败：{}".format(exc),
        ) from exc
    store.set_platform_config(
        PLATFORM_WECOM,
        {"bot_id": bot_id, "secret": secret, "bind_method": "manual"},
    )
    return {"status": "ok"}


@router.get("/wechat/status")
def wechat_status(request: Request):
    return _get_login_manager(request).status()


@router.post("/wechat/login")
def wechat_login(
    request: Request,
    principal=Depends(require_permission("publish.manage")),
):
    # Always allow (re)starting a scan: a fresh scan re-associates the bot and
    # reclaims the connection if it was kicked by binding the account elsewhere.
    return _get_login_manager(request).start()
