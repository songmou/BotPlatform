"""Stable data contracts shared by the bot core and channel adapters."""

from __future__ import annotations

import base64
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple


DIRECT = "direct"
GROUP = "group"
CHANNEL = "channel"
THREAD = "thread"
CONVERSATION_TYPES = {DIRECT, GROUP, CHANNEL, THREAD}


def _mapping(value: Mapping[str, Any]) -> Dict[str, Any]:
    return {str(key): item for key, item in value.items()}


@dataclass(frozen=True)
class AttachmentRef:
    kind: str
    adapter_ref: Mapping[str, Any]
    content_type: str = ""
    filename: str = ""
    size_bytes: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "adapter_ref": _mapping(self.adapter_ref),
            "content_type": self.content_type,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AttachmentRef":
        raw_ref = value.get("adapter_ref")
        if not isinstance(raw_ref, Mapping):
            raise ValueError("附件缺少适配器引用")
        raw_size = value.get("size_bytes")
        return cls(
            kind=str(value.get("kind") or ""),
            adapter_ref=_mapping(raw_ref),
            content_type=str(value.get("content_type") or ""),
            filename=str(value.get("filename") or ""),
            size_bytes=int(raw_size) if raw_size is not None else None,
        )


@dataclass(frozen=True)
class DeliveryEndpoint:
    channel_id: str
    platform: str
    account_id: str
    conversation_type: str
    conversation_id: str
    recipient_id: str
    thread_id: str = ""
    route_context: Mapping[str, Any] = field(default_factory=dict)
    endpoint_id: str = ""

    def __post_init__(self) -> None:
        if self.conversation_type not in CONVERSATION_TYPES:
            raise ValueError("未知会话类型：{}".format(self.conversation_type))
        if not all(
            (
                self.channel_id,
                self.platform,
                self.account_id,
                self.conversation_id,
                self.recipient_id,
            )
        ):
            raise ValueError("消息端点字段不完整")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "platform": self.platform,
            "account_id": self.account_id,
            "conversation_type": self.conversation_type,
            "conversation_id": self.conversation_id,
            "recipient_id": self.recipient_id,
            "thread_id": self.thread_id,
            "route_context": _mapping(self.route_context),
            "endpoint_id": self.endpoint_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DeliveryEndpoint":
        route = value.get("route_context")
        if not isinstance(route, Mapping):
            route = {}
        return cls(
            channel_id=str(value.get("channel_id") or ""),
            platform=str(value.get("platform") or ""),
            account_id=str(value.get("account_id") or ""),
            conversation_type=str(value.get("conversation_type") or DIRECT),
            conversation_id=str(value.get("conversation_id") or ""),
            recipient_id=str(value.get("recipient_id") or ""),
            thread_id=str(value.get("thread_id") or ""),
            route_context=_mapping(route),
            endpoint_id=str(value.get("endpoint_id") or ""),
        )


@dataclass(frozen=True)
class InboundMessage:
    event_id: str
    channel_id: str
    platform: str
    account_id: str
    sender_id: str
    conversation_type: str
    conversation_id: str
    text: str = ""
    attachments: Tuple[AttachmentRef, ...] = ()
    thread_id: str = ""
    mentions: Tuple[str, ...] = ()
    reply_context: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        if self.conversation_type not in CONVERSATION_TYPES:
            raise ValueError("未知会话类型：{}".format(self.conversation_type))
        if not all(
            (
                self.event_id,
                self.channel_id,
                self.platform,
                self.account_id,
                self.sender_id,
                self.conversation_id,
            )
        ):
            raise ValueError("入站消息字段不完整")

    @property
    def endpoint(self) -> DeliveryEndpoint:
        return DeliveryEndpoint(
            channel_id=self.channel_id,
            platform=self.platform,
            account_id=self.account_id,
            conversation_type=self.conversation_type,
            conversation_id=self.conversation_id,
            recipient_id=self.sender_id,
            thread_id=self.thread_id,
            route_context=self.reply_context,
        )

    @property
    def first_image(self) -> Optional[AttachmentRef]:
        return next(
            (attachment for attachment in self.attachments if attachment.kind == "image"),
            None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "channel_id": self.channel_id,
            "platform": self.platform,
            "account_id": self.account_id,
            "sender_id": self.sender_id,
            "conversation_type": self.conversation_type,
            "conversation_id": self.conversation_id,
            "text": self.text,
            "attachments": [item.to_dict() for item in self.attachments],
            "thread_id": self.thread_id,
            "mentions": list(self.mentions),
            "reply_context": _mapping(self.reply_context),
            "occurred_at": self.occurred_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InboundMessage":
        raw_attachments = value.get("attachments")
        attachments: Sequence[Any] = (
            raw_attachments if isinstance(raw_attachments, list) else []
        )
        raw_context = value.get("reply_context")
        if not isinstance(raw_context, Mapping):
            raw_context = {}
        raw_mentions = value.get("mentions")
        mentions = raw_mentions if isinstance(raw_mentions, list) else []
        return cls(
            event_id=str(value.get("event_id") or ""),
            channel_id=str(value.get("channel_id") or ""),
            platform=str(value.get("platform") or ""),
            account_id=str(value.get("account_id") or ""),
            sender_id=str(value.get("sender_id") or ""),
            conversation_type=str(value.get("conversation_type") or DIRECT),
            conversation_id=str(value.get("conversation_id") or ""),
            text=str(value.get("text") or ""),
            attachments=tuple(
                AttachmentRef.from_dict(item)
                for item in attachments
                if isinstance(item, Mapping)
            ),
            thread_id=str(value.get("thread_id") or ""),
            mentions=tuple(str(item) for item in mentions),
            reply_context=_mapping(raw_context),
            occurred_at=str(value.get("occurred_at") or ""),
        )


@dataclass(frozen=True)
class OutboundMessage:
    text: str = ""
    image_bytes: Optional[bytes] = None
    image_content_type: str = ""
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        if not self.text.strip() and not self.image_bytes:
            raise ValueError("出站消息不能为空")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "image_base64": (
                base64.b64encode(self.image_bytes).decode("ascii")
                if self.image_bytes is not None
                else None
            ),
            "image_content_type": self.image_content_type,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class ChannelCapabilities:
    receive_text: bool = True
    receive_image: bool = False
    send_text: bool = True
    send_image: bool = False
    typing: bool = False
    proactive: bool = False
    direct: bool = True
    group: bool = False
    max_image_bytes: Optional[int] = None
    max_text_chars: Optional[int] = None


class MessagingAdapter(Protocol):
    channel_id: str
    platform: str
    account_id: str
    capabilities: ChannelCapabilities

    def start(
        self,
        emit: Callable[[InboundMessage], None],
        stop_event: Any,
    ) -> None: ...

    def send(
        self,
        endpoint: DeliveryEndpoint,
        message: OutboundMessage,
    ) -> None: ...

    def typing(self, endpoint: DeliveryEndpoint) -> AbstractContextManager[Any]: ...

    def load_attachment(self, attachment: AttachmentRef) -> bytes: ...

    def close(self) -> None: ...
