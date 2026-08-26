from __future__ import annotations

import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from src.core.plugins.base import PluginContext
from src.core.plugins.web_crawler import WebCrawlerPlugin
from tests._web_api_base import WebApiTestBase


PUBLIC_RECORD = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


class WebCrawlerApiTests(WebApiTestBase):
    def app_kwargs(self) -> dict:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=(
                    "<html><title>今日行情</title><body><div class='price'>970.03</div>"
                    "<p>{}</p></body></html>"
                ).format("行情正文" * 100),
            )

        self.crawler_http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(self.crawler_http.close)
        self.crawler = WebCrawlerPlugin(
            {"min_host_interval_seconds": 0.1, "max_workers": 1},
            PluginContext(
                project_root=self.data_root,
                tenant_registry=self.registry,
                data_root=self.data_root / "plugins",
                plugin_id="web_crawler",
            ),
            client=self.crawler_http,
            sleep=lambda _seconds: None,
        )
        manager = SimpleNamespace(
            get=lambda plugin_id: self.crawler if plugin_id == "web_crawler" else None,
            errors={},
        )
        return {"plugin_manager": manager}

    def setUp(self) -> None:
        super().setUp()
        response = self.client.post(
            "/api/v2/platform/organizations", json={"name": "爬虫测试组织"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.organization_id = response.json()["organization"]["organization_id"]
        self.base = "/api/v2/orgs/{}/".format(self.organization_id)

    @staticmethod
    def source_payload():
        return {
            "name": "金价",
            "seed_urls": ["https://example.test/gold"],
            "allowed_domains": ["example.test"],
            "max_depth": 0,
            "max_pages": 5,
            "render_mode": "static",
            "retention_versions": 5,
            "templates": [{
                "name": "行情",
                "url_pattern": ".*",
                "schema": {
                    "type": "object",
                    "properties": {"price": {"type": "number"}},
                    "required": ["price"],
                },
                "fields": {"price": {"selector": ".price"}},
            }],
        }

    def test_source_run_page_record_and_delete_lifecycle(self) -> None:
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            created = self.client.post(self.base + "crawl-sources", json=self.source_payload())
        self.assertEqual(created.status_code, 201, created.text)
        source_id = created.json()["source_id"]
        self.assertEqual(
            self.client.get(self.base + "crawl-sources").json()["items"][0]["name"],
            "金价",
        )
        queued = self.client.post(
            self.base + "crawl-sources/{}/runs".format(source_id)
        )
        self.assertEqual(queued.status_code, 202, queued.text)
        run_id = queued.json()["run_id"]
        claimed = self.crawler.store.claim_run("api-test")
        with patch(
            "src.core.plugins.browser_automation.socket.getaddrinfo",
            return_value=PUBLIC_RECORD,
        ):
            self.crawler.process_run(claimed)
        detail = self.client.get(self.base + "crawl-runs/" + run_id)
        self.assertEqual(detail.json()["status"], "succeeded")
        self.assertTrue(detail.json()["events"])
        pages = self.client.get(self.base + "crawl-pages").json()["items"]
        self.assertEqual(len(pages), 1)
        records = self.client.get(self.base + "crawl-records").json()["items"]
        self.assertEqual(records[0]["data"]["price"], 970.03)
        deleted = self.client.delete(self.base + "crawl-sources/" + source_id)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.client.get(self.base + "crawl-sources").json()["items"], [])

    def test_page_route_and_membership_guard(self) -> None:
        page = self.client.get(
            "/organization/crawler?organization_id=" + self.organization_id
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn("资料抓取", page.text)
        self.assertIn('class="page-container crawler-page"', page.text)
        self.assertIn('type="button" id="crawler-editor-close"', page.text)
        self.assertIn('id="crawler-name" type="text"', page.text)
        self.assertIn("五段 Cron（Asia/Shanghai）", page.text)
        self.assertIn("格式：分 时 日 月 周", page.text)
        self.assertIn('id="crawler-cron" type="text" placeholder="例如 0 1 * * *"', page.text)
        self.assertIn('id="crawler-cron-example"', page.text)
        denied = self.viewer_client.get(self.base + "crawl-sources")
        self.assertEqual(denied.status_code, 403)

    def test_http_loopback_source_can_be_saved(self) -> None:
        payload = self.source_payload()
        payload.update({
            "seed_urls": ["http://localhost:8080/gold"],
            "allowed_domains": ["localhost"],
        })
        created = self.client.post(self.base + "crawl-sources", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(
            created.json()["config"]["seed_urls"],
            ["http://localhost:8080/gold"],
        )


if __name__ == "__main__":
    unittest.main()
