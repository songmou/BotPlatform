"""Dynamically-provided auth tokens for remote MCP servers.

Some remote MCP servers (notably Feishu/Lark) can authenticate with an
*application* token that is minted server-side from ``app_id`` + ``app_secret``
and is therefore refreshable without any human in the loop.  This module
supplies such tokens so the :class:`McpClientManager` can inject them as
request headers and keep them fresh past their (typically 2-hour) expiry.

The token is cached in memory and refreshed a short buffer before expiry.  A
forced refresh is requested on every reconnect so an expired token never gets
reused after a teardown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.request
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Refresh this many seconds before the token actually expires, so a slow clock
# or network blip never leaves the manager holding an already-dead token.
_DEFAULT_REFRESH_BUFFER_SECONDS = 300


class TokenProvider:
    """Base class for token providers resolved from a server's config.

    Subclasses implement :meth:`get_headers`, returning the request headers that
    carry the dynamic token (e.g. ``{"X-Lark-MCP-TAT": "<token>"}``).
    """

    async def get_headers(self, force: bool = False) -> Dict[str, str]:
        raise NotImplementedError


class FeishuTatProvider(TokenProvider):
    """Fetches and caches a Feishu tenant access token (TAT).

    TAT is obtained from ``app_id`` + ``app_secret`` via the internal token
    endpoint and is refreshable, eliminating the manual re-authorization that
    the user-delegated ``X-Lark-MCP-UAT`` token requires every two hours.
    """

    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    DEFAULT_HEADER_NAME = "X-Lark-MCP-TAT"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        header_name: str = DEFAULT_HEADER_NAME,
        refresh_buffer_seconds: int = _DEFAULT_REFRESH_BUFFER_SECONDS,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._header_name = header_name
        self._refresh_buffer = refresh_buffer_seconds
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = threading.RLock()

    async def get_headers(self, force: bool = False) -> Dict[str, str]:
        token = await self._ensure_token(force=force)
        return {self._header_name: token}

    async def _ensure_token(self, force: bool = False) -> str:
        with self._lock:
            still_valid = (
                self._token is not None
                and time.time() < self._expires_at - self._refresh_buffer
            )
            if still_valid and not force:
                return self._token  # type: ignore[return-value]
        token, expires_in = await asyncio.to_thread(self._fetch_token)
        with self._lock:
            self._token = token
            self._expires_at = time.time() + float(expires_in)
        return token

    def _fetch_token(self) -> "tuple[str, int]":
        """Synchronously fetch a fresh TAT (runs in a worker thread)."""
        body = json.dumps(
            {"app_id": self._app_id, "app_secret": self._app_secret}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - surface a readable error
            raise RuntimeError("飞书 TAT 获取失败：{}".format(exc)) from exc
        if payload.get("code") not in (0, "0"):
            raise RuntimeError(
                "飞书 TAT 获取失败：{}".format(
                    payload.get("msg") or payload.get("error") or payload
                )
            )
        token = payload.get("tenant_access_token")
        expires_in = int(payload.get("expire", 7200))
        if not token:
            raise RuntimeError("飞书 TAT 获取失败：响应缺少 tenant_access_token")
        return token, expires_in


def build_provider(server_id: str, provider_cfg: Dict[str, object]) -> TokenProvider:
    """Construct a :class:`TokenProvider` from a server's ``token_provider`` field.

    Returns ``None`` when ``provider_cfg`` is empty/``None`` so callers can skip
    dynamic injection.  App secrets are intentionally *not* part of
    ``provider_cfg`` (they live in the keychain); the Feishu provider resolves
    its secret lazily via :func:`load_secret`.
    """
    if not isinstance(provider_cfg, dict):
        return None
    kind = provider_cfg.get("kind")
    if not kind:
        return None
    if kind == "feishu_tat":
        from src.core.config.mcp_headers import load_secret

        app_id = provider_cfg.get("app_id")
        app_secret = load_secret(server_id) if server_id else None
        if not app_id or not app_secret:
            raise RuntimeError(
                "MCP 服务 {} 缺少飞书应用凭证（app_id / app_secret），无法自动获取 TAT；"
                "请通过平台 API 用 token_provider 配置 app_id 并把 app_secret 存入密钥库".format(
                    server_id
                )
            )
        return FeishuTatProvider(app_id=str(app_id), app_secret=str(app_secret))
    raise RuntimeError("不支持的 token_provider 类型：{}".format(kind))
