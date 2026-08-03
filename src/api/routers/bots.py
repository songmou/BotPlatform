"""Message channel management endpoints."""

from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.deps import require_permission
from src.core.messaging.configuration import (
    ChannelConfigurationError,
    ChannelConfigurationStore,
)
from src.core.messaging.credentials import (
    ChannelCredentialError,
    ChannelCredentialStore,
)
from src.core.messaging.providers import channel_provider, list_channel_providers
from src.core.paths import CONFIG_DIR


class ChannelUpdate(BaseModel):
    type: str
    enabled: bool = True
    agent_id: str = ""
    settings: Dict[str, object] = Field(default_factory=dict)


class ChannelCredentialsUpdate(BaseModel):
    credentials: Dict[str, str]


def _configuration_store(request: Request) -> ChannelConfigurationStore:
    config = request.app.state.config
    return ChannelConfigurationStore(
        CONFIG_DIR / "channels.json",
        config.agents,
        config.app.default_agent,
    )


def _status(request: Request, channel_id: str):
    registry = getattr(request.app.state, "channel_statuses", None)
    return registry.get(channel_id) if registry is not None else None


def _channel_out(request: Request, config) -> dict:
    credential_store = ChannelCredentialStore()
    credential_configured = credential_store.configured(config.id)
    status = _status(request, config.id)
    provider = channel_provider(config.type)
    if status is not None:
        state = status.state
        detail = status.detail
        updated_at = status.updated_at
    elif not config.enabled:
        state = "disabled"
        detail = ""
        updated_at = ""
    elif not credential_configured:
        state = "missing_credentials"
        detail = ""
        updated_at = ""
    else:
        state = "unknown"
        detail = ""
        updated_at = ""
    return {
        "id": config.id,
        "type": config.type,
        "name": provider.name,
        "enabled": config.enabled,
        "agent_id": config.agent_id
        or request.app.state.config.app.default_agent,
        "settings": dict(config.settings),
        "credential_configured": credential_configured,
        "state": state,
        "detail": detail,
        "updated_at": updated_at,
    }


def _restart_required(request: Request, configs) -> bool:
    """Flag when an enabled channel has no live runtime status yet."""
    registry = getattr(request.app.state, "channel_statuses", None)
    return any(
        config.enabled
        and (registry is None or registry.get(config.id) is None)
        for config in configs.values()
    )


channels_router = APIRouter(prefix="/api/channels", tags=["channels"])


@channels_router.get("")
def list_channels(
    request: Request,
    _principal=Depends(require_permission("channels.read")),
):
    try:
        configs = _configuration_store(request).load()
    except ChannelConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "channels": [
            _channel_out(request, config)
            for config in configs.values()
        ],
        "agents": [
            {"id": agent.id, "name": agent.name}
            for agent in request.app.state.config.agents.values()
            if agent.enabled
        ],
        "providers": [
            {
                "type": provider.type,
                "name": provider.name,
                "credential_fields": list(provider.credential_fields),
                "secret_fields": list(provider.secret_fields),
            }
            for provider in list_channel_providers()
        ],
        "restart_required": _restart_required(request, configs),
    }


@channels_router.put("/{channel_id}")
def upsert_channel(
    channel_id: str,
    body: ChannelUpdate,
    request: Request,
    _principal=Depends(require_permission("channels.manage")),
):
    try:
        config = _configuration_store(request).upsert(
            {
                "id": channel_id,
                "type": body.type,
                "enabled": body.enabled,
                "agent_id": body.agent_id,
                "settings": dict(body.settings),
            }
        )
    except ChannelConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "channel": _channel_out(request, config),
        "restart_required": True,
    }


@channels_router.delete("/{channel_id}")
def delete_channel(
    channel_id: str,
    request: Request,
    _principal=Depends(require_permission("channels.manage")),
):
    try:
        store = _configuration_store(request)
        store.remove(channel_id)
        ChannelCredentialStore().delete(channel_id)
    except (ChannelConfigurationError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted": True, "restart_required": True}


@channels_router.put("/{channel_id}/credentials")
def save_channel_credentials(
    channel_id: str,
    body: ChannelCredentialsUpdate,
    request: Request,
    _principal=Depends(require_permission("channels.manage")),
):
    try:
        config = _configuration_store(request).load().get(channel_id)
        if config is None:
            raise ChannelConfigurationError(
                "未知消息渠道：{}".format(channel_id)
            )
        ChannelCredentialStore().save(
            channel_id,
            config.type,
            body.credentials,
        )
    except (ChannelConfigurationError, ChannelCredentialError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"configured": True, "restart_required": True}


@channels_router.delete("/{channel_id}/credentials")
def delete_channel_credentials(
    channel_id: str,
    request: Request,
    _principal=Depends(require_permission("channels.manage")),
):
    try:
        config = _configuration_store(request).load().get(channel_id)
        if config is None:
            raise ChannelConfigurationError(
                "未知消息渠道：{}".format(channel_id)
            )
        ChannelCredentialStore().delete(channel_id)
    except (ChannelConfigurationError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"configured": False, "restart_required": True}


@channels_router.post("/{channel_id}/test")
def test_channel(
    channel_id: str,
    request: Request,
    _principal=Depends(require_permission("channels.manage")),
):
    try:
        config = _configuration_store(request).load().get(channel_id)
        if config is None:
            raise ChannelConfigurationError(
                "未知消息渠道：{}".format(channel_id)
            )
        credentials = ChannelCredentialStore().load(
            channel_id,
            config.type,
            required=True,
        )
        assert credentials is not None
        channel_provider(config.type).validate_credentials(credentials)
    except (ChannelConfigurationError, ChannelCredentialError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    status = _status(request, channel_id)
    if status is not None and status.state in {"connected", "running"}:
        return {"ok": True, "state": "connected", "detail": "渠道已连接"}
    return {
        "ok": True,
        "state": "credentials_valid",
        "detail": "凭据格式有效，完整连通状态将在重启后确认",
    }
