"""Persist and deliver outbound notifications through the messaging layer."""

from __future__ import annotations

import os
import logging
import sqlite3
import threading
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.core.integrations.ilink import (
    Credentials,
    ILinkAPIError,
    ILinkClient,
    ILinkError,
    PartialDeliveryError,
    SessionExpired,
)
from src.core.integrations.images import ImageSource, ImageSourceError, ImageSourceLoader
from src.core.messaging import (
    AuthenticationExpired,
    ChannelAddressStore,
    DeliveryEndpoint,
    MessageRouter,
    MessagingError,
    OutboundMessage,
    PartialDeliveryError as MessagingPartialDeliveryError,
    RecipientUnavailable,
)
from src.core.storage.tenants import (
    ConversationStore,
    TenantContext,
    TenantRegistry,
    TenantStoreError,
)


LOGGER = logging.getLogger(__name__)


class RecipientStoreError(RuntimeError):
    """Raised when the persisted notification target is invalid."""


class NotificationError(RuntimeError):
    """Base error raised when an outbound notification cannot be sent."""


class NotificationCredentialsError(NotificationError):
    """Raised when saved channel credentials cannot be used."""


class NotificationRecipientError(NotificationError):
    """Raised when no valid recent delivery endpoint is available."""


class NotificationRecipientStaleError(NotificationRecipientError):
    """Raised when iLink requires a fresh recipient context token."""

    def __init__(self, message: str, api_error: ILinkAPIError) -> None:
        self.ret = api_error.ret
        self.errcode = api_error.errcode
        self.errmsg = api_error.errmsg
        super().__init__(message)


class NotificationDeliveryError(NotificationError):
    """Raised when a channel rejects or fails to deliver a notification."""


class NotificationImageError(NotificationError):
    """Raised when an outbound image cannot be loaded or validated."""


class NotificationPartialDeliveryError(NotificationDeliveryError):
    """Raised when a caption succeeded but the following image failed."""


@dataclass(frozen=True)
class Recipient:
    user_id: str
    context_token: str
    updated_at: str
    task_attempts: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> "Recipient":
        if not isinstance(data, dict):
            raise RecipientStoreError("收件地址文件必须是 JSON 对象")
        user_id = data.get("user_id")
        context_token = data.get("context_token")
        updated_at = data.get("updated_at")
        if not all(
            isinstance(value, str) and value
            for value in (user_id, context_token, updated_at)
        ):
            raise RecipientStoreError(
                "收件地址文件缺少 user_id、context_token 或 updated_at"
            )
        task_attempts = data.get("task_attempts", {})
        if not isinstance(task_attempts, dict) or not all(
            isinstance(task_id, str)
            and task_id
            and isinstance(interaction_at, str)
            and interaction_at
            for task_id, interaction_at in task_attempts.items()
        ):
            raise RecipientStoreError("收件地址文件的 task_attempts 必须是字符串映射")
        return cls(
            user_id=user_id,
            context_token=context_token,
            updated_at=updated_at,
            task_attempts=dict(task_attempts),
        )

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "context_token": self.context_token,
            "updated_at": self.updated_at,
            "task_attempts": self.task_attempts,
        }


class TenantRecipientStore:
    """Persist one current WeChat recipient snapshot per tenant."""

    def __init__(
        self,
        registry: TenantRegistry,
        now_provider: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.registry = registry
        self.now_provider = now_provider

    def update(self, tenant: TenantContext, context_token: str) -> None:
        recipient = Recipient(
            user_id=tenant.user_id,
            context_token=context_token,
            updated_at=self.now_provider().astimezone(timezone.utc).isoformat(),
        )
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO recipients(tenant_id, user_id, context_token, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(tenant_id) DO UPDATE SET "
                "user_id=excluded.user_id, context_token=excluded.context_token, "
                "updated_at=excluded.updated_at",
                (tenant.tenant_id, recipient.user_id, recipient.context_token, recipient.updated_at),
            )
            connection.execute(
                "DELETE FROM recipient_task_attempts WHERE tenant_id=?", (tenant.tenant_id,)
            )

    def load(self, tenant_id: str) -> Optional[Recipient]:
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT user_id, context_token, updated_at FROM recipients WHERE tenant_id=?",
                (tenant_id,),
            ).fetchone()
            if row is None:
                return None
            attempts = connection.execute(
                "SELECT task_id, interaction_at FROM recipient_task_attempts WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall()
        return Recipient(
            user_id=str(row["user_id"]),
            context_token=str(row["context_token"]),
            updated_at=str(row["updated_at"]),
            task_attempts={str(item["task_id"]): str(item["interaction_at"]) for item in attempts},
        )

    def claim_task_attempt(
        self, tenant_id: str, task_id: str, expected: Recipient
    ) -> bool:
        with self.registry.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT user_id, context_token, updated_at FROM recipients WHERE tenant_id=?",
                (tenant_id,),
            ).fetchone()
            if row is None or (
                row["user_id"] != expected.user_id
                or row["context_token"] != expected.context_token
                or row["updated_at"] != expected.updated_at
            ):
                return False
            attempt = connection.execute(
                "SELECT interaction_at FROM recipient_task_attempts "
                "WHERE tenant_id=? AND task_id=?",
                (tenant_id, task_id),
            ).fetchone()
            if attempt and attempt["interaction_at"] == expected.updated_at:
                return False
            connection.execute(
                "INSERT INTO recipient_task_attempts(tenant_id, task_id, interaction_at) "
                "VALUES (?, ?, ?) ON CONFLICT(tenant_id, task_id) DO UPDATE SET "
                "interaction_at=excluded.interaction_at",
                (tenant_id, task_id, expected.updated_at),
            )
            return True


@dataclass(frozen=True)
class NotificationResult:
    recipient_user_id: str
    channel_id: str = "wechat-main"


@dataclass(frozen=True)
class NotificationEnqueueResult:
    notification_ids: Tuple[str, ...]
    status: str


class NotificationOutboxStore:
    """Transactional, tenant-ordered storage for proactive notifications."""

    def __init__(
        self,
        registry: TenantRegistry,
        now_provider: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.registry = registry
        self.now_provider = now_provider

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is None:
            raise ValueError("通知时间必须包含时区")
        return value.astimezone(timezone.utc).isoformat()

    def enqueue(self, rows: Sequence[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
        if not rows:
            raise NotificationError("通知批次不能为空")
        created: List[Dict[str, Any]] = []
        unused_paths: List[str] = []
        with self.registry.database.transaction(immediate=True) as connection:
            for row in rows:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO notification_outbox("
                    "notification_id, tenant_id, batch_id, batch_position, "
                    "source_type, source_key, source_ref, kind, text_payload, "
                    "image_path, delivery_status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        row["notification_id"],
                        row["tenant_id"],
                        row["batch_id"],
                        row["batch_position"],
                        row["source_type"],
                        row.get("source_key"),
                        row.get("source_ref"),
                        row["kind"],
                        row.get("text_payload"),
                        row.get("image_path"),
                        row["created_at"],
                    ),
                )
                if cursor.rowcount == 1:
                    stored = connection.execute(
                        "SELECT * FROM notification_outbox WHERE notification_id=?",
                        (row["notification_id"],),
                    ).fetchone()
                elif row.get("source_key"):
                    stored = connection.execute(
                        "SELECT * FROM notification_outbox WHERE tenant_id=? "
                        "AND source_type=? AND source_key=?",
                        (row["tenant_id"], row["source_type"], row["source_key"]),
                    ).fetchone()
                    if row.get("image_path"):
                        unused_paths.append(str(row["image_path"]))
                else:
                    raise NotificationError("通知入队失败")
                if stored is None:
                    raise NotificationError("通知入队后无法读取")
                created.append(dict(stored))
        return created, unused_paths

    def enqueue_todo_reminder(
        self,
        tenant_id: str,
        todo_number: int,
        due_at: str,
        title: str,
    ) -> Dict[str, Any]:
        """Atomically claim a due reminder and create its delivery row."""

        notification_id = str(uuid.uuid4())
        source_key = "{}:{}".format(todo_number, due_at)
        created_at = self._iso(self.now_provider())
        with self.registry.database.transaction(immediate=True) as connection:
            event = connection.execute(
                "SELECT delivery_status FROM todo_reminder_events "
                "WHERE tenant_id=? AND todo_number=? AND due_at=?",
                (tenant_id, todo_number, due_at),
            ).fetchone()
            todo = connection.execute(
                "SELECT status, reminder_at FROM todos "
                "WHERE tenant_id=? AND todo_number=?",
                (tenant_id, todo_number),
            ).fetchone()
            if (
                event is None
                or todo is None
                or event["delivery_status"] not in ("pending", "sending")
                or todo["status"] != "pending"
                or todo["reminder_at"] != due_at
            ):
                raise NotificationError("待办提醒已被处理或取消")
            connection.execute(
                "INSERT OR IGNORE INTO notification_outbox("
                "notification_id, tenant_id, batch_id, batch_position, "
                "source_type, source_key, source_ref, kind, text_payload, "
                "delivery_status, created_at) "
                "VALUES (?, ?, ?, 0, 'todo', ?, ?, 'text', ?, 'pending', ?)",
                (
                    notification_id,
                    tenant_id,
                    notification_id,
                    source_key,
                    str(todo_number),
                    "【待办提醒】T{:04d} {}".format(todo_number, title),
                    created_at,
                ),
            )
            connection.execute(
                "UPDATE todo_reminder_events SET delivery_status='sending', "
                "attempt_count=CASE WHEN delivery_status='pending' "
                "THEN attempt_count+1 ELSE attempt_count END, updated_at=? "
                "WHERE tenant_id=? AND todo_number=?",
                (created_at, tenant_id, todo_number),
            )
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE tenant_id=? "
                "AND source_type='todo' AND source_key=?",
                (tenant_id, source_key),
            ).fetchone()
        if row is None:
            raise NotificationError("待办提醒入队失败")
        return dict(row)

    def claim_due(self, limit: int = 20) -> List[Dict[str, Any]]:
        now = self._iso(self.now_provider())
        lease = self._iso(self.now_provider() + timedelta(seconds=180))
        claimed: List[Dict[str, Any]] = []
        with self.registry.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT candidate.* FROM notification_outbox AS candidate "
                "WHERE (candidate.delivery_status='pending' "
                "OR (candidate.delivery_status='retry' AND candidate.next_attempt_at<=?) "
                "OR (candidate.delivery_status='sending' AND "
                "(candidate.lease_expires_at IS NULL OR candidate.lease_expires_at<=?))) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM notification_outbox AS earlier "
                "WHERE earlier.tenant_id=candidate.tenant_id "
                "AND earlier.outbox_id<candidate.outbox_id "
                "AND earlier.delivery_status IN "
                "('pending','sending','retry','waiting_recipient')) "
                "ORDER BY candidate.outbox_id LIMIT ?",
                (now, now, limit),
            ).fetchall()
            for row in rows:
                updated = connection.execute(
                    "UPDATE notification_outbox SET delivery_status='sending', "
                    "lease_expires_at=?, next_attempt_at=NULL "
                    "WHERE outbox_id=? AND (delivery_status='pending' "
                    "OR (delivery_status='retry' AND next_attempt_at<=?) "
                    "OR (delivery_status='sending' AND "
                    "(lease_expires_at IS NULL OR lease_expires_at<=?)))",
                    (lease, row["outbox_id"], now, now),
                ).rowcount
                if updated:
                    current = connection.execute(
                        "SELECT * FROM notification_outbox WHERE outbox_id=?",
                        (row["outbox_id"],),
                    ).fetchone()
                    if current is not None:
                        claimed.append(dict(current))
        return claimed

    def status(self, notification_ids: Sequence[str]) -> str:
        if not notification_ids:
            return "queued"
        placeholders = ",".join("?" for _ in notification_ids)
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT delivery_status FROM notification_outbox "
                "WHERE notification_id IN ({})".format(placeholders),
                tuple(notification_ids),
            ).fetchall()
        return (
            "sent"
            if len(rows) == len(notification_ids)
            and all(row["delivery_status"] == "sent" for row in rows)
            else "queued"
        )

    def finish(
        self,
        outbox_id: int,
        status: str,
        error: str = "",
        retry_delay_seconds: Optional[int] = None,
    ) -> Optional[str]:
        now = self.now_provider()
        timestamp = self._iso(now)
        image_path: Optional[str] = None
        with self.registry.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE outbox_id=?",
                (outbox_id,),
            ).fetchone()
            if row is None or row["delivery_status"] != "sending":
                return None
            attempts = int(row["attempt_count"]) + 1
            if status == "sent":
                connection.execute(
                    "UPDATE notification_outbox SET delivery_status='sent', "
                    "attempt_count=?, next_attempt_at=NULL, lease_expires_at=NULL, "
                    "sent_at=?, last_error=NULL, text_payload=NULL, image_path=NULL "
                    "WHERE outbox_id=?",
                    (attempts, timestamp, outbox_id),
                )
                image_path = str(row["image_path"]) if row["image_path"] else None
            elif status == "waiting_recipient":
                connection.execute(
                    "UPDATE notification_outbox SET delivery_status='waiting_recipient', "
                    "attempt_count=?, next_attempt_at=NULL, lease_expires_at=NULL, "
                    "last_error=? WHERE outbox_id=?",
                    (attempts, error[:1000] or None, outbox_id),
                )
            elif status == "failed":
                image_path = str(row["image_path"]) if row["image_path"] else None
                connection.execute(
                    "UPDATE notification_outbox SET delivery_status='failed', "
                    "attempt_count=?, next_attempt_at=NULL, lease_expires_at=NULL, "
                    "last_error=?, text_payload=NULL, image_path=NULL "
                    "WHERE outbox_id=?",
                    (attempts, error[:1000] or None, outbox_id),
                )
            else:
                delay = max(1, int(retry_delay_seconds or 600))
                next_attempt = self._iso(now + timedelta(seconds=delay))
                connection.execute(
                    "UPDATE notification_outbox SET delivery_status='retry', "
                    "attempt_count=?, next_attempt_at=?, lease_expires_at=NULL, "
                    "last_error=? WHERE outbox_id=?",
                    (attempts, next_attempt, error[:1000] or None, outbox_id),
                )
            self._sync_source(connection, dict(row), status, attempts, timestamp, error)
        return image_path

    @staticmethod
    def _sync_source(
        connection: Any,
        row: Mapping[str, Any],
        status: str,
        attempts: int,
        timestamp: str,
        error: str,
    ) -> None:
        source_type = str(row.get("source_type") or "")
        source_ref = str(row.get("source_ref") or "")
        if source_type == "todo" and source_ref.isdigit():
            number = int(source_ref)
            if status == "sent":
                connection.execute(
                    "UPDATE todo_reminder_events SET delivery_status='sent', sent_at=?, "
                    "last_error=NULL, updated_at=? WHERE tenant_id=? AND todo_number=?",
                    (timestamp, timestamp, row["tenant_id"], number),
                )
                connection.execute(
                    "UPDATE todos SET status='completed', completed_at=?, reminder_at=NULL, "
                    "updated_at=? WHERE tenant_id=? AND todo_number=? "
                    "AND status='pending' AND is_one_off=1",
                    (timestamp, timestamp, row["tenant_id"], number),
                )
            else:
                connection.execute(
                    "UPDATE todo_reminder_events SET delivery_status='sending', "
                    "last_error=?, updated_at=? WHERE tenant_id=? AND todo_number=?",
                    (error[:1000] or None, timestamp, row["tenant_id"], number),
                )

    def requeue_waiting_recipient(self, tenant_id: str) -> int:
        with self.registry.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE notification_outbox SET delivery_status='pending', "
                "next_attempt_at=NULL, lease_expires_at=NULL WHERE tenant_id=? "
                "AND delivery_status='waiting_recipient'",
                (tenant_id,),
            )
            return int(cursor.rowcount)

    def select_endpoint(self, outbox_id: int, endpoint_id: Optional[str]) -> None:
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE notification_outbox SET selected_endpoint_id=? "
                "WHERE outbox_id=?",
                (endpoint_id, outbox_id),
            )

    def get(self, notification_id: str) -> Optional[Dict[str, Any]]:
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def cleanup_orphan_images(self) -> int:
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT image_path FROM notification_outbox WHERE image_path IS NOT NULL"
            ).fetchall()
        referenced = {str(Path(str(row["image_path"])).resolve()) for row in rows}
        cutoff = self.now_provider().timestamp() - 600
        removed = 0
        for tenant in self.registry.list_contexts(include_internal=True):
            root = self.registry.tenant_root(tenant.tenant_id) / "notification_outbox"
            if not root.is_dir():
                continue
            for path in root.iterdir():
                if not path.is_file():
                    continue
                if str(path.resolve()) in referenced:
                    continue
                try:
                    if path.stat().st_mtime > cutoff:
                        continue
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed


class NotificationService:
    """Queue proactive notifications and provide the immediate delivery transport."""

    def __init__(
        self,
        credentials_loader: Optional[Callable[[], Optional[Credentials]]],
        recipient_store: TenantRecipientStore,
        client_factory: Callable[[Credentials], ILinkClient] = lambda credentials: ILinkClient(
            credentials=credentials
        ),
        image_loader: Optional[ImageSourceLoader] = None,
        message_router: Optional[MessageRouter] = None,
        address_store: Optional[ChannelAddressStore] = None,
        conversation_store: Optional[ConversationStore] = None,
    ) -> None:
        self.credentials_loader = credentials_loader
        self.recipient_store = recipient_store
        self.client_factory = client_factory
        self.image_loader = image_loader or ImageSourceLoader()
        self.message_router = message_router
        self.address_store = address_store
        self.conversation_store = conversation_store
        self.outbox = NotificationOutboxStore(
            recipient_store.registry,
        )
        self.outbox.cleanup_orphan_images()

    def _record_delivered_context(
        self,
        tenant_id: str,
        content: str,
        *,
        image: bool = False,
        idempotency_key: str = "",
    ) -> None:
        if self.conversation_store is None:
            return
        delivery_key = (
            "notification:{}".format(idempotency_key)
            if idempotency_key
            else ""
        )
        try:
            self.conversation_store.record_outbound_message(
                tenant_id,
                content,
                image=image,
                delivery_key=delivery_key,
            )
        except (OSError, sqlite3.Error, TenantStoreError):
            LOGGER.exception("记录已送达主动消息的对话上下文失败 tenant=%s", tenant_id)

    def _conversation_lock(self, tenant_id: str):
        if self.conversation_store is None:
            return nullcontext()
        return self.conversation_store.lock_for(tenant_id)

    @staticmethod
    def _source_key(base: Optional[str], position: int, count: int) -> Optional[str]:
        if not base:
            return None
        return base if count == 1 else "{}:{}".format(base, position)

    def _validate_tenant(self, tenant_id: str) -> None:
        try:
            self.recipient_store.registry.get(tenant_id)
        except TenantStoreError as exc:
            raise NotificationRecipientError(str(exc)) from exc

    def enqueue_text_to_tenant(
        self,
        tenant_id: str,
        message: str,
        *,
        source_type: str = "notification",
        source_key: Optional[str] = None,
        source_ref: Optional[str] = None,
        attempt_immediately: bool = False,
    ) -> NotificationEnqueueResult:
        """Persist literal text before any delivery attempt."""
        if not isinstance(message, str) or not message.strip():
            raise NotificationError("通知内容不能为空")
        self._validate_tenant(tenant_id)
        now = datetime.now(timezone.utc).isoformat()
        notification_id = str(uuid.uuid4())
        try:
            stored, _ = self.outbox.enqueue(
                [
                    {
                        "notification_id": notification_id,
                        "tenant_id": tenant_id,
                        "batch_id": notification_id,
                        "batch_position": 0,
                        "source_type": source_type,
                        "source_key": source_key,
                        "source_ref": source_ref,
                        "kind": "text",
                        "text_payload": message,
                        "created_at": now,
                    }
                ]
            )
        except NotificationError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise NotificationError("保存待发送通知失败") from exc
        ids = tuple(str(item["notification_id"]) for item in stored)
        if attempt_immediately:
            self._attempt_immediate()
        return NotificationEnqueueResult(ids, self.outbox.status(ids))

    def enqueue_image_to_tenant(
        self,
        tenant_id: str,
        source: ImageSource,
        caption: str = "",
        *,
        source_type: str = "notification",
        source_key: Optional[str] = None,
        source_ref: Optional[str] = None,
        attempt_immediately: bool = False,
    ) -> NotificationEnqueueResult:
        """Snapshot and persist an image batch before any delivery attempt."""
        self._validate_tenant(tenant_id)
        try:
            image_bytes = self.image_loader.load(source)
        except ImageSourceError as exc:
            raise NotificationImageError(str(exc)) from exc
        batch_id = str(uuid.uuid4())
        image_id = str(uuid.uuid4())
        root = self.recipient_store.registry.tenant_root(tenant_id) / "notification_outbox"
        image_path = root / "{}.image".format(image_id)
        temporary = root / "{}.tmp".format(image_id)
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                os.chmod(str(root), 0o700)
            temporary.write_bytes(image_bytes)
            if os.name != "nt":
                os.chmod(str(temporary), 0o600)
            temporary.replace(image_path)
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise NotificationImageError("缓存待发送图片失败") from exc

        normalized_caption = caption if isinstance(caption, str) else ""
        count = 2 if normalized_caption.strip() else 1
        now = datetime.now(timezone.utc).isoformat()
        rows: List[Dict[str, Any]] = []
        if normalized_caption.strip():
            text_id = str(uuid.uuid4())
            rows.append(
                {
                    "notification_id": text_id,
                    "tenant_id": tenant_id,
                    "batch_id": batch_id,
                    "batch_position": 0,
                    "source_type": source_type,
                    "source_key": self._source_key(source_key, 0, count),
                    "source_ref": source_ref,
                    "kind": "text",
                    "text_payload": normalized_caption,
                    "created_at": now,
                }
            )
        rows.append(
            {
                "notification_id": image_id,
                "tenant_id": tenant_id,
                "batch_id": batch_id,
                "batch_position": len(rows),
                "source_type": source_type,
                "source_key": self._source_key(source_key, len(rows), count),
                "source_ref": source_ref,
                "kind": "image",
                "image_path": str(image_path),
                "created_at": now,
            }
        )
        try:
            stored, unused_paths = self.outbox.enqueue(rows)
        except (NotificationError, OSError, sqlite3.Error) as exc:
            try:
                image_path.unlink()
            except OSError:
                pass
            if isinstance(exc, NotificationError):
                raise
            raise NotificationError("保存待发送图片通知失败") from exc
        for unused in unused_paths:
            try:
                Path(unused).unlink()
            except OSError:
                pass
        ids = tuple(str(item["notification_id"]) for item in stored)
        if attempt_immediately:
            self._attempt_immediate()
        return NotificationEnqueueResult(ids, self.outbox.status(ids))

    def enqueue_todo_reminder(
        self,
        tenant_id: str,
        todo_number: int,
        due_at: str,
        title: str,
    ) -> NotificationEnqueueResult:
        """Atomically transfer a due todo reminder into the Outbox."""

        self._validate_tenant(tenant_id)
        try:
            row = self.outbox.enqueue_todo_reminder(
                tenant_id, todo_number, due_at, title
            )
        except NotificationError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise NotificationError("保存待办提醒失败") from exc
        ids = (str(row["notification_id"]),)
        return NotificationEnqueueResult(ids, self.outbox.status(ids))

    @staticmethod
    def _retry_delay(attempt_count: int) -> int:
        delays = (2, 10, 30, 120, 600)
        return delays[min(max(0, attempt_count), len(delays) - 1)]

    def _attempt_immediate(self, maximum: int = 20) -> None:
        for _ in range(maximum):
            if self.dispatch_due(limit=1) == 0:
                return

    def dispatch_due(self, limit: int = 20) -> int:
        delivered = 0
        for claimed in self.outbox.claim_due(limit):
            try:
                tenant_id = str(claimed["tenant_id"])
                endpoint = self._selected_endpoint(claimed)
                if claimed["kind"] == "text":
                    self.send_text_to_tenant(
                        tenant_id,
                        str(claimed["text_payload"] or ""),
                        endpoint=endpoint,
                        idempotency_key=str(claimed["notification_id"]),
                    )
                else:
                    raw_path = str(claimed["image_path"] or "")
                    if not raw_path:
                        raise NotificationImageError("待发送图片缓存记录缺失")
                    path = Path(raw_path)
                    expected_root = (
                        self.recipient_store.registry.tenant_root(tenant_id)
                        / "notification_outbox"
                    ).resolve()
                    resolved = path.resolve()
                    if expected_root not in resolved.parents:
                        raise NotificationImageError("待发送图片缓存路径无效")
                    self.send_image_to_tenant(
                        tenant_id,
                        ImageSource.local(resolved),
                        caption="",
                        endpoint=endpoint,
                        idempotency_key=str(claimed["notification_id"]),
                    )
            except NotificationRecipientStaleError as exc:
                self.outbox.select_endpoint(int(claimed["outbox_id"]), None)
                self.outbox.finish(
                    int(claimed["outbox_id"]),
                    "waiting_recipient",
                    str(exc),
                )
            except NotificationRecipientError as exc:
                self.outbox.finish(
                    int(claimed["outbox_id"]),
                    "waiting_recipient",
                    str(exc),
                )
            except NotificationImageError as exc:
                cleanup_path = self.outbox.finish(
                    int(claimed["outbox_id"]), "failed", str(exc)
                )
                if cleanup_path:
                    try:
                        Path(cleanup_path).unlink()
                    except OSError:
                        pass
            except (NotificationError, OSError, ValueError) as exc:
                self.outbox.finish(
                    int(claimed["outbox_id"]),
                    "retry",
                    str(exc),
                    self._retry_delay(int(claimed["attempt_count"])),
                )
            except Exception as exc:
                LOGGER.exception(
                    "主动通知投递出现未分类异常 notification=%s",
                    claimed["notification_id"],
                )
                self.outbox.finish(
                    int(claimed["outbox_id"]),
                    "retry",
                    type(exc).__name__,
                    self._retry_delay(int(claimed["attempt_count"])),
                )
            else:
                cleanup_path = self.outbox.finish(int(claimed["outbox_id"]), "sent")
                if cleanup_path:
                    try:
                        Path(cleanup_path).unlink()
                    except OSError:
                        pass
                delivered += 1
        return delivered

    def on_recipient_refreshed(self, tenant_id: str) -> int:
        return self.outbox.requeue_waiting_recipient(tenant_id)

    def pin_channel(
        self,
        notification_ids: Sequence[str],
        tenant_id: str,
        channel_id: str,
    ) -> None:
        if self.address_store is None:
            if channel_id != "wechat-main":
                raise NotificationRecipientError("当前通知服务不支持选择消息渠道")
            return
        endpoint = self.address_store.latest_endpoint(
            tenant_id,
            channel_id=channel_id,
        )
        if endpoint is None:
            raise NotificationRecipientError(
                "该用户在渠道 {} 上没有有效收件地址".format(channel_id)
            )
        for notification_id in notification_ids:
            row = self.outbox.get(notification_id)
            if row is not None:
                self.outbox.select_endpoint(
                    int(row["outbox_id"]),
                    endpoint.endpoint_id,
                )

    def _selected_endpoint(
        self,
        claimed: Mapping[str, Any],
    ) -> Optional[DeliveryEndpoint]:
        if self.message_router is None or self.address_store is None:
            return None
        selected_id = str(claimed.get("selected_endpoint_id") or "")
        endpoint = (
            self.address_store.endpoint(selected_id)
            if selected_id
            else None
        )
        if endpoint is None:
            endpoint = self.address_store.latest_endpoint(str(claimed["tenant_id"]))
            if endpoint is None:
                raise NotificationRecipientError("该用户尚无有效的消息收件地址")
            self.outbox.select_endpoint(
                int(claimed["outbox_id"]),
                endpoint.endpoint_id,
            )
        return endpoint

    def send_text_to(self, recipient: Recipient, message: str) -> NotificationResult:
        """Send to a recipient snapshot without reloading the most recent user."""
        if not isinstance(message, str) or not message.strip():
            raise NotificationError("通知内容不能为空")
        credentials = self._load_credentials()
        return self._deliver(credentials, recipient, message)

    def send_text_to_tenant(
        self,
        tenant_id: str,
        message: str,
        *,
        endpoint: Optional[DeliveryEndpoint] = None,
        channel_id: Optional[str] = None,
        idempotency_key: str = "",
    ) -> NotificationResult:
        """Send to an explicit tenant; never fall back to a last-active user."""
        if not isinstance(message, str) or not message.strip():
            raise NotificationError("通知内容不能为空")
        with self._conversation_lock(tenant_id):
            if self.message_router is not None and self.address_store is not None:
                selected = endpoint or self.address_store.latest_endpoint(
                    tenant_id,
                    channel_id=channel_id,
                )
                if selected is None:
                    raise NotificationRecipientError("该用户尚无有效的消息收件地址")
                result = self._deliver_endpoint(
                    selected,
                    OutboundMessage(
                        text=message,
                        idempotency_key=idempotency_key,
                    ),
                )
            else:
                credentials = self._load_credentials()
                try:
                    recipient = self.recipient_store.load(tenant_id)
                except (RecipientStoreError, TypeError) as exc:
                    raise NotificationRecipientError(str(exc)) from exc
                if recipient is None:
                    raise NotificationRecipientError("该用户尚无有效的微信收件地址")
                result = self._deliver(credentials, recipient, message)
            self._record_delivered_context(
                tenant_id,
                message,
                idempotency_key=idempotency_key,
            )
        return result

    def send_image_to(
        self,
        recipient: Recipient,
        source: ImageSource,
        caption: str = "",
    ) -> NotificationResult:
        """Send an image to a recipient snapshot without reloading it."""
        credentials = self._load_credentials()
        return self._deliver_image(credentials, recipient, source, caption)

    def send_image_to_tenant(
        self,
        tenant_id: str,
        source: ImageSource,
        caption: str = "",
        *,
        endpoint: Optional[DeliveryEndpoint] = None,
        channel_id: Optional[str] = None,
        idempotency_key: str = "",
    ) -> NotificationResult:
        """Send an image to an explicit tenant recipient."""
        with self._conversation_lock(tenant_id):
            if self.message_router is not None and self.address_store is not None:
                selected = endpoint or self.address_store.latest_endpoint(
                    tenant_id,
                    channel_id=channel_id,
                )
                if selected is None:
                    raise NotificationRecipientError("该用户尚无有效的消息收件地址")
                try:
                    image_bytes = self.image_loader.load(source)
                except ImageSourceError as exc:
                    raise NotificationImageError(str(exc)) from exc
                result = self._deliver_endpoint(
                    selected,
                    OutboundMessage(
                        text=caption,
                        image_bytes=image_bytes,
                        idempotency_key=idempotency_key,
                    ),
                )
            else:
                credentials = self._load_credentials()
                try:
                    recipient = self.recipient_store.load(tenant_id)
                except (RecipientStoreError, TypeError) as exc:
                    raise NotificationRecipientError(str(exc)) from exc
                if recipient is None:
                    raise NotificationRecipientError("该用户尚无有效的微信收件地址")
                result = self._deliver_image(credentials, recipient, source, caption)
            context = "[主动推送图片]"
            if caption.strip():
                context += "\n" + caption
            self._record_delivered_context(
                tenant_id,
                context,
                image=True,
                idempotency_key=idempotency_key,
            )
        return result

    def _load_credentials(self) -> Credentials:
        if self.credentials_loader is None:
            raise NotificationCredentialsError("当前未配置消息渠道凭证加载器")
        try:
            credentials = self.credentials_loader()
        except (ILinkError, OSError) as exc:
            raise NotificationCredentialsError(
                "读取微信登录凭证失败：{}".format(exc)
            ) from exc
        if credentials is None:
            raise NotificationCredentialsError(
                "尚无微信登录凭证，请先启动机器人并扫码登录"
            )
        return credentials

    def _deliver_endpoint(
        self,
        endpoint: DeliveryEndpoint,
        message: OutboundMessage,
    ) -> NotificationResult:
        assert self.message_router is not None
        try:
            self.message_router.send(endpoint, message)
        except AuthenticationExpired as exc:
            raise NotificationCredentialsError(
                "消息渠道登录凭证已失效，请重新登录"
            ) from exc
        except RecipientUnavailable as exc:
            if self.address_store is not None:
                self.address_store.mark_stale(endpoint.endpoint_id)
            api_error = ILinkAPIError(1, None, str(exc))
            raise NotificationRecipientStaleError(str(exc), api_error) from exc
        except MessagingPartialDeliveryError as exc:
            raise NotificationPartialDeliveryError(str(exc)) from exc
        except MessagingError as exc:
            raise NotificationDeliveryError(
                "消息通知发送失败：{}".format(exc)
            ) from exc
        return NotificationResult(
            recipient_user_id=endpoint.recipient_id,
            channel_id=endpoint.channel_id,
        )

    def _deliver(
        self, credentials: Credentials, recipient: Recipient, message: str
    ) -> NotificationResult:
        try:
            client = self.client_factory(credentials)
        except Exception as exc:
            raise NotificationDeliveryError(
                "创建微信客户端失败：{}".format(exc)
            ) from exc
        try:
            client.send_text(recipient.user_id, recipient.context_token, message)
        except SessionExpired as exc:
            raise NotificationCredentialsError(
                "微信登录凭证已失效，请重新启动机器人扫码登录"
            ) from exc
        except ILinkAPIError as exc:
            if exc.recipient_context_expired:
                raise NotificationRecipientStaleError(
                    "微信收件上下文已失效，等待用户再次私聊机器人",
                    exc,
                ) from exc
            raise NotificationDeliveryError("微信通知发送失败：{}".format(exc)) from exc
        except ILinkError as exc:
            raise NotificationDeliveryError("微信通知发送失败：{}".format(exc)) from exc
        finally:
            client.close()

        return NotificationResult(recipient_user_id=recipient.user_id)

    def _deliver_image(
        self,
        credentials: Credentials,
        recipient: Recipient,
        source: ImageSource,
        caption: str,
    ) -> NotificationResult:
        try:
            image_bytes = self.image_loader.load(source)
        except ImageSourceError as exc:
            raise NotificationImageError(str(exc)) from exc

        try:
            client = self.client_factory(credentials)
        except Exception as exc:
            raise NotificationDeliveryError(
                "创建微信客户端失败：{}".format(exc)
            ) from exc
        try:
            client.send_image(
                recipient.user_id,
                recipient.context_token,
                image_bytes,
                caption=caption,
            )
        except PartialDeliveryError as exc:
            raise NotificationPartialDeliveryError(str(exc)) from exc
        except SessionExpired as exc:
            raise NotificationCredentialsError(
                "微信登录凭证已失效，请重新启动机器人扫码登录"
            ) from exc
        except ILinkAPIError as exc:
            if exc.recipient_context_expired:
                raise NotificationRecipientStaleError(
                    "微信收件上下文已失效，等待用户再次私聊机器人",
                    exc,
                ) from exc
            raise NotificationDeliveryError("微信图片发送失败：{}".format(exc)) from exc
        except ILinkError as exc:
            raise NotificationDeliveryError("微信图片发送失败：{}".format(exc)) from exc
        finally:
            client.close()

        return NotificationResult(recipient_user_id=recipient.user_id)


class NotificationDispatcher:
    """Continuously deliver durable notifications without owning bot replies."""

    def __init__(
        self,
        service: NotificationService,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self.service = service
        self.poll_interval_seconds = poll_interval_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="notification-outbox",
            daemon=True,
        )
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def on_recipient_refreshed(self, tenant_id: str) -> int:
        count = self.service.on_recipient_refreshed(tenant_id)
        self.wake()
        return count

    def shutdown(self) -> None:
        if not self._started:
            return
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=10)
        self._started = False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                while self.service.dispatch_due():
                    if self._stop.is_set():
                        return
            except Exception:
                LOGGER.exception("主动通知 Outbox 投递循环异常")
            self._wake.wait(self.poll_interval_seconds)
            self._wake.clear()
