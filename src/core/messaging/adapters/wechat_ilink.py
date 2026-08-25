"""Translate the WeChat iLink protocol into the common messaging contract."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from src.core.integrations.ilink import (
    ILinkAPIError,
    ILinkClient,
    ILinkError,
    MAX_IMAGE_BYTES,
    PartialDeliveryError as ILinkPartialDeliveryError,
    SessionExpired,
    USER_MESSAGE_TYPE,
    extract_text_and_image,
)
from src.core.messaging.contracts import (
    DIRECT,
    GROUP,
    AttachmentRef,
    ChannelCapabilities,
    DeliveryEndpoint,
    InboundMessage,
    OutboundMessage,
)
from src.core.messaging.errors import (
    AuthenticationExpired,
    PartialDeliveryError,
    RecipientUnavailable,
    TransientTransportError,
    UnsupportedCapability,
)


LOGGER = logging.getLogger(__name__)


WECHAT_ILINK = "wechat_ilink"


class WeChatILinkAdapter:
    platform = WECHAT_ILINK
    capabilities = ChannelCapabilities(
        receive_text=True,
        receive_image=True,
        send_text=True,
        send_image=True,
        typing=True,
        proactive=True,
        reply=True,
        direct=True,
        group=False,
        max_image_bytes=MAX_IMAGE_BYTES,
    )

    def __init__(
        self,
        client: ILinkClient,
        channel_id: str = "wechat-main",
        retry_seconds: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
        token_resolver: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self.client = client
        self.channel_id = channel_id
        self.retry_seconds = retry_seconds
        self._sleep = sleeper
        # Resolves the freshest WeChat context_token for a recipient_id from
        # the recipients store. Proactive results are delivered minutes after
        # enqueue, by which point the token frozen on the endpoint has expired;
        # this lets a send fall back to the latest token instead of silently
        # failing with RecipientUnavailable.
        self.token_resolver = token_resolver

    @property
    def account_id(self) -> str:
        credentials = self.client.credentials
        if credentials is None or not credentials.bot_id:
            raise AuthenticationExpired("微信渠道尚未完成登录")
        return credentials.bot_id

    def _resolve_token(self, endpoint: DeliveryEndpoint) -> str:
        """Return a usable context_token, preferring the endpoint's own then
        the resolver's latest value."""
        token = str(endpoint.route_context.get("context_token") or "").strip()
        if not token and self.token_resolver is not None:
            token = str(self.token_resolver(endpoint.recipient_id) or "").strip()
        if not token:
            raise RecipientUnavailable("微信收件上下文缺失，等待用户再次私聊机器人")
        return token

    @staticmethod
    def _event_id(message: Dict[str, Any]) -> str:
        for key in ("message_id", "msg_id", "client_id", "seq"):
            value = str(message.get(key) or "").strip()
            if value:
                return value
        encoded = json.dumps(
            message,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def normalize(self, message: Dict[str, Any]) -> InboundMessage:
        if message.get("message_type") != USER_MESSAGE_TYPE:
            raise ValueError("不是微信用户消息")
        sender_id = str(message.get("from_user_id") or "").strip()
        if not sender_id:
            raise ValueError("微信消息缺少发送者")
        context_token = str(message.get("context_token") or "").strip()
        group_id = str(
            message.get("group_id")
            or message.get("chatroom_id")
            or message.get("room_id")
            or ""
        ).strip()
        # 仅以群字段判定群组消息：个人微信协议常把私聊标成 single/p2p/c2c 等，
        # 若依赖 chat_type 白名单极易误判为群聊，进而被 channels 的 private_only
        # 策略静默忽略。此处与 ilink.is_private_user_message 的判定对齐（只看群字段）。
        conversation_type = GROUP if group_id else DIRECT
        # 缺少 context_token 不再静默丢弃：记日志后照常归一化，回复时由
        # store.latest_context_token 兜底；最坏情况也只是回复阶段抛出可见的
        # "上下文缺失"错误文本，而不是整条消息被无声吞掉。
        if conversation_type == DIRECT and not context_token:
            LOGGER.warning(
                "微信私聊消息缺少 context_token，回复可能失败（sender=%s）",
                sender_id,
            )
        conversation_id = group_id or sender_id
        text, image_item = extract_text_and_image(message)
        attachments: Tuple[AttachmentRef, ...] = ()
        if image_item is not None:
            attachments = (
                AttachmentRef(
                    kind="image",
                    adapter_ref=dict(image_item),
                ),
            )
        raw_time = message.get("create_time") or message.get("timestamp")
        occurred_at = datetime.now(timezone.utc).isoformat()
        if raw_time:
            try:
                numeric = float(raw_time)
                if numeric > 10_000_000_000:
                    numeric /= 1000
                occurred_at = datetime.fromtimestamp(
                    numeric, timezone.utc
                ).isoformat()
            except (TypeError, ValueError, OSError):
                pass
        return InboundMessage(
            event_id=self._event_id(message),
            channel_id=self.channel_id,
            platform=self.platform,
            account_id=self.account_id,
            sender_id=sender_id,
            conversation_type=conversation_type,
            conversation_id=conversation_id,
            text=text,
            attachments=attachments,
            addressed_to_bot=conversation_type == DIRECT,
            reply_context=(
                {"context_token": context_token} if context_token else {}
            ),
            occurred_at=occurred_at,
        )

    @staticmethod
    def _translate(exc: ILinkError) -> Exception:
        if isinstance(exc, SessionExpired):
            return AuthenticationExpired("微信登录凭证已失效")
        if isinstance(exc, ILinkPartialDeliveryError):
            return PartialDeliveryError(str(exc))
        if isinstance(exc, ILinkAPIError) and exc.recipient_context_expired:
            return RecipientUnavailable(
                "微信收件上下文已失效，等待用户再次私聊机器人"
            )
        return TransientTransportError(str(exc))

    def _send_with_token(
        self,
        endpoint: DeliveryEndpoint,
        message: OutboundMessage,
        token: str,
    ) -> None:
        if message.image_bytes is not None:
            self.client.send_image(
                endpoint.recipient_id,
                token,
                message.image_bytes,
                caption=message.text,
                client_id=message.idempotency_key or None,
            )
        else:
            self.client.send_text(
                endpoint.recipient_id,
                token,
                message.text,
                client_id=message.idempotency_key or None,
            )

    def send(
        self,
        endpoint: DeliveryEndpoint,
        message: OutboundMessage,
    ) -> None:
        if endpoint.channel_id != self.channel_id:
            raise UnsupportedCapability("消息端点不属于当前微信渠道")
        token = self._resolve_token(endpoint)
        try:
            self._send_with_token(endpoint, message, token)
        except ILinkError as exc:
            translated = self._translate(exc)
            # Token likely expired: retry once with the freshest resolver token
            # before giving up (covers proactive results enqueued minutes ago).
            if (
                isinstance(translated, RecipientUnavailable)
                and self.token_resolver is not None
            ):
                fresh = str(
                    self.token_resolver(endpoint.recipient_id) or ""
                ).strip()
                if fresh and fresh != token:
                    self._send_with_token(endpoint, message, fresh)
                    return
            raise translated from exc

    @contextmanager
    def typing(self, endpoint: DeliveryEndpoint) -> Iterator[None]:
        token = self._resolve_token(endpoint)
        errors: List[ILinkError] = []
        try:
            with self.client.typing(
                endpoint.recipient_id,
                token,
                on_error=errors.append,
            ):
                yield
        except ILinkError as exc:
            translated = self._translate(exc)
            if (
                isinstance(translated, RecipientUnavailable)
                and self.token_resolver is not None
            ):
                fresh = str(
                    self.token_resolver(endpoint.recipient_id) or ""
                ).strip()
                if fresh and fresh != token:
                    with self.client.typing(
                        endpoint.recipient_id,
                        fresh,
                        on_error=errors.append,
                    ):
                        yield
                    return
            raise translated from exc

    def load_attachment(self, attachment: AttachmentRef) -> bytes:
        if attachment.kind != "image":
            raise UnsupportedCapability("微信适配器暂不支持该附件类型")
        try:
            return self.client.download_image(dict(attachment.adapter_ref))
        except ILinkError as exc:
            raise self._translate(exc) from exc

    def start(self, emit, stop_event) -> None:
        cursor = ""
        while not stop_event.is_set():
            try:
                updates = self.client.get_updates(cursor)
            except SessionExpired as exc:
                raise AuthenticationExpired("微信登录凭证已失效") from exc
            except ILinkError:
                if stop_event.wait(self.retry_seconds):
                    return
                continue
            if updates.get("get_updates_buf"):
                cursor = str(updates["get_updates_buf"])
            for raw in updates.get("msgs") or []:
                if stop_event.is_set():
                    return
                if not isinstance(raw, dict):
                    continue
                try:
                    emit(self.normalize(raw))
                except ValueError as exc:
                    LOGGER.warning("微信消息被丢弃：%s（原始=%s）", exc, raw)
                    continue

    def close(self) -> None:
        self.client.close()
