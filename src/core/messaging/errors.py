"""Channel-neutral messaging failures."""

from __future__ import annotations

from typing import Optional


class MessagingError(RuntimeError):
    """Base class for failures exposed by a messaging adapter."""


class AuthenticationExpired(MessagingError):
    """The channel credentials are no longer usable."""


class TransientTransportError(MessagingError):
    """A retryable network or remote-service failure."""


class RateLimited(TransientTransportError):
    """The remote service asked the caller to retry later."""

    def __init__(self, message: str, retry_after_seconds: Optional[float] = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class RecipientUnavailable(MessagingError):
    """The saved delivery endpoint must be refreshed by a new interaction."""


class PermanentDeliveryError(MessagingError):
    """The message cannot be delivered without changing its target or content."""


class UnsupportedCapability(PermanentDeliveryError):
    """The selected adapter does not support the requested operation."""


class PartialDeliveryError(PermanentDeliveryError):
    """Some parts of a multipart outbound message were already delivered."""
