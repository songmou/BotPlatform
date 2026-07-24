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
import threading
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


def namespaced_name(server_id: str, tool_name: str) -> str:
    """Build the collision-safe tool name exposed to agents."""
    return "{}__{}".format(server_id, tool_name)


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
        self._connections: Dict[str, _Connection] = {}
        self._lock = threading.RLock()
        self._started = False

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
            for sid in list(self._connections.keys()):
                try:
                    self._disconnect_locked(sid)
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
            for sid in set(self._connections.keys()) - set(wanted.keys()):
                self._disconnect_locked(sid)
            for sid, cfg in wanted.items():
                existing = self._connections.get(sid)
                if existing is not None and existing.cfg == cfg:
                    continue
                if existing is not None:
                    self._disconnect_locked(sid)
                try:
                    self._connect_locked(cfg)
                except Exception as exc:  # noqa: BLE001 - keep other servers alive
                    logger.warning("MCP 服务 %s 连接失败：%s", sid, exc)

    def connect_server(self, cfg: Dict[str, Any]) -> List[str]:
        with self._lock:
            self._connect_locked(cfg)
            conn = self._connections.get(cfg["id"])
            return list(conn.tools.keys()) if conn else []

    def disconnect_server(self, server_id: str) -> None:
        with self._lock:
            self._disconnect_locked(server_id)

    def server_ids(self) -> List[str]:
        with self._lock:
            return list(self._connections.keys())

    def tool_names(self, server_id: Optional[str] = None) -> List[str]:
        with self._lock:
            if server_id is not None:
                conn = self._connections.get(server_id)
                return list(conn.tools.keys()) if conn else []
            names: List[str] = []
            for conn in self._connections.values():
                names.extend(conn.tools.keys())
            return names

    def server_tools(self, server_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connections.get(server_id)
            if conn is None:
                return []
            return [
                {
                    "name": tool["real_name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                }
                for tool in conn.tools.values()
            ]

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

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        with self._lock:
            target: Optional[Any] = None
            for conn in self._connections.values():
                tool = conn.tools.get(name)
                if tool is not None:
                    target = (conn.session, tool["real_name"])
                    break
        if target is None:
            raise RuntimeError("未知 MCP 工具：{}".format(name))
        session, real_name = target
        result = self._run(self._call(session, real_name, arguments))
        return self._serialize_result(result)

    # ---- async internals ----
    async def _connection_lifecycle(
        self, cfg: Dict[str, Any], ready: "concurrent.futures.Future"
    ) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        sid = cfg["id"]
        stop_event = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                transport = cfg.get("transport", "stdio")
                headers = cfg.get("headers") or None
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

    def _connect_locked(self, cfg: Dict[str, Any]) -> None:
        if self._loop is None:
            raise RuntimeError("McpClientManager 尚未启动")
        sid = cfg["id"]
        ready: "concurrent.futures.Future" = concurrent.futures.Future()
        task_future = asyncio.run_coroutine_threadsafe(
            self._connection_lifecycle(cfg, ready), self._loop
        )
        try:
            session, tools, stop_event = ready.result(timeout=self._timeout)
        except Exception:
            task_future.cancel()
            raise
        self._connections[sid] = _Connection(
            sid, cfg, session, tools, stop_event, task_future
        )

    def _disconnect_locked(self, server_id: str) -> None:
        conn = self._connections.pop(server_id, None)
        if conn is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._set_event(conn.stop_event), self._loop
            ).result(timeout=10)
            conn.task_future.result(timeout=10)
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.warning("关闭 MCP 服务 %s 失败：%s", server_id, exc)
            conn.task_future.cancel()

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
