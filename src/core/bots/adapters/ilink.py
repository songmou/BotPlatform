"""iLink bot adapter — wraps ILinkClient with BotAdapter interface."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Callable, List, Optional, Tuple

from src.core.bots.base import BotIdentity, BotAdapterError, InboundMessage, SessionExpiredError
from src.core.integrations.ilink import (
    Credentials,
    ILinkClient,
    ILinkError,
    SessionExpired,
    extract_text_and_image,
    is_private_user_message,
)


def inbound_from_raw(msg) -> Optional[InboundMessage]:
    """Convert a raw iLink message dict to an InboundMessage; None if not a private user message."""
    if not is_private_user_message(msg):
        return None
    text, image_item = extract_text_and_image(msg)
    return InboundMessage(
        user_id=str(msg["from_user_id"]),
        reply_token=str(msg["context_token"]),
        text=text,
        image_ref=image_item,
        raw=msg,
    )


class ILinkAdapter:
    """BotAdapter implementation wrapping the existing ILinkClient."""

    def __init__(
        self,
        credentials: Optional[Credentials] = None,
        client: Optional[ILinkClient] = None,
    ) -> None:
        self._client = client or ILinkClient(credentials=credentials)

    @property
    def identity(self) -> BotIdentity:
        c = self._client.credentials
        if c is None:
            return BotIdentity(channel="ilink", bot_id="", user_id="")
        return BotIdentity(channel="ilink", bot_id=c.bot_id, user_id=c.user_id)

    @property
    def is_connected(self) -> bool:
        return self._client.credentials is not None

    def login(
        self,
        show_qr: Callable[[str], None],
        status_changed: Optional[Callable[[str], None]] = None,
    ) -> Credentials:
        try:
            return self._client.login(show_qr, status_changed=status_changed)
        except SessionExpired as exc:
            raise SessionExpiredError(str(exc)) from exc
        except ILinkError as exc:
            raise BotAdapterError(str(exc)) from exc

    def get_updates(self, cursor: str) -> Tuple[str, List[InboundMessage]]:
        try:
            raw = self._client.get_updates(cursor)
        except SessionExpired as exc:
            raise SessionExpiredError(str(exc)) from exc
        except ILinkError as exc:
            raise BotAdapterError(str(exc)) from exc

        new_cursor = (
            str(raw["get_updates_buf"])
            if raw.get("get_updates_buf")
            else cursor
        )
        messages: List[InboundMessage] = []
        for msg in raw.get("msgs") or []:
            inbound = inbound_from_raw(msg)
            if inbound is not None:
                messages.append(inbound)
        return new_cursor, messages

    def send_text(self, user_id: str, reply_token: str, text: str) -> None:
        try:
            self._client.send_text(user_id, reply_token, text)
        except SessionExpired as exc:
            raise SessionExpiredError(str(exc)) from exc
        except ILinkError as exc:
            raise BotAdapterError(str(exc)) from exc

    def send_image(
        self,
        user_id: str,
        reply_token: str,
        image_bytes: bytes,
        caption: str = "",
    ) -> None:
        try:
            self._client.send_image(user_id, reply_token, image_bytes, caption)
        except SessionExpired as exc:
            raise SessionExpiredError(str(exc)) from exc
        except ILinkError as exc:
            raise BotAdapterError(str(exc)) from exc

    def download_image(self, image_ref: any) -> bytes:
        try:
            return self._client.download_image(image_ref)
        except ILinkError as exc:
            raise BotAdapterError(str(exc)) from exc

    def typing(
        self,
        user_id: str,
        reply_token: str,
        on_error: Optional[Callable] = None,
    ) -> AbstractContextManager:
        return self._client.typing(user_id, reply_token, on_error=on_error)

    def close(self) -> None:
        self._client.close()
