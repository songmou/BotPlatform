"""Dynamically-provided auth tokens for remote MCP servers.

Some remote MCP servers (notably Feishu/Lark) can authenticate with an
*application* token that is minted server-side from ``app_id`` + ``app_secret``
and is therefore refreshable without any human in the loop.  This module
supplies such tokens so the :class:`McpClientManager` can inject them as
request headers and keep them fresh past their (typically 2-hour) expiry.

The token is cached in memory and refreshed a short buffer before expiry.  A
forced refresh is requested on every reconnect so an expired token never gets
reused after a teardown.

Feishu also offers a *user-delegated* token (UAT, ``X-Lark-MCP-UAT``) required
by user-context tools such as ``get-user`` / ``list-docs``.  Unlike the TAT,
the UAT is obtained via an OAuth authorization-code flow and must be refreshed
with a ``refresh_token``.  See :class:`FeishuUatProvider`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.request
from typing import Dict, Optional, Tuple

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


def build_provider(
    server_id: str, provider_cfg: Dict[str, object], user_id: Optional[str] = None
) -> TokenProvider:
    """Construct a :class:`TokenProvider` from a server's ``token_provider`` field.

    Returns ``None`` when ``provider_cfg`` is empty/``None`` so callers can skip
    dynamic injection.  App secrets are intentionally *not* part of
    ``provider_cfg`` (they live in the keychain); the Feishu providers resolve
    their secret lazily via :func:`load_secret`.

    ``user_id`` is required for user-delegated (UAT) providers; it may be passed
    explicitly (per-call) or taken from ``provider_cfg["user_id"]`` (a pinned
    operator).  When omitted for a UAT provider, the caller is expected to
    supply it at call time.
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
    if kind == "feishu_uat":
        app_id = provider_cfg.get("app_id")
        if not app_id:
            raise RuntimeError(
                "MCP 服务 {} 的 token_provider 缺少 app_id，无法使用 UAT".format(server_id)
            )
        uid = user_id or provider_cfg.get("user_id")
        if not uid:
            raise RuntimeError(
                "MCP 服务 {} 的 UAT 需要绑定具体用户（token_provider.user_id 或调用时传入），"
                "请先完成飞书授权".format(server_id)
            )
        return FeishuUatProvider(
            server_id=str(server_id), user_id=str(uid), app_id=str(app_id)
        )
    raise RuntimeError("不支持的 token_provider 类型：{}".format(kind))


# ---------------------------------------------------------------------------
# Feishu user-delegated token (UAT) support
# ---------------------------------------------------------------------------
# Feishu OIDC (v1) endpoints.  The user_access_token / refresh_access_token
# endpoints require an *app_access_token* as a Bearer header (not app_id /
# app_secret in the body), so a short-lived app_access_token is fetched first.
_APP_ACCESS_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
_UAT_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"
_UAT_REFRESH_URL = "https://open.feishu.cn/open-apis/authen/v1/oidc/refresh_access_token"
_AUTHORIZE_URL = "https://open.feishu.cn/connect/oauth2/authorize"

# Module-level cache for app_access_token, keyed by (server_id).  app_secret is
# resolved from the keychain at fetch time; the token itself is app-scoped.
_APP_TOKEN_CACHE: Dict[str, Tuple[str, float]] = {}
_APP_TOKEN_LOCK = threading.Lock()


def _http_post_json(url: str, body: Dict[str, object], headers: Optional[Dict[str, str]] = None) -> Dict[str, object]:
    """POST JSON and parse the Feishu response, raising on transport failure."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface a readable error
        raise RuntimeError("飞书请求失败：{}".format(exc)) from exc


class FeishuUatProvider(TokenProvider):
    """Fetches, caches and refreshes a Feishu user access token (UAT).

    The UAT is user-scoped: it is obtained by the end user completing the Feishu
    OAuth flow (see :func:`exchange_feishu_code`) and stored per ``(server_id,
    user_id)`` in the keychain.  When the token nears expiry it is refreshed
    with the stored ``refresh_token`` (which itself needs an app_access_token).
    """

    DEFAULT_HEADER_NAME = "X-Lark-MCP-UAT"

    def __init__(
        self,
        server_id: str,
        user_id: str,
        app_id: str,
        header_name: str = DEFAULT_HEADER_NAME,
        refresh_buffer_seconds: int = _DEFAULT_REFRESH_BUFFER_SECONDS,
    ) -> None:
        self._server_id = server_id
        self._user_id = user_id
        self._app_id = app_id
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
            if self._token and time.time() < self._expires_at - self._refresh_buffer and not force:
                return self._token  # type: ignore[return-value]
        stored = self._load_stored()
        access = stored.get("access_token")
        refresh = stored.get("refresh_token")
        expires_at = float(stored.get("expires_at", 0) or 0)
        if access and time.time() < expires_at - self._refresh_buffer:
            with self._lock:
                self._token = access
                self._expires_at = expires_at
            return access
        # Need a fresh token via refresh.  Attempt it whenever a refresh_token
        # is present; if it has expired/invalid the server rejects it and we
        # surface the actionable "please re-authorize" error below.
        if refresh:
            try:
                payload = self._refresh(refresh)
            except Exception as exc:  # noqa: BLE001 - fall through to clear error
                logger.warning("飞书 UAT 刷新失败（用户 %s）：%s", self._user_id, exc)
                payload = None
            if payload:
                self._persist_payload(payload)
                return payload["access_token"]  # type: ignore[index]
        raise RuntimeError(
            "飞书用户令牌（UAT）缺失或已过期，请先在面板完成飞书授权"
        )

    # ---- storage ----
    def _load_stored(self) -> Dict[str, object]:
        from src.core.config.mcp_headers import load_user_token

        return load_user_token(self._server_id, self._user_id) or {}

    def _persist_payload(self, payload: Dict[str, object]) -> None:
        from src.core.config.mcp_headers import save_user_token

        expires_in = float(payload.get("expires_in", 7200) or 7200)
        save_user_token(
            self._server_id,
            self._user_id,
            {
                "access_token": payload.get("access_token"),
                "refresh_token": payload.get("refresh_token"),
                "expires_at": time.time() + expires_in,
                "open_id": payload.get("open_id"),
                "union_id": payload.get("union_id"),
            },
        )
        with self._lock:
            self._token = payload.get("access_token")  # type: ignore[assignment]
            self._expires_at = time.time() + expires_in

    # ---- Feishu API calls ----
    def _app_access_token(self) -> str:
        from src.core.config.mcp_headers import load_secret

        app_secret = load_secret(self._server_id)
        if not app_secret:
            raise RuntimeError("飞书应用 app_secret 未配置，无法获取 app_access_token")
        with _APP_TOKEN_LOCK:
            cached = _APP_TOKEN_CACHE.get(self._server_id)
            if cached is not None and time.time() < cached[1] - 300:
                return cached[0]
            payload = _http_post_json(
                _APP_ACCESS_TOKEN_URL,
                {"app_id": self._app_id, "app_secret": app_secret},
            )
            if payload.get("code") not in (0, "0"):
                raise RuntimeError(
                    "飞书 app_access_token 获取失败：{}".format(
                        payload.get("msg") or payload.get("error") or payload
                    )
                )
            token = payload.get("app_access_token")
            expires_in = float(payload.get("expire", 7200) or 7200)
            if not token:
                raise RuntimeError("飞书 app_access_token 获取失败：响应缺少 app_access_token")
            _APP_TOKEN_CACHE[self._server_id] = (token, time.time() + expires_in)
            return token  # type: ignore[return-value]

    def _refresh(self, refresh_token: str) -> Dict[str, object]:
        app_token = self._app_access_token()
        payload = _http_post_json(
            _UAT_REFRESH_URL,
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers={"Authorization": "Bearer {}".format(app_token)},
        )
        if payload.get("code") not in (0, "0"):
            raise RuntimeError(
                "飞书 UAT 刷新失败：{}".format(
                    payload.get("msg") or payload.get("error") or payload
                )
            )
        return payload

    @classmethod
    def exchange_code(
        cls,
        server_id: str,
        user_id: str,
        app_id: str,
        code: str,
    ) -> Dict[str, object]:
        """Exchange an OAuth authorization code for a UAT and persist it.

        Used by the panel OAuth callback.  Returns the Feishu payload
        (includes ``open_id`` / ``union_id`` for reference).
        """
        from src.core.config.mcp_headers import load_secret

        app_secret = load_secret(server_id)
        if not app_secret:
            raise RuntimeError("飞书应用 app_secret 未配置，无法换取 UAT")
        # Fetch a short-lived app_access_token to authorize the code exchange.
        app_payload = _http_post_json(
            _APP_ACCESS_TOKEN_URL,
            {"app_id": app_id, "app_secret": app_secret},
        )
        if app_payload.get("code") not in (0, "0"):
            raise RuntimeError(
                "飞书 app_access_token 获取失败：{}".format(
                    app_payload.get("msg") or app_payload.get("error") or app_payload
                )
            )
        app_token = app_payload.get("app_access_token")
        if not app_token:
            raise RuntimeError("飞书 app_access_token 获取失败：响应缺少 app_access_token")
        payload = _http_post_json(
            _UAT_TOKEN_URL,
            {"grant_type": "authorization_code", "code": code},
            headers={"Authorization": "Bearer {}".format(app_token)},
        )
        if payload.get("code") not in (0, "0"):
            raise RuntimeError(
                "飞书授权换取令牌失败：{}".format(
                    payload.get("msg") or payload.get("error") or payload
                )
            )
        provider = cls(server_id, user_id, app_id)
        provider._persist_payload(payload)
        return payload


def exchange_feishu_code(
    server_id: str, user_id: str, app_id: str, code: str
) -> Dict[str, object]:
    """Module-level convenience wrapper around :meth:`FeishuUatProvider.exchange_code`."""
    return FeishuUatProvider.exchange_code(server_id, user_id, app_id, code)


def build_feishu_authorize_url(app_id: str, redirect_uri: str, state: str, scopes: Optional[str] = None) -> str:
    """Build the Feishu OAuth authorize URL the operator opens to grant UAT.

    ``redirect_uri`` must match the redirect URI configured for the Feishu app.
    """
    from urllib.parse import urlencode

    params = {
        "app_id": app_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
    }
    if scopes:
        params["scope"] = scopes
    return "{}?{}".format(_AUTHORIZE_URL, urlencode(params))
