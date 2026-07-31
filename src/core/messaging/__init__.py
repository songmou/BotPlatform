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
from .manager import ChannelManager, ChannelStatus, ChannelStatusRegistry
from .store import ChannelAddressStore, ChannelBindingError, MessageInboxStore
from .credentials import (
    ChannelCredentialError,
    ChannelCredentialStore,
    validate_channel_credentials,
)
from .providers import (
    ChannelProvider,
    build_channel_adapter,
    channel_provider,
    list_channel_providers,
)

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
    "ChannelStatusRegistry",
    "ChannelAddressStore",
    "ChannelBindingError",
    "MessageInboxStore",
    "ChannelCredentialError",
    "ChannelCredentialStore",
    "validate_channel_credentials",
    "ChannelProvider",
    "build_channel_adapter",
    "channel_provider",
    "list_channel_providers",
]
