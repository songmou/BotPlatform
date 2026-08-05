"""Schema introspection with TTL caching.

Caches table/column metadata per datasource to avoid redundant round-trips
to the remote database.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple


class SchemaCache:
    """Thread-safe TTL cache for datasource schema snapshots.

    Usage:
        cache = SchemaCache(ttl_seconds=900)
        snapshot = cache.get("ds1")  # returns cached or None
        cache.set("ds1", tables)      # store fresh snapshot
        cache.invalidate("ds1")       # purge one entry
        cache.clear()                 # purge all
    """

    def __init__(self, ttl_seconds: int = 900) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.RLock()
        self._store: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

    def get(self, datasource_id: str) -> Optional[List[Dict[str, Any]]]:
        """Return cached tables if fresh, otherwise None."""
        with self._lock:
            entry = self._store.get(datasource_id)
            if entry is None:
                return None
            fetched_at, tables = entry
            if time.time() - fetched_at > self._ttl:
                del self._store[datasource_id]
                return None
            return tables

    def set(self, datasource_id: str, tables: List[Dict[str, Any]]) -> None:
        """Store a fresh snapshot."""
        with self._lock:
            self._store[datasource_id] = (time.time(), tables)

    def invalidate(self, datasource_id: str) -> None:
        """Remove a specific entry."""
        with self._lock:
            self._store.pop(datasource_id, None)

    def clear(self) -> None:
        """Purge all entries."""
        with self._lock:
            self._store.clear()
