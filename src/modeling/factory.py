"""Construct a configured model adapter behind the common contract."""

from __future__ import annotations

import os
from typing import Optional

from src.config.loader import ModelProfile

from .adapters import OllamaAdapter, OpenAICompatibleAdapter
from .contracts import ModelClient, ModelError
from .observability import ModelCallLogger, ObservedModelClient


def create_model_client(
    profile: ModelProfile,
    *,
    logger: Optional[ModelCallLogger] = None,
) -> ModelClient:
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
