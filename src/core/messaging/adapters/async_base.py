"""Utilities for adapting asynchronous channel SDKs to the sync core."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from typing import Any, Awaitable, Iterator, Optional

from src.core.messaging.errors import TransientTransportError


class AsyncAdapterBridge:
    """Own one event loop and expose safe synchronous coroutine calls."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread_id: Optional[int] = None
        self._ready = threading.Event()

    def _set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._loop_thread_id = threading.get_ident()
        self._ready.set()

    def _clear_loop(self) -> None:
        self._ready.clear()
        self._loop = None
        self._loop_thread_id = None

    def _run_sync(
        self,
        awaitable: Awaitable[Any],
        *,
        timeout: float = 30.0,
    ) -> Any:
        if not self._ready.wait(timeout=min(timeout, 5.0)):
            closer = getattr(awaitable, "close", None)
            if callable(closer):
                closer()
            raise TransientTransportError("消息渠道尚未连接")
        loop = self._loop
        if loop is None or loop.is_closed():
            closer = getattr(awaitable, "close", None)
            if callable(closer):
                closer()
            raise TransientTransportError("消息渠道连接已经关闭")
        if self._loop_thread_id == threading.get_ident():
            closer = getattr(awaitable, "close", None)
            if callable(closer):
                closer()
            raise TransientTransportError("消息渠道发生循环内同步调用")
        future = asyncio.run_coroutine_threadsafe(awaitable, loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            future.cancel()
            raise TransientTransportError("消息渠道操作超时") from exc

    @contextmanager
    def typing(self, _endpoint: Any) -> Iterator[None]:
        yield
