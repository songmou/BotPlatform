"""Organization channel tenant routing and legacy cleanup tests."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from src.core.application.bot import MessageBot
from src.core.integrations.keychain import KeychainError, KeychainService
from src.core.messaging import (
    DIRECT,
    ChannelAddressStore,
    ChannelCapabilities,
    InboundMessage,
    MessageRouter,
)
from src.core.modeling import CanonicalMessage
from src.core.storage.organizations import OrganizationStore
from src.core.storage.tenants import ConversationStore, TenantRegistry, TenantStoreError


NOW = "2026-08-14T00:00:00+00:00"


class _Adapter:
    platform = "feishu"
    account_id = "bot"
    capabilities = ChannelCapabilities()

    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id
        self.sent = []

    def start(self, _emit, _stop_event):
        return None

    def send(self, endpoint, message):
        self.sent.append((endpoint, message))

    @contextmanager
    def typing(self, _endpoint):
        yield

    def load_attachment(self, _attachment):
        return b""

    def close(self):
        return None


class _Agent:
    image_prompt = "请描述图片"

    def chat(self, *_args, **_kwargs):
        raise AssertionError("命令不应进入智能体对话")


class OrganizationChannelTenantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.registry = TenantRegistry(Path(self.temporary.name) / "data")

    def _seed_organization(self, channel_types=("feishu",)):
        organization_id = str(uuid.uuid4())
        channel_ids = [str(uuid.uuid4()) for _ in channel_types]
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO tenants(tenant_id, bot_id, user_id, created_at) "
                "VALUES (?, 'organization', ?, ?)",
                (organization_id, "organization:" + organization_id, NOW),
            )
            connection.execute(
                "INSERT INTO organizations(organization_id, name, status, legacy, "
                "created_at, updated_at) VALUES (?, '测试组织', 'active', 0, ?, ?)",
                (organization_id, NOW, NOW),
            )
            for index, (channel_id, channel_type) in enumerate(
                zip(channel_ids, channel_types)
            ):
                connection.execute(
                    "INSERT INTO organization_channels("
                    "channel_instance_id, organization_id, channel_id, channel_type, "
                    "agent_id, enabled, settings_json, revision, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, 'general', 1, '{}', 1, ?, ?)",
                    (
                        channel_id,
                        organization_id,
                        "channel-{}".format(index + 1),
                        channel_type,
                        NOW,
                        NOW,
                    ),
                )
        return organization_id, channel_ids

    @staticmethod
    def _message(channel_id: str, sender_id: str, text: str = "你好"):
        return InboundMessage(
            event_id=str(uuid.uuid4()),
            channel_id=channel_id,
            platform="feishu",
            account_id="bot",
            sender_id=sender_id,
            conversation_type=DIRECT,
            conversation_id=sender_id,
            text=text,
        )

    def test_organization_channels_share_tenant_but_isolate_direct_sessions(self):
        organization_id, channel_ids = self._seed_organization(
            ("feishu", "wechat_ilink")
        )
        OrganizationStore(self.registry)
        addresses = ChannelAddressStore(self.registry)

        first_message = self._message(channel_ids[0], "user-a")
        second_message = self._message(channel_ids[1], "user-b")
        first = addresses.resolve(first_message)
        second = addresses.resolve(second_message)
        self.assertEqual(first.tenant_id, organization_id)
        self.assertEqual(second.tenant_id, organization_id)
        self.assertIsNone(first.personal_tenant_id)
        self.assertIsNone(second.personal_tenant_id)

        first_conversation = addresses.ensure_organization_conversation(
            first_message, first
        )
        second_conversation = addresses.ensure_organization_conversation(
            second_message, second
        )
        first_endpoint = addresses.record_endpoint(first, first_message)
        second_endpoint = addresses.record_endpoint(second, second_message)
        self.assertIsNotNone(first_conversation)
        self.assertIsNotNone(second_conversation)
        self.assertIsNone(
            addresses.personal_tenant_for_endpoint(
                organization_id, first_endpoint.endpoint_id
            )
        )

        bot = MessageBot(
            _Agent(),
            MessageRouter(),
            interaction_logger=lambda *_args: None,
            tenant_registry=self.registry,
            address_store=addresses,
        )
        first_session = bot._session_key(first_message)
        second_session = bot._session_key(second_message)
        self.assertNotEqual(first_session, second_session)
        conversations = ConversationStore(self.registry, 10)
        conversations.save_context(
            organization_id,
            [CanonicalMessage("user", "飞书消息")],
            session_key=first_session,
        )
        conversations.save_context(
            organization_id,
            [CanonicalMessage("user", "微信消息")],
            session_key=second_session,
        )
        self.assertEqual(
            [item.content for item in conversations.load_context(
                organization_id, first_session
            )],
            ["飞书消息"],
        )
        self.assertEqual(
            [item.content for item in conversations.load_context(
                organization_id, second_session
            )],
            ["微信消息"],
        )
        with self.registry.database.read() as connection:
            private_count = connection.execute(
                "SELECT COUNT(*) FROM tenants "
                "WHERE bot_id LIKE 'organization-channel:%'"
            ).fetchone()[0]
            identity_tenants = {
                str(row[0])
                for row in connection.execute(
                    "SELECT tenant_id FROM channel_identities"
                ).fetchall()
            }
            endpoint_tenants = {
                str(row[0])
                for row in connection.execute(
                    "SELECT tenant_id FROM delivery_endpoints"
                ).fetchall()
            }
        self.assertEqual(private_count, 0)
        self.assertEqual(identity_tenants, {organization_id})
        self.assertEqual(endpoint_tenants, {organization_id})
        self.assertNotEqual(first_endpoint.endpoint_id, second_endpoint.endpoint_id)

    def test_startup_cleanup_discards_old_data_and_preserves_routing(self):
        organization_id, channel_ids = self._seed_organization()
        channel_id = channel_ids[0]
        private_id = str(uuid.uuid4())
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO tenants(tenant_id, bot_id, user_id, created_at) "
                "VALUES (?, ?, 'feishu:user-a', ?)",
                (private_id, "organization-channel:" + channel_id, NOW),
            )
            connection.execute(
                "INSERT INTO organizations(organization_id, name, status, legacy, "
                "created_at, updated_at) VALUES (?, '误建个人空间', 'active', 1, ?, ?)",
                (private_id, NOW, NOW),
            )
            connection.execute(
                "INSERT INTO channel_identities("
                "identity_id, tenant_id, channel_id, platform, account_id, "
                "external_user_id, active_organization_id, created_at, last_seen_at"
                ") VALUES ('identity-old', ?, ?, 'feishu', 'bot', 'user-a', ?, ?, ?)",
                (private_id, channel_id, organization_id, NOW, NOW),
            )
            connection.execute(
                "INSERT INTO delivery_endpoints("
                "endpoint_id, identity_id, tenant_id, channel_id, platform, "
                "account_id, conversation_type, conversation_id, recipient_id, "
                "last_seen_at) VALUES ("
                "'endpoint-old', 'identity-old', ?, ?, 'feishu', 'bot', "
                "'direct', 'user-a', 'user-a', ?)",
                (private_id, channel_id, NOW),
            )
            connection.execute(
                "INSERT INTO conversation_events(tenant_id, role, content, created_at) "
                "VALUES (?, 'user', '旧消息', ?)",
                (private_id, NOW),
            )
            reference = KeychainService.reference(private_id, "ctsoa")
            connection.execute(
                "INSERT INTO integrations(tenant_id, integration_id, metadata_json, "
                "updated_at) VALUES (?, 'ctsoa', ?, ?)",
                (
                    private_id,
                    json.dumps(
                        {
                            "keychain_service": reference.service,
                            "keychain_account": reference.account,
                        }
                    ),
                    NOW,
                ),
            )
        private_root = self.registry.tenant_root(private_id)
        (private_root / "workspace").mkdir(parents=True)
        (private_root / "workspace" / "old.txt").write_text(
            "discard", encoding="utf-8"
        )
        keychain = KeychainService(
            storage_path=self.registry.system_root / "integration_credentials.json"
        )
        keychain.set_secret(reference, "secret")

        organizations = OrganizationStore(self.registry)

        self.assertEqual(organizations.get(organization_id)["name"], "测试组织")
        with self.assertRaises(TenantStoreError):
            self.registry.get(private_id)
        self.assertFalse(private_root.exists())
        with self.assertRaises(KeychainError):
            keychain.get_secret(reference)
        with self.registry.database.read() as connection:
            identity = connection.execute(
                "SELECT tenant_id FROM channel_identities "
                "WHERE identity_id='identity-old'"
            ).fetchone()
            endpoint = connection.execute(
                "SELECT tenant_id FROM delivery_endpoints "
                "WHERE endpoint_id='endpoint-old'"
            ).fetchone()
            old_events = connection.execute(
                "SELECT COUNT(*) FROM conversation_events WHERE tenant_id=?",
                (private_id,),
            ).fetchone()[0]
        self.assertEqual(str(identity["tenant_id"]), organization_id)
        self.assertEqual(str(endpoint["tenant_id"]), organization_id)
        self.assertEqual(old_events, 0)

        tenant = ChannelAddressStore(self.registry).resolve(
            self._message(channel_id, "user-a")
        )
        self.assertEqual(tenant.tenant_id, organization_id)
        self.assertIsNone(tenant.personal_tenant_id)

    def test_delete_data_is_disabled_for_organization_channel(self):
        organization_id, channel_ids = self._seed_organization()
        OrganizationStore(self.registry)
        channel_id = channel_ids[0]
        addresses = ChannelAddressStore(self.registry)
        adapter = _Adapter(channel_id)
        bot = MessageBot(
            _Agent(),
            MessageRouter([adapter]),
            interaction_logger=lambda *_args: None,
            tenant_registry=self.registry,
            conversation_store=ConversationStore(self.registry, 10),
            address_store=addresses,
        )

        bot.handle_inbound(self._message(channel_id, "user-a", "/delete-data"))
        bot.handle_inbound(
            self._message(channel_id, "user-a", "/confirm-delete 123456")
        )

        self.assertEqual(len(adapter.sent), 2)
        self.assertIn("管理面板", adapter.sent[0][1].text)
        self.assertIn("管理面板", adapter.sent[1][1].text)
        self.assertEqual(self.registry.get(organization_id).tenant_id, organization_id)
        self.assertEqual(bot._deletion_pending, {})


if __name__ == "__main__":
    unittest.main()
