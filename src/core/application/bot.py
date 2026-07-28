"""Channel-neutral message handling and legacy WeChat presentation helpers."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from src.core.infrastructure.logging import log_interaction
from src.core.integrations.ilink import (
    Credentials,
    ILinkClient,
    ILinkError,
    SessionExpired,
    extract_text_and_image,
    is_private_user_message,
)
from src.core.integrations.images import ImageSource
from src.core.messaging import (
    DIRECT,
    AttachmentRef,
    ChannelAddressStore,
    ChannelCapabilities,
    DeliveryEndpoint,
    InboundMessage,
    MessageRouter,
    MessagingError,
    OutboundMessage,
)
from src.core.modeling import ModelError
from src.core.paths import CREDENTIALS_PATH
from src.core.services.agent import AgentService
from src.core.services.knowledge import KnowledgeService
from src.core.services.integration import IntegrationService
from src.core.services.memory import MemoryService
from src.core.plugins import PluginError
from src.core.services.script import ScriptService
from src.core.services.notification import NotificationDispatcher, TenantRecipientStore
from src.core.tooling import ApprovalRequired, ToolError
from src.core.storage.tenants import (
    ConversationStore,
    ScheduleStore,
    TenantContext,
    TenantRegistry,
    TenantStoreError,
    new_confirmation_code,
)
APPROVAL_TIMEOUT_TEXT = (
    "确认已超时，已默认按“不同意”处理；以上本机操作均未执行。"
)
APPROVAL_WORDS = {"同意", "确认"}
DENIAL_WORDS = {"不同意", "拒绝", "取消"}


def load_credentials(path: Path = CREDENTIALS_PATH) -> Optional[Credentials]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Credentials.from_dict(data)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ILinkError("微信凭证文件无效：{}".format(exc)) from exc


def save_credentials(credentials: Credentials, path: Path = CREDENTIALS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    payload = json.dumps(credentials.to_dict(), ensure_ascii=False, indent=2)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(temp_path), flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
        os.replace(str(temp_path), str(path))
        os.chmod(str(path), 0o600)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def delete_credentials(path: Path = CREDENTIALS_PATH) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def display_qr_code(content: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=2)
        qr.add_data(content)
        qr.make(fit=True)
        print("\n请使用微信扫描下方二维码，并在手机上确认：\n")
        for row in qr.get_matrix():
            print("".join("\033[40m  \033[0m" if cell else "\033[47m  \033[0m" for cell in row))
    except Exception as exc:
        print("终端二维码生成失败：{}".format(exc))
    print("\n二维码内容：{}\n".format(content))


def print_login_status(status: str) -> None:
    labels = {
        "wait": "等待扫码……",
        "scaned": "已扫码，请在手机上确认……",
        "expired": "二维码已过期，正在刷新……",
        "confirmed": "微信登录成功。",
    }
    if status in labels:
        print(labels[status])


class _LegacyILinkAdapter:
    """Compatibility wrapper for callers that still pass an ILink-like client."""

    channel_id = "wechat-main"
    platform = "wechat_ilink"
    capabilities = ChannelCapabilities(
        receive_text=True,
        receive_image=True,
        send_text=True,
        send_image=True,
        typing=True,
        proactive=True,
    )

    def __init__(self, client: Any) -> None:
        self.client = client

    @property
    def account_id(self) -> str:
        credentials = getattr(self.client, "credentials", None)
        return str(getattr(credentials, "bot_id", "") or "legacy-bot")

    def send(self, endpoint: DeliveryEndpoint, message: OutboundMessage) -> None:
        token = str(endpoint.route_context.get("context_token") or "")
        if message.image_bytes is not None and hasattr(self.client, "send_image"):
            self.client.send_image(
                endpoint.recipient_id,
                token,
                message.image_bytes,
                caption=message.text,
            )
        else:
            self.client.send_text(endpoint.recipient_id, token, message.text)

    @contextmanager
    def typing(self, endpoint: DeliveryEndpoint):
        token = str(endpoint.route_context.get("context_token") or "")
        with self.client.typing(endpoint.recipient_id, token, on_error=None):
            yield

    def load_attachment(self, attachment: AttachmentRef) -> bytes:
        return self.client.download_image(dict(attachment.adapter_ref))

    def start(self, _emit, _stop_event) -> None:
        raise RuntimeError("兼容适配器不负责接收循环")

    def close(self) -> None:
        closer = getattr(self.client, "close", None)
        if callable(closer):
            closer()


class MessageBot:
    def __init__(
        self,
        ilink: Optional[ILinkClient],
        agent_service: AgentService,
        interaction_logger: Callable[[str, str, str], None] = log_interaction,
        recipient_recorder: Optional[Callable[[str, str], None]] = None,
        timer_factory: Callable[[float, Callable[[], None]], Any] = threading.Timer,
        tenant_registry: Optional[TenantRegistry] = None,
        recipient_store: Optional[TenantRecipientStore] = None,
        conversation_store: Optional[ConversationStore] = None,
        schedule_store: Optional[ScheduleStore] = None,
        schedule_ids: Optional[Sequence[str]] = None,
        script_service: Optional[ScriptService] = None,
        knowledge_service: Optional[KnowledgeService] = None,
        memory_service: Optional[MemoryService] = None,
        codex_tasks_plugin: Optional[Any] = None,
        integration_service: Optional[IntegrationService] = None,
        notification_dispatcher: Optional[NotificationDispatcher] = None,
        message_router: Optional[MessageRouter] = None,
        address_store: Optional[ChannelAddressStore] = None,
    ) -> None:
        self.ilink = ilink
        self._legacy_mode = message_router is None
        self._legacy_adapter = _LegacyILinkAdapter(ilink) if ilink is not None else None
        self.message_router = message_router or MessageRouter(
            [self._legacy_adapter] if self._legacy_adapter is not None else []
        )
        self.address_store = address_store
        self.agent_service = agent_service
        self._log = interaction_logger
        self._record_recipient = recipient_recorder
        self._timer_factory = timer_factory
        self.tenant_registry = tenant_registry
        self.recipient_store = recipient_store
        self.conversation_store = conversation_store
        self.schedule_store = schedule_store
        self.schedule_ids = set(schedule_ids or [])
        self.script_service = script_service
        self.knowledge_service = knowledge_service
        self.memory_service = memory_service
        self.codex_tasks_plugin = codex_tasks_plugin
        self.integration_service = integration_service
        self.notification_dispatcher = notification_dispatcher
        self._approval_timer_lock = threading.Lock()
        self._approval_timers: Dict[str, Tuple[str, Any]] = {}
        self._deletion_pending: Dict[str, Tuple[str, datetime]] = {}
        self._memory_clear_pending: Dict[str, Tuple[str, datetime]] = {}

    @staticmethod
    def _subject_key(subject: Any) -> str:
        return subject.tenant_id if isinstance(subject, TenantContext) else str(subject)

    def _resolve_tenant(self, message: InboundMessage) -> Optional[TenantContext]:
        if self.tenant_registry is None:
            return None
        if self.address_store is not None:
            return self.address_store.resolve(message)
        return self.tenant_registry.resolve(message.account_id, message.sender_id)

    def _append_transcript(
        self,
        tenant: Optional[TenantContext],
        role: str,
        content: str,
        image: bool = False,
    ) -> None:
        if tenant is not None and self.conversation_store is not None:
            self.conversation_store.append_transcript(
                tenant.tenant_id, role, content, image=image
            )

    def _reply(
        self,
        endpoint: DeliveryEndpoint,
        text: str,
        tenant: Optional[TenantContext] = None,
        record: bool = True,
    ) -> None:
        self.message_router.send(endpoint, OutboundMessage(text=text))
        if record:
            self._append_transcript(tenant, "assistant", text)

    @staticmethod
    def _outcome_text(outcome: Any) -> str:
        thinking = str(getattr(outcome, "thinking", "") or "").strip()
        if not thinking:
            return outcome.text
        return "思考过程：\n{}\n\n回答：\n{}".format(thinking, outcome.text)

    @staticmethod
    def _typing_error(exc: Exception) -> None:
        print("消息渠道输入状态更新失败：{}".format(exc), file=sys.stderr)

    def _direction(self, action: str, endpoint: DeliveryEndpoint) -> str:
        if self._legacy_mode and endpoint.platform == "wechat_ilink":
            return "微信{}".format(action)
        return "消息{}[{}]".format(action, endpoint.channel_id)

    def _cancel_approval_timer(self, user_id: Any) -> None:
        key = self._subject_key(user_id)
        with self._approval_timer_lock:
            tracked = self._approval_timers.pop(key, None)
        if tracked:
            tracked[1].cancel()

    def _schedule_approval_timeout(
        self,
        subject: Any,
        endpoint: DeliveryEndpoint,
        outcome: ApprovalRequired,
    ) -> None:
        approval_id = outcome.approval_id
        key = self._subject_key(subject)
        delay = max(
            0.0,
            (outcome.expires_at - datetime.now(timezone.utc)).total_seconds(),
        )

        def expire() -> None:
            try:
                now = datetime.now(timezone.utc)
                if now < outcome.expires_at:
                    self._schedule_approval_timeout(
                        subject, endpoint, outcome
                    )
                    return
                if not self.agent_service.expire_approval(
                    subject, approval_id, now=now
                ):
                    return
                try:
                    if (
                        isinstance(subject, TenantContext)
                        and self.notification_dispatcher is not None
                    ):
                        self.notification_dispatcher.service.enqueue_text_to_tenant(
                            subject.tenant_id,
                            APPROVAL_TIMEOUT_TEXT,
                            source_type="approval_timeout",
                            source_key=approval_id,
                        )
                        self.notification_dispatcher.wake()
                    else:
                        self.message_router.send(
                            endpoint,
                            OutboundMessage(text=APPROVAL_TIMEOUT_TEXT),
                        )
                except Exception as exc:
                    print(
                        "发送确认超时通知失败：{}".format(exc),
                        file=sys.stderr,
                    )
                    return
                self._log(
                    self._direction("输出", endpoint),
                    endpoint.recipient_id,
                    APPROVAL_TIMEOUT_TEXT,
                )
            finally:
                with self._approval_timer_lock:
                    tracked = self._approval_timers.get(key)
                    if (
                        tracked
                        and tracked[0] == approval_id
                        and tracked[1] is timer
                    ):
                        self._approval_timers.pop(key, None)

        timer = self._timer_factory(delay, expire)
        if hasattr(timer, "daemon"):
            timer.daemon = True
        with self._approval_timer_lock:
            previous = self._approval_timers.get(key)
            self._approval_timers[key] = (approval_id, timer)
        if previous:
            previous[1].cancel()
        timer.start()

    def _track_approval_outcome(
        self,
        subject: Any,
        endpoint: DeliveryEndpoint,
        outcome: Any,
    ) -> None:
        if isinstance(outcome, ApprovalRequired):
            self._schedule_approval_timeout(subject, endpoint, outcome)
        else:
            self._cancel_approval_timer(subject)

    def _send_outcome(
        self,
        subject: Any,
        endpoint: DeliveryEndpoint,
        outcome: Any,
        tenant: Optional[TenantContext] = None,
    ) -> None:
        self._track_approval_outcome(subject, endpoint, outcome)
        reply = self._outcome_text(outcome)
        self._reply(endpoint, reply, tenant)
        self._log(
            self._direction("输出", endpoint),
            endpoint.recipient_id,
            reply,
        )

    def handle_message(self, message: Dict[str, Any]) -> None:
        """One-release compatibility entry point for raw iLink dictionaries."""
        if not is_private_user_message(message):
            return
        text, image_item = extract_text_and_image(message)
        inbound = InboundMessage(
            event_id=str(
                message.get("message_id")
                or message.get("msg_id")
                or message.get("client_id")
                or "legacy-{}".format(id(message))
            ),
            channel_id="wechat-main",
            platform="wechat_ilink",
            account_id=(
                self._legacy_adapter.account_id
                if self._legacy_adapter is not None
                else "legacy-bot"
            ),
            sender_id=str(message["from_user_id"]),
            conversation_type=DIRECT,
            conversation_id=str(message["from_user_id"]),
            text=text,
            attachments=(
                (AttachmentRef("image", dict(image_item)),)
                if image_item is not None
                else ()
            ),
            reply_context={"context_token": str(message["context_token"])},
        )
        self.handle_inbound(inbound)

    def handle_inbound(self, message: InboundMessage) -> None:
        if message.conversation_type != DIRECT:
            return

        user_id = message.sender_id
        endpoint = message.endpoint
        tenant = self._resolve_tenant(message)
        if tenant is not None and self.address_store is not None:
            endpoint = self.address_store.record_endpoint(tenant, message)
        subject: Any = tenant or user_id
        if (
            tenant is not None
            and self.recipient_store is not None
            and self.address_store is None
        ):
            self.recipient_store.update(
                tenant,
                str(endpoint.route_context.get("context_token") or ""),
            )
            if self.notification_dispatcher is not None:
                try:
                    self.notification_dispatcher.on_recipient_refreshed(
                        tenant.tenant_id
                    )
                except Exception as exc:
                    print(
                        "恢复主动微信通知失败：{}".format(exc),
                        file=sys.stderr,
                    )
            if self.codex_tasks_plugin is not None:
                try:
                    refresher = getattr(
                        self.codex_tasks_plugin,
                        "on_recipient_refreshed",
                        None,
                    )
                    if callable(refresher):
                        refresher(tenant.tenant_id)
                except Exception as exc:
                    print(
                        "恢复 Codex 微信通知失败：{}".format(exc),
                        file=sys.stderr,
                    )
        if self._record_recipient:
            try:
                self._record_recipient(
                    user_id,
                    str(endpoint.route_context.get("context_token") or ""),
                )
            except OSError as exc:
                print("保存最近微信用户失败：{}".format(exc), file=sys.stderr)
        text = message.text
        image_item = message.first_image

        normalized_text = text.strip()
        lowered_text = normalized_text.lower()

        if not image_item and (
            lowered_text == "/codex" or lowered_text.startswith("/codex ")
        ):
            log_text = (
                "/codex answer <已隐藏>"
                if lowered_text.startswith("/codex answer ")
                else text
            )
            self._log(self._direction("输入", endpoint), user_id, log_text)
            if tenant is None or self.codex_tasks_plugin is None:
                reply = "当前未启用 Codex 任务确认。"
            else:
                try:
                    resolver = getattr(
                        self.codex_tasks_plugin,
                        "resolve_channel_command",
                        None,
                    ) or getattr(
                        self.codex_tasks_plugin,
                        "resolve_wechat_command",
                    )
                    reply = resolver(tenant, normalized_text)
                except PluginError as exc:
                    reply = str(exc)
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if (
            tenant is not None
            and self.integration_service is not None
            and self.integration_service.has_pending(tenant)
        ):
            handled, reply = self.integration_service.consume(tenant, normalized_text)
            if handled:
                self._reply(endpoint, reply, tenant, record=False)
                self._log(self._direction("输出", endpoint), user_id, reply)
                return

        if text or image_item:
            transcript_input = text or self.agent_service.image_prompt
            self._append_transcript(
                tenant,
                "user",
                "[图片] {}".format(transcript_input) if image_item else transcript_input,
                image=bool(image_item),
            )

        if not image_item and (
            lowered_text.startswith("/approve") or lowered_text.startswith("/deny")
        ):
            self._log(self._direction("输入", endpoint), user_id, text)
            parts = normalized_text.split()
            if len(parts) != 2 or parts[0].lower() not in {"/approve", "/deny"}:
                reply = "格式错误，请使用 /approve <编号> 或 /deny <编号>。"
            else:
                try:
                    with self.message_router.typing(endpoint):
                        outcome = self.agent_service.resolve_approval(
                            subject,
                            parts[1],
                            approved=parts[0].lower() == "/approve",
                        )
                except ToolError as exc:
                    reply = str(exc)
                except ModelError as exc:
                    self._cancel_approval_timer(subject)
                    print("继续处理确认操作失败：{}".format(exc), file=sys.stderr)
                    reply = "本机操作已经处理，但模型生成后续回复失败，请重试查询结果。"
                else:
                    self._send_outcome(subject, endpoint, outcome, tenant)
                    return
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        decision_words = APPROVAL_WORDS | DENIAL_WORDS
        if (
            not image_item
            and normalized_text in decision_words
            and self.agent_service.has_pending_approval(subject)
        ):
            self._log(self._direction("输入", endpoint), user_id, text)
            try:
                with self.message_router.typing(endpoint):
                    outcome = self.agent_service.resolve_pending_approval(
                        subject,
                        approved=normalized_text in APPROVAL_WORDS,
                    )
            except ToolError as exc:
                reply = str(exc)
            except ModelError as exc:
                self._cancel_approval_timer(subject)
                print("继续处理确认操作失败：{}".format(exc), file=sys.stderr)
                reply = "本机操作已经处理，但模型生成后续回复失败，请重试查询结果。"
            else:
                self._send_outcome(subject, endpoint, outcome, tenant)
                return
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if not image_item and lowered_text == "/id":
            reply = (
                "你的租户编号：{}".format(tenant.tenant_id)
                if tenant is not None
                else "当前未启用多用户存储。"
            )
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if not image_item and lowered_text == "/schedules":
            if tenant is None or self.schedule_store is None:
                reply = "当前未启用用户定时任务。"
            elif not self.schedule_ids:
                reply = "当前没有可订阅的定时任务。"
            else:
                lines = ["定时任务订阅："]
                for task_id in sorted(self.schedule_ids):
                    status = (
                        "开启"
                        if self.schedule_store.is_enabled(tenant.tenant_id, task_id)
                        else "关闭"
                    )
                    lines.append("- {}：{}".format(task_id, status))
                lines.append("使用 /schedule on|off <任务编号> 修改。")
                reply = "\n".join(lines)
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if not image_item and lowered_text == "/knowledge":
            if tenant is None or self.knowledge_service is None:
                reply = "当前未启用私人知识库。"
            else:
                sources = self.knowledge_service.list(tenant.tenant_id)
                ready = sum(1 for item in sources if item["status"] == "ready")
                pending = len(sources) - ready
                chunks = sum(int(item["chunks"]) for item in sources)
                reply = "私人知识库：{} 个来源，{} 个分块；已完成 {}，待向量化 {}。".format(
                    len(sources), chunks, ready, pending
                )
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if not image_item and (
            lowered_text == "/soul" or lowered_text.startswith("/soul ")
        ):
            if tenant is None or self.memory_service is None:
                reply = "当前未启用长期用户画像。"
            elif lowered_text not in {"/soul", "/soul rebuild"}:
                reply = "格式错误，请使用 /soul 或 /soul rebuild。"
            else:
                try:
                    profile = self.memory_service.get_soul(
                        tenant.tenant_id,
                        force_rebuild=lowered_text == "/soul rebuild",
                    )
                    reply = (
                        "长期用户画像（修订 {}，更新时间 {}）：\n{}"
                    ).format(
                        profile["revision"],
                        profile.get("updated_at") or "未知",
                        str(profile["content"]).strip(),
                    )
                except Exception as exc:
                    print("读取长期用户画像失败：{}".format(exc), file=sys.stderr)
                    reply = "长期用户画像暂时不可用，正常聊天不受影响。"
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if not image_item and (lowered_text == "/memory" or lowered_text.startswith("/memory ")):
            if tenant is None or self.memory_service is None:
                reply = "当前未启用长期记忆。"
            else:
                parts = normalized_text.split()
                if len(parts) == 1:
                    items = self.memory_service.list(tenant.tenant_id)
                    lines = ["长期记忆：{} 项。".format(len(items))]
                    for item in items[:20]:
                        label = "有效" if item["status"] == "active" else "待确认"
                        lines.append("- {} [{}] {}：{}".format(
                            str(item["memory_id"])[:8], label, item["kind"], item["content"]
                        ))
                    lines.append("使用 /memory confirm <编号>、/memory forget <编号> 或 /memory clear 管理。")
                    reply = "\n".join(lines)
                elif len(parts) == 3 and parts[1].lower() == "confirm":
                    reply = (
                        "已确认该项长期记忆。"
                        if self.memory_service.confirm(tenant.tenant_id, parts[2])
                        else "未找到唯一的待确认记忆编号。"
                    )
                elif len(parts) == 3 and parts[1].lower() == "forget":
                    reply = (
                        "已停用该项长期记忆。"
                        if self.memory_service.forget(tenant.tenant_id, parts[2])
                        else "未找到唯一的有效记忆编号。"
                    )
                elif len(parts) == 2 and parts[1].lower() == "clear":
                    code = new_confirmation_code()
                    self._memory_clear_pending[tenant.tenant_id] = (
                        code, datetime.now(timezone.utc) + timedelta(minutes=5)
                    )
                    reply = "此操作会停用全部长期记忆。如确定，请在 5 分钟内回复 /memory clear {}。".format(code)
                elif len(parts) == 3 and parts[1].lower() == "clear":
                    pending = self._memory_clear_pending.get(tenant.tenant_id)
                    if not pending or datetime.now(timezone.utc) >= pending[1] or parts[2] != pending[0]:
                        self._memory_clear_pending.pop(tenant.tenant_id, None)
                        reply = "长期记忆清除确认无效或已经过期。"
                    else:
                        self._memory_clear_pending.pop(tenant.tenant_id, None)
                        count = self.memory_service.clear(tenant.tenant_id)
                        reply = "已停用全部长期记忆，共 {} 项。".format(count)
                else:
                    reply = "格式错误，请使用 /memory、/memory confirm <编号>、/memory forget <编号> 或 /memory clear。"
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if not image_item and lowered_text.startswith("/schedule "):
            parts = normalized_text.split()
            if tenant is None or self.schedule_store is None:
                reply = "当前未启用用户定时任务。"
            elif len(parts) != 3 or parts[1].lower() not in {"on", "off"}:
                reply = "格式错误，请使用 /schedule on|off <任务编号>。"
            elif parts[2] not in self.schedule_ids:
                reply = "未知或未开放的定时任务：{}。".format(parts[2])
            else:
                enabled = parts[1].lower() == "on"
                self.schedule_store.set_enabled(tenant.tenant_id, parts[2], enabled)
                reply = "已{}定时任务 {}。".format(
                    "开启" if enabled else "关闭", parts[2]
                )
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if not image_item and (
            lowered_text == "/integration" or lowered_text.startswith("/integration ")
        ):
            parts = normalized_text.split()
            if tenant is None or self.integration_service is None:
                reply = "当前未启用用户集成配置。"
            else:
                try:
                    if len(parts) == 1 or (
                        len(parts) >= 2 and parts[1].lower() == "status"
                    ):
                        reply = self.integration_service.status(
                            tenant, parts[2] if len(parts) == 3 else ""
                        )
                    elif len(parts) == 3 and parts[1].lower() == "setup":
                        reply = self.integration_service.setup(tenant, parts[2])
                    elif len(parts) == 3 and parts[1].lower() == "delete":
                        reply = self.integration_service.delete(tenant, parts[2])
                    else:
                        reply = (
                            "格式错误，请使用 /integration status [编号]、"
                            "/integration setup <编号> 或 /integration delete <编号>。"
                        )
                except ValueError as exc:
                    reply = str(exc)
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if not image_item and lowered_text == "/delete-data":
            if tenant is None:
                reply = "当前未启用多用户存储。"
            else:
                code = new_confirmation_code()
                self._deletion_pending[tenant.tenant_id] = (
                    code,
                    datetime.now(timezone.utc) + timedelta(minutes=5),
                )
                reply = (
                    "此操作会永久删除你的历史、设置、订阅、工作区、脚本产物和集成凭据。"
                    "Codex 账号级任务历史仍由 Codex 自身保存，不会随本操作删除。"
                    "如确定，请在 5 分钟内回复 /confirm-delete {}。"
                ).format(code)
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if not image_item and lowered_text.startswith("/confirm-delete"):
            parts = normalized_text.split()
            pending = self._deletion_pending.get(tenant.tenant_id) if tenant else None
            if tenant is None or self.tenant_registry is None:
                reply = "当前未启用多用户存储。"
            elif len(parts) != 2 or not pending or datetime.now(timezone.utc) >= pending[1]:
                self._deletion_pending.pop(tenant.tenant_id, None)
                reply = "删除确认无效或已经过期，请重新使用 /delete-data。"
            elif parts[1] != pending[0]:
                reply = "删除确认码不匹配。"
            else:
                self._deletion_pending.pop(tenant.tenant_id, None)
                self._memory_clear_pending.pop(tenant.tenant_id, None)
                self._cancel_approval_timer(subject)
                try:
                    if self.script_service is not None:
                        self.script_service.cancel_tenant(tenant.tenant_id)
                    close_resources = getattr(
                        self.agent_service, "close_tenant_resources", None
                    )
                    if callable(close_resources):
                        close_resources(tenant.tenant_id)
                    if self.integration_service is not None:
                        self.integration_service.delete_all(tenant)
                    self.tenant_registry.delete(tenant)
                except (OSError, ValueError, TenantStoreError) as exc:
                    reply = "暂时无法删除用户数据：{}。请稍后重试。".format(exc)
                else:
                    reply = (
                        "你的全部 BotPlatform 租户数据已经删除；下次私聊将创建新的租户编号。"
                        "Codex 账号级任务历史仍需在 Codex 中单独管理。"
                    )
                    self._reply(endpoint, reply, None, record=False)
                    self._log(self._direction("输出", endpoint), user_id, reply)
                    return
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if not image_item and lowered_text == "/clear":
            self._log(self._direction("输入", endpoint), user_id, text)
            self.agent_service.clear_history(subject)
            self._cancel_approval_timer(subject)
            reply = "对话上下文已清空。"
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return
        if not image_item and lowered_text == "/agent":
            self._log(self._direction("输入", endpoint), user_id, text)
            reply = self.agent_service.describe_active()
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return
        if not image_item and (
            lowered_text == "/model" or lowered_text.startswith("/model ")
        ):
            self._log(self._direction("输入", endpoint), user_id, text)
            parts = normalized_text.split()
            try:
                if len(parts) == 1:
                    reply = self.agent_service.model_status(subject)
                elif len(parts) == 2:
                    reply = self.agent_service.set_model_mode(subject, parts[1])
                    self._cancel_approval_timer(subject)
                else:
                    reply = "格式错误，请使用 /model 或 /model auto|local|flash|pro。"
            except ModelError as exc:
                reply = str(exc)
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return
        if not image_item and lowered_text == "/tools":
            self._log(self._direction("输入", endpoint), user_id, text)
            reply = self.agent_service.tools_text(subject)
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return
        if not image_item and lowered_text == "/help":
            self._log(self._direction("输入", endpoint), user_id, text)
            reply = self.agent_service.help_text()
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return
        if not text and not image_item:
            return

        question = text or self.agent_service.image_prompt
        input_log = "[图片] {}".format(question) if image_item else question
        self._log(self._direction("输入", endpoint), user_id, input_log)
        image_bytes: Optional[bytes] = None
        try:
            with self.message_router.typing(endpoint):
                if image_item:
                    image_bytes = self.message_router.load_attachment(
                        message.channel_id,
                        image_item,
                    )
                outcome = self.agent_service.chat(
                    subject, question, image_bytes=image_bytes
                )
            answer = self._outcome_text(outcome)
        except SessionExpired:
            raise
        except (ILinkError, MessagingError, ModelError) as exc:
            print("处理用户消息失败：{}".format(exc), file=sys.stderr)
            error_reply = "处理消息失败：{}。请稍后重试。".format(exc)
            self._reply(endpoint, error_reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, error_reply)
            return

        self._track_approval_outcome(subject, endpoint, outcome)
        self._reply(endpoint, answer, tenant)
        self._log(self._direction("输出", endpoint), user_id, answer)

    def run(self) -> None:
        cursor = ""
        print("机器人已启动，正在等待微信消息。按 Ctrl+C 退出。")
        while True:
            try:
                updates = self.ilink.get_updates(cursor)
            except SessionExpired:
                raise
            except ILinkError as exc:
                print("接收微信消息失败：{}；2 秒后重试。".format(exc), file=sys.stderr)
                time.sleep(2.0)
                continue

            if updates.get("get_updates_buf"):
                cursor = str(updates["get_updates_buf"])
            for message in updates.get("msgs") or []:
                try:
                    self.handle_message(message)
                except SessionExpired:
                    raise
                except ILinkError as exc:
                    print("回复微信消息失败：{}".format(exc), file=sys.stderr)


# Compatibility name retained for one release.
WeChatBot = MessageBot
