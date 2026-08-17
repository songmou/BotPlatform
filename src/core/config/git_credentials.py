"""Storage for per-tenant, per-host git credentials (HTTPS tokens).

Tokens are isolated by tenant: the storage key includes the tenant id, so
one tenant can never read or use another tenant's credentials.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List

from src.core.integrations.keychain import (
    KeychainError,
    KeychainReference,
    KeychainService,
)
from src.core.paths import SYSTEM_DATA_DIR

logger = logging.getLogger(__name__)

GIT_CREDENTIALS_FILE = SYSTEM_DATA_DIR / "git_credentials.json"

_SERVICE = "git.credentials"

#: Hostname, optionally with a port: ``github.com``, ``git.corp.local:8443``.
_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*"
    r"(?::\d{1,5})?$"
)


def _normalize_host(host: str) -> str:
    """Normalize a host the same way lookups do, or return an empty string.

    Lookups derive the host from a URL via ``urlparse().hostname.lower()``, so
    anything stored differently (with a scheme, path or credentials) could
    never be found again.
    """
    candidate = (host or "").strip().lower()
    if not candidate or not _HOST_RE.match(candidate):
        return ""
    return candidate


def _reference(tenant_id: str, host: str) -> KeychainReference:
    return KeychainReference(_SERVICE, "{}:{}".format(tenant_id, host))


def _store() -> KeychainService:
    # Built per call so tests can patch GIT_CREDENTIALS_FILE.
    return KeychainService(storage_path=GIT_CREDENTIALS_FILE)


def load_token(tenant_id: str, host: str) -> str:
    """Return the stored token for a tenant+host, or empty string if absent."""
    normalized = _normalize_host(host)
    if not tenant_id or not normalized:
        return ""
    try:
        return _store().get_secret(_reference(tenant_id, normalized))
    except KeychainError:
        return ""


def save_token(tenant_id: str, host: str, token: str) -> None:
    """Store a token for a tenant+host. Empty token deletes the entry."""
    if not tenant_id:
        raise KeychainError("git 凭据的租户不能为空")
    normalized = _normalize_host(host)
    if not normalized:
        raise KeychainError(
            "git 凭据的域名格式无效，请只填写域名（如 github.com），不要包含协议或路径"
        )
    if not token:
        delete_token(tenant_id, normalized)
        return
    _store().set_secret(_reference(tenant_id, normalized), token)


def delete_token(tenant_id: str, host: str) -> None:
    normalized = _normalize_host(host)
    if not tenant_id or not normalized:
        return
    try:
        _store().delete_secret(_reference(tenant_id, normalized))
    except KeychainError:
        logger.warning("无法删除 git 凭据：租户 %s / %s", tenant_id, normalized)


def list_hosts(tenant_id: str) -> List[str]:
    """Return the configured hosts for a tenant (never the token values)."""
    if not tenant_id:
        return []
    try:
        keys = _store().list_keys()
    except (KeychainError, OSError):
        return []
    prefix = _SERVICE + "\n" + tenant_id + ":"
    return sorted(key[len(prefix):] for key in keys if key.startswith(prefix))


def list_credentials(tenant_id: str) -> List[Dict[str, object]]:
    """Return host descriptors for a tenant without exposing token values."""
    return [{"host": host, "configured": True} for host in list_hosts(tenant_id)]
