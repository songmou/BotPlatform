"""Run channel receivers in parallel and dispatch a durable inbox serially."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List

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


class ChannelManager:
    """Own adapter lifecycles; only the inbox consumer invokes business logic."""

    def __init__(
        self,
        adapters: List[MessagingAdapter],
        inbox: MessageInboxStore,
        handler: Callable[[InboundMessage], None],
        poll_interval_seconds: float = 0.2,
    ) -> None:
        if not adapters:
            raise ValueError("至少需要一个已启用消息渠道")
        self.adapters = list(adapters)
        self.inbox = inbox
        self.handler = handler
        self.poll_interval_seconds = poll_interval_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: List[threading.Thread] = []
        self._status_lock = threading.Lock()
        self._statuses: Dict[str, ChannelStatus] = {}

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _set_status(
        self,
        adapter: MessagingAdapter,
        state: str,
        detail: str = "",
    ) -> None:
        with self._status_lock:
            self._statuses[adapter.channel_id] = ChannelStatus(
                channel_id=adapter.channel_id,
                platform=adapter.platform,
                state=state,
                detail=detail[:500],
                updated_at=self._timestamp(),
            )

    def emit(self, message: InboundMessage) -> None:
        if self.inbox.enqueue(message):
            self._wake.set()

    def _receive(self, adapter: MessagingAdapter) -> None:
        self._set_status(adapter, "running")
        try:
            adapter.start(self.emit, self._stop)
        except AuthenticationExpired as exc:
            self._set_status(adapter, "authentication_required", str(exc))
        except Exception as exc:
            LOGGER.exception("消息渠道退出 channel=%s", adapter.channel_id)
            self._set_status(adapter, "failed", type(exc).__name__)
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
                if message.conversation_type != DIRECT:
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
        for thread in list(self._threads[1:]):
            while thread.is_alive() and not self._stop.is_set():
                thread.join(timeout=0.5)
        if all(
            status.state in {"authentication_required", "failed", "stopped"}
            for status in self.statuses()
        ):
            auth = next(
                (
                    status
                    for status in self.statuses()
                    if status.state == "authentication_required"
                ),
                None,
            )
            if auth:
                raise AuthenticationExpired(auth.detail)

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
        with self._status_lock:
            return [
                self._statuses[key]
                for key in sorted(self._statuses)
            ]
