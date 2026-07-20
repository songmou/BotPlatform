#!/usr/bin/env python3
"""WeChat iLink bot backed by a configurable model provider."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, TextIO, Tuple

from agent_service import AgentService
from config_loader import ConfigError, load_project_config
from console_logging import (
    log_interaction,
    log_model_call,
    log_model_fallback,
    log_tool_call,
)
from ilink import (
    Credentials,
    ILinkClient,
    ILinkError,
    SessionExpired,
    extract_text_and_image,
    is_private_user_message,
)
from image_source import ImageSource
from embedding_service import EmbeddingClient
from instance_lock import AlreadyRunning, SingleInstanceLock
from knowledge_service import KnowledgeService
from memory_service import MemoryService, OllamaMemoryExtractor
from modeling import ModelError, ModelRouter
from modeling.factory import create_model_client
from notification_service import NotificationError, NotificationService
from notification_service import TenantRecipientStore
from plugins import PluginContext, PluginError, build_plugins
from preflight import check_configuration, print_report
from scheduler_service import SchedulerService
from script_service import ScriptService
from tooling import ApprovalRequired, ToolError, ToolRuntime
from tenant_store import (
    ConversationStore,
    ScheduleStore,
    SettingsStore,
    TenantContext,
    TenantRegistry,
    TenantStoreError,
    new_confirmation_code,
)


PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
CREDENTIALS_PATH = DATA_DIR / "system" / "credentials.json"
INSTANCE_LOCK_PATH = DATA_DIR / "system" / "bot.lock"
CONFIG_DIR = PROJECT_ROOT / "config"
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


class WeChatBot:
    def __init__(
        self,
        ilink: ILinkClient,
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
    ) -> None:
        self.ilink = ilink
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
        self._approval_timer_lock = threading.Lock()
        self._approval_timers: Dict[str, Tuple[str, Any]] = {}
        self._deletion_pending: Dict[str, Tuple[str, datetime]] = {}
        self._memory_clear_pending: Dict[str, Tuple[str, datetime]] = {}

    @staticmethod
    def _subject_key(subject: Any) -> str:
        return subject.tenant_id if isinstance(subject, TenantContext) else str(subject)

    def _resolve_tenant(self, user_id: str) -> Optional[TenantContext]:
        if self.tenant_registry is None:
            return None
        credentials = self.ilink.credentials
        if credentials is None:
            raise ILinkError("微信登录凭证不可用")
        return self.tenant_registry.resolve(credentials.bot_id, user_id)

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
        user_id: str,
        context_token: str,
        text: str,
        tenant: Optional[TenantContext] = None,
        record: bool = True,
    ) -> None:
        self.ilink.send_text(user_id, context_token, text)
        if record:
            self._append_transcript(tenant, "assistant", text)

    @staticmethod
    def _outcome_text(outcome: Any) -> str:
        thinking = str(getattr(outcome, "thinking", "") or "").strip()
        if not thinking:
            return outcome.text
        return "思考过程：\n{}\n\n回答：\n{}".format(thinking, outcome.text)

    @staticmethod
    def _typing_error(exc: ILinkError) -> None:
        print("微信正在输入状态更新失败：{}".format(exc), file=sys.stderr)

    def _cancel_approval_timer(self, user_id: Any) -> None:
        key = self._subject_key(user_id)
        with self._approval_timer_lock:
            tracked = self._approval_timers.pop(key, None)
        if tracked:
            tracked[1].cancel()

    def _schedule_approval_timeout(
        self,
        subject: Any,
        user_id: str,
        context_token: str,
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
                        subject, user_id, context_token, outcome
                    )
                    return
                if not self.agent_service.expire_approval(
                    subject, approval_id, now=now
                ):
                    return
                try:
                    self.ilink.send_text(
                        user_id, context_token, APPROVAL_TIMEOUT_TEXT
                    )
                except ILinkError as exc:
                    print(
                        "发送确认超时通知失败：{}".format(exc),
                        file=sys.stderr,
                    )
                    return
                self._log("微信输出", user_id, APPROVAL_TIMEOUT_TEXT)
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
        self, subject: Any, user_id: str, context_token: str, outcome: Any
    ) -> None:
        if isinstance(outcome, ApprovalRequired):
            self._schedule_approval_timeout(subject, user_id, context_token, outcome)
        else:
            self._cancel_approval_timer(subject)

    def _send_outcome(
        self,
        subject: Any,
        user_id: str,
        context_token: str,
        outcome: Any,
        tenant: Optional[TenantContext] = None,
    ) -> None:
        self._track_approval_outcome(subject, user_id, context_token, outcome)
        reply = self._outcome_text(outcome)
        self._reply(user_id, context_token, reply, tenant)
        self._log("微信输出", user_id, reply)

    def handle_message(self, message: Dict[str, Any]) -> None:
        if not is_private_user_message(message):
            return

        user_id = str(message["from_user_id"])
        context_token = str(message["context_token"])
        tenant = self._resolve_tenant(user_id)
        subject: Any = tenant or user_id
        if tenant is not None and self.recipient_store is not None:
            self.recipient_store.update(tenant, context_token)
        if self._record_recipient:
            try:
                self._record_recipient(user_id, context_token)
            except OSError as exc:
                print("保存最近微信用户失败：{}".format(exc), file=sys.stderr)
        text, image_item = extract_text_and_image(message)

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
            self._log("微信输入", user_id, log_text)
            if tenant is None or self.codex_tasks_plugin is None:
                reply = "当前未启用 Codex 任务确认。"
            else:
                try:
                    reply = self.codex_tasks_plugin.resolve_wechat_command(
                        tenant, normalized_text
                    )
                except PluginError as exc:
                    reply = str(exc)
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
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
            self._log("微信输入", user_id, text)
            parts = normalized_text.split()
            if len(parts) != 2 or parts[0].lower() not in {"/approve", "/deny"}:
                reply = "格式错误，请使用 /approve <编号> 或 /deny <编号>。"
            else:
                try:
                    with self.ilink.typing(
                        user_id, context_token, on_error=self._typing_error
                    ):
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
                    self._send_outcome(subject, user_id, context_token, outcome, tenant)
                    return
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
            return

        decision_words = APPROVAL_WORDS | DENIAL_WORDS
        if (
            not image_item
            and normalized_text in decision_words
            and self.agent_service.has_pending_approval(subject)
        ):
            self._log("微信输入", user_id, text)
            try:
                with self.ilink.typing(
                    user_id, context_token, on_error=self._typing_error
                ):
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
                self._send_outcome(subject, user_id, context_token, outcome, tenant)
                return
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
            return

        if not image_item and lowered_text == "/id":
            reply = (
                "你的租户编号：{}".format(tenant.tenant_id)
                if tenant is not None
                else "当前未启用多用户存储。"
            )
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
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
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
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
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
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
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
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
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
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
                    "此操作会永久删除你的历史、设置、订阅、工作区和脚本产物。"
                    "Codex 账号级任务历史仍由 Codex 自身保存，不会随本操作删除。"
                    "如确定，请在 5 分钟内回复 /confirm-delete {}。"
                ).format(code)
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
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
                    self.tenant_registry.delete(tenant)
                except (OSError, ValueError, TenantStoreError) as exc:
                    reply = "暂时无法删除用户数据：{}。请稍后重试。".format(exc)
                else:
                    reply = (
                        "你的全部 BotPlatform 租户数据已经删除；下次私聊将创建新的租户编号。"
                        "Codex 账号级任务历史仍需在 Codex 中单独管理。"
                    )
                    self._reply(user_id, context_token, reply, None, record=False)
                    self._log("微信输出", user_id, reply)
                    return
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
            return

        if not image_item and lowered_text == "/clear":
            self._log("微信输入", user_id, text)
            self.agent_service.clear_history(subject)
            self._cancel_approval_timer(subject)
            reply = "对话上下文已清空。"
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
            return
        if not image_item and lowered_text == "/agent":
            self._log("微信输入", user_id, text)
            reply = self.agent_service.describe_active()
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
            return
        if not image_item and (
            lowered_text == "/model" or lowered_text.startswith("/model ")
        ):
            self._log("微信输入", user_id, text)
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
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
            return
        if not image_item and lowered_text == "/tools":
            self._log("微信输入", user_id, text)
            reply = self.agent_service.tools_text(subject)
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
            return
        if not image_item and lowered_text == "/help":
            self._log("微信输入", user_id, text)
            reply = self.agent_service.help_text()
            self._reply(user_id, context_token, reply, tenant)
            self._log("微信输出", user_id, reply)
            return
        if not text and not image_item:
            return

        question = text or self.agent_service.image_prompt
        input_log = "[图片] {}".format(question) if image_item else question
        self._log("微信输入", user_id, input_log)
        image_bytes: Optional[bytes] = None
        try:
            with self.ilink.typing(
                user_id, context_token, on_error=self._typing_error
            ):
                if image_item:
                    image_bytes = self.ilink.download_image(image_item)
                outcome = self.agent_service.chat(
                    subject, question, image_bytes=image_bytes
                )
            answer = self._outcome_text(outcome)
        except SessionExpired:
            raise
        except (ILinkError, ModelError) as exc:
            print("处理用户消息失败：{}".format(exc), file=sys.stderr)
            error_reply = "处理消息失败：{}。请稍后重试。".format(exc)
            self._reply(
                user_id,
                context_token,
                error_reply,
                tenant,
            )
            self._log("微信输出", user_id, error_reply)
            return

        self._track_approval_outcome(subject, user_id, context_token, outcome)
        self._reply(user_id, context_token, answer, tenant)
        self._log("微信输出", user_id, answer)

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


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="微信 iLink 多模型机器人")
    parser.add_argument(
        "--logout",
        action="store_true",
        help="删除已保存的微信登录凭证并重新扫码",
    )
    subparsers = parser.add_subparsers(dest="command")
    notify_parser = subparsers.add_parser(
        "notify",
        help="向指定租户发送一条微信通知",
    )
    notify_parser.add_argument("--user", required=True, help="目标租户编号")
    notify_input = notify_parser.add_mutually_exclusive_group()
    notify_input.add_argument("--message", help="要发送的通知文本")
    notify_input.add_argument(
        "--stdin",
        action="store_true",
        help="从标准输入读取通知文本",
    )
    notify_image = notify_parser.add_mutually_exclusive_group()
    notify_image.add_argument("--image", help="要发送的本地图片路径")
    notify_image.add_argument("--image-url", help="要下载并发送的 HTTP(S) 图片 URL")
    subparsers.add_parser(
        "check-config",
        help="检查环境和配置，不启动机器人",
    )
    args = parser.parse_args(argv)
    if args.command == "notify" and args.logout:
        parser.error("--logout 不能与 notify 同时使用")
    if args.command == "notify" and not any(
        (args.message is not None, args.stdin, args.image, args.image_url)
    ):
        parser.error("notify 至少需要 --message、--stdin、--image 或 --image-url 之一")
    return args


def run_notify_command(
    args: argparse.Namespace,
    input_stream: Optional[TextIO] = None,
    output_stream: Optional[TextIO] = None,
    error_stream: Optional[TextIO] = None,
    service: Optional[NotificationService] = None,
) -> int:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    error_stream = error_stream or sys.stderr

    if args.stdin:
        try:
            message = input_stream.read()
        except OSError as exc:
            print("读取通知内容失败：{}".format(exc), file=error_stream)
            return 1
    else:
        message = args.message

    image_path = getattr(args, "image", None)
    image_url = getattr(args, "image_url", None)
    has_image = bool(image_path or image_url)
    if not has_image and (not isinstance(message, str) or not message.strip()):
        print("发送微信通知失败：通知内容不能为空。", file=error_stream)
        return 1
    caption = message if isinstance(message, str) and message.strip() else ""

    tenant_id = str(getattr(args, "user", "") or "").strip()
    if not tenant_id:
        print("发送微信通知失败：必须使用 --user 指定目标租户。", file=error_stream)
        return 1
    if service is None:
        try:
            registry = TenantRegistry(DATA_DIR)
            registry.get(tenant_id)
        except TenantStoreError as exc:
            print("发送微信通知失败：{}。".format(exc), file=error_stream)
            return 1
        notification_service = NotificationService(
            credentials_loader=lambda: load_credentials(CREDENTIALS_PATH),
            recipient_store=TenantRecipientStore(registry),
        )
    else:
        notification_service = service
    try:
        if image_path:
            local_path = Path(image_path).expanduser()
            if not local_path.is_absolute():
                local_path = Path.cwd() / local_path
            source = ImageSource.local(local_path)
            notification_service.send_image_to_tenant(
                tenant_id, source, caption=caption
            )
        elif image_url:
            source = ImageSource.remote(image_url)
            notification_service.send_image_to_tenant(
                tenant_id, source, caption=caption
            )
        else:
            notification_service.send_text_to_tenant(tenant_id, message)
    except NotificationError as exc:
        print("发送微信通知失败：{}。".format(exc), file=error_stream)
        return 1

    success_message = "微信图片通知已发送。" if has_image else "微信通知已发送。"
    print(success_message, file=output_stream)
    return 0


def run_bot(args: argparse.Namespace, project_config=None) -> int:
    if project_config is None:
        try:
            project_config = load_project_config(CONFIG_DIR)
        except ConfigError as exc:
            print("配置加载失败：{}".format(exc), file=sys.stderr)
            return 1

    try:
        tenant_registry = TenantRegistry(DATA_DIR)
    except TenantStoreError as exc:
        print("租户数据加载失败：{}".format(exc), file=sys.stderr)
        return 1
    recipient_store = TenantRecipientStore(tenant_registry)
    notification_service = NotificationService(
        credentials_loader=load_credentials,
        recipient_store=recipient_store,
    )
    conversation_store = ConversationStore(
        tenant_registry, project_config.app.history_rounds * 2
    )
    settings_store = SettingsStore(tenant_registry)
    schedule_store = ScheduleStore(tenant_registry)
    embedding_client = (
        EmbeddingClient(project_config.embedding)
        if project_config.embedding.enabled
        else None
    )
    knowledge_service = KnowledgeService(tenant_registry, embedding_client)
    local_memory_profile = next(
        (
            profile
            for profile in project_config.models.values()
            if profile.enabled and profile.type == "ollama"
        ),
        None,
    )
    memory_service = MemoryService(
        tenant_registry,
        OllamaMemoryExtractor(local_memory_profile) if local_memory_profile else None,
    )

    if args.logout:
        delete_credentials()
        print("已清除微信登录凭证。")

    clients = {}
    try:
        for profile_id, profile in project_config.models.items():
            if not profile.enabled:
                continue
            clients[profile_id] = create_model_client(profile, logger=log_model_call)
        model = ModelRouter(
            clients,
            primary_profile_id=project_config.app.active_model,
            fallback_profile_id=project_config.app.fallback_model,
            local_profile_id=project_config.app.local_model,
            flash_profile_id=project_config.app.flash_model,
            pro_profile_id=project_config.app.pro_model,
            vision_profile_id=project_config.app.vision_model,
            cooldown_seconds=project_config.app.fallback_cooldown_seconds,
            fallback_logger=log_model_fallback,
        )
    except (ModelError, ValueError) as exc:
        for client in clients.values():
            client.close()
        memory_service.close()
        if embedding_client:
            embedding_client.close()
        print("模型客户端创建失败：{}".format(exc), file=sys.stderr)
        return 1
    try:
        identity = model.identity
        print(
            "正在检查模型档案 {}：{} / {}……".format(
                identity.profile_id, identity.provider, identity.configured_model
            )
        )
        model.ensure_ready()
        print(
            "模型已就绪：档案={}，提供商={}，模型={}".format(
                identity.profile_id, identity.provider, identity.configured_model
            )
        )
        if model.cooling_down:
            print(
                "默认模型暂不可用，文字请求将临时使用已配置的兜底模型；"
                "冷却后会重试默认模型。",
                file=sys.stderr,
            )
        print(
            "当前 Agent：{}（{}）".format(
                project_config.active_agent.name, project_config.active_agent.id
            )
        )
        while True:
            try:
                credentials = load_credentials()
            except ILinkError as exc:
                print(str(exc), file=sys.stderr)
                print("将删除无效凭证并重新扫码。")
                delete_credentials()
                credentials = None

            ilink = ILinkClient(credentials=credentials)
            scheduler: Optional[SchedulerService] = None
            script_service: Optional[ScriptService] = None
            tool_runtime: Optional[ToolRuntime] = None
            try:
                if credentials is None:
                    credentials = ilink.login(display_qr_code, status_changed=print_login_status)
                    save_credentials(credentials)
                    print("微信凭证已保存到 {}。".format(CREDENTIALS_PATH))
                else:
                    print("已加载保存的微信凭证，bot_id={}。".format(credentials.bot_id))

                script_service = ScriptService(
                    project_config.scripts,
                    credentials,
                    recipient_store,
                    PROJECT_ROOT,
                    tenant_registry,
                )
                platform_plugins = build_plugins(
                    project_config.plugins,
                    context=PluginContext(
                        project_root=PROJECT_ROOT,
                        tenant_registry=tenant_registry,
                        notification_service=notification_service,
                    ),
                ) if project_config.tools.enabled else []
                codex_tasks_plugin = next(
                    (plugin for plugin in platform_plugins if plugin.id == "codex_tasks"),
                    None,
                )
                tool_runtime = (
                    ToolRuntime(
                        project_config.tools,
                        project_config.app.timezone,
                        audit_logger=log_tool_call,
                        script_service=script_service,
                        tenant_registry=tenant_registry,
                        knowledge_service=knowledge_service,
                        plugins=platform_plugins,
                    )
                    if project_config.tools.enabled
                    else None
                )
                if (
                    tool_runtime
                    and "run_command" in project_config.active_agent.tools
                    and not tool_runtime.command_runner.available
                ):
                    print(
                        "警告：macOS 命令沙箱不可用，run_command 已禁用；文件和系统工具仍可使用。",
                        file=sys.stderr,
                    )
                agent_service = AgentService(
                    model,
                    project_config.app,
                    project_config.agents,
                    tool_runtime=tool_runtime,
                    conversation_store=conversation_store,
                    settings_store=settings_store,
                    knowledge_service=knowledge_service,
                    memory_service=memory_service,
                )
                scheduler = SchedulerService(
                    credentials=credentials,
                    tasks=project_config.schedules,
                    timezone_name=project_config.app.timezone,
                    agent_service=agent_service,
                    recipient_store=recipient_store,
                    script_service=script_service,
                    tenant_registry=tenant_registry,
                    schedule_store=schedule_store,
                )
                scheduler.start()
                print(
                    "定时任务已启动：启用 {} / 共 {} 项，时区 {}。".format(
                        scheduler.enabled_count,
                        len(project_config.schedules),
                        project_config.app.timezone,
                    )
                )
                WeChatBot(
                    ilink,
                    agent_service,
                    tenant_registry=tenant_registry,
                    recipient_store=recipient_store,
                    conversation_store=conversation_store,
                    schedule_store=schedule_store,
                    schedule_ids=[
                        task.id for task in project_config.schedules if task.enabled
                    ],
                    script_service=script_service,
                    knowledge_service=knowledge_service,
                    memory_service=memory_service,
                    codex_tasks_plugin=codex_tasks_plugin,
                ).run()
            except SessionExpired:
                print("微信登录已失效，将重新扫码。", file=sys.stderr)
                delete_credentials()
                continue
            finally:
                if scheduler:
                    scheduler.shutdown()
                if script_service:
                    script_service.shutdown()
                if tool_runtime:
                    tool_runtime.close()
                ilink.close()
    except KeyboardInterrupt:
        print("\n机器人已停止。")
        return 0
    except (ILinkError, ModelError, OSError, TenantStoreError) as exc:
        print("启动失败：{}".format(exc), file=sys.stderr)
        return 1
    finally:
        model.close()
        memory_service.close()
        if embedding_client:
            embedding_client.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.command == "notify":
        return run_notify_command(args)

    if args.command == "check-config":
        report = check_configuration(CONFIG_DIR)
        print_report(report, sys.stdout)
        return 0 if report.ok else 1

    instance_lock = SingleInstanceLock(INSTANCE_LOCK_PATH)
    try:
        instance_lock.acquire()
    except AlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print("启动失败：无法获取机器人运行锁：{}".format(exc), file=sys.stderr)
        return 1

    try:
        report = check_configuration(CONFIG_DIR)
        print_report(report, sys.stdout)
        if not report.ok or report.config is None:
            return 1
        return run_bot(args, report.config)
    finally:
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
