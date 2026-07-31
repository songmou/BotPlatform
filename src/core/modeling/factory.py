"""Construct a configured model adapter behind the common contract."""

from __future__ import annotations

import os
from typing import Optional

from src.core.config.loader import ModelProfile

from .adapters import (
    OllamaAdapter,
    OllamaEmbeddingAdapter,
    OpenAICompatibleAdapter,
    OpenAIEmbeddingAdapter,
    OpenAIRerankAdapter,
)
from .contracts import (
    EmbeddingClient,
    ModelClient,
    ModelError,
    RerankClient,
)
from .observability import ModelCallLogger, ObservedModelClient


def create_model_client(
    profile: ModelProfile,
    *,
    logger: Optional[ModelCallLogger] = None,
) -> ModelClient:
    if profile.modality != "chat":
        raise ModelError(
            "模型档案 {} 不是对话模型，无法作为对话客户端构建".format(profile.id),
            provider=profile.provider,
        )
    common = dict(
        profile_id=profile.id,
        provider=profile.provider,
        base_url=profile.base_url,
        model=profile.model,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
        timeout_seconds=profile.timeout_seconds,
        capabilities=profile.capabilities,
    )
    if profile.type == "ollama":
        client: ModelClient = OllamaAdapter(**common)
    elif profile.type == "openai_compatible":
        api_key = os.getenv(profile.api_key_env or "")
        if not api_key:
            raise ModelError(
                "模型档案 {} 缺少 API Key 环境变量 {}".format(
                    profile.id, profile.api_key_env or "（未配置）"
                ),
                provider=profile.provider,
            )
        client = OpenAICompatibleAdapter(
            **common,
            api_key=api_key,
            request_extra=profile.request_extra,
            assistant_passthrough_fields=profile.assistant_passthrough_fields,
        )
    else:
        raise ModelError(
            "模型档案 {} 使用了未知适配器 {}".format(profile.id, profile.type),
            provider=profile.provider,
        )
    return ObservedModelClient(client, logger) if logger else client


def _require_api_key(profile: ModelProfile) -> str:
    api_key = os.getenv(profile.api_key_env or "")
    if not api_key:
        raise ModelError(
            "模型档案 {} 缺少 API Key 环境变量 {}".format(
                profile.id, profile.api_key_env or "（未配置）"
            ),
            provider=profile.provider,
        )
    return api_key


def create_embedding_client(profile: ModelProfile) -> EmbeddingClient:
    if profile.modality != "embedding":
        raise ModelError(
            "模型档案 {} 不是向量模型".format(profile.id),
            provider=profile.provider,
        )
    dimensions = profile.dimensions or 0
    if dimensions < 1:
        raise ModelError(
            "向量模型档案 {} 缺少有效的维度".format(profile.id),
            provider=profile.provider,
        )
    if profile.type == "ollama":
        return OllamaEmbeddingAdapter(
            profile_id=profile.id,
            base_url=profile.base_url,
            model=profile.model,
            dimensions=dimensions,
            timeout_seconds=profile.timeout_seconds,
        )
    if profile.type == "openai_compatible":
        return OpenAIEmbeddingAdapter(
            profile_id=profile.id,
            base_url=profile.base_url,
            api_key=_require_api_key(profile),
            model=profile.model,
            dimensions=dimensions,
            timeout_seconds=profile.timeout_seconds,
        )
    raise ModelError(
        "向量模型档案 {} 使用了未知适配器 {}".format(profile.id, profile.type),
        provider=profile.provider,
    )


def create_rerank_client(profile: ModelProfile) -> RerankClient:
    if profile.modality != "rerank":
        raise ModelError(
            "模型档案 {} 不是重排模型".format(profile.id),
            provider=profile.provider,
        )
    if profile.type != "openai_compatible":
        raise ModelError(
            "重排模型档案 {} 使用了未知适配器 {}".format(profile.id, profile.type),
            provider=profile.provider,
        )
    return OpenAIRerankAdapter(
        profile_id=profile.id,
        base_url=profile.base_url,
        api_key=_require_api_key(profile),
        model=profile.model,
        timeout_seconds=profile.timeout_seconds,
    )
