"""Tests for the pending-script-input routing in MessageBot.handle_inbound."""
from __future__ import annotations

import tempfile
import time
import types
import unittest
from pathlib import Path

from src.core.application.bot import MessageBot
from src.core.services.script_input import PendingScriptInput
from src.core.storage.tenants import ConversationStore, TenantRegistry


class FakeScriptService:
    def __init__(self, tenant) -> None:
        self.tenant = tenant
        self._pending: PendingScriptInput = None
        self.resumed = False
        self.resumed_text: str = ""
        self.cleared = False

    def set_pending(self) -> None:
        self._pending = PendingScriptInput(
            tenant_id=self.tenant.tenant_id,
            session_key="direct",
            run_id="run1",
            script_id="ctsoa_check",
            script_name="CTS OA 待办",
            param="validate_code",
            prompt="",
            hint="",
            expires_at=time.time() + 300,
        )

    def peek_pending_input(self, tenant):
        return self._pending

    def consume_pending_input(self, tenant):
        pending = self._pending
        self._pending = None
        return pending

    def clear_pending_input(self, tenant) -> None:
        self.cleared = True
        self._pending = None

    def resume_pending_input(self, tenant, pending, text):
        self.resumed = True
        self.resumed_text = text
        return {"run_id": "x"}


class _AgentStub:
    def chat(self, *args, **kwargs):
        return "ok"


class _RouterStub:
    def send(self, *args, **kwargs):
        pass


class PendingScriptRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = TenantRegistry(self.root / "data")
        self.tenant = self.registry.resolve("bot", "member")
        self.conversation_store = ConversationStore(self.registry, 10)
        self.script_service = FakeScriptService(self.tenant)
        self.replies: list = []
        self.bot = MessageBot(
            _AgentStub(),
            _RouterStub(),
            interaction_logger=lambda *args: None,
            tenant_registry=self.registry,
            conversation_store=self.conversation_store,
            script_service=self.script_service,
        )
        self.bot._reply = lambda endpoint, text, tenant, record=True: self.replies.append(
            (text, record)
        )
        self.bot._log = lambda *args: None
        self.endpoint = types.SimpleNamespace(channel_id="wechat-ilink:abc")

    def _msg(self, conv_type: str = "direct"):
        message = types.SimpleNamespace()
        message.conversation_type = conv_type
        return message

    def test_routes_reply_to_resume_and_records_user_message(self) -> None:
        self.script_service.set_pending()
        handled = self.bot._route_pending_script_input(
            self._msg(), self.tenant, self.endpoint, "member", "6841", False
        )
        self.assertTrue(handled)
        self.assertTrue(self.script_service.resumed)
        self.assertEqual(self.script_service.resumed_text, "6841")
        context = self.conversation_store.load_context(
            self.tenant.personal_tenant_id or self.tenant.tenant_id
        )
        self.assertTrue(
            any(msg.role == "user" and "6841" in msg.content for msg in context)
        )

    def test_cancel_clears_pending(self) -> None:
        self.script_service.set_pending()
        handled = self.bot._route_pending_script_input(
            self._msg(), self.tenant, self.endpoint, "member", "取消", False
        )
        self.assertTrue(handled)
        self.assertTrue(self.script_service.cleared)
        self.assertFalse(self.script_service.resumed)

    def test_slash_command_passes_through_and_keeps_pending(self) -> None:
        self.script_service.set_pending()
        handled = self.bot._route_pending_script_input(
            self._msg(), self.tenant, self.endpoint, "member", "/help", False
        )
        self.assertFalse(handled)
        self.assertFalse(self.script_service.resumed)
        self.assertIsNotNone(self.script_service.peek_pending_input(self.tenant))

    def test_no_pending_passes_through(self) -> None:
        handled = self.bot._route_pending_script_input(
            self._msg(), self.tenant, self.endpoint, "member", "你好", False
        )
        self.assertFalse(handled)

    def test_group_message_not_routed(self) -> None:
        self.script_service.set_pending()
        handled = self.bot._route_pending_script_input(
            self._msg("group"), self.tenant, self.endpoint, "member", "6841", False
        )
        self.assertFalse(handled)
        self.assertFalse(self.script_service.resumed)

    def test_empty_input_prompted_without_resume(self) -> None:
        self.script_service.set_pending()
        handled = self.bot._route_pending_script_input(
            self._msg(), self.tenant, self.endpoint, "member", "", False
        )
        self.assertTrue(handled)
        self.assertFalse(self.script_service.resumed)
        self.assertTrue(any("不能为空" in text for text, _ in self.replies))


if __name__ == "__main__":
    unittest.main()
