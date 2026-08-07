"""Storage for model API keys, kept out of git-tracked config.

Mirrors the thin ``mcp_headers`` wrapper around :class:`KeychainService` but
stores the raw secret string keyed by the model profile id. The plaintext is
never written to ``models.json``; only a reference is resolved at runtime.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.core.integrations.keychain import (
    KeychainError,
    KeychainReference,
    KeychainService,
)
from src.core.paths import SYSTEM_DATA_DIR

logger = logging.getLogger(__name__)

MODEL_API_KEYS_FILE = SYSTEM_DATA_DIR / "model_api_keys.json"

_SERVICE = "model.api_key"


def _reference(profile_id: str) -> KeychainReference:
    return KeychainReference(_SERVICE, profile_id)


def _store() -> KeychainService:
    # Built per call so tests can patch MODEL_API_KEYS_FILE.
    return KeychainService(storage_path=MODEL_API_KEYS_FILE)


def save_model_api_key(profile_id: str, api_key: str) -> None:
    """Persist the raw API key for a profile.

    An empty/whitespace key clears any existing value instead of storing it,
    because :meth:`KeychainService.set_secret` rejects empty secrets.
    """
    if not api_key:
        delete_model_api_key(profile_id)
        return
    try:
        _store().set_secret(_reference(profile_id), api_key)
    except KeychainError as exc:
        logger.warning("保存模型 %s 的 API Key 失败：%s", profile_id, exc)


def get_model_api_key(profile_id: str) -> str:
    """Return the raw API key, or an empty string when none is configured."""
    try:
        return _store().get_secret(_reference(profile_id))
    except KeychainError:
        return ""


def delete_model_api_key(profile_id: str) -> None:
    try:
        _store().delete_secret(_reference(profile_id))
    except KeychainError:
        logger.debug("模型 %s 没有可删除的 API Key 存储", profile_id)


def model_api_key_set(profile_id: str) -> bool:
    return bool(get_model_api_key(profile_id))
