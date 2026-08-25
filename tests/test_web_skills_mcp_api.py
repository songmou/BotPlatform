"""Integration tests for the MCP service template catalog.

The template catalog is served by ``platform_runtime`` at
``/api/v2/platform/mcp/templates`` (and ``/mcp/templates/{key}``). The legacy
``/api/mcp`` server CRUD routers were removed during the catalog migration;
server CRUD now lives behind ``/api/v2/platform/catalog/mcp``. These tests
cover only the read-only template blueprints (no secrets).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import src.api.routers.platform_runtime as platform_runtime_module

from tests._web_api_base import WebApiTestBase


TEMPLATES = {
    "templates": [
        {
            "key": "tencent_docs",
            "name": "腾讯文档",
            "description": "测试模板",
            "category": "文档协作",
            "transport": "streamablehttp",
            "url": "https://docs.qq.com/openapi/mcp",
            "auth": {
                "kind": "header",
                "key": "Authorization",
                "label": "Token",
                "secret": True,
                "help": "获取地址：https://docs.qq.com/open/auth/mcp.html",
            },
            "help_url": "https://docs.qq.com/open/auth/mcp.html",
        },
        {
            "key": "notion",
            "name": "Notion",
            "description": "stdio 模板",
            "category": "文档协作",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@notionhq/notion-mcp-server"],
            "auth": {
                "kind": "env",
                "key": "NOTION_TOKEN",
                "label": "Integration Token",
                "secret": True,
            },
        },
    ]
}


class McpTemplateApiTest(WebApiTestBase):
    def setUp(self):
        super().setUp()
        self._file_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._file_dir.cleanup)
        self.templates_file = Path(self._file_dir.name) / "mcp_templates.json"
        self.templates_file.write_text(
            json.dumps(TEMPLATES, ensure_ascii=False), encoding="utf-8"
        )
        patcher = patch.object(
            platform_runtime_module, "MCP_TEMPLATES_FILE", self.templates_file
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_list_templates(self):
        response = self.client.get("/api/v2/platform/mcp/templates")
        self.assertEqual(response.status_code, 200, response.text)
        keys = [t["key"] for t in response.json()]
        self.assertIn("tencent_docs", keys)
        self.assertIn("notion", keys)

    def test_list_templates_empty_when_file_absent(self):
        with patch.object(
            platform_runtime_module,
            "MCP_TEMPLATES_FILE",
            Path(self._file_dir.name) / "nope.json",
        ):
            response = self.client.get("/api/v2/platform/mcp/templates")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_get_template_by_key(self):
        response = self.client.get("/api/v2/platform/mcp/templates/tencent_docs")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["key"], "tencent_docs")
        self.assertEqual(data["transport"], "streamablehttp")
        self.assertIsNotNone(data["auth"])
        self.assertEqual(data["auth"]["key"], "Authorization")

    def test_get_template_not_found(self):
        response = self.client.get("/api/v2/platform/mcp/templates/missing")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    import unittest

    unittest.main()
