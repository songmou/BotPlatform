from __future__ import annotations

import socket
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core.config.loader import load_project_config
from src.core.plugins.base import PluginError, PluginToolDefinition
from src.core.plugins.browser_automation import (
    BrowserAutomationConfig,
    BrowserAutomationPlugin,
    BrowserSession,
    BrowserUnavailableError,
    _AgentPageController,
    validate_public_https_url,
)
from src.core.storage.tenants import TenantRegistry
from src.core.tooling import ToolRuntime


SOURCE_CONFIG = Path(__file__).resolve().parents[1] / "config"


class FakeChromium:
    def __init__(self, bundled: Path, successful_channel: str = "chrome") -> None:
        self.executable_path = str(bundled)
        self.successful_channel = successful_channel
        self.calls = []
        self.browser = object()

    def launch(self, **options):
        self.calls.append(options)
        if options.get("channel") == self.successful_channel:
            return self.browser
        raise RuntimeError("launch failed\ninternal detail that should not be copied")


class BrowserCoreTests(unittest.TestCase):
    def test_launch_falls_back_from_explicit_and_bundled_to_chrome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            explicit = root / "explicit"
            bundled = root / "bundled"
            explicit.write_text("browser")
            bundled.write_text("browser")
            chromium = FakeChromium(bundled)
            session = BrowserSession(
                BrowserAutomationConfig(executable_path=str(explicit))
            )

            browser, label = session._launch(chromium)

        self.assertIs(browser, chromium.browser)
        self.assertEqual(label, "Google Chrome")
        self.assertEqual(
            [call.get("executable_path") or call.get("channel") for call in chromium.calls],
            [str(explicit), str(bundled), "chrome"],
        )

    def test_launch_falls_back_to_edge_and_reports_concise_failure(self) -> None:
        chromium = FakeChromium(Path("/definitely/missing"), successful_channel="msedge")
        browser, label = BrowserSession(BrowserAutomationConfig())._launch(chromium)
        self.assertIs(browser, chromium.browser)
        self.assertEqual(label, "Microsoft Edge")

        chromium = FakeChromium(Path("/definitely/missing"), successful_channel="none")
        with self.assertRaises(BrowserUnavailableError) as caught:
            BrowserSession(BrowserAutomationConfig())._launch(chromium)
        message = str(caught.exception)
        self.assertIn("Playwright Chromium", message)
        self.assertIn("Google Chrome", message)
        self.assertIn("Microsoft Edge", message)
        self.assertNotIn("internal detail", message)

    def test_url_policy_allows_public_https_and_blocks_unsafe_targets(self) -> None:
        public_record = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        with patch("src.core.plugins.browser_automation.socket.getaddrinfo", return_value=public_record):
            validate_public_https_url("https://example.test/path")
        validate_public_https_url("data:text/plain,ok", subresource=True)
        for url in (
            "http://example.com",
            "file:///tmp/private",
            "https://127.0.0.1",
            "https://169.254.169.254/latest/meta-data",
            "https://10.0.0.1",
        ):
            with self.subTest(url=url), self.assertRaises(PluginError):
                validate_public_https_url(url)

    def test_stale_element_reference_is_rejected(self) -> None:
        controller = object.__new__(_AgentPageController)
        controller.session = SimpleNamespace(page=SimpleNamespace(url="https://example.com"))
        controller.snapshot_url = "https://example.com"
        controller.version = 2
        controller.references = {"v2e1": MagicMock()}
        with self.assertRaisesRegex(PluginError, "已过期"):
            controller.interact("click", "v1e1", None)

class FakePlugin:
    id = "fake"

    def __init__(self) -> None:
        self.closed = False
        self.calls = []
        self.tool_definitions = {
            "fake_read": PluginToolDefinition(
                "fake",
                {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            )
        }

    def is_available(self, name):
        return name == "fake_read"

    def execute(self, name, arguments, tenant):
        self.calls.append((name, arguments, tenant.tenant_id))
        return {"tenant_id": tenant.tenant_id}

    def preview(self, name, arguments, tenant):
        return name

    def close_tenant(self, tenant_id):
        pass

    def close(self):
        self.closed = True


class PluginFrameworkTests(unittest.TestCase):
    def test_plugin_is_routed_audited_and_closed_without_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_project_config(SOURCE_CONFIG)
            registry = TenantRegistry(Path(directory) / "data")
            tenant = registry.resolve("bot", "user")
            logs = []
            plugin = FakePlugin()
            runtime = ToolRuntime(
                config.tools,
                "Asia/Shanghai",
                plugins=[plugin],
                tenant_registry=registry,
                audit_logger=lambda *values: logs.append(values),
            )
            runtime.bind_tenant(tenant)
            schema = runtime.schemas(["fake_read"])
            result = runtime.execute("fake_read", {})
            runtime.close()

        self.assertEqual(schema[0]["function"]["name"], "fake_read")
        self.assertFalse(runtime.requires_approval("fake_read"))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["tenant_id"], tenant.tenant_id)
        self.assertEqual(plugin.calls[0][2], tenant.tenant_id)
        self.assertEqual(logs[0][1:3], ("fake_read", "成功"))
        self.assertTrue(plugin.closed)

    def test_browser_plugin_is_enabled_and_tools_are_automatic(self) -> None:
        config = load_project_config(SOURCE_CONFIG)
        plugin_config = config.plugins["browser_automation"]
        self.assertTrue(plugin_config.enabled)
        plugin = BrowserAutomationPlugin(plugin_config.settings)
        try:
            self.assertIn("browser_open", config.active_agent.tools)
            self.assertFalse(plugin.tool_definitions["browser_interact"].requires_approval)
            self.assertEqual(plugin.config.session_ttl_seconds, 600)
            self.assertEqual(plugin.config.max_snapshot_chars, 12_000)
        finally:
            plugin.close()

    def test_browser_model_tool_sequence_and_tenant_isolation(self) -> None:
        class FakeManagedSession:
            def __init__(self, session_id, tenant_id, config):
                self.session_id = session_id
                self.tenant_id = tenant_id
                self.last_used = time.monotonic()
                self.calls = []
                self.closed = False

            def call(self, method, *arguments):
                self.calls.append((method, arguments))
                self.last_used = time.monotonic()
                return {
                    "url": arguments[0] if method == "open" else "https://example.test/",
                    "title": method,
                    "text": "page",
                    "elements": [{"ref": "v1e1", "name": "button"}],
                }

            def close(self):
                self.closed = True

        public_record = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        plugin = BrowserAutomationPlugin({})
        tenant = SimpleNamespace(tenant_id="tenant-a")
        try:
            with patch(
                "src.core.plugins.browser_automation._ManagedAgentSession",
                FakeManagedSession,
            ), patch(
                "src.core.plugins.browser_automation.socket.getaddrinfo",
                return_value=public_record,
            ):
                opened = plugin.execute(
                    "browser_open", {"url": "https://example.test/"}, tenant
                )
                session_id = opened["session_id"]
                plugin.execute("browser_snapshot", {"session_id": session_id}, tenant)
                plugin.execute(
                    "browser_interact",
                    {
                        "session_id": session_id,
                        "action": "click",
                        "ref": "v1e1",
                    },
                    tenant,
                )
                plugin.execute(
                    "browser_wait",
                    {"session_id": session_id, "condition": "timeout", "timeout_seconds": 1},
                    tenant,
                )
                with self.assertRaisesRegex(PluginError, "不属于当前用户"):
                    plugin.execute(
                        "browser_snapshot",
                        {"session_id": session_id},
                        SimpleNamespace(tenant_id="tenant-b"),
                    )
                managed = plugin._sessions[session_id]
                closed = plugin.execute(
                    "browser_close", {"session_id": session_id}, tenant
                )
            self.assertTrue(closed["closed"])
            self.assertTrue(managed.closed)
            self.assertEqual(
                [name for name, _arguments in managed.calls],
                ["open", "snapshot", "interact", "wait"],
            )
        finally:
            plugin.close()


if __name__ == "__main__":
    unittest.main()
