"""Model management endpoints with CRUD and switching."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request

from src.api.deps import get_config, get_router
from src.api.schemas import (
    ModelCreate,
    ModelProfileOut,
    ModelStatusOut,
    ModelSwitchRequest,
    ModelUpdate,
)
from src.core.config.loader import ModelProfile
from src.core.modeling import ModelCapabilities
from src.core.modeling.factory import create_model_client
from src.core.paths import CONFIG_DIR

router = APIRouter(prefix="/api/models", tags=["models"])

MODELS_FILE = CONFIG_DIR / "models.json"


def _profile_to_out(profile: ModelProfile, model_router) -> ModelProfileOut:
    return ModelProfileOut(
        id=profile.id,
        enabled=profile.enabled,
        type=profile.type,
        provider=profile.provider,
        model=profile.model,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
        timeout_seconds=profile.timeout_seconds,
        capabilities={
            "tools": profile.capabilities.tools,
            "vision": profile.capabilities.vision,
            "reasoning": profile.capabilities.reasoning,
        },
        is_primary=(profile.id == model_router.primary_profile_id),
        is_fallback=(profile.id == model_router.fallback_profile_id),
    )


def _load_models_json() -> dict:
    if MODELS_FILE.exists():
        return json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    return {"profiles": {}}


def _save_models_json(data: dict) -> None:
    MODELS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _make_profile(profile_id: str, data: dict) -> ModelProfile:
    caps = data.get("capabilities", {})
    return ModelProfile(
        id=profile_id,
        enabled=data.get("enabled", True),
        type=data.get("type", "openai_compatible"),
        provider=data.get("provider", ""),
        base_url=data.get("base_url", ""),
        model=data.get("model", ""),
        temperature=data.get("temperature", 0.7),
        max_tokens=data.get("max_tokens", 2048),
        timeout_seconds=data.get("timeout_seconds", 120),
        capabilities=ModelCapabilities(
            tools=caps.get("tools", False),
            vision=caps.get("vision", False),
            reasoning=caps.get("reasoning", False),
        ),
        api_key_env=data.get("api_key_env"),
        request_extra=data.get("request_extra", {}),
        assistant_passthrough_fields=data.get("assistant_passthrough_fields", []),
    )


@router.get("", response_model=list[ModelProfileOut])
def list_models(request: Request):
    config = get_config(request)
    model_router = get_router(request)
    return [_profile_to_out(p, model_router) for p in config.models.values()]


@router.get("/status", response_model=ModelStatusOut)
def model_status(request: Request):
    model_router = get_router(request)
    return ModelStatusOut(
        primary_profile_id=model_router.primary_profile_id,
        fallback_profile_id=model_router.fallback_profile_id,
        local_profile_id=model_router.local_profile_id,
        flash_profile_id=model_router.flash_profile_id,
        pro_profile_id=model_router.pro_profile_id,
        vision_profile_id=model_router.vision_profile_id,
        cooling_down=model_router.cooling_down,
        last_primary_error=model_router.last_primary_error,
    )


@router.put("/switch")
def switch_model(body: ModelSwitchRequest, request: Request):
    model_router = get_router(request)
    profile_id = body.profile_id.strip()
    if profile_id not in model_router.clients:
        raise HTTPException(status_code=404, detail="模型档案不存在或未启用")
    model_router.primary_profile_id = profile_id
    if model_router.fallback_profile_id == profile_id:
        others = [pid for pid in model_router.clients if pid != profile_id]
        if others:
            model_router.fallback_profile_id = others[0]
    return {"status": "ok", "primary_profile_id": profile_id}


@router.get("/{profile_id}", response_model=ModelProfileOut)
def get_model(profile_id: str, request: Request):
    config = get_config(request)
    model_router = get_router(request)
    profile = config.models.get(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="模型档案不存在")
    return _profile_to_out(profile, model_router)


@router.post("", response_model=ModelProfileOut, status_code=201)
def create_model(body: ModelCreate, request: Request):
    config = get_config(request)
    model_router = get_router(request)
    profile_id = body.id.strip()
    if not profile_id or not re.match(r"^[a-z][a-z0-9_]{0,63}$", profile_id):
        raise HTTPException(status_code=400, detail="ID 只能包含小写字母、数字和下划线，且以字母开头")
    if profile_id in config.models:
        raise HTTPException(status_code=409, detail="模型档案 ID 已存在")
    if not body.model.strip():
        raise HTTPException(status_code=400, detail="模型名称不能为空")

    data = {
        "enabled": body.enabled,
        "type": body.type,
        "provider": body.provider,
        "base_url": body.base_url,
        "model": body.model,
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
        "timeout_seconds": body.timeout_seconds,
        "capabilities": body.capabilities,
    }
    if body.api_key_env:
        data["api_key_env"] = body.api_key_env

    models_json = _load_models_json()
    models_json["profiles"][profile_id] = data
    _save_models_json(models_json)

    profile = _make_profile(profile_id, data)
    config.models[profile_id] = profile

    if body.enabled:
        try:
            client = create_model_client(profile)
            model_router.clients[profile_id] = client
        except Exception:
            pass

    return _profile_to_out(profile, model_router)


@router.put("/{profile_id}", response_model=ModelProfileOut)
def update_model(profile_id: str, body: ModelUpdate, request: Request):
    config = get_config(request)
    model_router = get_router(request)
    existing = config.models.get(profile_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="模型档案不存在")

    caps = body.capabilities
    if caps is None:
        caps = {
            "tools": existing.capabilities.tools,
            "vision": existing.capabilities.vision,
            "reasoning": existing.capabilities.reasoning,
        }

    data = {
        "enabled": body.enabled if body.enabled is not None else existing.enabled,
        "type": body.type or existing.type,
        "provider": body.provider if body.provider is not None else existing.provider,
        "base_url": body.base_url if body.base_url is not None else existing.base_url,
        "model": body.model if body.model is not None else existing.model,
        "temperature": body.temperature if body.temperature is not None else existing.temperature,
        "max_tokens": body.max_tokens if body.max_tokens is not None else existing.max_tokens,
        "timeout_seconds": body.timeout_seconds if body.timeout_seconds is not None else existing.timeout_seconds,
        "capabilities": caps,
    }
    api_key_env = body.api_key_env if body.api_key_env is not None else existing.api_key_env
    if api_key_env:
        data["api_key_env"] = api_key_env
    if existing.request_extra:
        data["request_extra"] = existing.request_extra
    if existing.assistant_passthrough_fields:
        data["assistant_passthrough_fields"] = existing.assistant_passthrough_fields

    models_json = _load_models_json()
    models_json["profiles"][profile_id] = data
    _save_models_json(models_json)

    profile = _make_profile(profile_id, data)
    config.models[profile_id] = profile

    model_router.clients.pop(profile_id, None)
    if profile.enabled:
        try:
            client = create_model_client(profile)
            model_router.clients[profile_id] = client
        except Exception:
            pass

    return _profile_to_out(profile, model_router)


@router.delete("/{profile_id}")
def delete_model(profile_id: str, request: Request):
    config = get_config(request)
    model_router = get_router(request)
    if profile_id not in config.models:
        raise HTTPException(status_code=404, detail="模型档案不存在")
    if profile_id == model_router.primary_profile_id:
        raise HTTPException(status_code=400, detail="不能删除当前主模型，请先切换")

    models_json = _load_models_json()
    models_json["profiles"].pop(profile_id, None)
    _save_models_json(models_json)

    config.models.pop(profile_id, None)
    client = model_router.clients.pop(profile_id, None)
    if client:
        client.close()

    return {"status": "ok"}
