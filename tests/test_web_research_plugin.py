from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from src.core.config.loader import load_project_config
from src.core.plugins.base import PluginError
from src.core.plugins.web_research import (
    BRAVE_ENDPOINT,
    DUCKDUCKGO_ENDPOINT,
    SERPER_ENDPOINT,
    TAVILY_ENDPOINT,
    WebResearchConfig,
    WebResearchPlugin,
    extract_readable_text,
    load_search_secrets,
    parse_duckduckgo_results,
)


SOURCE_CONFIG = Path(__file__).resolve().parents[1] / "config"
TENANT = SimpleNamespace(tenant_id="tenant-test")
PUBLIC_RECORD = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

DDG_FIXTURE = """
<html><body>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.test%2Fone&rut=abc">First   Result</a>
    <div class="result__snippet">First <b>snippet</b> text</div>
  </div>
  <div class="result">
    <a class="result__a" href="https://example.test/two">Second Result</a>
    <div class="result__snippet">Second snippet</div>
  </div>
  <div class="result">
    <a class="result__a" href="https://example.test/two">Duplicate URL</a>
  </div>
  <div class="result">
    <a class="result__a" href="javascript:void(0)">Bad scheme</a>
  </div>
</body></html>
"""


def make_plugin(handler=None, settings=None, secrets=None, **kwargs):
    client = None
    if handler is not None:
        client = httpx.Client(transport=httpx.MockTransport(handler))
    return WebResearchPlugin(
        settings or {},
        client=client,
        secrets=secrets if secrets is not None else {},
        **kwargs,
    )


class ConfigAndDefinitionTests(unittest.TestCase):
    def test_tool_definitions_are_read_only_and_complete(self) -> None:
        plugin = make_plugin()
        expected = {
            "web_search_duckduckgo",
            "web_search_tavily",
            "web_search_brave",
            "web_search_serper",
            "web_fetch",
            "web_fetch_browser",
        }
        self.assertEqual(set(plugin.tool_definitions), expected)
        for name, definition in plugin.tool_definitions.items():
            with self.subTest(tool=name):
                self.assertFalse(definition.requires_approval)
                self.assertFalse(definition.direct_response)
                self.assertFalse(definition.parameters["additionalProperties"])

    def test_validate_settings_rejects_unknown_and_out_of_range(self) -> None:
        WebResearchPlugin.validate_settings(
            {"timeout_seconds": 10, "max_results": 5, "browser": {"headless": True}}
        )
        for bad in (
            {"unknown_key": 1},
            {"timeout_seconds": 0},
            {"timeout_seconds": True},
            {"max_results": 21},
            {"max_content_chars": 100},
            {"max_fetch_bytes": 1},
            {"max_redirects": 6},
            {"user_agent": 5},
            {"browser": "chrome"},
            {"browser": {"unknown": 1}},
        ):
            with self.subTest(settings=bad), self.assertRaises(ValueError):
                WebResearchPlugin.validate_settings(bad)

    def test_config_defaults_and_neutral_project_registration(self) -> None:
        config = WebResearchConfig.from_mapping({})
        self.assertEqual(config.timeout_seconds, 20)
        self.assertEqual(config.max_results, 8)
        self.assertEqual(config.max_content_chars, 8_000)

        project = load_project_config(SOURCE_CONFIG)
        plugin_config = project.plugins["web_research"]
        self.assertTrue(plugin_config.enabled)
        for agent_id in ("researcher", "general"):
            tools = set(
                project.agents[agent_id].plugin_tools["web_research"]
            )
            self.assertLessEqual(
                {"web_search_duckduckgo", "web_fetch", "web_fetch_browser"}, tools
            )

    def test_provider_availability_follows_api_keys(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            plugin = make_plugin()
            for tool in ("web_search_tavily", "web_search_brave", "web_search_serper"):
                self.assertFalse(plugin.is_available(tool))
            keyed = make_plugin(
                secrets={
                    "TAVILY_API_KEY": "t",
                    "BRAVE_API_KEY": "b",
                    "SERPER_API_KEY": "s",
                }
            )
            for tool in ("web_search_tavily", "web_search_brave", "web_search_serper"):
                self.assertTrue(keyed.is_available(tool))
        self.assertTrue(make_plugin().is_available("web_search_duckduckgo"))
        self.assertTrue(make_plugin().is_available("web_fetch"))
        self.assertFalse(make_plugin().is_available("nonexistent_tool"))

    def test_load_search_secrets_parses_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "search.env"
            env_path.write_text(
                "# comment line\n"
                "TAVILY_API_KEY=tvly-abc\n"
                'BRAVE_API_KEY="brave-key"\n'
                "EMPTY=\n"
                "not a pair\n",
                encoding="utf-8",
            )
            env_path.chmod(0o600)
            secrets = load_search_secrets(env_path)
            missing = load_search_secrets(Path(directory) / "absent.env")
        self.assertEqual(
            secrets, {"TAVILY_API_KEY": "tvly-abc", "BRAVE_API_KEY": "brave-key"}
        )
        self.assertEqual(missing, {})

    def test_close_only_releases_owned_client(self) -> None:
        injected = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        plugin = WebResearchPlugin({}, client=injected, secrets={})
        plugin.close()
        self.assertIs(plugin._client, injected)
        injected.close()

        owner = WebResearchPlugin({}, secrets={})
        owner._http()
        owner.close()
        self.assertIsNone(owner._client)


class ContentExtractionTests(unittest.TestCase):
    def test_extract_readable_text_strips_noise_and_truncates(self) -> None:
        markup = (
            "<html><head><title>  Demo   Page </title>"
            "<script>var secret = 1;</script><style>body{}</style></head>"
            "<body><nav>menu</nav><p>Hello   world</p><p>Second</p>"
            "<footer>copyright</footer></body></html>"
        )
        title, content, truncated = extract_readable_text(markup, 8_000)
        self.assertEqual(title, "Demo Page")
        self.assertIn("Hello world", content)
        self.assertNotIn("secret", content)
        self.assertNotIn("menu", content)
        self.assertNotIn("copyright", content)
        self.assertFalse(truncated)

        _, clipped, was_truncated = extract_readable_text(
            "<html><body>{}</body></html>".format("字" * 900), 500
        )
        self.assertTrue(was_truncated)
        self.assertTrue(clipped.endswith("……"))

    def test_parse_duckduckgo_results_unwraps_and_deduplicates(self) -> None:
        results = parse_duckduckgo_results(DDG_FIXTURE, 10)
        self.assertEqual(
            [item["url"] for item in results],
            ["https://example.test/one", "https://example.test/two"],
        )
        self.assertEqual(results[0]["title"], "First Result")
        self.assertEqual(results[0]["snippet"], "First snippet text")
        self.assertEqual(parse_duckduckgo_results("<html></html>", 5), [])


class SearchProviderTests(unittest.TestCase):
    def test_duckduckgo_search_parses_fixture(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(str(request.url).startswith(DUCKDUCKGO_ENDPOINT))
            self.assertEqual(request.url.params["q"], "python 教程")
            return httpx.Response(200, text=DDG_FIXTURE)

        plugin = make_plugin(handler)
        result = plugin.execute(
            "web_search_duckduckgo", {"query": "python 教程", "max_results": 5}, TENANT
        )
        self.assertEqual(result["provider"], "duckduckgo")
        self.assertEqual(result["result_count"], 2)
        self.assertIn("不可信", result["notice"])

    def test_tavily_search_normalizes_results_and_answer(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), TAVILY_ENDPOINT)
            self.assertEqual(request.headers["Authorization"], "Bearer tvly-key")
            return httpx.Response(
                200,
                json={
                    "answer": "  综合回答  ",
                    "results": [
                        {"title": "A", "url": "https://a.test/", "content": "alpha"},
                        {"title": "B", "url": "ftp://bad", "content": "skip"},
                        "not-a-dict",
                    ],
                },
            )

        plugin = make_plugin(handler, secrets={"TAVILY_API_KEY": "tvly-key"})
        result = plugin.execute("web_search_tavily", {"query": "q"}, TENANT)
        self.assertEqual(result["answer"], "综合回答")
        self.assertEqual(
            result["results"],
            [{"title": "A", "url": "https://a.test/", "snippet": "alpha"}],
        )

    def test_brave_and_serper_normalize_results(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).startswith(BRAVE_ENDPOINT):
                self.assertEqual(request.headers["X-Subscription-Token"], "brave-key")
                return httpx.Response(
                    200,
                    json={
                        "web": {
                            "results": [
                                {
                                    "title": "Brave Hit",
                                    "url": "https://b.test/",
                                    "description": "desc",
                                }
                            ]
                        }
                    },
                )
            self.assertEqual(str(request.url), SERPER_ENDPOINT)
            self.assertEqual(request.headers["X-API-KEY"], "serper-key")
            return httpx.Response(
                200,
                json={
                    "organic": [
                        {"title": "Serper Hit", "link": "https://s.test/", "snippet": "snip"}
                    ]
                },
            )

        plugin = make_plugin(
            handler, secrets={"BRAVE_API_KEY": "brave-key", "SERPER_API_KEY": "serper-key"}
        )
        brave = plugin.execute("web_search_brave", {"query": "q"}, TENANT)
        serper = plugin.execute("web_search_serper", {"query": "q"}, TENANT)
        self.assertEqual(
            brave["results"],
            [{"title": "Brave Hit", "url": "https://b.test/", "snippet": "desc"}],
        )
        self.assertEqual(
            serper["results"],
            [{"title": "Serper Hit", "url": "https://s.test/", "snippet": "snip"}],
        )

    def test_provider_errors_map_to_chinese_messages(self) -> None:
        codes = iter([401, 429, 500])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(next(codes), json={})

        plugin = make_plugin(handler, secrets={"TAVILY_API_KEY": "k"})
        for expected in ("鉴权失败", "被限流", "HTTP 500"):
            with self.subTest(expected=expected), self.assertRaisesRegex(
                PluginError, expected
            ):
                plugin.execute("web_search_tavily", {"query": "q"}, TENANT)

    def test_search_timeout_and_missing_key_are_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow upstream")

        plugin = make_plugin(handler)
        with self.assertRaisesRegex(PluginError, "超时"):
            plugin.execute("web_search_duckduckgo", {"query": "q"}, TENANT)
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(PluginError, "TAVILY_API_KEY"):
                make_plugin().execute("web_search_tavily", {"query": "q"}, TENANT)

    def test_invalid_arguments_are_rejected(self) -> None:
        plugin = make_plugin(secrets={"TAVILY_API_KEY": "k"})
        with self.assertRaisesRegex(PluginError, "query"):
            plugin.execute("web_search_duckduckgo", {"query": "   "}, TENANT)
        with self.assertRaisesRegex(PluginError, "max_results"):
            plugin.execute("web_search_duckduckgo", {"query": "q", "max_results": 0}, TENANT)
        with self.assertRaisesRegex(PluginError, "search_depth"):
            plugin.execute(
                "web_search_tavily", {"query": "q", "search_depth": "deep"}, TENANT
            )
        with self.assertRaisesRegex(PluginError, "未知联网检索工具"):
            plugin.execute("web_unknown", {}, TENANT)


class WebFetchTests(unittest.TestCase):
    def test_url_policy_blocks_unsafe_targets(self) -> None:
        plugin = make_plugin()
        for url in (
            "http://example.test/",
            "https://127.0.0.1/",
            "https://10.0.0.1/page",
            "https://user:pass@example.test/",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url), self.assertRaises(PluginError):
                plugin.execute("web_fetch", {"url": url}, TENANT)

    def test_fetch_follows_redirect_and_extracts_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(
                    302, headers={"location": "https://example.test/final"}
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text=(
                    "<html><head><title>Final</title><script>x()</script></head>"
                    "<body><p>正文内容</p></body></html>"
                ),
            )

        plugin = make_plugin(handler)
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            result = plugin.execute(
                "web_fetch", {"url": "https://example.test/start"}, TENANT
            )
        self.assertEqual(result["final_url"], "https://example.test/final")
        self.assertEqual(result["title"], "Final")
        self.assertIn("正文内容", result["content"])
        self.assertNotIn("x()", result["content"])
        self.assertFalse(result["truncated"])
        self.assertIn("不可信", result["notice"])

    def test_fetch_rejects_redirect_loop_and_binary_content(self) -> None:
        def redirect_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://example.test/next"})

        plugin = make_plugin(redirect_handler, settings={"max_redirects": 1})
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            with self.assertRaisesRegex(PluginError, "跳转次数"):
                plugin.execute("web_fetch", {"url": "https://example.test/"}, TENANT)

        def binary_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=b"%PDF"
            )

        binary_plugin = make_plugin(binary_handler)
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            with self.assertRaisesRegex(PluginError, "仅支持抓取"):
                binary_plugin.execute(
                    "web_fetch", {"url": "https://example.test/file.pdf"}, TENANT
                )

    def test_fetch_reports_http_error_and_empty_body_hint(self) -> None:
        statuses = iter([404, 200])

        def handler(request: httpx.Request) -> httpx.Response:
            status = next(statuses)
            return httpx.Response(
                status,
                headers={"content-type": "text/html"},
                text="<html><body><script>only_js()</script></body></html>",
            )

        plugin = make_plugin(handler)
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            with self.assertRaisesRegex(PluginError, "HTTP 404"):
                plugin.execute("web_fetch", {"url": "https://example.test/"}, TENANT)
            result = plugin.execute("web_fetch", {"url": "https://example.test/"}, TENANT)
        self.assertEqual(result["content"], "")
        self.assertIn("web_fetch_browser", result["hint"])


class BrowserFetchTests(unittest.TestCase):
    def test_browser_fetch_uses_session_and_normalizes_output(self) -> None:
        calls = []

        class FakePage:
            url = "https://example.test/final"

            def goto(self, url, wait_until, timeout):
                calls.append(("goto", url, wait_until))

            def locator(self, selector):
                calls.append(("locator", selector))
                return SimpleNamespace(
                    inner_text=lambda timeout: "Rendered   line\n\n\nSecond"
                )

            def title(self):
                return "  Rendered   Title  "

        class FakeSession:
            launch_name = "FakeBrowser"
            page = FakePage()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        plugin = make_plugin(
            browser_factory=lambda: SimpleNamespace(session=lambda: FakeSession())
        )
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            result = plugin.execute(
                "web_fetch_browser",
                {"url": "https://example.test/", "wait_until": "networkidle"},
                TENANT,
            )
        self.assertEqual(calls[0], ("goto", "https://example.test/", "networkidle"))
        self.assertEqual(result["title"], "Rendered Title")
        self.assertEqual(result["content"], "Rendered line\n\nSecond")
        self.assertEqual(result["browser"], "FakeBrowser")
        self.assertEqual(result["final_url"], "https://example.test/final")

    def test_browser_fetch_rejects_invalid_wait_until(self) -> None:
        plugin = make_plugin(browser_factory=lambda: None)
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            with self.assertRaisesRegex(PluginError, "wait_until"):
                plugin.execute(
                    "web_fetch_browser",
                    {"url": "https://example.test/", "wait_until": "forever"},
                    TENANT,
                )

    def test_preview_mentions_tool_and_target(self) -> None:
        plugin = make_plugin()
        text = plugin.preview("web_fetch", {"url": "https://example.test/"}, TENANT)
        self.assertIn("web_fetch", text)
        self.assertIn("https://example.test/", text)


if __name__ == "__main__":
    unittest.main()
