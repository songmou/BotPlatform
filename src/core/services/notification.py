"""Send outbound WeChat notifications to explicit tenant recipients."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from src.core.integrations.ilink import (
    Credentials,
    ILinkClient,
    ILinkError,
    PartialDeliveryError,
    SessionExpired,
)
from src.core.integrations.images import ImageSource, ImageSourceError, ImageSourceLoader
from src.core.storage.tenants import TenantContext, TenantRegistry


class RecipientStoreError(RuntimeError):
    """Raised when the persisted notification target is invalid."""


class NotificationError(RuntimeError):
    """Base error raised when an outbound notification cannot be sent."""


class NotificationCredentialsError(NotificationError):
    """Raised when saved WeChat credentials cannot be used."""


class NotificationRecipientError(NotificationError):
    """Raised when no valid recent WeChat recipient is available."""


class NotificationDeliveryError(NotificationError):
    """Raised when WeChat rejects or fails to deliver a notification."""


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


class NotificationService:
    """Deliver literal text without invoking an Agent or model."""

    def __init__(
        self,
        credentials_loader: Callable[[], Optional[Credentials]],
        recipient_store: TenantRecipientStore,
        client_factory: Callable[[Credentials], ILinkClient] = lambda credentials: ILinkClient(
            credentials=credentials
        ),
        image_loader: Optional[ImageSourceLoader] = None,
    ) -> None:
        self.credentials_loader = credentials_loader
        self.recipient_store = recipient_store
        self.client_factory = client_factory
        self.image_loader = image_loader or ImageSourceLoader()

    def send_text_to(self, recipient: Recipient, message: str) -> NotificationResult:
        """Send to a recipient snapshot without reloading the most recent user."""
        if not isinstance(message, str) or not message.strip():
            raise NotificationError("通知内容不能为空")
        credentials = self._load_credentials()
        return self._deliver(credentials, recipient, message)

    def send_text_to_tenant(self, tenant_id: str, message: str) -> NotificationResult:
        """Send to an explicit tenant; never fall back to a last-active user."""
        if not isinstance(message, str) or not message.strip():
            raise NotificationError("通知内容不能为空")
        credentials = self._load_credentials()
        try:
            recipient = self.recipient_store.load(tenant_id)
        except (RecipientStoreError, TypeError) as exc:
            raise NotificationRecipientError(str(exc)) from exc
        if recipient is None:
            raise NotificationRecipientError("该用户尚无有效的微信收件地址")
        return self._deliver(credentials, recipient, message)

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
        self, tenant_id: str, source: ImageSource, caption: str = ""
    ) -> NotificationResult:
        """Send an image to an explicit tenant recipient."""
        credentials = self._load_credentials()
        try:
            recipient = self.recipient_store.load(tenant_id)
        except (RecipientStoreError, TypeError) as exc:
            raise NotificationRecipientError(str(exc)) from exc
        if recipient is None:
            raise NotificationRecipientError("该用户尚无有效的微信收件地址")
        return self._deliver_image(credentials, recipient, source, caption)

    def _load_credentials(self) -> Credentials:
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
        except ILinkError as exc:
            raise NotificationDeliveryError("微信图片发送失败：{}".format(exc)) from exc
        finally:
            client.close()

        return NotificationResult(recipient_user_id=recipient.user_id)
