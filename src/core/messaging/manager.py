"""Run channel receivers in parallel and dispatch a durable inbox serially."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional

from .contracts import DIRECT, InboundMessage, MessagingAdapter
from .errors import AuthenticationExpired, MessagingError
from .store import MessageInboxStore


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelStatus:
    channel_id: str
    platform: str
    state: str
    detail: str = ""
    updated_at: str = ""


class ChannelStatusRegistry:
    """Thread-safe live status snapshot shared with the management API."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._statuses: Dict[str, ChannelStatus] = {}

    def set(
        self,
        channel_id: str,
        platform: str,
        state: str,
        detail: str = "",
    ) -> None:
        with self._lock:
            self._statuses[channel_id] = ChannelStatus(
                channel_id=channel_id,
                platform=platform,
                state=state,
                detail=detail[:500],
                updated_at=datetime.now(timezone.utc).isoformat(),
            )

    def get(self, channel_id: str) -> Optional[ChannelStatus]:
        with self._lock:
            return self._statuses.get(channel_id)

    def snapshot(
        self,
        channel_ids: Optional[Iterable[str]] = None,
    ) -> List[ChannelStatus]:
        with self._lock:
            keys = (
                sorted(set(channel_ids))
                if channel_ids is not None
                else sorted(self._statuses)
            )
            return [
                self._statuses[key]
                for key in keys
                if key in self._statuses
            ]


class ChannelManager:
    """Own adapter lifecycles; only the inbox consumer invokes business logic."""

    def __init__(
        self,
        adapters: List[MessagingAdapter],
        inbox: MessageInboxStore,
        handler: Callable[[InboundMessage], None],
        poll_interval_seconds: float = 0.2,
        status_registry: Optional[ChannelStatusRegistry] = None,
    ) -> None:
        self.adapters = list(adapters)
        self.inbox = inbox
        self.handler = handler
        self.poll_interval_seconds = poll_interval_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: List[threading.Thread] = []
        self.status_registry = status_registry or ChannelStatusRegistry()

    def _set_status(
        self,
        adapter: MessagingAdapter,
        state: str,
        detail: str = "",
    ) -> None:
        self.status_registry.set(
            adapter.channel_id,
            adapter.platform,
            state,
            detail,
        )

    def emit(self, message: InboundMessage) -> None:
        if self.inbox.enqueue(message):
            self._wake.set()

    def _receive(self, adapter: MessagingAdapter) -> None:
        self._set_status(adapter, "running", "渠道接收循环已启动")
        try:
            adapter.start(self.emit, self._stop)
        except AuthenticationExpired as exc:
            self._set_status(adapter, "authentication_required", str(exc))
        except Exception as exc:
            LOGGER.exception("消息渠道退出 channel=%s", adapter.channel_id)
            self._set_status(adapter, "failed", str(exc) or type(exc).__name__)
        else:
            self._set_status(adapter, "stopped")

    def _consume(self) -> None:
        while not self._stop.is_set():
            row = self.inbox.claim()
            if row is None:
                self._wake.wait(self.poll_interval_seconds)
                self._wake.clear()
                continue
            inbox_id = int(row["inbox_id"])
            try:
                message = self.inbox.decode(row)
                if (
                    message.conversation_type != DIRECT
                    and not message.addressed_to_bot
                ):
                    self.inbox.finish(inbox_id, "ignored")
                    continue
                self.handler(message)
            except (MessagingError, OSError) as exc:
                self.inbox.finish(inbox_id, "retry", str(exc))
            except ValueError as exc:
                self.inbox.finish(inbox_id, "failed", str(exc))
            except Exception as exc:
                LOGGER.exception(
                    "处理入站消息失败 channel=%s event=%s",
                    row["channel_id"],
                    row["event_id"],
                )
                self.inbox.finish(inbox_id, "retry", type(exc).__name__)
            else:
                self.inbox.finish(inbox_id, "done")

    def start(self) -> None:
        if self._threads:
            return
        self.inbox.cleanup()
        self._stop.clear()
        consumer = threading.Thread(
            target=self._consume,
            name="message-inbox",
            daemon=True,
        )
        self._threads.append(consumer)
        consumer.start()
        for adapter in self.adapters:
            thread = threading.Thread(
                target=self._receive,
                args=(adapter,),
                name="channel-{}".format(adapter.channel_id),
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def run(self) -> None:
        self.start()
        while not self._stop.is_set():
            self._stop.wait(0.5)

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        for adapter in reversed(self.adapters):
            try:
                adapter.close()
            except Exception:  # noqa: BLE001 - best effort on shutdown
                LOGGER.warning("关闭消息适配器 %s 失败", adapter.channel_id, exc_info=True)
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=5.0)
        self._threads.clear()

    def statuses(self) -> List[ChannelStatus]:
        return self.status_registry.snapshot(
            adapter.channel_id for adapter in self.adapters
        )
