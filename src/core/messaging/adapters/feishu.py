"""Feishu/Lark long-connection messaging adapter."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

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


FEISHU = "feishu"

# The lark-channel-sdk WebSocket client drives a process-wide module-level
# event loop, so only one Feishu connection can be active per process.
_FEISHU_SDK_LOCK = threading.Lock()


def _value(item: Any, *names: str, default: Any = "") -> Any:
    for name in names:
        if isinstance(item, Mapping) and name in item:
            return item[name]
        value = getattr(item, name, None)
        if value is not None:
            return value
    return default


def _content_mapping(message: Any) -> Dict[str, Any]:
    content = _value(message, "content", default={})
    if isinstance(content, Mapping):
        return dict(content)
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"text": content}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


class FeishuAdapter(AsyncAdapterBridge):
    platform = FEISHU
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
        max_text_chars=30_000,
        max_image_bytes=10 * 1024 * 1024,
    )

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        channel_id: str = "feishu-main",
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        super().__init__()
        self.app_id = app_id
        self.app_secret = app_secret
        self.channel_id = channel_id
        self._client_factory = client_factory or self._default_client_factory
        self._client: Any = None
        if client_factory is None:
            # The SDK's WebSocket client captures its module-level event loop
            # at import time and later drives it with run_until_complete from
            # a worker thread. Import it here (outside any running asyncio
            # loop) so the captured loop stays idle; importing it later from
            # _serve would capture the running loop and crash with
            # "This event loop is already running".
            try:
                import lark_channel.ws.client  # noqa: F401
            except ImportError:
                pass

    @property
    def account_id(self) -> str:
        return self.app_id

    @staticmethod
    def _default_client_factory(app_id: str, app_secret: str) -> Any:
        try:
            from lark_channel import FeishuChannel, SecurityConfig
        except ImportError as exc:
            raise AuthenticationExpired(
                "飞书渠道依赖未安装，请安装 lark-channel-sdk"
            ) from exc
        return FeishuChannel(
            app_id=app_id,
            app_secret=app_secret,
            security=SecurityConfig(mode="audit"),
        )

    @staticmethod
    def _timestamp(raw: Any) -> str:
        try:
            numeric = float(raw)
            if numeric > 10_000_000_000:
                numeric /= 1000
            return datetime.fromtimestamp(numeric, timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return datetime.now(timezone.utc).isoformat()

    def normalize(self, event: Any) -> InboundMessage:
        message = _value(event, "message", default=event)
        if bool(_value(event, "sender_is_bot", default=False)):
            raise ValueError("忽略机器人发送的飞书消息")
        conversation = _value(event, "conversation", default={})
        sender = _value(event, "sender", default={})
        sender_id_value = _value(
            event,
            "sender_id",
            default=_value(sender, "open_id", "sender_id", default=""),
        )
        if isinstance(sender_id_value, Mapping):
            sender_id_value = _value(
                sender_id_value,
                "open_id",
                "user_id",
                default="",
            )
        sender_id = str(sender_id_value or "").strip()
        chat_id = str(
            _value(message, "chat_id", default=_value(event, "chat_id", default=""))
            or ""
        ).strip()
        message_id = str(
            _value(
                message,
                "message_id",
                "id",
                default=_value(
                    event,
                    "message_id",
                    "event_id",
                    "id",
                    default="",
                ),
            )
            or ""
        ).strip()
        if not sender_id or not chat_id or not message_id:
            raise ValueError("飞书消息缺少发送者、会话或消息编号")
        chat_type = str(
            _value(message, "chat_type", default=_value(event, "chat_type", default=""))
            or ""
        ).lower()
        conversation_type = DIRECT if chat_type in {"p2p", "direct", "private"} else GROUP
        content = _content_mapping(message)
        text = str(
            _value(
                event,
                "body_text",
                "content_text",
                default=content.get("text", ""),
            )
            or ""
        )
        raw_mentions = _value(message, "mentions", default=_value(event, "mentions", default=[]))
        mentions = []
        if isinstance(raw_mentions, Iterable) and not isinstance(
            raw_mentions, (str, bytes, Mapping)
        ):
            for mention in raw_mentions:
                mention_id = _value(
                    mention,
                    "id",
                    "open_id",
                    "user_id",
                    default=mention,
                )
                if isinstance(mention_id, Mapping):
                    mention_id = _value(mention_id, "open_id", "user_id", default="")
                if mention_id:
                    mentions.append(str(mention_id))
                key = str(_value(mention, "key", default="") or "")
                name = str(_value(mention, "name", default="") or "")
                if key:
                    text = text.replace(key, "")
                if name:
                    text = text.replace("@{}".format(name), "")
        explicit_addressed = _value(
            event,
            "is_bot_mentioned",
            "mentioned_bot",
            default=None,
        )
        addressed = (
            True
            if conversation_type == DIRECT
            else bool(mentions) if explicit_addressed is None else bool(explicit_addressed)
        )
        message_type = str(
            _value(
                message,
                "message_type",
                "msg_type",
                "raw_content_type",
                default="",
            )
            or ""
        ).lower()
        attachments_list = []
        image_key = str(content.get("image_key") or content.get("file_key") or "")
        if message_type in {"image", "img"} and image_key:
            attachments_list.append(
                AttachmentRef(
                    kind="image",
                    adapter_ref={
                        "message_id": message_id,
                        "file_key": image_key,
                    },
                )
            )
        raw_resources = _value(event, "resources", default=[])
        if isinstance(raw_resources, Iterable) and not isinstance(
            raw_resources, (str, bytes, Mapping)
        ):
            for resource in raw_resources:
                resource_type = str(
                    _value(resource, "type", "resource_type", default="")
                    or ""
                ).lower()
                file_key = str(
                    _value(resource, "file_key", "key", default="")
                    or ""
                )
                if resource_type in {"image", "img"} and file_key:
                    attachments_list.append(
                        AttachmentRef(
                            kind="image",
                            adapter_ref={
                                "message_id": message_id,
                                "file_key": file_key,
                            },
                        )
                    )
        attachments = tuple(attachments_list[:1])
        return InboundMessage(
            event_id=message_id,
            channel_id=self.channel_id,
            platform=self.platform,
            account_id=self.account_id,
            sender_id=sender_id,
            conversation_type=conversation_type,
            conversation_id=chat_id,
            text=text.strip(),
            attachments=attachments,
            thread_id=str(
                _value(
                    event,
                    "thread_id",
                    default=_value(conversation, "thread_id", default=""),
                )
                or ""
            ),
            mentions=tuple(mentions),
            addressed_to_bot=addressed,
            reply_context={"message_id": message_id},
            occurred_at=self._timestamp(
                _value(message, "create_time", default=_value(event, "create_time", default=""))
            ),
        )

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        text = str(exc).strip() or type(exc).__name__
        lowered = text.lower()
        code = str(getattr(exc, "code", "") or getattr(exc, "status_code", ""))
        if any(word in lowered for word in ("unauthorized", "app_secret", "token invalid")):
            return AuthenticationExpired("飞书应用凭据无效")
        if code == "429" or "rate limit" in lowered or "频率" in text:
            return RateLimited("飞书消息发送频率受限")
        if code.startswith("4") and code != "429":
            return PermanentDeliveryError("飞书拒绝消息：{}".format(text))
        return TransientTransportError("飞书连接失败：{}".format(text))

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
        if not _FEISHU_SDK_LOCK.acquire(blocking=False):
            raise RuntimeError(
                "同一进程同时只能运行一个飞书连接，请先停用其他飞书连接"
            )
        try:
            self._client = self._client_factory(self.app_id, self.app_secret)

            async def receive(event: Any) -> None:
                if stop_event.is_set():
                    return
                try:
                    message = self.normalize(event)
                except ValueError:
                    return
                emit(message)

            on = getattr(self._client, "on", None)
            if not callable(on):
                raise AuthenticationExpired("飞书渠道 SDK 不支持消息监听")
            on("message", receive)

            async def watch_stop() -> None:
                await loop.run_in_executor(None, stop_event.wait)
                await self._disconnect()

            watcher = asyncio.create_task(watch_stop())
            try:
                connect = getattr(self._client, "connect", None)
                if not callable(connect):
                    raise AuthenticationExpired("飞书渠道 SDK 不支持长连接")
                result = connect()
                if asyncio.iscoroutine(result):
                    await result
                if not stop_event.is_set():
                    await loop.run_in_executor(None, stop_event.wait)
            finally:
                watcher.cancel()
                await self._disconnect()
        finally:
            _FEISHU_SDK_LOCK.release()
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
            raise TransientTransportError("飞书渠道尚未连接")
        target = endpoint.target_id
        options = {}
        reply_to = str(endpoint.route_context.get("message_id") or "")
        if reply_to:
            options["reply_to"] = reply_to
        if message.image_bytes is not None:
            send = getattr(self._client, "send", None)
            if not callable(send):
                raise UnsupportedCapability("当前飞书 SDK 不支持发送消息")
            image: Dict[str, Any] = {"source": message.image_bytes}
            if message.text.strip():
                image["caption"] = message.text
            result = send(target, {"image": image}, options)
            if asyncio.iscoroutine(result):
                result = await result
            self._raise_failed_result(result)
            return
        send = getattr(self._client, "send", None)
        if not callable(send):
            raise UnsupportedCapability("当前飞书 SDK 不支持发送消息")
        result = send(target, {"markdown": message.text}, options)
        if asyncio.iscoroutine(result):
            result = await result
        self._raise_failed_result(result)

    @staticmethod
    def _raise_failed_result(result: Any) -> None:
        if result is None or bool(_value(result, "success", default=True)):
            return
        error = _value(result, "error", default="发送失败")
        code = str(_value(error, "code", default="") or "")
        text = str(_value(error, "message", default=error) or "发送失败")
        if code == "rate_limited":
            raise RateLimited("飞书消息发送频率受限")
        if code in {"permission_denied", "target_revoked"}:
            raise PermanentDeliveryError("飞书拒绝消息：{}".format(text))
        raise TransientTransportError("飞书发送失败：{}".format(text))

    def send(self, endpoint: DeliveryEndpoint, message: OutboundMessage) -> None:
        if endpoint.channel_id != self.channel_id:
            raise UnsupportedCapability("消息端点不属于当前飞书渠道")
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
            raise TransientTransportError("飞书渠道尚未连接")
        loader = getattr(self._client, "download_resource", None)
        if not callable(loader):
            raise UnsupportedCapability("当前飞书 SDK 不支持下载图片")
        reference = dict(attachment.adapter_ref)
        result = loader(
            reference.get("file_key", ""),
            resource_type="image",
            message_id=reference.get("message_id") or None,
        )
        if asyncio.iscoroutine(result):
            result = await result
        data = _value(result, "data", "buffer", "content", default=result)
        if not isinstance(data, (bytes, bytearray)):
            raise PermanentDeliveryError("飞书图片数据格式无效")
        return bytes(data)

    def load_attachment(self, attachment: AttachmentRef) -> bytes:
        if attachment.kind != "image":
            raise UnsupportedCapability("飞书适配器暂不支持该附件类型")
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
