"""Enterprise WeChat intelligent-bot WebSocket adapter."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

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
    PermanentDeliveryError,
    RateLimited,
    TransientTransportError,
    UnsupportedCapability,
)

from .async_base import AsyncAdapterBridge


WECOM_AIBOT = "wecom_aibot"


def _value(item: Any, *names: str, default: Any = "") -> Any:
    for name in names:
        if isinstance(item, Mapping) and name in item:
            return item[name]
        value = getattr(item, name, None)
        if value is not None:
            return value
    return default


def _mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


class WeComAIBotAdapter(AsyncAdapterBridge):
    platform = WECOM_AIBOT
    capabilities = ChannelCapabilities(
        receive_text=True,
        receive_image=True,
        send_text=True,
        send_image=True,
        typing=False,
        proactive=True,
        direct=True,
        group=True,
        group_mentions=True,
        reply=True,
        max_text_chars=20_000,
        max_image_bytes=10 * 1024 * 1024,
    )

    def __init__(
        self,
        bot_id: str,
        secret: str,
        *,
        channel_id: str = "wecom-main",
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        super().__init__()
        self.bot_id = bot_id
        self.secret = secret
        self.channel_id = channel_id
        self._client_factory = client_factory or self._default_client_factory
        self._client: Any = None

    @property
    def account_id(self) -> str:
        return self.bot_id

    @staticmethod
    def _default_client_factory(bot_id: str, secret: str) -> Any:
        try:
            from wecom_aibot_sdk import WSClient
        except ImportError as exc:
            raise AuthenticationExpired(
                "企业微信渠道依赖未安装，请安装 wecom-aibot-sdk"
            ) from exc
        return WSClient(bot_id=bot_id, secret=secret)

    @staticmethod
    def _timestamp(raw: Any) -> str:
        try:
            numeric = float(raw)
            if numeric > 10_000_000_000:
                numeric /= 1000
            return datetime.fromtimestamp(numeric, timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return datetime.now(timezone.utc).isoformat()

    def normalize(self, frame: Any) -> InboundMessage:
        body = _mapping(_value(frame, "body", default={}))
        headers = _mapping(_value(frame, "headers", default={}))
        message = _mapping(body.get("message") or body)
        text_item = _mapping(message.get("text") or body.get("text"))
        mixed = _mapping(message.get("mixed") or body.get("mixed"))
        raw_sender = (
            message.get("from")
            or message.get("sender")
            or body.get("from")
            or body.get("sender")
        )
        sender = _mapping(raw_sender)
        sender_id = str(
            message.get("from_user_id")
            or message.get("userid")
            or sender.get("userid")
            or sender.get("user_id")
            or sender.get("id")
            or (raw_sender if isinstance(raw_sender, str) else "")
            or ""
        ).strip()
        chat_id = str(
            message.get("chatid")
            or message.get("chat_id")
            or body.get("chatid")
            or body.get("chat_id")
            or sender_id
        ).strip()
        event_id = str(
            _value(frame, "req_id", "request_id", default="")
            or headers.get("req_id")
            or message.get("msgid")
            or message.get("message_id")
            or body.get("msgid")
            or ""
        ).strip()
        if not sender_id or not chat_id or not event_id:
            raise ValueError("企业微信消息缺少发送者、会话或事件编号")
        chat_type = str(
            message.get("chat_type")
            or message.get("chattype")
            or body.get("chat_type")
            or body.get("chattype")
            or ""
        ).lower()
        conversation_type = (
            DIRECT
            if chat_type in {"single", "direct", "private", "p2p", "1", ""}
            and chat_id == sender_id
            else GROUP
        )
        text = str(
            text_item.get("content")
            or message.get("content")
            or mixed.get("text")
            or ""
        )
        if text.startswith("{"):
            try:
                decoded = json.loads(text)
                if isinstance(decoded, Mapping):
                    text = str(decoded.get("content") or decoded.get("text") or text)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        raw_mentions = (
            message.get("mentions")
            or body.get("mentions")
            or text_item.get("mentioned_list")
            or []
        )
        mentions = []
        if isinstance(raw_mentions, Iterable) and not isinstance(
            raw_mentions, (str, bytes, Mapping)
        ):
            for mention in raw_mentions:
                mention_id = _value(
                    mention,
                    "userid",
                    "user_id",
                    "id",
                    default=mention,
                )
                if mention_id:
                    mentions.append(str(mention_id))
                name = str(_value(mention, "name", default="") or "")
                if name:
                    text = text.replace("@{}".format(name), "")
        explicit = (
            message.get("is_bot_mentioned")
            if "is_bot_mentioned" in message
            else body.get("is_bot_mentioned")
        )
        addressed = (
            True
            if conversation_type == DIRECT
            else True if explicit is None else bool(explicit)
        )
        attachments: Tuple[AttachmentRef, ...] = ()
        image = _mapping(
            message.get("image")
            or body.get("image")
            or mixed.get("image")
        )
        if image:
            reference = {
                key: image[key]
                for key in ("url", "aeskey", "aes_key", "file_id", "media_id")
                if image.get(key)
            }
            if reference:
                attachments = (
                    AttachmentRef(kind="image", adapter_ref=reference),
                )
        return InboundMessage(
            event_id=event_id,
            channel_id=self.channel_id,
            platform=self.platform,
            account_id=self.account_id,
            sender_id=sender_id,
            conversation_type=conversation_type,
            conversation_id=chat_id,
            text=text.strip(),
            attachments=attachments,
            mentions=tuple(mentions),
            addressed_to_bot=addressed,
            reply_context={"req_id": event_id},
            occurred_at=self._timestamp(
                message.get("create_time")
                or body.get("create_time")
                or body.get("timestamp")
            ),
        )

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        text = str(exc).strip() or type(exc).__name__
        lowered = text.lower()
        code = str(getattr(exc, "code", "") or getattr(exc, "status_code", ""))
        if any(
            word in lowered
            for word in ("unauthorized", "authenticate", "bot secret", "invalid secret")
        ):
            return AuthenticationExpired("企业微信机器人凭据无效")
        if code == "429" or "rate limit" in lowered or "频率" in text:
            return RateLimited("企业微信消息发送频率受限")
        if code.startswith("4") and code != "429":
            return PermanentDeliveryError("企业微信拒绝消息：{}".format(text))
        return TransientTransportError("企业微信连接失败：{}".format(text))

    async def _disconnect(self) -> None:
        client = self._client
        if client is None:
            return
        disconnect = getattr(client, "disconnect", None) or getattr(client, "close", None)
        if callable(disconnect):
            result = disconnect()
            if asyncio.iscoroutine(result):
                await result

    async def _serve(self, emit, stop_event) -> None:
        loop = asyncio.get_running_loop()
        self._set_loop(loop)
        try:
            self._client = self._client_factory(self.bot_id, self.secret)

            async def receive(frame: Any) -> None:
                if stop_event.is_set():
                    return
                try:
                    message = self.normalize(frame)
                except ValueError:
                    return
                emit(message)

            on = getattr(self._client, "on", None)
            if not callable(on):
                raise AuthenticationExpired("企业微信 SDK 不支持消息监听")
            for event_name in ("message.text", "message.image", "message.mixed"):
                on(event_name, receive)

            async def watch_stop() -> None:
                await loop.run_in_executor(None, stop_event.wait)
                await self._disconnect()

            watcher = asyncio.create_task(watch_stop())
            try:
                connect = getattr(self._client, "connect", None)
                if not callable(connect):
                    raise AuthenticationExpired("企业微信 SDK 不支持长连接")
                result = connect()
                if asyncio.iscoroutine(result):
                    await result
                if not stop_event.is_set():
                    await loop.run_in_executor(None, stop_event.wait)
            finally:
                watcher.cancel()
                await self._disconnect()
        finally:
            self._clear_loop()

    def start(self, emit, stop_event) -> None:
        try:
            asyncio.run(self._serve(emit, stop_event))
        except AuthenticationExpired:
            raise
        except Exception as exc:
            raise self._translate(exc) from exc

    async def _send_async(
        self,
        endpoint: DeliveryEndpoint,
        message: OutboundMessage,
    ) -> None:
        if self._client is None:
            raise TransientTransportError("企业微信渠道尚未连接")
        target = endpoint.target_id
        if message.image_bytes is not None:
            uploader = getattr(self._client, "upload_media", None)
            if not callable(uploader):
                raise UnsupportedCapability("当前企业微信 SDK 不支持图片上传")
            uploaded = uploader(
                message.image_bytes,
                type="image",
                filename="image.png",
            )
            if asyncio.iscoroutine(uploaded):
                uploaded = await uploaded
            media_id = str(
                _value(uploaded, "media_id", "file_id", default=uploaded) or ""
            )
            sender = getattr(self._client, "send_media_message", None)
            if not callable(sender):
                raise UnsupportedCapability("当前企业微信 SDK 不支持发送图片")
            result = sender(target, "image", media_id)
            if asyncio.iscoroutine(result):
                await result
            if not message.text.strip():
                return
        sender = getattr(self._client, "send_message", None)
        if not callable(sender):
            raise UnsupportedCapability("当前企业微信 SDK 不支持发送消息")
        body = {"msgtype": "markdown", "markdown": {"content": message.text}}
        result = sender(target, body)
        if asyncio.iscoroutine(result):
            await result

    def send(self, endpoint: DeliveryEndpoint, message: OutboundMessage) -> None:
        if endpoint.channel_id != self.channel_id:
            raise UnsupportedCapability("消息端点不属于当前企业微信渠道")
        try:
            self._run_sync(self._send_async(endpoint, message))
        except (
            AuthenticationExpired,
            PermanentDeliveryError,
            RateLimited,
            TransientTransportError,
            UnsupportedCapability,
        ):
            raise
        except Exception as exc:
            raise self._translate(exc) from exc

    async def _load_attachment_async(self, attachment: AttachmentRef) -> bytes:
        if self._client is None:
            raise TransientTransportError("企业微信渠道尚未连接")
        loader = getattr(self._client, "download_file", None)
        if not callable(loader):
            raise UnsupportedCapability("当前企业微信 SDK 不支持下载图片")
        reference = dict(attachment.adapter_ref)
        result = loader(
            reference.get("url", ""),
            reference.get("aeskey") or reference.get("aes_key"),
        )
        if asyncio.iscoroutine(result):
            result = await result
        data = _value(result, "buffer", "data", "content", default=result)
        if not isinstance(data, (bytes, bytearray)):
            raise PermanentDeliveryError("企业微信图片数据格式无效")
        return bytes(data)

    def load_attachment(self, attachment: AttachmentRef) -> bytes:
        if attachment.kind != "image":
            raise UnsupportedCapability("企业微信适配器暂不支持该附件类型")
        try:
            return self._run_sync(self._load_attachment_async(attachment))
        except Exception as exc:
            if isinstance(
                exc,
                (
                    PermanentDeliveryError,
                    TransientTransportError,
                    UnsupportedCapability,
                ),
            ):
                raise
            raise self._translate(exc) from exc

    def close(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._run_sync(self._disconnect(), timeout=5.0)
            except Exception:
                pass
