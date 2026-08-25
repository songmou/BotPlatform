"""MCP client manager bridging the async ``mcp`` SDK to the sync tool runtime.

The platform's tool runtime and both chat execution paths are synchronous, while
the official ``mcp`` SDK is asyncio-based. This manager owns a dedicated
background event-loop thread and exposes a fully synchronous API by submitting
coroutines via ``asyncio.run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


def namespaced_name(server_id: str, tool_name: str) -> str:
    """Build the collision-safe tool name exposed to agents."""
    return "{}__{}".format(server_id, tool_name)


_DISCONNECT_MARKERS = (
    "ClosedResourceError",
    "BrokenResourceError",
    "EndOfStream",
    "ConnectionError",
    "ConnectionResetError",
    # A silent 30s hang (server alive but unresponsive) raises TimeoutError
    # rather than a transport-level break.  Treat it as a disconnect so the
    # manager still reconnects and retries once instead of failing outright.
    "TimeoutError",
)


def _is_disconnected(exc: BaseException) -> bool:
    """Report whether an error means the MCP transport is no longer usable."""
    stack = [exc]
    while stack:
        current = stack.pop()
        if type(current).__name__ in _DISCONNECT_MARKERS:
            return True
        sub = getattr(current, "exceptions", None)
        if sub:
            stack.extend(sub)
        cause = getattr(current, "__cause__", None)
        if cause is not None:
            stack.append(cause)
    return False


def _format_mcp_error(exc: BaseException) -> str:
    """Unwrap anyio/exceptiongroup ExceptionGroups into a readable message.

    The MCP SDK runs transport reader/writer tasks inside an anyio TaskGroup;
    when the handshake fails the real error is wrapped in an ExceptionGroup
    whose ``str()`` only shows "unhandled errors in a TaskGroup (N
    sub-exceptions)". Recurse into ``exc.exceptions`` so operators see the
    underlying cause (e.g. the real HTTP 401 from the remote server).
    """
    exceptions = getattr(exc, "exceptions", None)
    if exceptions:
        inner = "; ".join(_format_mcp_error(sub) for sub in exceptions)
        return "{}: {}".format(type(exc).__name__, inner)
    return str(exc) or type(exc).__name__


# Substrings that, appearing in a tool-call failure, indicate the remote MCP
# server rejected the auth token (expired / invalid).  Such failures must
# trigger a token refresh + reconnect rather than being surfaced to the agent.
_AUTH_ERROR_MARKERS = (
    "401",
    "unauthorized",
    "unauthenticated",
    "forbidden",
    "token expired",
    "invalid token",
    "tenant_access_token",
    "access token",
    "鉴权",
    "令牌",
    "未授权",
    "-32003",
)


def _is_auth_error(exc: BaseException) -> bool:
    """Report whether a failure is an auth-token rejection worth refreshing."""
    message = _format_mcp_error(exc).lower()
    return any(marker in message for marker in _AUTH_ERROR_MARKERS)


class _Connection:
    """A live MCP session plus the tools discovered on one server.

    The connection is owned by a dedicated lifecycle task that opens and closes
    the transport within a single asyncio task, satisfying anyio's requirement
    that cancel scopes be entered and exited in the same task.
    """

    def __init__(
        self,
        server_id: str,
        cfg: Dict[str, Any],
        session: Any,
        tools: Dict[str, Dict[str, Any]],
        stop_event: asyncio.Event,
        task_future: "concurrent.futures.Future",
    ) -> None:
        self.server_id = server_id
        self.cfg = cfg
        self.session = session
        # namespaced name -> {description, parameters, real_name}
        self.tools = tools
        self.stop_event = stop_event
        self.task_future = task_future


class McpClientManager:
    """Manages MCP server connections and exposes their tools synchronously."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        # Connections are keyed by (server_id, user_id) so a user-delegated
        # token (UAT) is bound to the right user.  user_id defaults to "" for
        # app-level (TAT) servers that are not user-scoped.
        self._connections: Dict[tuple, _Connection] = {}
        self._token_providers: Dict[tuple, Any] = {}
        self._configs: Dict[str, Dict[str, Any]] = {}
        self._provider_lock = threading.RLock()
        self._lock = threading.RLock()
        self._started = False

    @staticmethod
    def _conn_key(server_id: str, user_id: Optional[str]) -> tuple:
        return (server_id, user_id or "")

    # ---- lifecycle ----
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_loop, name="mcp-client-loop", daemon=True
            )
            self._thread.start()
            self._started = True

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, coro: Any, timeout: Optional[float] = None) -> Any:
        if self._loop is None:
            raise RuntimeError("McpClientManager 尚未启动")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout or self._timeout)

    def close(self) -> None:
        with self._lock:
            for key in list(self._connections.keys()):
                try:
                    self._disconnect_locked(key)
                except Exception:  # noqa: BLE001 - best effort on shutdown
                    pass
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._started = False

    # ---- sync public API ----
    def reload(self, servers: List[Dict[str, Any]]) -> None:
        """Reconcile live connections with the desired server configs."""
        wanted: Dict[str, Dict[str, Any]] = {}
        for cfg in servers:
            if not cfg.get("enabled", True):
                continue
            sid = cfg.get("id")
            if sid:
                wanted[sid] = cfg
        with self._lock:
            live_sids = {key[0] for key in self._connections.keys()}
            for sid in live_sids - set(wanted.keys()):
                for key in [k for k in self._connections.keys() if k[0] == sid]:
                    self._disconnect_locked(key)
            for sid, cfg in wanted.items():
                self._configs[sid] = cfg
                # UAT servers without a pinned user are connected lazily, after
                # the operator completes the Feishu OAuth flow (which creates a
                # per-user connection).  Skip the shared bootstrap for them to
                # avoid a noisy "missing token" failure at startup.
                provider_cfg = cfg.get("token_provider") or {}
                if provider_cfg.get("kind") == "feishu_uat" and not provider_cfg.get("user_id"):
                    continue
                existing = self._connections.get((sid, ""))
                if existing is not None and existing.cfg == cfg:
                    continue
                if existing is not None:
                    self._disconnect_locked((sid, ""))
                try:
                    self._connect_locked(cfg, "")
                except Exception as exc:  # noqa: BLE001 - keep other servers alive
                    logger.warning(
                        "MCP 服务 %s 连接失败：%s", sid, _format_mcp_error(exc), exc_info=exc
                    )

    def connect_server(self, cfg: Dict[str, Any], user_id: Optional[str] = None) -> List[str]:
        with self._lock:
            self._connect_locked(cfg, user_id)
            conn = self._connections.get(self._conn_key(cfg["id"], user_id))
            return list(conn.tools.keys()) if conn else []

    def server_ids(self) -> List[str]:
        with self._lock:
            return sorted({key[0] for key in self._connections.keys()})

    def tool_names(self, server_id: Optional[str] = None) -> List[str]:
        with self._lock:
            if server_id is not None:
                names: List[str] = []
                for (sid, _user_id), conn in self._connections.items():
                    if sid != server_id:
                        continue
                    for name in conn.tools:
                        if name not in names:
                            names.append(name)
                return names
            names: List[str] = []
            for conn in self._connections.values():
                for name in conn.tools:
                    if name not in names:
                        names.append(name)
            return names

    def server_tools(self, server_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            tools: List[Dict[str, Any]] = []
            seen = set()
            for (sid, _user_id), conn in self._connections.items():
                if sid != server_id:
                    continue
                for namespaced, tool in conn.tools.items():
                    if namespaced in seen:
                        continue
                    seen.add(namespaced)
                    tools.append(
                        {
                            "name": tool["real_name"],
                            "description": tool["description"],
                            "parameters": tool["parameters"],
                        }
                    )
            return tools

    def has_tool(self, name: str) -> bool:
        with self._lock:
            return any(name in conn.tools for conn in self._connections.values())

    def is_available(self, name: str) -> bool:
        return self.has_tool(name)

    def tool_schema(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for conn in self._connections.values():
                tool = conn.tools.get(name)
                if tool is not None:
                    return {
                        "description": tool["description"],
                        "parameters": tool["parameters"],
                    }
        return None

    def call_tool(
        self, name: str, arguments: Dict[str, Any], user_id: Optional[str] = None
    ) -> Any:
        target = self._resolve_target(name, user_id)
        if target is None:
            raise RuntimeError("未知 MCP 工具：{}".format(name))
        server_id, session, real_name = target
        try:
            result = self._run(self._call(session, real_name, arguments))
        except Exception as exc:  # noqa: BLE001 - retried once when disconnected
            if _is_disconnected(exc):
                logger.warning("MCP 服务 %s 连接已断开，正在重连", server_id)
                session, real_name = self._reconnect(server_id, name, user_id)
                result = self._run(self._call(session, real_name, arguments))
            elif _is_auth_error(exc):
                # Token likely expired mid-session.  Force a fresh token and
                # reconnect so the next attempt carries a valid credential.
                logger.warning("MCP 服务 %s 鉴权失败，强制刷新令牌并重连", server_id)
                self._invalidate_provider(server_id, user_id)
                session, real_name = self._reconnect(server_id, name, user_id)
                result = self._run(self._call(session, real_name, arguments))
            else:
                raise
        return self._serialize_result(result)

    def _resolve_target(
        self, name: str, user_id: Optional[str] = None
    ) -> Optional[tuple]:
        with self._lock:
            exact = None
            shared = None
            fallback = None
            for (sid, uid), conn in self._connections.items():
                tool = conn.tools.get(name)
                if tool is None:
                    continue
                if fallback is None:
                    fallback = (sid, conn.session, tool["real_name"])
                if uid == "":
                    shared = (sid, conn.session, tool["real_name"])
                if user_id is not None and uid == user_id:
                    exact = (sid, conn.session, tool["real_name"])
                    break
            if user_id is not None:
                # A user-scoped request may use the shared app-level connection,
                # but must never borrow another user's delegated connection.
                return exact or shared
            return fallback

    def _reconnect(self, server_id: str, name: str, user_id: Optional[str] = None) -> tuple:
        with self._lock:
            cfg = self._configs.get(server_id)
            if cfg is None:
                raise RuntimeError("MCP 服务 {} 未配置".format(server_id))
            # Pick the connection key: the user-scoped one if requested, else
            # any connection for this server.
            conn_key = self._conn_key(server_id, user_id)
            if conn_key not in self._connections:
                conn_key = next(
                    (k for k in self._connections.keys() if k[0] == server_id), None
                )
            if conn_key is None:
                raise RuntimeError("MCP 服务 {} 未连接".format(server_id))
            self._disconnect_locked(conn_key)
            self._connect_locked(cfg, conn_key[1])
            conn = self._connections.get(conn_key)
            tool = conn.tools.get(name) if conn is not None else None
            if conn is None or tool is None:
                raise RuntimeError("未知 MCP 工具：{}".format(name))
            return conn.session, tool["real_name"]

    # ---- async internals ----
    async def _connection_lifecycle(
        self,
        cfg: Dict[str, Any],
        ready: "concurrent.futures.Future",
        user_id: Optional[str] = None,
    ) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        sid = cfg["id"]
        stop_event = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                transport = cfg.get("transport", "stdio")
                headers = dict(cfg.get("headers") or {})
                for header_name, env_key in (cfg.get("headers_env") or {}).items():
                    value = os.getenv(env_key or "")
                    if not value:
                        continue
                    for existing in [
                        k for k in headers if k.lower() == header_name.lower()
                    ]:
                        del headers[existing]
                    headers[header_name] = value
                headers = headers or None
                provider_cfg = cfg.get("token_provider")
                if isinstance(provider_cfg, dict) and provider_cfg.get("kind"):
                    provider = self._resolve_provider(cfg, user_id)
                    provider_headers = await provider.get_headers()
                    headers = {**(headers or {}), **provider_headers}
                if transport == "sse":
                    from mcp.client.sse import sse_client

                    read, write = await stack.enter_async_context(
                        sse_client(cfg["url"], headers=headers)
                    )
                elif transport in ("streamablehttp", "http"):
                    from mcp.client.streamable_http import streamablehttp_client

                    read, write, _ = await stack.enter_async_context(
                        streamablehttp_client(cfg["url"], headers=headers)
                    )
                else:
                    params = StdioServerParameters(
                        command=cfg["command"],
                        args=list(cfg.get("args") or []),
                        env=cfg.get("env") or None,
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), timeout=self._timeout)
                listing = await asyncio.wait_for(session.list_tools(), timeout=self._timeout)
                tools: Dict[str, Dict[str, Any]] = {}
                for tool in listing.tools:
                    schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
                    parameters = dict(schema) if schema else {}
                    parameters.setdefault("type", "object")
                    parameters.setdefault("properties", {})
                    tools[namespaced_name(sid, tool.name)] = {
                        "description": tool.description or "",
                        "parameters": parameters,
                        "real_name": tool.name,
                    }
                ready.set_result((session, tools, stop_event))
                # Hold the transport open until teardown is requested.
                await stop_event.wait()
            # The AsyncExitStack tears down here, in the same task that opened it.
        except Exception as exc:  # noqa: BLE001 - report to the waiting caller
            if not ready.done():
                ready.set_exception(exc)

    @staticmethod
    async def _set_event(event: asyncio.Event) -> None:
        event.set()

    def _connect_locked(self, cfg: Dict[str, Any], user_id: Optional[str] = None) -> None:
        if self._loop is None:
            raise RuntimeError("McpClientManager 尚未启动")
        sid = cfg["id"]
        key = self._conn_key(sid, user_id)
        ready: "concurrent.futures.Future" = concurrent.futures.Future()
        task_future = asyncio.run_coroutine_threadsafe(
            self._connection_lifecycle(cfg, ready, user_id), self._loop
        )
        try:
            session, tools, stop_event = ready.result(timeout=self._timeout)
        except Exception:
            task_future.cancel()
            raise
        self._connections[key] = _Connection(
            sid, cfg, session, tools, stop_event, task_future
        )
        self._configs[sid] = cfg

    def _disconnect_locked(self, conn_key: tuple) -> None:
        conn = self._connections.pop(conn_key, None)
        if conn is None:
            return
        # Drop any cached token provider so the next connect re-resolves the
        # secret and re-fetches a fresh token.
        self._invalidate_provider(conn.server_id, conn_key[1])
        try:
            asyncio.run_coroutine_threadsafe(
                self._set_event(conn.stop_event), self._loop
            ).result(timeout=10)
            conn.task_future.result(timeout=10)
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.warning(
                "关闭 MCP 服务 %s 失败：%s", conn.server_id, _format_mcp_error(exc), exc_info=exc
            )
            conn.task_future.cancel()

    def _resolve_provider(self, cfg: Dict[str, Any], user_id: Optional[str] = None) -> Any:
        """Return the (cached) token provider for a server, building it if needed.

        Providers are cached per (server_id, user_id) so a TAT (app-level, no
        user) stays in memory across the lifetime of a connection, and each
        user's UAT is isolated.  The cache is cleared on disconnect/reconnect
        (see ``_invalidate_provider``) so a reconnect always re-fetches.
        """
        from src.core.tooling.mcp_token_providers import build_provider

        server_id = cfg.get("id")
        provider_cfg = cfg.get("token_provider")
        if not isinstance(provider_cfg, dict) or not provider_cfg.get("kind"):
            raise RuntimeError(
                "MCP 服务 {} 的 token_provider 配置无效".format(server_id)
            )
        cache_key = self._conn_key(server_id, user_id)
        with self._provider_lock:
            cached = self._token_providers.get(cache_key)
            if cached is not None:
                return cached
            provider = build_provider(server_id, provider_cfg, user_id=user_id)
            self._token_providers[cache_key] = provider
            return provider

    def _invalidate_provider(
        self, server_id: str, user_id: Optional[str] = None
    ) -> None:
        with self._provider_lock:
            self._token_providers.pop(self._conn_key(server_id, user_id), None)

    async def _call(self, session: Any, real_name: str, arguments: Dict[str, Any]) -> Any:
        return await asyncio.wait_for(
            session.call_tool(real_name, arguments=arguments), timeout=self._timeout
        )

    @staticmethod
    def _serialize_result(result: Any) -> Any:
        content = getattr(result, "content", None)
        is_error = bool(getattr(result, "isError", False))
        if not content:
            if is_error:
                raise RuntimeError("MCP 工具调用失败")
            return {"ok": True}
        parts: List[Any] = []
        for block in content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
            else:
                dump = getattr(block, "model_dump", None)
                parts.append(dump() if callable(dump) else str(block))
        if is_error:
            raise RuntimeError("；".join(str(p) for p in parts) or "MCP 工具调用失败")
        if len(parts) == 1:
            return parts[0]
        return parts
