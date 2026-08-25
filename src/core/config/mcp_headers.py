"""Storage for MCP request header values, kept out of git-tracked config."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.core.integrations.keychain import (
    KeychainError,
    KeychainReference,
    KeychainService,
)
from src.core.paths import SYSTEM_DATA_DIR

logger = logging.getLogger(__name__)

MCP_HEADERS_FILE = SYSTEM_DATA_DIR / "mcp_headers.json"

_SERVICE = "mcp.headers"
_SECRET_SERVICE = "mcp.secret"


def _reference(server_id: str) -> KeychainReference:
    return KeychainReference(_SERVICE, server_id)


def _store() -> KeychainService:
    # Built per call so tests can patch MCP_HEADERS_FILE.
    return KeychainService(storage_path=MCP_HEADERS_FILE)


def load_headers(server_id: str) -> Dict[str, str]:
    try:
        raw = _store().get_secret(_reference(server_id))
    except KeychainError:
        return {}
    try:
        headers = json.loads(raw)
    except ValueError:
        logger.warning("MCP 服务 %s 的请求头存储格式无效，已忽略", server_id)
        return {}
    if not isinstance(headers, dict):
        logger.warning("MCP 服务 %s 的请求头存储格式无效，已忽略", server_id)
        return {}
    return {str(k): str(v) for k, v in headers.items()}


def save_headers(server_id: str, headers: Dict[str, str]) -> None:
    if not headers:
        delete_headers(server_id)
        return
    payload = json.dumps(
        {str(k): str(v) for k, v in headers.items()},
        ensure_ascii=False,
        sort_keys=True,
    )
    _store().set_secret(_reference(server_id), payload)


def delete_headers(server_id: str) -> None:
    try:
        _store().delete_secret(_reference(server_id))
    except KeychainError:
        logger.warning("无法删除 MCP 服务 %s 的请求头存储", server_id)


def merge_headers(servers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return servers with stored header values merged in, store winning."""
    merged: List[Dict[str, Any]] = []
    for server in servers:
        if not isinstance(server, dict) or not server.get("id"):
            merged.append(server)
            continue
        stored = load_headers(str(server["id"]))
        if not stored:
            merged.append(server)
            continue
        headers = dict(server.get("headers") or {})
        headers.update(stored)
        merged.append({**server, "headers": headers})
    return merged


def _secret_reference(server_id: str) -> KeychainReference:
    return KeychainReference(_SECRET_SERVICE, server_id)


def load_secret(server_id: str) -> Optional[str]:
    """Return the stored app secret for a server, or ``None`` if absent."""
    try:
        raw = _store().get_secret(_secret_reference(server_id))
    except KeychainError:
        return None
    return raw or None


def save_secret(server_id: str, secret: str) -> None:
    """Persist an app secret (e.g. Feishu app_secret) for a server."""
    if not secret:
        delete_secret(server_id)
        return
    _store().set_secret(_secret_reference(server_id), str(secret))


def delete_secret(server_id: str) -> None:
    """Remove a stored app secret for a server."""
    try:
        _store().delete_secret(_secret_reference(server_id))
    except KeychainError:
        logger.warning("无法删除 MCP 服务 %s 的密钥存储", server_id)


# ---------------------------------------------------------------------------
# Per-user OAuth tokens (UAT): user_access_token + refresh_token, scoped by
# both server_id and the platform user who completed the Feishu authorization.
# ---------------------------------------------------------------------------
_USER_TOKEN_SERVICE = "mcp.uat"


def _user_reference(server_id: str, user_id: str) -> KeychainReference:
    return KeychainReference(_USER_TOKEN_SERVICE, "{}\n{}".format(server_id, user_id))


def load_user_token(server_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Return the stored UAT payload for a (server, user), or ``None``."""
    try:
        raw = _store().get_secret(_user_reference(server_id, user_id))
    except KeychainError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("MCP 服务 %s 用户 %s 的 UAT 存储格式无效，已忽略", server_id, user_id)
        return None
    return data if isinstance(data, dict) else None


def save_user_token(server_id: str, user_id: str, token: Dict[str, Any]) -> None:
    """Persist a UAT payload (access_token/refresh_token/expiry) for a user."""
    if not token:
        delete_user_token(server_id, user_id)
        return
    payload = json.dumps(
        {str(k): v for k, v in token.items()},
        ensure_ascii=False,
        sort_keys=True,
    )
    _store().set_secret(_user_reference(server_id, user_id), payload)


def delete_user_token(server_id: str, user_id: str) -> None:
    """Remove a stored UAT payload for a (server, user)."""
    try:
        _store().delete_secret(_user_reference(server_id, user_id))
    except KeychainError:
        logger.warning("无法删除 MCP 服务 %s 用户 %s 的 UAT 存储", server_id, user_id)
