from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.integrations.ilink import Credentials, ILinkError, PartialDeliveryError, SessionExpired
from src.integrations.images import ImageSource
from src.services.notification import (
    NotificationCredentialsError,
    NotificationDeliveryError,
    NotificationError,
    NotificationPartialDeliveryError,
    NotificationRecipientError,
    NotificationService,
    TenantRecipientStore,
)
from src.storage.tenants import TenantRegistry


class FakeILink:
    def __init__(self, failure=None) -> None:
        self.failure = failure
        self.sent = []
        self.closed = False

    def send_text(self, user_id, context_token, text):
        self.sent.append((user_id, context_token, text))
        if self.failure:
            raise self.failure

    def send_image(self, user_id, context_token, image_bytes, caption=""):
        self.sent.append((user_id, context_token, image_bytes, caption))
        if self.failure:
            raise self.failure

    def close(self):
        self.closed = True


class NotificationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = TenantRegistry(Path(self.temp.name) / "data")
        self.tenant = self.registry.resolve("bot", "user@im.wechat")
        self.store = TenantRecipientStore(self.registry)
        self.credentials = Credentials("token", "https://gateway", "bot", "owner")
        self.clients = []

    def service(self, credentials=None, failure=None):
        selected_credentials = self.credentials if credentials is None else credentials

        def factory(_credentials):
            client = FakeILink(failure=failure)
            self.clients.append(client)
            return client

        return NotificationService(
            credentials_loader=lambda: selected_credentials,
            recipient_store=self.store,
            client_factory=factory,
            image_loader=FakeImageLoader(),
        )

    def test_sends_literal_text_to_recent_recipient_and_closes_client(self) -> None:
        self.store.update(self.tenant, "context-token")
        service = self.service()

        result = service.send_text_to_tenant(
            self.tenant.tenant_id, "  第一行\n第二行  "
        )

        self.assertEqual(result.recipient_user_id, "user@im.wechat")
        self.assertEqual(
            self.clients[0].sent,
            [("user@im.wechat", "context-token", "  第一行\n第二行  ")],
        )
        self.assertTrue(self.clients[0].closed)

    def test_send_text_to_uses_recipient_snapshot(self) -> None:
        self.store.update(self.tenant, "old-context")
        snapshot = self.store.load(self.tenant.tenant_id)
        self.store.update(self.tenant, "new-context")

        result = self.service().send_text_to(snapshot, "静默提醒")

        self.assertEqual(result.recipient_user_id, "user@im.wechat")
        self.assertEqual(
            self.clients[0].sent,
            [("user@im.wechat", "old-context", "静默提醒")],
        )

    def test_attempt_state_is_scoped_and_resets_on_interaction(self) -> None:
        self.store.update(self.tenant, "first-context")
        first = self.store.load(self.tenant.tenant_id)
        self.assertTrue(
            self.store.claim_task_attempt(
                self.tenant.tenant_id, "reminder", first
            )
        )
        self.assertFalse(
            self.store.claim_task_attempt(
                self.tenant.tenant_id, "reminder", first
            )
        )

        self.store.update(self.tenant, "fresh-context")
        refreshed = self.store.load(self.tenant.tenant_id)
        self.assertEqual(refreshed.task_attempts, {})

    def test_missing_credentials_and_recipient_fail_before_creating_client(self) -> None:
        service = NotificationService(
            credentials_loader=lambda: None,
            recipient_store=self.store,
            client_factory=lambda credentials: self.clients.append(credentials),
        )
        with self.assertRaisesRegex(NotificationCredentialsError, "尚无微信登录凭证"):
            service.send_text_to_tenant(self.tenant.tenant_id, "通知")
        self.assertEqual(self.clients, [])

        service = self.service()
        with self.assertRaisesRegex(NotificationRecipientError, "尚无有效"):
            service.send_text_to_tenant(self.tenant.tenant_id, "通知")
        self.assertEqual(self.clients, [])

    def test_empty_message_is_rejected(self) -> None:
        service = self.service()
        with self.assertRaisesRegex(NotificationError, "通知内容不能为空"):
            service.send_text_to_tenant(self.tenant.tenant_id, " \n ")
        self.assertEqual(self.clients, [])

    def test_expired_session_and_delivery_failure_are_not_retried(self) -> None:
        self.store.update(self.tenant, "context")
        service = self.service(failure=SessionExpired("expired"))
        with self.assertRaisesRegex(NotificationCredentialsError, "凭证已失效"):
            service.send_text_to_tenant(self.tenant.tenant_id, "通知")
        self.assertEqual(len(self.clients), 1)
        self.assertTrue(self.clients[0].closed)

        self.clients = []
        service = self.service(failure=ILinkError("send failed"))
        with self.assertRaisesRegex(NotificationDeliveryError, "send failed"):
            service.send_text_to_tenant(self.tenant.tenant_id, "通知")
        self.assertEqual(len(self.clients), 1)
        self.assertTrue(self.clients[0].closed)

    def test_image_uses_source_loader_caption_and_recipient_snapshot(self) -> None:
        self.store.update(self.tenant, "old-context")
        snapshot = self.store.load(self.tenant.tenant_id)
        self.store.update(self.tenant, "new-context")
        service = self.service()

        result = service.send_image_to(
            snapshot,
            ImageSource.remote("http://internal/image.png?secret=1"),
            caption="图片说明",
        )

        self.assertEqual(result.recipient_user_id, "user@im.wechat")
        self.assertEqual(
            self.clients[0].sent,
            [("user@im.wechat", "old-context", b"validated-image", "图片说明")],
        )
        self.assertTrue(self.clients[0].closed)

    def test_image_partial_delivery_error_is_preserved(self) -> None:
        self.store.update(self.tenant, "context")
        service = self.service(
            failure=PartialDeliveryError("图片发送失败，但文字说明可能已经发送")
        )
        with self.assertRaisesRegex(
            NotificationPartialDeliveryError, "文字说明可能已经发送"
        ):
            service.send_image_to_tenant(
                self.tenant.tenant_id,
                ImageSource.local(Path("image.png")),
                "说明",
            )
        self.assertEqual(len(self.clients), 1)
        self.assertTrue(self.clients[0].closed)


class FakeImageLoader:
    def __init__(self) -> None:
        self.sources = []

    def load(self, source):
        self.sources.append(source)
        return b"validated-image"


if __name__ == "__main__":
    unittest.main()
