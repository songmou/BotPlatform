from __future__ import annotations

import itertools
import os
import re
import tempfile
import time
import unittest
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from src.core.config.loader import ScriptDefinition, ToolConfig
from src.core.integrations.ilink import Credentials
from src.core.integrations.keychain import KeychainReference, KeychainService
from src.core.application import MessageBot, run_notify_command
from src.core.messaging import (
    DIRECT,
    ChannelCapabilities,
    InboundMessage,
    MessageRouter,
)
from src.core.services.integration import IntegrationService
from src.core.services.notification import TenantRecipientStore
from src.core.services.script import ScriptService
from src.core.storage.tenants import (
    ConversationStore,
    IntegrationStore,
    ScheduleStore,
    SettingsStore,
    TenantRegistry,
    TenantStoreError,
)
from src.core.tooling import ToolError, ToolRuntime


class FakeKeychain:
    def __init__(self):
        self.values = {}

    def reference(self, tenant_id, integration_id):
        return KeychainReference("test.{}.{}".format(tenant_id, integration_id))

    def set_secret(self, reference, secret):
        self.values[(reference.service, reference.account)] = secret

    def get_secret(self, reference):
        return self.values[(reference.service, reference.account)]

    def delete_secret(self, reference):
        self.values.pop((reference.service, reference.account), None)

    def exists(self, reference):
        return (reference.service, reference.account) in self.values


_EVENT_IDS = itertools.count(1)


def direct_message(user, text):
    return InboundMessage(
        event_id="event-{}".format(next(_EVENT_IDS)),
        channel_id="wechat-main",
        platform="wechat_ilink",
        account_id="bot",
        sender_id=user,
        conversation_type=DIRECT,
        conversation_id=user,
        text=text,
        reply_context={"context_token": "context"},
    )


class FakeChannel:
    channel_id = "wechat-main"
    platform = "wechat_ilink"
    account_id = "bot"
    capabilities = ChannelCapabilities(typing=True)

    def __init__(self):
        self.sent = []

    def start(self, emit, stop_event):
        raise AssertionError("tests call handle_inbound directly")

    def send(self, endpoint, message):
        self.sent.append(
            (
                endpoint.recipient_id,
                str(endpoint.route_context.get("context_token") or ""),
                message.text,
            )
        )

    @contextmanager
    def typing(self, endpoint):
        yield

    def load_attachment(self, attachment):
        raise AssertionError("attachments are not used in these tests")

    def close(self):
        pass


class MultiTenantStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = TenantRegistry(self.root / "data")
        self.a = self.registry.resolve("bot", "wechat-a")
        self.b = self.registry.resolve("bot", "wechat-b")

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits (0o700/0o600) are asserted")
    def test_identity_state_history_and_deletion_are_isolated(self):
        self.assertNotEqual(self.a.tenant_id, self.b.tenant_id)
        self.assertNotIn("wechat-a", str(self.registry.tenant_root(self.a.tenant_id)))

        conversations = ConversationStore(self.registry, max_messages=2)
        from src.core.modeling import CanonicalMessage

        conversations.save_context(
            self.a.tenant_id,
            [CanonicalMessage("user", "a1"), CanonicalMessage("assistant", "a2")],
        )
        conversations.append_transcript(self.a.tenant_id, "user", "永久-A")
        self.assertEqual(conversations.load_context(self.b.tenant_id), [])
        self.assertFalse(
            (
                self.registry.tenant_root(self.b.tenant_id)
                / "conversation"
                / "transcript.jsonl"
            ).exists()
        )

        settings = SettingsStore(self.registry)
        settings.set_model_mode(self.a.tenant_id, "pro")
        self.assertEqual(settings.model_mode(self.a.tenant_id), "pro")
        self.assertEqual(settings.model_mode(self.b.tenant_id), "auto")

        root = self.registry.tenant_root(self.a.tenant_id)
        self.assertEqual(os.stat(root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.registry.database_path).st_mode & 0o777, 0o600)
        self.assertFalse((root / "conversation" / "context.json").exists())

        old_id = self.a.tenant_id
        self.registry.delete(self.a)
        self.assertFalse(root.exists())
        with self.assertRaises(TenantStoreError):
            self.registry.get(old_id)
        recreated = self.registry.resolve("bot", "wechat-a")
        self.assertNotEqual(recreated.tenant_id, old_id)

    @unittest.skipUnless(os.name == "posix", "symlink creation needs privileges on Windows")
    def test_recipient_schedule_and_tool_roots_are_tenant_scoped(self):
        recipients = TenantRecipientStore(self.registry)
        recipients.update(self.a, "context-a")
        recipients.update(self.b, "context-b")
        self.assertEqual(recipients.load(self.a.tenant_id).context_token, "context-a")
        self.assertEqual(recipients.load(self.b.tenant_id).context_token, "context-b")

        schedules = ScheduleStore(self.registry)
        schedules.set_enabled(self.a.tenant_id, "daily", True)
        self.assertTrue(schedules.is_enabled(self.a.tenant_id, "daily"))
        self.assertFalse(schedules.is_enabled(self.b.tenant_id, "daily"))
        self.assertEqual(
            [item.tenant_id for item in schedules.enabled_tenants("daily")],
            [self.a.tenant_id],
        )

        harmless = self.root / "harmless"
        harmless.mkdir()
        config = ToolConfig(
            enabled=True,
            default_working_directory=str(harmless),
            allowed_roots=[str(harmless)],
            denied_globs=[],
            approval_ttl_seconds=60,
            max_tool_rounds=2,
            max_total_tool_calls=4,
            max_read_bytes=1024,
            max_write_bytes=1024,
            max_directory_entries=20,
            max_search_results=20,
            max_command_output_bytes=1024,
            default_command_timeout_seconds=5,
            max_command_timeout_seconds=5,
            enabled_command_profiles=[],
        )
        runtime = ToolRuntime(
            config,
            "Asia/Shanghai",
            tenant_registry=self.registry,
            sandbox_available=False,
        )
        runtime.bind_tenant(self.a)
        a_workspace = self.registry.tenant_root(self.a.tenant_id) / "workspace"
        b_workspace = self.registry.tenant_root(self.b.tenant_id) / "workspace"
        (a_workspace / "own.txt").write_text("a", encoding="utf-8")
        (b_workspace / "other.txt").write_text("b", encoding="utf-8")
        self.assertEqual(runtime.resolve_path("own.txt", must_exist=True).name, "own.txt")
        with self.assertRaises(ToolError):
            runtime.resolve_path(str(b_workspace / "other.txt"), must_exist=True)
        (a_workspace / "escape").symlink_to(b_workspace, target_is_directory=True)
        with self.assertRaises(ToolError):
            runtime.resolve_path("escape/other.txt", must_exist=True)

    def test_same_script_can_run_for_two_tenants_without_cross_read(self):
        script = self.root / "fake_script.py"
        script.write_text(
            """
import json, os, time
from pathlib import Path
time.sleep(0.1)
root = Path(os.environ['ILINKBOT_SCRIPT_DATA_ROOT'])
root.mkdir(parents=True, exist_ok=True)
Path(os.environ['ILINKBOT_SCRIPT_RESULT_FILE']).write_text(
    json.dumps({'status': 'success', 'summary': os.environ['ILINKBOT_TENANT_ID']}),
    encoding='utf-8')
""".strip(),
            encoding="utf-8",
        )
        recipients = TenantRecipientStore(self.registry)
        service = ScriptService(
            {
                "fake": ScriptDefinition(
                    id="fake",
                    name="fake",
                    description="fake",
                    entrypoint=str(script),
                    timeout_seconds=5,
                    data_directory="fake",
                )
            },
            Credentials("token", "https://gateway", "bot", "owner"),
            recipients,
            self.root,
            self.registry,
        )
        self.addCleanup(service.shutdown)
        first = service.submit(self.a, "fake", {}, "model")
        second = service.submit(self.b, "fake", {}, "model")
        self.assertEqual(first["status"], "running")
        self.assertEqual(second["status"], "running")
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                a_run = service.get_run(self.a, first["run_id"])
                b_run = service.get_run(self.b, second["run_id"])
            except ValueError:
                time.sleep(0.02)
                continue
            if a_run["status"] == b_run["status"] == "success":
                break
            time.sleep(0.02)
        self.assertEqual(a_run["summary"], self.a.tenant_id)
        self.assertEqual(b_run["summary"], self.b.tenant_id)
        with self.assertRaises(ValueError):
            service.get_run(self.b, first["run_id"])

    def test_integration_secret_and_migrated_reference_are_supported(self):
        keychain = FakeKeychain()
        service = IntegrationService(IntegrationStore(self.registry), keychain=keychain)
        service.setup(self.a, "ctsehr")
        self.assertTrue(service.consume(self.a, "account-a")[0])
        secret = "very-private-password"
        handled, reply = service.consume(self.a, secret)
        self.assertTrue(handled)
        self.assertIn("安全保存", reply)
        self.assertIn("查看打卡", reply)
        self.assertIn("已配置", service.status(self.a, "ctsehr"))

        migrated = KeychainReference("legacy.service", "credential")
        keychain.set_secret(migrated, secret)
        IntegrationStore(self.registry).set(
            self.a.tenant_id,
            "ctsehr",
            {
                "account": "account-a",
                "keychain_service": migrated.service,
                "keychain_account": migrated.account,
            },
        )
        self.assertIn("已配置", service.status(self.a, "ctsehr"))
        service.delete(self.a, "ctsehr")
        self.assertFalse(keychain.exists(migrated))


class TenantCommandTests(unittest.TestCase):
    def test_soul_commands_show_and_rebuild_tenant_profile(self):
        class FakeAgent:
            image_prompt = "看图"

            def has_pending_approval(self, _subject):
                return False

        class FakeMemory:
            def __init__(self):
                self.calls = []

            def get_soul(self, tenant_id, force_rebuild=False):
                self.calls.append((tenant_id, force_rebuild))
                return {
                    "revision": 2 if force_rebuild else 1,
                    "updated_at": "2026-07-24T00:00:00+00:00",
                    "content": "# SOUL\n\n## 习惯与交流偏好\n- 简洁回答\n",
                    "source_memory_ids": ["memory"],
                }

        with tempfile.TemporaryDirectory() as directory:
            registry = TenantRegistry(Path(directory) / "data")
            channel = FakeChannel()
            memory = FakeMemory()
            bot = MessageBot(
                FakeAgent(),
                MessageRouter([channel]),
                tenant_registry=registry,
                conversation_store=ConversationStore(registry, 12),
                memory_service=memory,
            )

            def message(text):
                return direct_message("wechat-user", text)

            bot.handle_inbound(message("/soul"))
            bot.handle_inbound(message("/soul rebuild"))
            tenant = registry.resolve("bot", "wechat-user")
            self.assertEqual(
                memory.calls,
                [(tenant.tenant_id, False), (tenant.tenant_id, True)],
            )
            self.assertIn("修订 2", channel.sent[-1][2])
            self.assertIn("简洁回答", channel.sent[-1][2])

    def test_file_credentials_are_private_and_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "credentials.json"
            service = KeychainService(storage_path=target)
            first = service.reference(
                "00000000-0000-0000-0000-000000000001", "ctsehr"
            )
            second = service.reference(
                "00000000-0000-0000-0000-000000000002", "ctsehr"
            )
            service.set_secret(first, "first-secret")
            service.set_secret(second, "second-secret")
            self.assertEqual(service.get_secret(first), "first-secret")
            self.assertEqual(service.get_secret(second), "second-secret")
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_integration_password_bypasses_transcript(self):
        class FakeAgent:
            image_prompt = "看图"

            def has_pending_approval(self, _subject):
                return False

        with tempfile.TemporaryDirectory() as directory:
            registry = TenantRegistry(Path(directory) / "data")
            conversations = ConversationStore(registry, 12)
            keychain = FakeKeychain()
            integrations = IntegrationService(
                IntegrationStore(registry), keychain=keychain
            )
            bot = MessageBot(
                FakeAgent(),
                MessageRouter([FakeChannel()]),
                tenant_registry=registry,
                conversation_store=conversations,
                integration_service=integrations,
            )

            def message(text):
                return direct_message("wechat-user", text)

            bot.handle_inbound(message("/integration setup ctsehr"))
            bot.handle_inbound(message("account-value"))
            bot.handle_inbound(message("secret-value"))
            tenant = registry.resolve("bot", "wechat-user")
            with registry.database.read() as connection:
                transcript = "\n".join(
                    str(row["content"])
                    for row in connection.execute(
                        "SELECT content FROM conversation_events WHERE tenant_id=?",
                        (tenant.tenant_id,),
                    ).fetchall()
                )
            self.assertNotIn("account-value", transcript)
            self.assertNotIn("secret-value", transcript)

    def test_commands_subscription_delete_and_explicit_notify_target(self):
        class FakeAgent:
            image_prompt = "看图"

            def has_pending_approval(self, _subject):
                return False

        with tempfile.TemporaryDirectory() as directory:
            registry = TenantRegistry(Path(directory) / "data")
            conversations = ConversationStore(registry, 12)
            recipients = TenantRecipientStore(registry)
            schedules = ScheduleStore(registry)
            channel = FakeChannel()
            bot = MessageBot(
                FakeAgent(),
                MessageRouter([channel]),
                tenant_registry=registry,
                recipient_store=recipients,
                conversation_store=conversations,
                schedule_store=schedules,
                schedule_ids=["daily"],
            )

            def message(text):
                return direct_message("wechat-user", text)

            bot.handle_inbound(message("/schedule on daily"))
            tenant = registry.resolve("bot", "wechat-user")
            self.assertTrue(schedules.is_enabled(tenant.tenant_id, "daily"))
            self.assertEqual(
                recipients.load(tenant.tenant_id).context_token, "context"
            )

            error = __import__("io").StringIO()
            result = run_notify_command(
                Namespace(
                    message="notice",
                    stdin=False,
                    image=None,
                    image_url=None,
                    user=None,
                ),
                error_stream=error,
            )
            self.assertEqual(result, 1)
            self.assertIn("--user", error.getvalue())

            bot.handle_inbound(message("/delete-data"))
            confirmation = channel.sent[-1][2]
            code = re.search(r"/confirm-delete (\d{6})", confirmation).group(1)
            wrong = "{:06d}".format((int(code) + 1) % 1_000_000)
            bot.handle_inbound(message("/confirm-delete {}".format(wrong)))
            self.assertTrue(registry.tenant_root(tenant.tenant_id).exists())
            bot.handle_inbound(message("/confirm-delete {}".format(code)))
            self.assertFalse(registry.tenant_root(tenant.tenant_id).exists())
            with self.assertRaises(TenantStoreError):
                registry.get(tenant.tenant_id)

    def test_direct_message_requeues_waiting_notifications_with_address_store(self):
        from src.core.messaging.store import ChannelAddressStore

        class FakeAgent:
            image_prompt = "看图"

            def has_pending_approval(self, _subject):
                return False

        class FakeDispatcher:
            def __init__(self):
                self.refreshed = []

            def on_recipient_refreshed(self, tenant_id):
                self.refreshed.append(tenant_id)
                return 1

        with tempfile.TemporaryDirectory() as directory:
            registry = TenantRegistry(Path(directory) / "data")
            dispatcher = FakeDispatcher()
            channel = FakeChannel()
            bot = MessageBot(
                FakeAgent(),
                MessageRouter([channel]),
                interaction_logger=lambda *_entry: None,
                tenant_registry=registry,
                notification_dispatcher=dispatcher,
                address_store=ChannelAddressStore(registry),
            )
            bot.handle_inbound(direct_message("wechat-user", "/id"))
            tenant = registry.resolve("bot", "wechat-user")
            self.assertEqual(dispatcher.refreshed, [tenant.tenant_id])
            self.assertIn(tenant.tenant_id, channel.sent[-1][2])

    def test_plugin_like_command_is_handled_as_an_ordinary_message(self):
        class FakeAgent:
            image_prompt = "看图"
            def __init__(self):
                self.calls = []

            def has_pending_approval(self, _subject):
                return False

            def chat(self, tenant, text, image_bytes=None, **_kwargs):
                self.calls.append((tenant.tenant_id, text, image_bytes))
                return SimpleNamespace(text="普通消息已处理", thinking="")

        with tempfile.TemporaryDirectory() as directory:
            registry = TenantRegistry(Path(directory) / "data")
            agent = FakeAgent()
            channel = FakeChannel()
            bot = MessageBot(
                agent,
                MessageRouter([channel]),
                tenant_registry=registry,
                recipient_store=TenantRecipientStore(registry),
            )
            bot.handle_inbound(
                direct_message("wechat-user", "/plugin action ABCD1234")
            )
            tenant = registry.resolve("bot", "wechat-user")
            self.assertEqual(
                agent.calls,
                [(tenant.tenant_id, "/plugin action ABCD1234", None)],
            )
            self.assertEqual(channel.sent[-1][2], "普通消息已处理")


if __name__ == "__main__":
    unittest.main()
