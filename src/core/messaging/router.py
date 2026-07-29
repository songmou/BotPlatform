"""Route channel-neutral operations to an in-process adapter registry."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Dict, Iterable, Iterator

from .contracts import (
    AttachmentRef,
    ChannelCapabilities,
    DeliveryEndpoint,
    MessagingAdapter,
    OutboundMessage,
)
from .errors import UnsupportedCapability


class MessageRouter:
    def __init__(self, adapters: Iterable[MessagingAdapter] = ()) -> None:
        self._adapters: Dict[str, MessagingAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: MessagingAdapter) -> None:
        if adapter.channel_id in self._adapters:
            raise ValueError("渠道实例编号重复：{}".format(adapter.channel_id))
        self._adapters[adapter.channel_id] = adapter

    def reset(self, adapters: Iterable[MessagingAdapter] = ()) -> None:
        """Replace the registered adapters in place.

        Long-lived holders (such as ``NotificationService``) keep their
        reference to this router across channel re-logins; swapping the
        adapter set here repoints delivery at the freshly authenticated
        clients without rebuilding the router. Closing the previous adapters
        remains the caller's responsibility.
        """
        self._adapters = {}
        for adapter in adapters:
            self.register(adapter)

    def adapter(self, channel_id: str) -> MessagingAdapter:
        try:
            return self._adapters[channel_id]
        except KeyError as exc:
            raise UnsupportedCapability("渠道未启用：{}".format(channel_id)) from exc

    def send(self, endpoint: DeliveryEndpoint, message: OutboundMessage) -> None:
        self.adapter(endpoint.channel_id).send(endpoint, message)

    @contextmanager
    def typing(self, endpoint: DeliveryEndpoint) -> Iterator[None]:
        adapter = self.adapter(endpoint.channel_id)
        if not adapter.capabilities.typing:
            yield
            return
        with adapter.typing(endpoint):
            yield

    def load_attachment(
        self,
        channel_id: str,
        attachment: AttachmentRef,
    ) -> bytes:
        return self.adapter(channel_id).load_attachment(attachment)

    def capabilities(self, channel_id: str) -> ChannelCapabilities:
        return self.adapter(channel_id).capabilities

    def close(self) -> None:
        for adapter in reversed(list(self._adapters.values())):
            adapter.close()

    @property
    def adapters(self) -> Dict[str, MessagingAdapter]:
        return dict(self._adapters)
