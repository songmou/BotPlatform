"""Generic "script awaiting input" registry.

When a script result declares it is waiting for user input (for example a
CAPTCHA), the platform registers a :class:`PendingScriptInput` here and routes
the user's next direct reply back to the script's resume entry point instead of
handing it to the model. This is a self-contained, dependency-free module so it
can be unit-tested and reused by any script, not just CTS OA.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass
class PendingScriptInput:
    """A script is paused, waiting for the user to supply ``param``."""

    tenant_id: str
    session_key: str
    run_id: str
    script_id: str
    script_name: str
    param: str
    prompt: str
    hint: str
    expires_at: float


class ScriptInputRegistry:
    """In-memory registry of scripts paused for user input.

    Keyed by ``(tenant_id, session_key)`` (one pending input per session, mirroring
    the proactive-notification routing). Entries auto-expire; an expired entry is
    treated as absent on the next lookup.
    """

    def __init__(self) -> None:
        self._items: Dict[Tuple[str, str], PendingScriptInput] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(tenant_id: str, session_key: str = "direct") -> Tuple[str, str]:
        return (tenant_id, session_key)

    def register(
        self,
        tenant_id: str,
        run_id: str,
        script_id: str,
        script_name: str,
        await_input: Dict[str, Any],
        session_key: str = "direct",
        now: Optional[float] = None,
    ) -> Optional[PendingScriptInput]:
        """Register a pending input from a script's ``await_input`` payload.

        Returns the created :class:`PendingScriptInput`, or ``None`` when the
        payload is missing the required ``param`` field.
        """
        param = await_input.get("param")
        if not isinstance(param, str) or not param:
            return None
        try:
            ttl = float(await_input.get("ttl_seconds", 300))
        except (TypeError, ValueError):
            ttl = 300.0
        if ttl <= 0:
            ttl = 300.0
        stamp = now if now is not None else time.time()
        pending = PendingScriptInput(
            tenant_id=tenant_id,
            session_key=session_key,
            run_id=run_id,
            script_id=script_id,
            script_name=script_name,
            param=param,
            prompt=str(await_input.get("prompt", "")),
            hint=str(await_input.get("hint", "")),
            expires_at=stamp + ttl,
        )
        with self._lock:
            self._items[self._key(tenant_id, session_key)] = pending
        return pending

    def peek(
        self,
        tenant_id: str,
        session_key: str = "direct",
        now: Optional[float] = None,
    ) -> Optional[PendingScriptInput]:
        """Return the pending input without consuming it; clears expired entries."""
        stamp = now if now is not None else time.time()
        with self._lock:
            key = self._key(tenant_id, session_key)
            pending = self._items.get(key)
            if pending is None:
                return None
            if stamp >= pending.expires_at:
                self._items.pop(key, None)
                return None
            return pending

    def consume(
        self,
        tenant_id: str,
        session_key: str = "direct",
        now: Optional[float] = None,
    ) -> Optional[PendingScriptInput]:
        """Return and remove the pending input; clears expired entries."""
        stamp = now if now is not None else time.time()
        with self._lock:
            key = self._key(tenant_id, session_key)
            pending = self._items.get(key)
            if pending is None:
                return None
            if stamp >= pending.expires_at:
                self._items.pop(key, None)
                return None
            return self._items.pop(key, None)

    def clear(self, tenant_id: str, session_key: str = "direct") -> None:
        """Drop any pending input for the session."""
        with self._lock:
            self._items.pop(self._key(tenant_id, session_key), None)
