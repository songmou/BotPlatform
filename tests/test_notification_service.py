from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from src.core.integrations.ilink import (
    Credentials,
    ILinkAPIError,
    ILinkError,
    PartialDeliveryError,
    SessionExpired,
)
from src.core.integrations.images import ImageSource, ImageSourceError
from src.core.plugins.codex_tasks import CodexTaskStore
from src.core.services.notification import (
    NotificationCredentialsError,
    NotificationDeliveryError,
    NotificationDispatcher,
    NotificationError,
    NotificationPartialDeliveryError,
    NotificationRecipientError,
    NotificationRecipientStaleError,
    NotificationService,
    TenantRecipientStore,
)
from src.core.storage.tenants import TenantRegistry


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

    def test_prepare_failed_preserves_structured_recipient_error(self) -> None:
        self.store.update(self.tenant, "stale-context")
        service = self.service(
            failure=ILinkAPIError(1, 1001, "prepare failed")
        )
        with self.assertRaises(NotificationRecipientStaleError) as raised:
            service.send_text_to_tenant(self.tenant.tenant_id, "通知")
        self.assertEqual(raised.exception.ret, 1)
        self.assertEqual(raised.exception.errcode, 1001)
        self.assertEqual(raised.exception.errmsg, "prepare failed")
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

    def test_outbox_retries_forever_and_preserves_tenant_fifo(self) -> None:
        self.store.update(self.tenant, "context")
        failures = [ILinkError("temporary"), None, None]

        def factory(_credentials):
            client = FakeILink(failure=failures.pop(0))
            self.clients.append(client)
            return client

        service = NotificationService(
            credentials_loader=lambda: self.credentials,
            recipient_store=self.store,
            client_factory=factory,
            image_loader=FakeImageLoader(),
        )
        first = service.enqueue_text_to_tenant(
            self.tenant.tenant_id,
            "第一条",
            source_type="test",
            source_key="first",
        )
        second = service.enqueue_text_to_tenant(
            self.tenant.tenant_id,
            "第二条",
            source_type="test",
            source_key="second",
        )

        self.assertEqual(service.dispatch_due(), 0)
        self.assertEqual(len(self.clients), 1)
        with self.registry.database.transaction(immediate=True) as connection:
            states = connection.execute(
                "SELECT source_key, delivery_status FROM notification_outbox "
                "ORDER BY outbox_id"
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in states],
                [("first", "retry"), ("second", "pending")],
            )
            connection.execute(
                "UPDATE notification_outbox SET next_attempt_at=? "
                "WHERE notification_id=?",
                ("2000-01-01T00:00:00+00:00", first.notification_ids[0]),
            )

        self.assertEqual(service.dispatch_due(), 1)
        self.assertEqual(service.dispatch_due(), 1)
        self.assertEqual(self.clients[1].sent[-1][2], "第一条")
        self.assertEqual(self.clients[2].sent[-1][2], "第二条")
        self.assertEqual(service.outbox.status(second.notification_ids), "sent")

    def test_waiting_recipient_requeues_after_new_private_message(self) -> None:
        self.store.update(self.tenant, "stale")
        service = self.service(failure=ILinkAPIError(1, 1001, "prepare failed"))
        queued = service.enqueue_text_to_tenant(
            self.tenant.tenant_id, "积压通知", source_type="test"
        )

        self.assertEqual(service.dispatch_due(), 0)
        self.assertEqual(
            service.outbox.get(queued.notification_ids[0])["delivery_status"],
            "waiting_recipient",
        )

        self.store.update(self.tenant, "fresh")
        service.client_factory = lambda _credentials: FakeILink()
        self.assertEqual(service.on_recipient_refreshed(self.tenant.tenant_id), 1)
        self.assertEqual(service.dispatch_due(), 1)
        self.assertEqual(service.outbox.status(queued.notification_ids), "sent")

    def test_image_caption_is_split_snapshotted_and_cleaned_after_delivery(self) -> None:
        self.store.update(self.tenant, "context")
        service = self.service()
        queued = service.enqueue_image_to_tenant(
            self.tenant.tenant_id,
            ImageSource.local(Path("original-will-not-be-read.png")),
            caption="图片说明",
            source_type="test",
            source_key="image-batch",
            attempt_immediately=True,
        )

        self.assertEqual(queued.status, "sent")
        self.assertEqual(self.clients[0].sent[-1][2], "图片说明")
        self.assertEqual(self.clients[1].sent[-1][2], b"validated-image")
        self.assertEqual(self.clients[1].sent[-1][3], "")
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT text_payload, image_path, delivery_status "
                "FROM notification_outbox ORDER BY outbox_id"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [(None, None, "sent"), (None, None, "sent")],
        )
        self.assertEqual(
            list(
                (
                    self.registry.tenant_root(self.tenant.tenant_id)
                    / "notification_outbox"
                ).iterdir()
            ),
            [],
        )

    def test_dedupe_and_expired_lease_are_recoverable(self) -> None:
        first = self.service().enqueue_text_to_tenant(
            self.tenant.tenant_id,
            "一次",
            source_type="test",
            source_key="same",
        )
        service = self.service()
        duplicate = service.enqueue_text_to_tenant(
            self.tenant.tenant_id,
            "不会覆盖",
            source_type="test",
            source_key="same",
        )
        self.assertEqual(first.notification_ids, duplicate.notification_ids)

        claimed = service.outbox.claim_due()
        self.assertEqual(len(claimed), 1)
        self.assertEqual(service.outbox.claim_due(), [])
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE notification_outbox SET lease_expires_at=? "
                "WHERE notification_id=?",
                ("2000-01-01T00:00:00+00:00", first.notification_ids[0]),
            )
        reclaimed = service.outbox.claim_due()
        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(
            reclaimed[0]["notification_id"], first.notification_ids[0]
        )

    def test_existing_codex_event_is_adopted_and_status_is_mirrored(self) -> None:
        self.store.update(self.tenant, "context")
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO codex_task_runs("
                "thread_id, tenant_id, project_id, title, status, created_at, "
                "notification_status, origin, phase, updated_at, last_seen_at"
                ") VALUES ('thread-outbox', ?, 'project', '可靠通知', 'completed', "
                "'2026-01-01T00:00:00+00:00', 'pending', 'external', 'completed', "
                "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
                (self.tenant.tenant_id,),
            )
            connection.execute(
                "INSERT INTO codex_task_events("
                "event_key, thread_id, tenant_id, event_type, message, "
                "delivery_status, created_at"
                ") VALUES ('codex-outbox-key', 'thread-outbox', ?, 'completed', "
                "'Codex 已完成', 'pending', '2026-01-01T00:00:00+00:00')",
                (self.tenant.tenant_id,),
            )

        service = self.service()
        self.assertEqual(service.dispatch_due(), 1)
        with self.registry.database.read() as connection:
            event = connection.execute(
                "SELECT delivery_status, sent_at FROM codex_task_events "
                "WHERE event_key='codex-outbox-key'"
            ).fetchone()
            task = connection.execute(
                "SELECT notification_status FROM codex_task_runs "
                "WHERE thread_id='thread-outbox'"
            ).fetchone()
        self.assertEqual(event["delivery_status"], "sent")
        self.assertIsNotNone(event["sent_at"])
        self.assertEqual(task["notification_status"], "sent")

    def test_new_codex_event_and_outbox_row_are_created_together(self) -> None:
        store = CodexTaskStore(self.registry, durable_outbox=True)
        store.create(
            "thread-atomic",
            self.tenant.tenant_id,
            "project",
            "原子通知",
            notify=True,
        )
        event = store.enqueue_event(
            "codex-atomic-key",
            "thread-atomic",
            self.tenant.tenant_id,
            "completed",
            "Codex 原子入队",
        )

        self.assertEqual(event["delivery_status"], "sending")
        with self.registry.database.read() as connection:
            outbox = connection.execute(
                "SELECT source_ref, delivery_status, text_payload "
                "FROM notification_outbox WHERE source_key='codex-atomic-key'"
            ).fetchone()
        self.assertEqual(outbox["source_ref"], str(event["event_id"]))
        self.assertEqual(outbox["delivery_status"], "pending")
        self.assertEqual(outbox["text_payload"], "Codex 原子入队")

    def test_permanently_missing_image_fails_and_unblocks_later_text(self) -> None:
        self.store.update(self.tenant, "context")
        service = self.service()
        image = service.enqueue_image_to_tenant(
            self.tenant.tenant_id,
            ImageSource.local(Path("snapshot.png")),
            source_type="test",
        )
        text = service.enqueue_text_to_tenant(
            self.tenant.tenant_id,
            "仍需发送",
            source_type="test",
        )
        image_row = service.outbox.get(image.notification_ids[0])
        Path(str(image_row["image_path"])).unlink()
        service.image_loader = MissingImageLoader()

        self.assertEqual(service.dispatch_due(), 0)
        self.assertEqual(
            service.outbox.get(image.notification_ids[0])["delivery_status"],
            "failed",
        )
        self.assertEqual(service.dispatch_due(), 1)
        self.assertEqual(service.outbox.status(text.notification_ids), "sent")

    def test_tenant_delete_cascades_outbox_and_removes_cached_image(self) -> None:
        service = self.service()
        queued = service.enqueue_image_to_tenant(
            self.tenant.tenant_id,
            ImageSource.local(Path("snapshot.png")),
            source_type="test",
        )
        image_path = Path(
            str(service.outbox.get(queued.notification_ids[0])["image_path"])
        )
        self.assertTrue(image_path.exists())

        self.registry.delete(self.tenant)

        self.assertFalse(image_path.exists())
        with self.registry.database.read() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM notification_outbox"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_dispatcher_delivers_queued_work_in_background(self) -> None:
        self.store.update(self.tenant, "context")
        service = self.service()
        queued = service.enqueue_text_to_tenant(
            self.tenant.tenant_id, "后台补发", source_type="test"
        )
        dispatcher = NotificationDispatcher(service, poll_interval_seconds=0.01)
        dispatcher.start()
        try:
            deadline = time.monotonic() + 1
            while (
                service.outbox.status(queued.notification_ids) != "sent"
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
        finally:
            dispatcher.shutdown()

        self.assertEqual(service.outbox.status(queued.notification_ids), "sent")


class FakeImageLoader:
    def __init__(self) -> None:
        self.sources = []

    def load(self, source):
        self.sources.append(source)
        return b"validated-image"


class MissingImageLoader:
    def load(self, _source):
        raise ImageSourceError("图片缓存不存在")


if __name__ == "__main__":
    unittest.main()
