"""Model management endpoints with CRUD and switching."""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException, Request

from src.api.deps import get_config, get_model_analytics_store, get_router
from src.api.schemas import (
    ModelCreate,
    ModelProfileOut,
    ModelRoleCandidate,
    ModelRolesOut,
    ModelRolesUpdate,
    ModelStatusOut,
    ModelSwitchRequest,
    ModelUpdate,
)
from src.core.config.loader import ConfigError, ModelProfile, _load_models
from src.core.modeling.factory import create_model_client
from src.core.paths import CONFIG_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"])

MODELS_FILE = CONFIG_DIR / "models.json"
APP_FILE = CONFIG_DIR / "app.json"


def _profile_to_out(
    profile: ModelProfile, model_router, restart_required: bool = False
) -> ModelProfileOut:
    return ModelProfileOut(
        id=profile.id,
        enabled=profile.enabled,
        type=profile.type,
        provider=profile.provider,
        base_url=profile.base_url,
        api_key_env=profile.api_key_env,
        model=profile.model,
        modality=profile.modality,
        dimensions=profile.dimensions,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
        timeout_seconds=profile.timeout_seconds,
        capabilities={
            "tools": profile.capabilities.tools,
            "vision": profile.capabilities.vision,
            "reasoning": profile.capabilities.reasoning,
        },
        billing_currency=profile.billing_currency,
        pricing=(
            {
                "input_per_million": profile.pricing.input_per_million,
                "cached_input_per_million": profile.pricing.cached_input_per_million,
                "output_per_million": profile.pricing.output_per_million,
                "reasoning_output_per_million": (
                    profile.pricing.reasoning_output_per_million
                ),
            }
            if profile.pricing
            else None
        ),
        is_primary=(profile.id == model_router.primary_profile_id),
        is_fallback=(profile.id == model_router.fallback_profile_id),
        restart_required=restart_required,
    )


def _load_models_json() -> dict:
    if MODELS_FILE.exists():
        return json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    return {"profiles": {}}


def _save_models_json(data: dict) -> None:
    MODELS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _load_app_json() -> dict:
    return json.loads(APP_FILE.read_text(encoding="utf-8"))


def _save_app_json(data: dict) -> None:
    APP_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _persist_and_validate(models_json: dict, profile_id: str) -> ModelProfile:
    """Write models.json then re-run the loader; roll back on validation error."""
    backup = (
        MODELS_FILE.read_text(encoding="utf-8") if MODELS_FILE.exists() else None
    )
    _save_models_json(models_json)
    try:
        profiles = _load_models(MODELS_FILE)
    except ConfigError as exc:
        if backup is not None:
            MODELS_FILE.write_text(backup, encoding="utf-8")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return profiles[profile_id]


def _profile_data(body, existing: ModelProfile = None) -> dict:
    """Build a raw models.json profile entry honouring the modality field rules."""
    def pick(name, fallback):
        value = getattr(body, name, None)
        if value is not None:
            return value
        return fallback

    modality = pick("modality", existing.modality if existing else "chat")
    data = {
        "enabled": pick("enabled", existing.enabled if existing else True),
        "modality": modality,
        "type": pick("type", existing.type if existing else "openai_compatible"),
        "provider": pick("provider", existing.provider if existing else ""),
        "base_url": pick("base_url", existing.base_url if existing else ""),
        "model": pick("model", existing.model if existing else ""),
        "timeout_seconds": pick(
            "timeout_seconds", existing.timeout_seconds if existing else 120
        ),
    }
    api_key_env = pick("api_key_env", existing.api_key_env if existing else None)
    if api_key_env:
        data["api_key_env"] = api_key_env
    if modality == "embedding":
        dimensions = pick("dimensions", existing.dimensions if existing else None)
        if dimensions is not None:
            data["dimensions"] = dimensions
        return data
    if modality == "rerank":
        return data
    # chat modality carries generation and billing fields.
    caps = pick(
        "capabilities",
        {
            "tools": existing.capabilities.tools,
            "vision": existing.capabilities.vision,
            "reasoning": existing.capabilities.reasoning,
        }
        if existing
        else {"tools": False, "vision": False, "reasoning": False},
    )
    data["capabilities"] = caps
    data["temperature"] = pick(
        "temperature", existing.temperature if existing else 0.7
    )
    data["max_tokens"] = pick("max_tokens", existing.max_tokens if existing else 2048)
    if existing and existing.request_extra:
        data["request_extra"] = existing.request_extra
    if existing and existing.assistant_passthrough_fields:
        data["assistant_passthrough_fields"] = existing.assistant_passthrough_fields
    return data


_ROLE_FIELDS = (
    "active_model",
    "fallback_model",
    "local_model",
    "flash_model",
    "pro_model",
    "vision_model",
    "embedding_model",
    "rerank_model",
)


def _roles_referencing(app, model_router, profile_id: str) -> list:
    labels = {
        "active_model": "主模型",
        "fallback_model": "备用模型",
        "local_model": "本地模型",
        "flash_model": "快速模型",
        "pro_model": "高阶模型",
        "vision_model": "视觉模型",
        "embedding_model": "向量模型",
        "rerank_model": "重排模型",
    }
    used = []
    for field in _ROLE_FIELDS:
        if getattr(app, field, "") == profile_id:
            used.append(labels[field])
    if model_router.primary_profile_id == profile_id and "主模型" not in used:
        used.append("主模型")
    return used


def _validated_pricing(raw):
    if raw is None:
        return None
    allowed = {
        "input_per_million",
        "cached_input_per_million",
        "output_per_million",
        "reasoning_output_per_million",
    }
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise HTTPException(status_code=400, detail="模型计价字段无效")
    if raw.get("input_per_million") is None or raw.get("output_per_million") is None:
        raise HTTPException(status_code=400, detail="模型计价必须包含普通输入和普通输出价格")
    normalized = {}
    for key in allowed:
        value = raw.get(key)
        if value is None and key in {
            "cached_input_per_million",
            "reasoning_output_per_million",
        }:
            normalized[key] = None
            continue
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise HTTPException(status_code=400, detail="模型价格必须是非负十进制数")
        if not amount.is_finite() or amount < 0:
            raise HTTPException(status_code=400, detail="模型价格必须是非负十进制数")
        normalized[key] = format(amount, "f")
    return normalized


def _client_logger(request: Request):
    store = get_model_analytics_store(request)
    return store.record_model_call if store is not None else None


@router.get("", response_model=list[ModelProfileOut])
def list_models(request: Request):
    config = get_config(request)
    model_router = get_router(request)
    return [_profile_to_out(p, model_router) for p in config.models.values()]


@router.get("/status", response_model=ModelStatusOut)
def model_status(request: Request):
    config = get_config(request)
    model_router = get_router(request)
    return ModelStatusOut(
        primary_profile_id=model_router.primary_profile_id,
        fallback_profile_id=model_router.fallback_profile_id,
        local_profile_id=model_router.local_profile_id,
        flash_profile_id=model_router.flash_profile_id,
        pro_profile_id=model_router.pro_profile_id,
        vision_profile_id=model_router.vision_profile_id,
        embedding_profile_id=config.app.embedding_model or None,
        rerank_profile_id=config.app.rerank_model or None,
        cooling_down=model_router.cooling_down,
        last_primary_error=model_router.last_primary_error,
    )


def _candidates(config, predicate) -> list:
    return [
        ModelRoleCandidate(id=p.id, model=p.model, enabled=p.enabled)
        for p in config.models.values()
        if predicate(p)
    ]


@router.get("/roles", response_model=ModelRolesOut)
def get_roles(request: Request):
    config = get_config(request)
    app = config.app
    return ModelRolesOut(
        active_model=app.active_model,
        fallback_model=app.fallback_model,
        local_model=app.local_model,
        flash_model=app.flash_model,
        pro_model=app.pro_model,
        vision_model=app.vision_model,
        embedding_model=app.embedding_model,
        rerank_model=app.rerank_model,
        chat_candidates=_candidates(config, lambda p: p.modality == "chat"),
        vision_candidates=_candidates(
            config, lambda p: p.modality == "chat" and p.capabilities.vision
        ),
        embedding_candidates=_candidates(
            config, lambda p: p.modality == "embedding"
        ),
        rerank_candidates=_candidates(config, lambda p: p.modality == "rerank"),
    )


@router.put("/roles")
def update_roles(body: ModelRolesUpdate, request: Request):
    config = get_config(request)
    model_router = get_router(request)

    def _resolve(field: str, value, modality: str, require_vision: bool = False):
        binding = (value or "").strip()
        if not binding:
            return ""
        profile = config.models.get(binding)
        if profile is None:
            raise HTTPException(status_code=400, detail="{} 引用的模型不存在".format(field))
        if profile.modality != modality:
            raise HTTPException(
                status_code=400, detail="{} 必须引用 {} 类型的模型".format(field, modality)
            )
        if require_vision and not profile.capabilities.vision:
            raise HTTPException(status_code=400, detail="视觉模型必须启用 vision 能力")
        return binding

    fields = body.model_fields_set
    app_json = _load_app_json()
    restart_required = False

    if "vision_model" in fields:
        vision = _resolve("vision_model", body.vision_model, "chat", require_vision=True)
        app_json["vision_model"] = vision
        object.__setattr__(config.app, "vision_model", vision)
        model_router.vision_profile_id = vision or None
    if "embedding_model" in fields:
        embedding = _resolve("embedding_model", body.embedding_model, "embedding")
        app_json["embedding_model"] = embedding
        object.__setattr__(config.app, "embedding_model", embedding)
        restart_required = True
    if "rerank_model" in fields:
        rerank = _resolve("rerank_model", body.rerank_model, "rerank")
        app_json["rerank_model"] = rerank
        object.__setattr__(config.app, "rerank_model", rerank)
        restart_required = True

    _save_app_json(app_json)
    return {"status": "ok", "restart_required": restart_required}


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

    data = _profile_data(body)
    if data["modality"] == "chat" and body.pricing is not None:
        data["pricing"] = _validated_pricing(body.pricing)

    models_json = _load_models_json()
    models_json.setdefault("profiles", {})[profile_id] = data
    profile = _persist_and_validate(models_json, profile_id)
    config.models[profile_id] = profile

    restart_required = profile.modality != "chat"
    if profile.modality == "chat" and profile.enabled:
        try:
            client = create_model_client(profile, logger=_client_logger(request))
            model_router.clients[profile_id] = client
        except Exception:  # noqa: BLE001 - profile is saved; client stays offline
            logger.warning("创建模型客户端 %s 失败", profile_id, exc_info=True)

    return _profile_to_out(profile, model_router, restart_required=restart_required)


@router.put("/{profile_id}", response_model=ModelProfileOut)
def update_model(profile_id: str, body: ModelUpdate, request: Request):
    config = get_config(request)
    model_router = get_router(request)
    existing = config.models.get(profile_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="模型档案不存在")

    data = _profile_data(body, existing)
    if data["modality"] == "chat":
        pricing_supplied = "pricing" in body.model_fields_set
        if pricing_supplied and body.pricing is not None:
            data["pricing"] = _validated_pricing(body.pricing)
        elif not pricing_supplied and existing.pricing is not None:
            data["pricing"] = {
                "input_per_million": existing.pricing.input_per_million,
                "cached_input_per_million": existing.pricing.cached_input_per_million,
                "output_per_million": existing.pricing.output_per_million,
                "reasoning_output_per_million": (
                    existing.pricing.reasoning_output_per_million
                ),
            }

    models_json = _load_models_json()
    models_json.setdefault("profiles", {})[profile_id] = data
    profile = _persist_and_validate(models_json, profile_id)
    config.models[profile_id] = profile

    restart_required = profile.modality != "chat"
    client = model_router.clients.pop(profile_id, None)
    if client:
        client.close()
    if profile.modality == "chat" and profile.enabled:
        try:
            client = create_model_client(profile, logger=_client_logger(request))
            model_router.clients[profile_id] = client
        except Exception:  # noqa: BLE001 - profile is saved; client stays offline
            logger.warning("重建模型客户端 %s 失败", profile_id, exc_info=True)

    return _profile_to_out(profile, model_router, restart_required=restart_required)


@router.delete("/{profile_id}")
def delete_model(profile_id: str, request: Request):
    config = get_config(request)
    model_router = get_router(request)
    if profile_id not in config.models:
        raise HTTPException(status_code=404, detail="模型档案不存在")
    used = _roles_referencing(config.app, model_router, profile_id)
    if used:
        raise HTTPException(
            status_code=400,
            detail="不能删除被角色绑定引用的模型（{}），请先解除绑定".format(
                "、".join(used)
            ),
        )

    models_json = _load_models_json()
    models_json.get("profiles", {}).pop(profile_id, None)
    _save_models_json(models_json)

    config.models.pop(profile_id, None)
    client = model_router.clients.pop(profile_id, None)
    if client:
        client.close()

    return {"status": "ok"}
