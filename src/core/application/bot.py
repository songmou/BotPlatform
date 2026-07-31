"""Channel-neutral message handling for the bot core."""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from src.core.infrastructure.logging import log_interaction
from src.core.integrations.ilink import (
    Credentials,
    ILinkError,
    SessionExpired,
)
from src.core.messaging import (
    DIRECT,
    ChannelAddressStore,
    ChannelBindingError,
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
from src.core.config.loader import ChannelConfig
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


class MessageBot:
    def __init__(
        self,
        agent_service: AgentService,
        message_router: MessageRouter,
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
        integration_service: Optional[IntegrationService] = None,
        notification_dispatcher: Optional[NotificationDispatcher] = None,
        address_store: Optional[ChannelAddressStore] = None,
        channel_configs: Optional[Dict[str, ChannelConfig]] = None,
    ) -> None:
        self.message_router = message_router
        self.address_store = address_store
        self.channel_configs = dict(channel_configs or {})
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
        self.integration_service = integration_service
        self.notification_dispatcher = notification_dispatcher
        self._approval_timer_lock = threading.Lock()
        self._approval_timers: Dict[str, Tuple[str, Any]] = {}
        self._deletion_pending: Dict[str, Tuple[str, datetime]] = {}
        self._memory_clear_pending: Dict[str, Tuple[str, datetime]] = {}
        self._message_context = threading.local()

    @staticmethod
    def _subject_key(subject: Any) -> str:
        if isinstance(subject, TenantContext):
            return subject.personal_tenant_id or subject.tenant_id
        return str(subject)

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
                tenant.personal_tenant_id or tenant.tenant_id,
                role,
                content,
                image=image,
                session_key=str(
                    getattr(self._message_context, "session_key", "direct")
                    or "direct"
                ),
            )

    def _channel_config(self, channel_id: str) -> Optional[ChannelConfig]:
        return self.channel_configs.get(channel_id)

    def _channel_agent_id(self, channel_id: str) -> Optional[str]:
        config = self._channel_config(channel_id)
        return (config.agent_id or None) if config is not None else None

    def _accept_message(self, message: InboundMessage) -> bool:
        if message.conversation_type == DIRECT:
            return True
        config = self._channel_config(message.channel_id)
        policy = (
            str(config.settings.get("group_policy") or "mention_only")
            if config is not None
            else "mention_only"
        )
        return policy == "mention_only" and message.addressed_to_bot

    @staticmethod
    def _session_key(message: InboundMessage) -> str:
        if message.conversation_type == DIRECT:
            return "direct"
        return "{}:{}:{}:{}".format(
            message.channel_id,
            message.conversation_type,
            message.conversation_id,
            message.sender_id,
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

    def handle_inbound(self, message: InboundMessage) -> None:
        if not self._accept_message(message):
            return

        user_id = message.sender_id
        endpoint = message.endpoint
        self._message_context.session_key = self._session_key(message)
        normalized_text = message.text.strip()
        lowered_text = normalized_text.lower()
        tenant: Optional[TenantContext] = None
        binding_completed = False
        if (
            message.conversation_type == DIRECT
            and lowered_text.startswith("/bind ")
            and self.address_store is not None
        ):
            parts = normalized_text.split()
            if len(parts) != 2:
                reply = "格式错误，请使用 /bind <绑定码>。"
                self._reply(endpoint, reply, None, record=False)
                self._log(self._direction("输出", endpoint), user_id, reply)
                return
            try:
                tenant = self.address_store.bind_with_code(message, parts[1])
            except ChannelBindingError as exc:
                reply = str(exc)
                self._reply(endpoint, reply, None, record=False)
                self._log(self._direction("输出", endpoint), user_id, reply)
                return
            binding_completed = True
        if tenant is None:
            try:
                tenant = self._resolve_tenant(message)
            except ChannelBindingError as exc:
                reply = str(exc)
                self._reply(endpoint, reply, None, record=False)
                self._log(self._direction("输出", endpoint), user_id, reply)
                return
        if tenant is not None and self.address_store is not None:
            endpoint = self.address_store.record_endpoint(tenant, message)
        if binding_completed:
            reply = "跨渠道身份绑定成功，当前渠道将使用同一租户数据。"
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return
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
        if tenant is not None and message.conversation_type == DIRECT:
            if self.notification_dispatcher is not None:
                try:
                    self.notification_dispatcher.on_recipient_refreshed(
                        tenant.personal_tenant_id or tenant.tenant_id
                    )
                except Exception as exc:
                    print(
                        "恢复主动微信通知失败：{}".format(exc),
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

        if (
            message.conversation_type == DIRECT
            and not image_item
            and lowered_text == "/bind"
        ):
            if tenant is None or self.address_store is None:
                reply = "当前未启用跨渠道身份绑定。"
            else:
                try:
                    code = self.address_store.issue_binding_code(tenant, message)
                except ChannelBindingError as exc:
                    reply = str(exc)
                else:
                    reply = (
                        "跨渠道绑定码：{}\n"
                        "请在 10 分钟内通过新渠道首次私聊发送 /bind {}。"
                    ).format(code, code)
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if (
            message.conversation_type == DIRECT
            and not image_item
            and lowered_text == "/claim"
        ):
            if tenant is None or self.address_store is None:
                reply = "当前未启用组织认领。"
            else:
                try:
                    token = self.address_store.issue_claim_token(message, tenant)
                except ChannelBindingError as exc:
                    reply = str(exc)
                else:
                    reply = (
                        "组织认领码：{}\n"
                        "请打开平台登录页，选择“认领组织”，并在 1 小时内完成账号创建。"
                    ).format(token)
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if (
            message.conversation_type == DIRECT
            and not image_item
            and (lowered_text == "/org" or lowered_text.startswith("/org "))
        ):
            if tenant is None or self.address_store is None:
                reply = "当前未启用组织切换。"
            else:
                parts = normalized_text.split()
                try:
                    if len(parts) == 1 or (
                        len(parts) == 2 and parts[1].lower() == "list"
                    ):
                        choices = self.address_store.organization_choices(message)
                        lines = ["可用组织："]
                        for item in choices:
                            marker = (
                                "（当前）"
                                if str(item["organization_id"]) == tenant.tenant_id
                                else ""
                            )
                            lines.append(
                                "- {} [{}] {}".format(
                                    item["name"],
                                    str(item["organization_id"])[:8],
                                    marker,
                                ).rstrip()
                            )
                        reply = "\n".join(lines)
                    elif len(parts) == 3 and parts[1].lower() == "use":
                        tenant = self.address_store.switch_organization(
                            message, parts[2]
                        )
                        subject = tenant
                        organization = getattr(
                            self.tenant_registry, "organization_store", None
                        ).get(tenant.tenant_id)
                        reply = "已切换到组织：{}。".format(organization["name"])
                    else:
                        reply = "格式错误，请使用 /org list 或 /org use <组织编号>。"
                except (ChannelBindingError, ValueError) as exc:
                    reply = str(exc)
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if (
            message.conversation_type == DIRECT
            and tenant is not None
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

        if (
            message.conversation_type != DIRECT
            and not image_item
            and normalized_text.startswith("/")
            and lowered_text != "/help"
        ):
            reply = "群聊仅支持安全问答；工具、知识库、记忆和数据管理请私聊机器人使用。"
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

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
            message.conversation_type == DIRECT
            and not image_item
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
                    private_key = tenant.personal_tenant_id or tenant.tenant_id
                    profile = self.memory_service.get_soul(
                        private_key,
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
                private_key = tenant.personal_tenant_id or tenant.tenant_id
                if len(parts) == 1:
                    items = self.memory_service.list(private_key)
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
                        if self.memory_service.confirm(private_key, parts[2])
                        else "未找到唯一的待确认记忆编号。"
                    )
                elif len(parts) == 3 and parts[1].lower() == "forget":
                    reply = (
                        "已停用该项长期记忆。"
                        if self.memory_service.forget(private_key, parts[2])
                        else "未找到唯一的有效记忆编号。"
                    )
                elif len(parts) == 2 and parts[1].lower() == "clear":
                    code = new_confirmation_code()
                    self._memory_clear_pending[private_key] = (
                        code, datetime.now(timezone.utc) + timedelta(minutes=5)
                    )
                    reply = "此操作会停用全部长期记忆。如确定，请在 5 分钟内回复 /memory clear {}。".format(code)
                elif len(parts) == 3 and parts[1].lower() == "clear":
                    pending = self._memory_clear_pending.get(private_key)
                    if not pending or datetime.now(timezone.utc) >= pending[1] or parts[2] != pending[0]:
                        self._memory_clear_pending.pop(private_key, None)
                        reply = "长期记忆清除确认无效或已经过期。"
                    else:
                        self._memory_clear_pending.pop(private_key, None)
                        count = self.memory_service.clear(private_key)
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
                private_key = tenant.personal_tenant_id or tenant.tenant_id
                self._deletion_pending[private_key] = (
                    code,
                    datetime.now(timezone.utc) + timedelta(minutes=5),
                )
                reply = (
                    "此操作会永久删除你的历史、设置、订阅、工作区、脚本产物和集成凭据。"
                    "如确定，请在 5 分钟内回复 /confirm-delete {}。"
                ).format(code)
            self._reply(endpoint, reply, tenant)
            self._log(self._direction("输出", endpoint), user_id, reply)
            return

        if not image_item and lowered_text.startswith("/confirm-delete"):
            parts = normalized_text.split()
            private_key = (
                tenant.personal_tenant_id or tenant.tenant_id
                if tenant
                else ""
            )
            pending = self._deletion_pending.get(private_key) if tenant else None
            if tenant is None or self.tenant_registry is None:
                reply = "当前未启用多用户存储。"
            elif len(parts) != 2 or not pending or datetime.now(timezone.utc) >= pending[1]:
                self._deletion_pending.pop(private_key, None)
                reply = "删除确认无效或已经过期，请重新使用 /delete-data。"
            elif parts[1] != pending[0]:
                reply = "删除确认码不匹配。"
            else:
                self._deletion_pending.pop(private_key, None)
                self._memory_clear_pending.pop(private_key, None)
                self._cancel_approval_timer(subject)
                try:
                    is_member = bool(
                        tenant.member_user_id is not None
                        and tenant.personal_tenant_id
                    )
                    if self.script_service is not None and not is_member:
                        self.script_service.cancel_tenant(tenant.tenant_id)
                    close_resources = getattr(
                        self.agent_service, "close_tenant_resources", None
                    )
                    if callable(close_resources) and not is_member:
                        close_resources(tenant.tenant_id)
                    organization_store = getattr(
                        self.tenant_registry, "organization_store", None
                    )
                    organization_backup = None
                    if not is_member and organization_store is not None:
                        with self.tenant_registry.database.read() as connection:
                            registered = connection.execute(
                                "SELECT 1 FROM organizations "
                                "WHERE organization_id=?",
                                (tenant.tenant_id,),
                            ).fetchone()
                        if registered is not None:
                            organization_backup = (
                                organization_store.backup_organization(
                                    tenant.tenant_id
                                )
                            )
                    if self.integration_service is not None:
                        self.integration_service.delete_all(tenant)
                    deletion_context = (
                        self.tenant_registry.get(tenant.personal_tenant_id)
                        if is_member
                        else tenant
                    )
                    if organization_backup is not None:
                        organization_store.delete_after_backup(tenant.tenant_id)
                    else:
                        self.tenant_registry.delete(deletion_context)
                except (OSError, ValueError, TenantStoreError) as exc:
                    reply = "暂时无法删除用户数据：{}。请稍后重试。".format(exc)
                else:
                    reply = (
                        "你在当前组织中的个人对话、记忆、待办和个人凭据已经删除；"
                        "组织共享资源及成员关系不受影响。"
                        if is_member
                        else "你的全部 BotPlatform 租户数据已经删除；下次私聊将创建新的租户编号。"
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
        if not image_item and (
            lowered_text == "/feedback" or lowered_text.startswith("/feedback ")
        ):
            analytics_store = getattr(
                self.agent_service, "model_analytics_store", None
            )
            parts = normalized_text.split()
            if tenant is None or analytics_store is None:
                reply = "当前未启用模型质量反馈。"
            elif len(parts) < 2 or parts[1] not in {"好", "差"}:
                reply = (
                    "格式错误，请使用 /feedback 好 [备注]，或 "
                    "/feedback 差 [原因] [备注]。"
                )
            else:
                run_id = analytics_store.latest_successful_run(tenant.tenant_id)
                if run_id is None:
                    reply = "暂时没有可评价的模型回答。"
                else:
                    rating = "good" if parts[1] == "好" else "bad"
                    reasons = []
                    comment_start = 2
                    if rating == "bad":
                        reason = parts[2] if len(parts) > 2 else "其他"
                        if reason not in {
                            "答非所问",
                            "事实错误",
                            "格式表达",
                            "工具执行失败",
                            "响应过慢",
                            "其他",
                        }:
                            reason = "其他"
                            comment_start = 2
                        else:
                            comment_start = 3
                        reasons = [reason]
                    comment = " ".join(parts[comment_start:])
                    try:
                        analytics_store.put_feedback(
                            run_id,
                            actor_type="tenant",
                            actor_ref=tenant.tenant_id,
                            rating=rating,
                            reasons=reasons,
                            comment=comment,
                            tenant_id=tenant.tenant_id,
                        )
                    except (LookupError, PermissionError, ValueError) as exc:
                        reply = "提交反馈失败：{}".format(exc)
                    else:
                        reply = "感谢反馈，已记录本次评价。"
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
                    subject,
                    question,
                    image_bytes=image_bytes,
                    agent_id=self._channel_agent_id(message.channel_id),
                    conversation_id=self._session_key(message),
                    allow_tools=message.conversation_type == DIRECT,
                    allow_private_context=message.conversation_type == DIRECT,
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
