from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from src.core.plugins.base import PluginContext, PluginError
from src.core.plugins.web_crawler import (
    WebCrawlerPlugin,
    canonicalize_url,
    normalize_source_config,
    validate_crawler_url,
)
from src.core.services.knowledge import KnowledgeService
from src.core.storage.tenants import TenantRegistry


PUBLIC_RECORD = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
SENSITIVE_RECORD = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]


class WebCrawlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = TenantRegistry(self.root / "data")
        self.tenant = self.registry.resolve("bot", "crawler")
        self.context = PluginContext(
            project_root=self.root,
            tenant_registry=self.registry,
            data_root=self.root / "plugin-data",
            plugin_id="web_crawler",
        )

    def plugin(self, handler, **kwargs):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        plugin = WebCrawlerPlugin(
            {"min_host_interval_seconds": 0.1, "max_workers": 1},
            self.context,
            client=client,
            sleep=lambda _seconds: None,
            **kwargs,
        )
        self.addCleanup(client.close)
        return plugin

    @staticmethod
    def source(name="示例", **overrides):
        value = {
            "name": name,
            "seed_urls": ["https://example.test/start"],
            "allowed_domains": ["example.test"],
            "max_depth": 1,
            "max_pages": 10,
            "render_mode": "static",
            "retention_versions": 2,
            "templates": [],
        }
        value.update(overrides)
        return value

    def process(self, plugin, source):
        run = plugin.store.enqueue_run(self.tenant.tenant_id, source["source_id"])
        claimed = plugin.store.claim_run("test")
        self.assertEqual(claimed["run_id"], run["run_id"])
        plugin.process_run(claimed)
        return plugin.store.get_run(self.tenant.tenant_id, run["run_id"])

    def test_canonicalization_and_config_validation(self) -> None:
        self.assertEqual(
            canonicalize_url("https://EXAMPLE.test:443/a?utm_source=x&b=2&a=1#part"),
            "https://example.test/a?a=1&b=2",
        )
        self.assertEqual(
            canonicalize_url("http://EXAMPLE.test:80/a?utm_source=x&b=2#part"),
            "http://example.test/a?b=2",
        )
        self.assertEqual(
            canonicalize_url("http://example.test:8080/"),
            "http://example.test:8080/",
        )
        self.assertEqual(canonicalize_url("http://[::1]:80/"), "http://[::1]/")
        with self.assertRaisesRegex(PluginError, "不含凭据"):
            canonicalize_url("http://user:secret@example.test/")
        with self.assertRaisesRegex(PluginError, "HTTP/HTTPS"):
            canonicalize_url("ftp://example.test/")
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            config = normalize_source_config(self.source())
        self.assertEqual(config["allowed_domains"], ["example.test"])
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ), self.assertRaisesRegex(PluginError, "种子地址"):
            normalize_source_config(self.source(allowed_domains=["other.test"]))

    def test_crawler_url_policy_allows_private_and_loopback_but_blocks_sensitive_ranges(self) -> None:
        private_record = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.30.40", 0))
        ]
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=private_record,
        ):
            config = normalize_source_config(
                self.source(
                    seed_urls=["http://intranet.test/start"],
                    allowed_domains=["intranet.test"],
                )
            )
        self.assertEqual(config["seed_urls"], ["http://intranet.test/start"])
        loopback = normalize_source_config(
            self.source(
                seed_urls=["http://127.0.0.1:8080/start"],
                allowed_domains=["127.0.0.1"],
            )
        )
        self.assertEqual(loopback["allowed_domains"], ["127.0.0.1"])
        for url in (
            "http://169.254.169.254/latest/meta-data",
            "http://224.0.0.1/",
            "http://0.0.0.0/",
            "http://192.0.2.1/",
            "http://100.64.0.1/",
        ):
            with self.subTest(url=url), self.assertRaises(PluginError):
                validate_crawler_url(url)

    def test_static_crawl_follows_links_extracts_and_keeps_versions(self) -> None:
        bodies = {"/start": "100", "/next": "200"}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            value = bodies[request.url.path]
            link = '<a href="/next">下一页</a>' if request.url.path == "/start" else ""
            return httpx.Response(
                200,
                headers={"content-type": "text/html", "etag": '"{}"'.format(value)},
                text=(
                    "<html><head><title>价格{}</title></head><body>"
                    "<main><div class='price'>{}</div><p>{}</p></main>{}</body></html>"
                ).format(value, value, "正文" * 120, link),
            )

        plugin = self.plugin(handler)
        config = self.source(
            templates=[{
                "name": "价格",
                "url_pattern": "/start$",
                "schema": {
                    "type": "object",
                    "properties": {"price": {"type": "number"}},
                    "required": ["price"],
                    "additionalProperties": False,
                },
                "fields": {"price": {"selector": ".price"}},
            }]
        )
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            source = plugin.store.create_source(self.tenant.tenant_id, config)
            first = self.process(plugin, source)
            self.assertEqual(first["status"], "succeeded")
            self.assertEqual(first["pages_fetched"], 2)
            self.assertEqual(first["records_created"], 1)
            records = plugin.store.query_records(self.tenant.tenant_id)
            self.assertEqual(records[0]["data"]["price"], 100.0)
            self.assertEqual(len(plugin.store.list_pages(self.tenant.tenant_id)), 2)

            bodies["/start"] = "101"
            second = self.process(plugin, source)
            self.assertEqual(second["pages_changed"], 1)
            bodies["/start"] = "102"
            self.process(plugin, source)
        page = next(
            item for item in plugin.store.list_pages(self.tenant.tenant_id)
            if item["canonical_url"].endswith("/start")
        )
        detail = plugin.store.get_page(self.tenant.tenant_id, page["page_id"])
        self.assertEqual(len(detail["snapshots"]), 2)
        self.assertTrue(all(item["available"] for item in detail["snapshots"]))
        self.assertTrue(all("storage_path" not in item for item in detail["snapshots"]))

    def test_sitemap_index_scope_and_canonical_deduplication(self) -> None:
        requested = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if request.url.path == "/robots.txt":
                return httpx.Response(
                    200,
                    text="User-agent: *\nAllow: /\nSitemap: https://example.test/index.xml\n",
                )
            if request.url.path == "/index.xml":
                return httpx.Response(
                    200,
                    headers={"content-type": "application/xml"},
                    text=(
                        "<sitemapindex><sitemap><loc>https://example.test/pages.xml"
                        "</loc></sitemap></sitemapindex>"
                    ),
                )
            if request.url.path == "/pages.xml":
                return httpx.Response(
                    200,
                    headers={"content-type": "application/xml"},
                    text=(
                        "<urlset><url><loc>https://example.test/allowed?utm_source=x&amp;a=1"
                        "</loc></url><url><loc>https://example.test/allowed?a=1</loc></url>"
                        "<url><loc>https://example.test/excluded</loc></url></urlset>"
                    ),
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><body><p>{}</p></body></html>".format("公开资料" * 100),
            )

        plugin = self.plugin(handler)
        config = self.source(
            max_depth=0,
            include_patterns=["^/(start|allowed)"],
            exclude_patterns=["excluded"],
        )
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            source = plugin.store.create_source(self.tenant.tenant_id, config)
            run = self.process(plugin, source)
        self.assertEqual(run["pages_fetched"], 2)
        pages = plugin.store.list_pages(self.tenant.tenant_id)
        self.assertEqual(
            sorted(item["canonical_url"] for item in pages),
            ["https://example.test/allowed?a=1", "https://example.test/start"],
        )
        self.assertFalse(any("/excluded" in value for value in requested))

    def test_redirect_to_link_local_address_is_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(302, headers={"location": "http://169.254.169.254/private"})

        plugin = self.plugin(handler)
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            source = plugin.store.create_source(
                self.tenant.tenant_id, self.source(max_depth=0)
            )
            run = self.process(plugin, source)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["pages_failed"], 1)
        self.assertEqual(plugin.store.list_pages(self.tenant.tenant_id), [])

    def test_http_loopback_source_can_be_crawled(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><title>本机资料</title><body>{}</body></html>".format(
                    "正文" * 120
                ),
            )

        plugin = self.plugin(handler)
        source = plugin.store.create_source(
            self.tenant.tenant_id,
            self.source(
                seed_urls=["http://127.0.0.1:8080/start"],
                allowed_domains=["127.0.0.1"],
                max_depth=0,
            ),
        )
        run = self.process(plugin, source)
        self.assertEqual(run["status"], "succeeded")
        pages = plugin.store.list_pages(self.tenant.tenant_id)
        self.assertEqual(pages[0]["canonical_url"], "http://127.0.0.1:8080/start")

    def test_dns_rebinding_is_rechecked_before_content_is_accepted(self) -> None:
        plugin = self.plugin(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><body>不可保存</body></html>",
            )
        )
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            side_effect=[PUBLIC_RECORD, SENSITIVE_RECORD],
        ), self.assertRaisesRegex(PluginError, "链路本地"):
            plugin._request("https://example.test/start")

    def test_transient_errors_retry_three_attempts_and_honor_retry_after(self) -> None:
        visits = 0
        sleeps = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal visits
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            visits += 1
            if visits == 1:
                return httpx.Response(429, headers={"retry-after": "3"})
            if visits == 2:
                return httpx.Response(503)
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><body><p>{}</p></body></html>".format("资料" * 150),
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        plugin = WebCrawlerPlugin(
            {"min_host_interval_seconds": 0.1, "max_workers": 1},
            self.context,
            client=client,
            sleep=sleeps.append,
        )
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            source = plugin.store.create_source(
                self.tenant.tenant_id, self.source(max_depth=0)
            )
            run = self.process(plugin, source)
        self.assertEqual(visits, 3)
        self.assertEqual(run["status"], "succeeded")
        self.assertIn(3, sleeps)

    def test_cancel_and_restart_recovery_restore_durable_state(self) -> None:
        plugin = self.plugin(lambda _request: httpx.Response(404))
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            source = plugin.store.create_source(self.tenant.tenant_id, self.source())
        queued = plugin.store.enqueue_run(self.tenant.tenant_id, source["source_id"])
        canceled = plugin.store.cancel_run(self.tenant.tenant_id, queued["run_id"])
        self.assertEqual(canceled["status"], "canceled")

        running = plugin.store.enqueue_run(self.tenant.tenant_id, source["source_id"])
        claimed = plugin.store.claim_run("old-process")
        self.assertEqual(claimed["run_id"], running["run_id"])
        plugin.store.add_frontier(running["run_id"], "https://example.test/start", 0)
        processing = plugin.store.next_frontier(running["run_id"])
        self.assertEqual(processing["status"], "queued")
        plugin.store.recover()
        self.assertEqual(
            plugin.store.get_run(self.tenant.tenant_id, running["run_id"])["status"],
            "queued",
        )
        with self.registry.database.read() as connection:
            status = connection.execute(
                "SELECT status FROM crawl_frontier WHERE run_id=?",
                (running["run_id"],),
            ).fetchone()["status"]
        self.assertEqual(status, "queued")

    def test_missing_raw_snapshot_is_reported_without_exposing_path(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><body><p>{}</p></body></html>".format("资料" * 150),
            )

        plugin = self.plugin(handler)
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            source = plugin.store.create_source(
                self.tenant.tenant_id, self.source(max_depth=0)
            )
            self.process(plugin, source)
        page = plugin.store.list_pages(self.tenant.tenant_id)[0]
        with self.registry.database.read() as connection:
            snapshot_row = connection.execute(
                "SELECT snapshot_id, storage_path, text_path FROM crawl_snapshots WHERE page_id=?",
                (page["page_id"],),
            ).fetchone()
        raw_path = Path(snapshot_row["storage_path"])
        raw_path.unlink()
        detail = plugin.store.get_page(self.tenant.tenant_id, page["page_id"])
        self.assertFalse(detail["snapshots"][0]["available"])
        self.assertNotIn("storage_path", detail["snapshots"][0])
        self.assertNotIn("text_path", detail["snapshots"][0])
        Path(snapshot_row["text_path"]).unlink()
        with self.assertRaisesRegex(PluginError, "已损坏"):
            plugin.store.snapshot_text(
                self.tenant.tenant_id, str(snapshot_row["snapshot_id"])
            )

    def test_robots_disallow_prevents_page_fetch(self) -> None:
        requested = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>不能访问</p>")

        plugin = self.plugin(handler)
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            source = plugin.store.create_source(self.tenant.tenant_id, self.source())
            run = self.process(plugin, source)
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["pages_fetched"], 0)
        self.assertEqual(requested, ["/robots.txt"])

    def test_conditional_request_marks_unchanged(self) -> None:
        visits = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal visits
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            visits += 1
            if request.headers.get("if-none-match") == '"v1"':
                return httpx.Response(304)
            return httpx.Response(
                200, headers={"content-type": "text/html", "etag": '"v1"'},
                text="<html><title>稳定</title><body>{}</body></html>".format("文字" * 120),
            )

        plugin = self.plugin(handler)
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            source = plugin.store.create_source(self.tenant.tenant_id, self.source(max_depth=0))
            self.process(plugin, source)
            run = self.process(plugin, source)
        self.assertEqual(visits, 2)
        self.assertEqual(run["pages_fetched"], 1)
        self.assertEqual(run["pages_changed"], 0)

    def test_browser_fallback_uses_rendered_content(self) -> None:
        class Page:
            url = "https://example.test/start"

            def goto(self, *_args, **_kwargs):
                return None

            def title(self):
                return "动态价格"

            def content(self):
                return "<html><body><div class='price'>970.03</div><p>{}</p></body></html>".format("行情" * 120)

        class Session:
            page = Page()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<script>load()</script>")

        plugin = self.plugin(
            handler, browser_factory=lambda: SimpleNamespace(session=lambda: Session())
        )
        config = self.source(
            max_depth=0,
            render_mode="auto",
            templates=[{
                "name": "行情", "url_pattern": ".*",
                "schema": {"type": "object", "properties": {"price": {"type": "number"}}, "required": ["price"]},
                "fields": {"price": {"selector": ".price"}},
            }],
        )
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            source = plugin.store.create_source(self.tenant.tenant_id, config)
            run = self.process(plugin, source)
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(plugin.store.query_records(self.tenant.tenant_id)[0]["data"]["price"], 970.03)

    def test_tools_are_tenant_scoped(self) -> None:
        plugin = self.plugin(lambda _request: httpx.Response(404))
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            plugin.store.create_source(self.tenant.tenant_id, self.source())
        result = plugin.execute("crawler_list_sources", {}, self.tenant)
        self.assertEqual(len(result["sources"]), 1)
        other = self.registry.resolve("bot", "other")
        self.assertEqual(plugin.execute("crawler_list_sources", {}, other)["sources"], [])

    def test_pdf_is_extracted_and_snapshotted(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-test")

        plugin = self.plugin(handler)
        config = self.source(
            seed_urls=["https://example.test/report.pdf"], max_depth=0,
            templates=[{
                "name": "报告", "url_pattern": "report\\.pdf$",
                "schema": {"type": "object", "properties": {"year": {"type": "integer"}}, "required": ["year"]},
                "fields": {"year": {"regex": "年度：(\\d{4})"}},
            }],
        )
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ), patch(
            "src.core.plugins.web_crawler.extract_document_text",
            return_value="年度：2026\n\n报告正文",
        ):
            source = plugin.store.create_source(self.tenant.tenant_id, config)
            run = self.process(plugin, source)
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(plugin.store.query_records(self.tenant.tenant_id)[0]["data"]["year"], 2026)
        page = plugin.store.list_pages(self.tenant.tenant_id)[0]
        self.assertEqual(page["content_type"], "application/pdf")

    def test_model_fallback_is_schema_validated(self) -> None:
        class Session:
            def complete(self, _request):
                return SimpleNamespace(message=SimpleNamespace(content='{"price": 88.5}'))

        router = SimpleNamespace(session=lambda _mode: Session())
        context = PluginContext(
            project_root=self.root, tenant_registry=self.registry,
            data_root=self.root / "plugin-data", plugin_id="web_crawler",
            model_router=router,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                200, headers={"content-type": "text/html"},
                text="<html><body><p>{}</p></body></html>".format("资料" * 120),
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        plugin = WebCrawlerPlugin(
            {"min_host_interval_seconds": 0.1, "max_workers": 1}, context,
            client=client, sleep=lambda _seconds: None,
        )
        config = self.source(
            max_depth=0,
            templates=[{
                "name": "模型行情", "url_pattern": ".*",
                "schema": {"type": "object", "properties": {"price": {"type": "number"}}, "required": ["price"]},
                "fields": {},
            }],
        )
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            source = plugin.store.create_source(self.tenant.tenant_id, config)
            self.process(plugin, source)
        record = plugin.store.query_records(self.tenant.tenant_id)[0]
        self.assertEqual(record["data"]["price"], 88.5)
        self.assertEqual(record["extraction_method"], "model")

    def test_web_knowledge_source_preserves_url_and_refreshes(self) -> None:
        knowledge = KnowledgeService(self.registry)
        category_id = knowledge.ensure_default_category(self.tenant.tenant_id)
        first = knowledge.upsert_web_source(
            self.tenant.tenant_id, category_id, "黄金行情", "今日黄金价格 100",
            "https://example.test/gold", "page-1", "2026-08-20T01:00:00+00:00",
        )
        second = knowledge.upsert_web_source(
            self.tenant.tenant_id, category_id, "黄金行情", "今日黄金价格 101",
            "https://example.test/gold", "page-1", "2026-08-20T02:00:00+00:00",
        )
        self.assertEqual(first["source_id"], second["source_id"])
        listed = knowledge.list(self.tenant.tenant_id, category_id)
        self.assertEqual(listed[0]["source_type"], "web")
        self.assertEqual(listed[0]["source_url"], "https://example.test/gold")
        preview = knowledge.preview_source(first["source_id"])
        self.assertIn("101", preview["content"])
        self.assertEqual(preview["fetched_at"], "2026-08-20T02:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
