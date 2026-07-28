from __future__ import annotations

import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from src.core.messaging import (
    DIRECT,
    GROUP,
    AuthenticationExpired,
    ChannelAddressStore,
    ChannelCapabilities,
    ChannelManager,
    InboundMessage,
    MessageInboxStore,
    MessageRouter,
    OutboundMessage,
)
from src.core.integrations.ilink import Credentials
from src.core.messaging.adapters import WeChatILinkAdapter
from src.core.services.notification import NotificationService, TenantRecipientStore
from src.core.storage.tenants import TenantRegistry


class FakeAdapter:
    platform = "fake"
    account_id = "bot"
    capabilities = ChannelCapabilities()

    def __init__(self, channel_id, messages=(), failure=None):
        self.channel_id = channel_id
        self.messages = list(messages)
        self.failure = failure
        self.closed = False
        self.sent = []

    def start(self, emit, _stop_event):
        for message in self.messages:
            emit(message)
        if self.failure:
            raise self.failure

    def send(self, endpoint, message):
        self.sent.append((endpoint, message))

    @contextmanager
    def typing(self, _endpoint):
        yield

    def load_attachment(self, _attachment):
        return b""

    def close(self):
        self.closed = True


class FakeILinkClient:
    def __init__(self):
        self.credentials = Credentials("token", "https://gateway", "wechat-bot", "owner")
        self.sent = []

    def send_text(self, user_id, context_token, text, client_id=None):
        self.sent.append((user_id, context_token, text, client_id))

    def close(self):
        return None


def inbound(channel_id, event_id, sender="user", kind=DIRECT, occurred_at=""):
    return InboundMessage(
        event_id=event_id,
        channel_id=channel_id,
        platform="fake",
        account_id="bot",
        sender_id=sender,
        conversation_type=kind,
        conversation_id=sender if kind == DIRECT else "group",
        text="hello",
        occurred_at=occurred_at or "2026-07-27T00:00:00+00:00",
    )


class MessagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.registry = TenantRegistry(Path(self.temporary.name) / "data")

    def test_inbox_deduplicates_and_recovers_expired_processing_lease(self):
        store = MessageInboxStore(self.registry)
        message = inbound("first", "event-1")
        self.assertTrue(store.enqueue(message))
        self.assertFalse(store.enqueue(message))

        claimed = store.claim(lease_seconds=-1)
        self.assertEqual(store.decode(claimed), message)
        reclaimed = store.claim()
        self.assertEqual(reclaimed["inbox_id"], claimed["inbox_id"])
        store.finish(int(reclaimed["inbox_id"]), "done")
        self.assertIsNone(store.claim())

    def test_wechat_adapter_normalizes_and_keeps_protocol_context_private(self):
        client = FakeILinkClient()
        adapter = WeChatILinkAdapter(client)
        message = adapter.normalize(
            {
                "message_id": "wechat-event",
                "message_type": 1,
                "from_user_id": "wechat-user",
                "context_token": "reply-token",
                "item_list": [
                    {"type": 1, "text_item": {"text": "hello"}},
                ],
            }
        )
        self.assertEqual(message.text, "hello")
        self.assertEqual(message.endpoint.recipient_id, "wechat-user")
        self.assertNotIn("context_token", message.to_dict())
        adapter.send(
            message.endpoint,
            OutboundMessage(text="world", idempotency_key="notification-id"),
        )
        self.assertEqual(
            client.sent,
            [("wechat-user", "reply-token", "world", "notification-id")],
        )
        with self.assertRaises(ValueError):
            adapter.normalize(
                {
                    "message_type": 2,
                    "from_user_id": "wechat-user",
                    "context_token": "reply-token",
                }
            )

    def test_identities_are_channel_scoped_and_latest_endpoint_wins(self):
        addresses = ChannelAddressStore(self.registry)
        first_message = inbound(
            "first",
            "first-event",
            occurred_at="2026-07-27T00:00:00+00:00",
        )
        second_message = inbound(
            "second",
            "second-event",
            occurred_at="2026-07-27T01:00:00+00:00",
        )
        first_tenant = addresses.resolve(first_message)
        second_tenant = addresses.resolve(second_message)
        self.assertNotEqual(first_tenant.tenant_id, second_tenant.tenant_id)

        addresses.record_endpoint(first_tenant, first_message)
        newer_same_tenant = InboundMessage(
            **{
                **second_message.to_dict(),
                "sender_id": "linked-user",
                "conversation_id": "linked-user",
            }
        )
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO channel_identities("
                "identity_id, tenant_id, channel_id, platform, account_id, "
                "external_user_id, created_at, last_seen_at"
                ") VALUES ('linked', ?, 'second', 'fake', 'bot', 'linked-user', "
                "'2026-07-27T00:00:00+00:00', '2026-07-27T01:00:00+00:00')",
                (first_tenant.tenant_id,),
            )
        addresses.record_endpoint(first_tenant, newer_same_tenant)
        self.assertEqual(
            addresses.latest_endpoint(first_tenant.tenant_id).channel_id,
            "second",
        )

    def test_manager_runs_receivers_in_parallel_dedupes_and_ignores_groups(self):
        handled = []
        first = FakeAdapter(
            "first",
            [inbound("first", "same"), inbound("first", "same")],
        )
        second = FakeAdapter(
            "second",
            [
                inbound("second", "direct"),
                inbound("second", "group", kind=GROUP),
            ],
            failure=RuntimeError("one channel failed"),
        )
        manager = ChannelManager(
            [first, second],
            MessageInboxStore(self.registry),
            handled.append,
            poll_interval_seconds=0.01,
        )
        manager.start()
        deadline = time.monotonic() + 1
        while len(handled) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        manager.shutdown()

        self.assertEqual(
            {(item.channel_id, item.event_id) for item in handled},
            {("first", "same"), ("second", "direct")},
        )
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_notification_uses_only_the_last_active_bound_endpoint(self):
        addresses = ChannelAddressStore(self.registry)
        old_message = inbound(
            "first",
            "old",
            occurred_at="2026-07-27T00:00:00+00:00",
        )
        tenant = addresses.resolve(old_message)
        addresses.record_endpoint(tenant, old_message)
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO channel_identities("
                "identity_id, tenant_id, channel_id, platform, account_id, "
                "external_user_id, created_at, last_seen_at"
                ") VALUES ('bound-second', ?, 'second', 'fake', 'bot', "
                "'bound-user', '2026-07-27T01:00:00+00:00', "
                "'2026-07-27T01:00:00+00:00')",
                (tenant.tenant_id,),
            )
        new_message = inbound(
            "second",
            "new",
            sender="bound-user",
            occurred_at="2026-07-27T01:00:00+00:00",
        )
        addresses.record_endpoint(tenant, new_message)

        first_adapter = FakeAdapter("first")
        second_adapter = FakeAdapter("second")
        service = NotificationService(
            credentials_loader=None,
            recipient_store=TenantRecipientStore(self.registry),
            message_router=MessageRouter([first_adapter, second_adapter]),
            address_store=addresses,
        )
        result = service.enqueue_text_to_tenant(
            tenant.tenant_id,
            "only once",
            attempt_immediately=True,
        )

        self.assertEqual(result.status, "sent")
        self.assertEqual(first_adapter.sent, [])
        self.assertEqual(second_adapter.sent[0][1].text, "only once")

    def test_authentication_failure_is_isolated_in_channel_status(self):
        healthy = FakeAdapter("healthy")
        expired = FakeAdapter(
            "expired",
            failure=AuthenticationExpired("expired"),
        )
        manager = ChannelManager(
            [healthy, expired],
            MessageInboxStore(self.registry),
            lambda _message: None,
        )
        manager.start()
        deadline = time.monotonic() + 1
        states = {}
        while time.monotonic() < deadline:
            states = {item.channel_id: item.state for item in manager.statuses()}
            if states.get("expired") == "authentication_required":
                break
            time.sleep(0.01)
        manager.shutdown()
        self.assertEqual(states["expired"], "authentication_required")
