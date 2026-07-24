"""BotAdapter Protocol, canonical message DTOs, and error types."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable


@dataclass(frozen=True)
class InboundMessage:
    user_id: str
    reply_token: str
    text: str
    image_ref: Optional[Any] = None
    raw: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class BotIdentity:
    channel: str
    bot_id: str
    user_id: str


class BotAdapterError(RuntimeError):
    """Recoverable adapter error — retry."""


class SessionExpiredError(BotAdapterError):
    """Terminal — re-login required."""


@runtime_checkable
class BotAdapter(Protocol):
    @property
    def identity(self) -> BotIdentity: ...

    @property
    def is_connected(self) -> bool: ...

    def login(
        self,
        show_qr: Callable[[str], None],
        status_changed: Optional[Callable[[str], None]] = None,
    ) -> None: ...

    def get_updates(self, cursor: str) -> Tuple[str, List[InboundMessage]]: ...

    def send_text(self, user_id: str, reply_token: str, text: str) -> None: ...

    def send_image(
        self,
        user_id: str,
        reply_token: str,
        image_bytes: bytes,
        caption: str = "",
    ) -> None: ...

    def download_image(self, image_ref: Any) -> bytes: ...

    def typing(
        self,
        user_id: str,
        reply_token: str,
        on_error: Optional[Callable] = None,
    ) -> AbstractContextManager: ...

    def close(self) -> None: ...
