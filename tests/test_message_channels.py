from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import src.api.routers.bots as bots_module
import src.core.messaging.credentials as credentials_module
from src.core.messaging import (
    DIRECT,
    GROUP,
    ChannelAddressStore,
    ChannelBindingError,
    ChannelCapabilities,
    InboundMessage,
    MessageRouter,
)
from src.core.messaging.adapters import FeishuAdapter, WeComAIBotAdapter
from src.core.application.bot import MessageBot
from src.core.config.loader import ChannelConfig
from src.core.modeling import CanonicalMessage
from src.core.storage.tenants import ConversationStore, TenantRegistry
from src.core.tooling import FinalAnswer
from tests._web_api_base import WebApiTestBase


def _message(
    channel_id: str,
    sender_id: str,
    event_id: str,
) -> InboundMessage:
    return InboundMessage(
        event_id=event_id,
        channel_id=channel_id,
        platform="test",
        account_id="bot",
        sender_id=sender_id,
        conversation_type=DIRECT,
        conversation_id=sender_id,
        text="hello",
        addressed_to_bot=True,
    )


class AdapterNormalizationTests(unittest.TestCase):
    def test_wecom_normalizes_group_callback_from_sdk_frame(self):
        adapter = WeComAIBotAdapter(
            "bot-id",
            "secret",
            channel_id="wecom-main",
            client_factory=lambda *_args: None,
        )
        message = adapter.normalize(
            {
                "headers": {"req_id": "request-1"},
                "body": {
                    "msgtype": "text",
                    "from_user_id": "zhangsan",
                    "chatid": "group-1",
                    "chattype": "group",
                    "text": {"content": "@机器人 项目进度"},
                },
            }
        )
        self.assertEqual(message.event_id, "request-1")
        self.assertEqual(message.conversation_type, GROUP)
        self.assertEqual(message.conversation_id, "group-1")
        self.assertTrue(message.addressed_to_bot)
        self.assertEqual(message.endpoint.target_id, "group-1")

    def test_feishu_uses_normalized_message_and_resource(self):
        adapter = FeishuAdapter(
            "cli_test",
            "secret",
            channel_id="feishu-main",
            client_factory=lambda *_args: None,
        )
        message = adapter.normalize(
            {
                "message_id": "om_1",
                "sender_id": "ou_user",
                "chat_id": "oc_group",
                "chat_type": "group",
                "body_text": "请总结图片",
                "mentioned_bot": True,
                "create_time": "1785400000000",
                "resources": [
                    {"resource_type": "image", "file_key": "img_1"},
                ],
            }
        )
        self.assertEqual(message.text, "请总结图片")
        self.assertEqual(message.conversation_type, GROUP)
        self.assertTrue(message.addressed_to_bot)
        self.assertEqual(message.first_image.adapter_ref["file_key"], "img_1")


class ChannelBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.registry = TenantRegistry(Path(self.temporary.name))
        self.addresses = ChannelAddressStore(self.registry)

    def test_one_time_code_links_a_second_channel_identity(self):
        source = _message("wechat-main", "wx-user", "source-1")
        tenant = self.addresses.resolve(source)
        self.addresses.record_endpoint(tenant, source)
        code = self.addresses.issue_binding_code(tenant, source)

        target = _message("feishu-main", "ou-user", "target-1")
        linked = self.addresses.bind_with_code(target, code)
        self.assertEqual(linked.tenant_id, tenant.tenant_id)
        self.assertEqual(
            self.addresses.resolve(target).tenant_id,
            tenant.tenant_id,
        )
        with self.assertRaises(ChannelBindingError):
            self.addresses.bind_with_code(
                _message("wecom-main", "new-user", "target-2"),
                code,
            )

    def test_invalid_binding_attempts_are_rate_limited(self):
        target = _message("feishu-main", "attacker", "target-1")
        for _index in range(5):
            with self.assertRaisesRegex(ChannelBindingError, "无效或已经过期"):
                self.addresses.bind_with_code(target, "AAAAAAAAAA")
        with self.assertRaisesRegex(ChannelBindingError, "尝试过于频繁"):
            self.addresses.bind_with_code(target, "BBBBBBBBBB")

    def test_conversation_context_is_partitioned_by_session(self):
        tenant = self.registry.resolve("bot", "user")
        store = ConversationStore(self.registry, max_messages=10)
        store.save_context(
            tenant.tenant_id,
            [CanonicalMessage("user", "私聊内容")],
            "direct",
        )
        store.save_context(
            tenant.tenant_id,
            [CanonicalMessage("user", "群聊内容")],
            "wecom-main:group:group-1",
        )
        self.assertEqual(store.load_context(tenant.tenant_id)[0].content, "私聊内容")
        self.assertEqual(
            store.load_context(
                tenant.tenant_id,
                "wecom-main:group:group-1",
            )[0].content,
            "群聊内容",
        )


class _GroupAdapter:
    platform = "test"
    account_id = "bot"
    channel_id = "wecom-main"
    capabilities = ChannelCapabilities(group=True, group_mentions=True)

    def __init__(self):
        self.sent = []

    def start(self, _emit, _stop_event):
        return None

    def send(self, endpoint, message):
        self.sent.append((endpoint, message))

    @contextmanager
    def typing(self, _endpoint):
        yield

    def load_attachment(self, _attachment):
        return b"image"

    def close(self):
        return None


class _AgentStub:
    image_prompt = "请描述图片"

    def __init__(self):
        self.calls = []

    def chat(self, subject, question, **kwargs):
        self.calls.append((subject, question, kwargs))
        return FinalAnswer("群聊安全回复")

    def has_pending_approval(self, _subject):
        raise AssertionError("群聊不得读取私聊审批状态")


class GroupPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.registry = TenantRegistry(Path(self.temporary.name))
        self.addresses = ChannelAddressStore(self.registry)
        self.adapter = _GroupAdapter()
        self.agent = _AgentStub()
        self.bot = MessageBot(
            self.agent,
            MessageRouter([self.adapter]),
            interaction_logger=lambda *_args: None,
            tenant_registry=self.registry,
            conversation_store=ConversationStore(self.registry, 10),
            address_store=self.addresses,
            channel_configs={
                "wecom-main": ChannelConfig(
                    id="wecom-main",
                    type="wecom_aibot",
                    enabled=True,
                    agent_id="general",
                    settings={"group_policy": "mention_only"},
                )
            },
        )

    @staticmethod
    def message(addressed):
        return InboundMessage(
            event_id="group-{}".format(addressed),
            channel_id="wecom-main",
            platform="wecom_aibot",
            account_id="bot",
            sender_id="member",
            conversation_type=GROUP,
            conversation_id="group-1",
            text="同意",
            addressed_to_bot=addressed,
        )

    def test_only_mentions_enter_group_safe_chat(self):
        self.bot.handle_inbound(self.message(False))
        self.assertEqual(self.agent.calls, [])
        self.bot.handle_inbound(self.message(True))
        self.assertEqual(len(self.agent.calls), 1)
        kwargs = self.agent.calls[0][2]
        self.assertFalse(kwargs["allow_tools"])
        self.assertFalse(kwargs["allow_private_context"])
        self.assertEqual(kwargs["agent_id"], "general")
        self.assertIn("wecom-main:group:group-1", kwargs["conversation_id"])
        self.assertEqual(self.adapter.sent[0][1].text, "群聊安全回复")


class ChannelsApiTests(WebApiTestBase):
    def setUp(self):
        self.channel_files = tempfile.TemporaryDirectory()
        self.addCleanup(self.channel_files.cleanup)
        root = Path(self.channel_files.name)
        self.config_root = root / "config"
        self.config_root.mkdir()
        self.credential_root = root / "credentials"
        self.credential_root.mkdir()
        config_patcher = patch.object(bots_module, "CONFIG_DIR", self.config_root)
        credential_patcher = patch.object(
            credentials_module,
            "channel_credentials_path",
            side_effect=lambda channel_id: self.credential_root
            / "{}.json".format(channel_id),
        )
        config_patcher.start()
        credential_patcher.start()
        self.addCleanup(config_patcher.stop)
        self.addCleanup(credential_patcher.stop)
        super().setUp()

    def test_channel_configuration_and_credentials_are_separate(self):
        listing = self.client.get("/api/channels")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(
            {item["type"] for item in listing.json()["providers"]},
            {"wechat_ilink", "wecom_aibot", "feishu"},
        )

        configured = self.client.put(
            "/api/channels/wecom-main",
            json={
                "type": "wecom_aibot",
                "enabled": True,
                "agent_id": "general",
                "settings": {"group_policy": "mention_only"},
            },
        )
        self.assertEqual(configured.status_code, 200, configured.text)
        saved = self.client.put(
            "/api/channels/wecom-main/credentials",
            json={
                "credentials": {
                    "bot_id": "bot-id",
                    "secret": "top-secret",
                }
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        response = self.client.get("/api/channels")
        self.assertEqual(response.status_code, 200, response.text)
        serialized = response.text
        self.assertNotIn("top-secret", serialized)
        channel = next(
            item
            for item in response.json()["channels"]
            if item["id"] == "wecom-main"
        )
        self.assertTrue(channel["credential_configured"])
        config_text = (self.config_root / "channels.json").read_text("utf-8")
        self.assertNotIn("top-secret", config_text)

    def test_viewer_can_read_but_cannot_manage_channels(self):
        self.assertEqual(self.viewer_client.get("/api/channels").status_code, 200)
        response = self.viewer_client.put(
            "/api/channels/feishu-main",
            json={
                "type": "feishu",
                "enabled": True,
                "agent_id": "general",
                "settings": {"group_policy": "mention_only"},
            },
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_delete_channel_removes_config_and_credentials(self):
        configured = self.client.put(
            "/api/channels/wecom-main",
            json={
                "type": "wecom_aibot",
                "enabled": True,
                "agent_id": "general",
                "settings": {"group_policy": "mention_only"},
            },
        )
        self.assertEqual(configured.status_code, 200, configured.text)
        saved = self.client.put(
            "/api/channels/wecom-main/credentials",
            json={"credentials": {"bot_id": "bot-id", "secret": "top-secret"}},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        credential_file = self.credential_root / "wecom-main.json"
        self.assertTrue(credential_file.is_file())

        deleted = self.client.delete("/api/channels/wecom-main")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["deleted"])

        listing = self.client.get("/api/channels")
        self.assertEqual(listing.status_code, 200, listing.text)
        ids = {item["id"] for item in listing.json()["channels"]}
        self.assertNotIn("wecom-main", ids)
        self.assertFalse(credential_file.is_file())

    def test_delete_unknown_channel_returns_error(self):
        response = self.client.delete("/api/channels/does-not-exist")
        self.assertEqual(response.status_code, 400, response.text)

    def test_viewer_cannot_delete_channel(self):
        response = self.viewer_client.delete("/api/channels/wecom-main")
        self.assertEqual(response.status_code, 403, response.text)


if __name__ == "__main__":
    unittest.main()
