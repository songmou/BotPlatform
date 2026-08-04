"""Storage for datasource password values, kept out of git-tracked config."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.core.integrations.keychain import (
    KeychainError,
    KeychainReference,
    KeychainService,
)
from src.core.paths import SYSTEM_DATA_DIR

logger = logging.getLogger(__name__)

DATASOURCE_SECRETS_FILE = SYSTEM_DATA_DIR / "datasource_secrets.json"

_SERVICE = "datasource.password"


def _reference(datasource_id: str) -> KeychainReference:
    return KeychainReference(_SERVICE, datasource_id)


def _store() -> KeychainService:
    # Built per call so tests can patch DATASOURCE_SECRETS_FILE.
    return KeychainService(storage_path=DATASOURCE_SECRETS_FILE)


def load_password(datasource_id: str) -> str:
    try:
        raw = _store().get_secret(_reference(datasource_id))
    except KeychainError:
        return ""
    if not isinstance(raw, str):
        logger.warning("数据源 %s 的密码存储格式无效，已忽略", datasource_id)
        return ""
    return raw


def save_password(datasource_id: str, password: str) -> None:
    if not password:
        delete_password(datasource_id)
        return
    _store().set_secret(_reference(datasource_id), password)


def delete_password(datasource_id: str) -> None:
    try:
        _store().delete_secret(_reference(datasource_id))
    except KeychainError:
        logger.warning("无法删除数据源 %s 的密码存储", datasource_id)


def merge_passwords(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return datasource entries with stored passwords merged in, store winning."""
    merged: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("id"):
            merged.append(entry)
            continue
        stored = load_password(str(entry["id"]))
        if not stored:
            merged.append(entry)
            continue
        merged.append({**entry, "password": stored})
    return merged


def strip_passwords(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return entries with all password values blanked, ready for disk write."""
    stripped: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            stripped.append(entry)
            continue
        stripped.append({**entry, "password": ""})
    return stripped
