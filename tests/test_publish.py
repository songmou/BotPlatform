"""Unit tests for agent publish store, routing, and panel WeChat login."""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

from src.core.integrations.ilink import ILinkError
from src.core.services.publish import (
    PLATFORM_WECHAT,
    AgentBindingResolver,
    PublishError,
    PublishStore,
)
from src.core.services.wechat_login import WeChatLoginManager


@dataclass
class _Preset:
    id: str
    name: str
    enabled: bool = True


class PublishStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = PublishStore(Path(self._tmp.name) / "publish.json")

    def test_publish_binds_single_agent(self):
        record = self.store.publish(PLATFORM_WECHAT, "coder")
        self.assertEqual(record["agent_id"], "coder")
        self.assertTrue(record["enabled"])
        self.assertEqual(self.store.bound_agent(PLATFORM_WECHAT)["agent_id"], "coder")

    def test_publish_replaces_previous_binding(self):
        self.store.publish(PLATFORM_WECHAT, "a")
        self.store.publish(PLATFORM_WECHAT, "b")
        agents = self.store.platform_agents(PLATFORM_WECHAT)
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["agent_id"], "b")

    def test_placeholder_platform_rejected(self):
        with self.assertRaises(PublishError):
            self.store.publish("dingtalk", "coder")

    def test_enable_disable_and_remove(self):
        self.store.publish(PLATFORM_WECHAT, "coder")
        self.assertTrue(self.store.set_agent_enabled(PLATFORM_WECHAT, "coder", False))
        self.assertFalse(self.store.bound_agent(PLATFORM_WECHAT)["enabled"])
        self.assertTrue(self.store.remove_agent(PLATFORM_WECHAT, "coder"))
        self.assertIsNone(self.store.bound_agent(PLATFORM_WECHAT))

    def test_platform_config_round_trip(self):
        self.store.set_platform_config(
            "wecom", {"bot_id": "b1", "secret": "s1", "bind_method": "manual"}
        )
        config = self.store.platform_config("wecom")
        self.assertEqual(config["bot_id"], "b1")
        self.assertEqual(config["secret"], "s1")


class AgentBindingResolverTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = PublishStore(Path(self._tmp.name) / "publish.json")
        self.resolver = AgentBindingResolver(self.store)
        self.agents = {
            "general": _Preset("general", "通用助手"),
            "coder": _Preset("coder", "编程助手"),
        }

    def test_no_binding_returns_none(self):
        result = self.resolver.resolve(PLATFORM_WECHAT, "u1", "你好", self.agents)
        self.assertIsNone(result.agent_id)

    def test_resolves_bound_agent(self):
        self.store.publish(PLATFORM_WECHAT, "coder")
        result = self.resolver.resolve(PLATFORM_WECHAT, "u1", "任意消息", self.agents)
        self.assertEqual(result.agent_id, "coder")
        self.assertIsNone(result.reply)

    def test_disabled_binding_not_resolved(self):
        self.store.publish(PLATFORM_WECHAT, "coder")
        self.store.set_agent_enabled(PLATFORM_WECHAT, "coder", False)
        result = self.resolver.resolve(PLATFORM_WECHAT, "u1", "在吗", self.agents)
        self.assertIsNone(result.agent_id)

    def test_missing_agent_not_resolved(self):
        self.store.publish(PLATFORM_WECHAT, "ghost")
        result = self.resolver.resolve(PLATFORM_WECHAT, "u1", "在吗", self.agents)
        self.assertIsNone(result.agent_id)


class _FakeCredentials:
    bot_id = "bot-1"


class _FakeLoginClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.closed = False

    def login(self, show_qr, status_changed=None):
        show_qr("qr-content-123")
        if status_changed:
            status_changed("scaned")
        if self.fail:
            raise ILinkError("二维码已过期")
        return _FakeCredentials()

    def close(self):
        self.closed = True


class WeChatLoginManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cred_path = Path(self._tmp.name) / "credentials.json"
        self.saved = []

    def _make(self, fail: bool = False) -> WeChatLoginManager:
        client = _FakeLoginClient(fail=fail)
        self.client = client
        return WeChatLoginManager(
            client_factory=lambda: client,
            credentials_saver=lambda creds, path: (
                self.saved.append((creds, path)),
                Path(path).write_text("{}", encoding="utf-8"),
            ),
            credentials_path=self.cred_path,
        )

    def _wait_final(self, manager: WeChatLoginManager) -> dict:
        for _ in range(100):
            status = manager.status()
            if status["state"] in {"success", "failed"}:
                return status
            time.sleep(0.02)
        self.fail("登录线程未在预期时间内结束")

    def test_successful_login_saves_credentials(self):
        manager = self._make()
        self.assertFalse(manager.is_connected())
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "success")
        self.assertEqual(status["bot_id"], "bot-1")
        self.assertTrue(status["connected"])
        self.assertEqual(self.saved[0][1], self.cred_path)
        self.assertTrue(self.client.closed)

    def test_failed_login_reports_error(self):
        manager = self._make(fail=True)
        manager.start()
        status = self._wait_final(manager)
        self.assertEqual(status["state"], "failed")
        self.assertIn("二维码已过期", status["error"])
        self.assertFalse(status["connected"])

    def test_qr_exposed_as_data_url(self):
        manager = self._make(fail=True)
        manager._on_qr("qr-content-123")
        self.assertTrue(manager.status()["qr"].startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()
