"""Tests for the remote-MCP token-provider subsystem (Feishu TAT auto-refresh).

Covers ``FeishuTatProvider`` token fetch/cache/refresh, the auth-error
detector, ``build_provider`` resolution, the loader whitelist, and the
keychain-backed app-secret storage used so the secret never lands in config
or the catalog DB.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.config.loader import validate_mcp_server_entries
from src.core.config.mcp_headers import (
    delete_secret,
    load_secret,
    save_secret,
)
from src.core.tooling import mcp_client as mcp_client_module
from src.core.tooling.mcp_token_providers import (
    FeishuTatProvider,
    build_provider,
)


class _FakeResponse:
    """Minimal ``urllib`` context-manager response for tests."""

    def __init__(self, payload: dict) -> None:
        self._bytes = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._bytes


class FeishuTatProviderTest(unittest.TestCase):
    def _patch_urlopen(self, payload):
        return patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse(payload),
        )

    def test_fetches_and_caches_token(self):
        provider = FeishuTatProvider(app_id="cli_x", app_secret="s3cr3t")
        with self._patch_urlopen(
            {"code": 0, "tenant_access_token": "TAT123", "expire": 7200}
        ) as urlopen:
            headers = asyncio.run(provider.get_headers())
            self.assertEqual(headers, {"X-Lark-MCP-TAT": "TAT123"})
            # Second call must reuse the cache (no second HTTP request).
            asyncio.run(provider.get_headers())
            self.assertEqual(urlopen.call_count, 1)
            self.assertEqual(provider._token, "TAT123")

    def test_force_refresh_ignores_cache(self):
        provider = FeishuTatProvider(app_id="cli_x", app_secret="s3cr3t")
        with self._patch_urlopen(
            {"code": 0, "tenant_access_token": "TAT-A", "expire": 7200}
        ) as urlopen:
            asyncio.run(provider.get_headers())
            asyncio.run(provider.get_headers(force=True))
            self.assertEqual(urlopen.call_count, 2)

    def test_error_response_raises(self):
        provider = FeishuTatProvider(app_id="cli_x", app_secret="s3cr3t")
        with self._patch_urlopen({"code": 999, "msg": "app not found"}):
            with self.assertRaises(RuntimeError):
                asyncio.run(provider.get_headers())

    def test_network_failure_raises_readable_error(self):
        provider = FeishuTatProvider(app_id="cli_x", app_secret="s3cr3t")
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            with self.assertRaises(RuntimeError):
                asyncio.run(provider.get_headers())


class AuthErrorDetectionTest(unittest.TestCase):
    def test_detects_auth_markers(self):
        for message in (
            "401 Unauthorized",
            "McpError: token expired",
            "invalid token",
            "tenant_access_token is invalid",
            "鉴权失败",
        ):
            exc = RuntimeError(message)
            self.assertTrue(
                mcp_client_module._is_auth_error(exc),
                "expected auth error for: {}".format(message),
            )

    def test_ignores_unrelated_errors(self):
        for message in ("read timeout", "connection reset", "tool not found"):
            exc = RuntimeError(message)
            self.assertFalse(mcp_client_module._is_auth_error(exc))


class BuildProviderTest(unittest.TestCase):
    def test_feishu_tat_resolves_with_secret(self):
        with patch(
            "src.core.config.mcp_headers.load_secret", return_value="s3cr3t"
        ):
            provider = build_provider(
                "feishu",
                {"kind": "feishu_tat", "app_id": "cli_x", "app_secret": "ignored"},
            )
        self.assertIsInstance(provider, FeishuTatProvider)
        self.assertEqual(provider._app_id, "cli_x")
        self.assertEqual(provider._app_secret, "s3cr3t")

    def test_missing_secret_raises(self):
        with patch(
            "src.core.config.mcp_headers.load_secret", return_value=None
        ):
            with self.assertRaises(RuntimeError):
                build_provider("feishu", {"kind": "feishu_tat", "app_id": "cli_x"})

    def test_unsupported_kind_raises(self):
        with self.assertRaises(RuntimeError):
            build_provider("x", {"kind": "nope"})

    def test_empty_config_returns_none(self):
        self.assertIsNone(build_provider("x", None))
        self.assertIsNone(build_provider("x", {}))


class LoaderWhitelistTest(unittest.TestCase):
    def test_token_provider_accepted(self):
        servers = validate_mcp_server_entries(
            [
                {
                    "id": "feishu",
                    "name": "飞书",
                    "transport": "streamablehttp",
                    "url": "https://mcp.feishu.cn/mcp",
                    "token_provider": {"kind": "feishu_tat", "app_id": "cli_x"},
                }
            ],
            "test",
        )
        self.assertEqual(len(servers), 1)
        self.assertEqual(
            servers[0]["token_provider"], {"kind": "feishu_tat", "app_id": "cli_x"}
        )

    def test_token_provider_must_be_object(self):
        with self.assertRaises(Exception):
            validate_mcp_server_entries(
                [
                    {
                        "id": "feishu",
                        "name": "飞书",
                        "transport": "streamablehttp",
                        "url": "https://mcp.feishu.cn/mcp",
                        "token_provider": "feishu_tat",
                    }
                ],
                "test",
            )


class KeychainSecretStorageTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._file = Path(self._dir.name) / "mcp_headers.json"
        patcher = patch(
            "src.core.config.mcp_headers.MCP_HEADERS_FILE", self._file
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._dir.cleanup)

    def test_save_load_delete_roundtrip(self):
        self.assertIsNone(load_secret("svc"))
        save_secret("svc", "topsecret")
        self.assertEqual(load_secret("svc"), "topsecret")
        delete_secret("svc")
        self.assertIsNone(load_secret("svc"))

    def test_save_empty_deletes(self):
        save_secret("svc", "topsecret")
        save_secret("svc", "")
        self.assertIsNone(load_secret("svc"))

    def test_delete_missing_is_safe(self):
        delete_secret("never_existed")


if __name__ == "__main__":
    unittest.main()
