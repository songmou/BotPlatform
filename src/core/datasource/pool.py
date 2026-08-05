"""Per-datasource connection pool using LifoQueue.

Keeps a small number of persistent connections to avoid per-query handshake
overhead while limiting concurrent pressure on the remote database.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Dict

from src.core.datasource.errors import DataSourceError

logger = logging.getLogger(__name__)

_IDLE_TTL_SECONDS = 300


class PoolItem:
    __slots__ = ("conn", "created_at")

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.created_at = time.time()

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.created_at


class ConnectionPool:
    """LIFO pool of pre-connected database connections.

    Callers borrow with pool.get() and return with pool.put(item).
    Items idle for > _IDLE_TTL_SECONDS are dropped on return.
    """

    def __init__(
        self, max_size: int, factory: Callable[[], Any], ping: Callable[[Any], bool]
    ) -> None:
        self._q: queue.LifoQueue[PoolItem] = queue.LifoQueue(maxsize=max_size)
        self._factory = factory
        self._ping = ping
        self._lock = threading.RLock()
        self._created_count = 0

    @property
    def size(self) -> int:
        return self._created_count

    def get(self, timeout: float = 5.0) -> PoolItem:
        """Borrow a connection, creating a new one if the pool is empty."""
        try:
            item = self._q.get_nowait()
        except queue.Empty:
            return self._create()
        # Check if still alive, else replace
        if not self._ping(item.conn):
            try:
                item.conn.close()
            except Exception:
                pass
            return self._create()
        return item

    def put(self, item: PoolItem) -> None:
        """Return a connection to the pool, dropping stale items."""
        if item.idle_seconds > _IDLE_TTL_SECONDS:
            try:
                item.conn.close()
            except Exception:
                pass
            return
        try:
            self._q.put_nowait(item)
        except queue.Full:
            try:
                item.conn.close()
            except Exception:
                pass

    def drain(self) -> None:
        """Close all pooled connections and reset."""
        with self._lock:
            while True:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                try:
                    item.conn.close()
                except Exception:
                    pass
            self._created_count = 0

    def _create(self) -> PoolItem:
        conn = self._factory()
        self._created_count += 1
        return PoolItem(conn)
