"""Channel-neutral messaging contracts and runtime services."""

from .contracts import (
    CHANNEL,
    DIRECT,
    GROUP,
    THREAD,
    AttachmentRef,
    ChannelCapabilities,
    DeliveryEndpoint,
    InboundMessage,
    MessagingAdapter,
    OutboundMessage,
)
from .errors import (
    AuthenticationExpired,
    MessagingError,
    PartialDeliveryError,
    PermanentDeliveryError,
    RateLimited,
    RecipientUnavailable,
    TransientTransportError,
    UnsupportedCapability,
)
from .router import MessageRouter
from .manager import ChannelManager, ChannelStatus
from .store import ChannelAddressStore, MessageInboxStore

__all__ = [
    "CHANNEL",
    "DIRECT",
    "GROUP",
    "THREAD",
    "AttachmentRef",
    "ChannelCapabilities",
    "DeliveryEndpoint",
    "InboundMessage",
    "MessagingAdapter",
    "OutboundMessage",
    "AuthenticationExpired",
    "MessagingError",
    "PartialDeliveryError",
    "PermanentDeliveryError",
    "RateLimited",
    "RecipientUnavailable",
    "TransientTransportError",
    "UnsupportedCapability",
    "MessageRouter",
    "ChannelManager",
    "ChannelStatus",
    "ChannelAddressStore",
    "MessageInboxStore",
]
