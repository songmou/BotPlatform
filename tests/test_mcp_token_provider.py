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
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.config.loader import validate_mcp_server_entries
from src.core.config.mcp_headers import (
    delete_secret,
    delete_user_token,
    load_secret,
    load_user_token,
    save_secret,
    save_user_token,
)
from src.core.tooling import mcp_client as mcp_client_module
from src.core.tooling.mcp_token_providers import (
    FeishuTatProvider,
    FeishuUatProvider,
    build_feishu_authorize_url,
    build_provider,
    exchange_feishu_code,
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


class _UrlRouter:
    """Return canned responses keyed by a substring of the request URL."""

    def __init__(self, mapping: dict) -> None:
        self.mapping = mapping
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        url = getattr(request, "full_url", str(request))
        for key, payload in self.mapping.items():
            if key in url:
                return _FakeResponse(payload)
        raise AssertionError("unexpected url: {}".format(url))


class FeishuUatProviderTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._file = Path(self._dir.name) / "mcp_headers.json"
        patcher = patch(
            "src.core.config.mcp_headers.MCP_HEADERS_FILE", self._file
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._dir.cleanup)

    def test_missing_token_raises_actionable_error(self):
        provider = FeishuUatProvider("feishu", "user1", "cli_x")
        with patch(
            "src.core.config.mcp_headers.load_secret", return_value="s3cr3t"
        ):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(provider.get_headers())
        self.assertIn("请先在面板完成飞书授权", str(ctx.exception))

    def test_refreshes_expired_token(self):
        # Seed an expired token + a usable refresh_token.
        save_user_token(
            "feishu",
            "user1",
            {
                "access_token": "OLD",
                "refresh_token": "REF",
                "expires_at": 0.0,  # already expired
            },
        )
        router = _UrlRouter(
            {
                "app_access_token": {"code": 0, "app_access_token": "APP", "expire": 7200},
                "oidc/refresh_access_token": {
                    "code": 0,
                    "access_token": "UAT2",
                    "refresh_token": "REF2",
                    "expires_in": 7200,
                },
            }
        )
        with patch("urllib.request.urlopen", router), patch(
            "src.core.config.mcp_headers.load_secret", return_value="s3cr3t"
        ):
            headers = asyncio.run(
                FeishuUatProvider("feishu", "user1", "cli_x").get_headers()
            )
        self.assertEqual(headers, {"X-Lark-MCP-UAT": "UAT2"})
        # Persisted token must be updated.
        stored = load_user_token("feishu", "user1")
        self.assertEqual(stored["access_token"], "UAT2")

    def test_uses_cached_valid_token(self):
        save_user_token(
            "feishu",
            "user1",
            {
                "access_token": "UAT1",
                "refresh_token": "REF",
                "expires_at": time.time() + 3600,
            },
        )
        provider = FeishuUatProvider("feishu", "user1", "cli_x")
        with patch("urllib.request.urlopen") as urlopen:
            headers = asyncio.run(provider.get_headers())
        self.assertEqual(headers, {"X-Lark-MCP-UAT": "UAT1"})
        urlopen.assert_not_called()


class FeishuOauthExchangeTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._file = Path(self._dir.name) / "mcp_headers.json"
        patcher = patch(
            "src.core.config.mcp_headers.MCP_HEADERS_FILE", self._file
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._dir.cleanup)

    def test_exchange_code_persists_uat(self):
        router = _UrlRouter(
            {
                "app_access_token": {"code": 0, "app_access_token": "APP", "expire": 7200},
                "oidc/access_token": {
                    "code": 0,
                    "access_token": "UAT",
                    "refresh_token": "REF",
                    "expires_in": 7200,
                    "open_id": "ou_abc",
                },
            }
        )
        with patch("urllib.request.urlopen", router), patch(
            "src.core.config.mcp_headers.load_secret", return_value="s3cr3t"
        ):
            payload = exchange_feishu_code("feishu", "user1", "cli_x", "AUTHCODE")
        self.assertEqual(payload["access_token"], "UAT")
        stored = load_user_token("feishu", "user1")
        self.assertEqual(stored["access_token"], "UAT")
        self.assertEqual(stored["open_id"], "ou_abc")

    def test_exchange_code_error_raises(self):
        router = _UrlRouter(
            {
                "app_access_token": {"code": 0, "app_access_token": "APP", "expire": 7200},
                "oidc/access_token": {"code": 20005, "msg": "invalid code"},
            }
        )
        with patch("urllib.request.urlopen", router), patch(
            "src.core.config.mcp_headers.load_secret", return_value="s3cr3t"
        ):
            with self.assertRaises(RuntimeError):
                exchange_feishu_code("feishu", "user1", "cli_x", "BADCODE")


class BuildProviderUatTest(unittest.TestCase):
    def test_feishu_uat_resolves_with_user_id(self):
        provider = build_provider(
            "feishu", {"kind": "feishu_uat", "app_id": "cli_x"}, user_id="user1"
        )
        self.assertIsInstance(provider, FeishuUatProvider)
        self.assertEqual(provider._user_id, "user1")
        self.assertEqual(provider._app_id, "cli_x")

    def test_feishu_uat_without_user_id_raises(self):
        with self.assertRaises(RuntimeError):
            build_provider("feishu", {"kind": "feishu_uat", "app_id": "cli_x"})


class FeishuAuthorizeUrlTest(unittest.TestCase):
    def test_builds_url_with_params(self):
        url = build_feishu_authorize_url("cli_x", "https://x/cb", "st4te")
        self.assertIn("open.feishu.cn", url)
        self.assertIn("app_id=cli_x", url)
        self.assertIn("redirect_uri=", url)
        self.assertIn("state=st4te", url)


class KeychainUserTokenStorageTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._file = Path(self._dir.name) / "mcp_headers.json"
        patcher = patch(
            "src.core.config.mcp_headers.MCP_HEADERS_FILE", self._file
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._dir.cleanup)

    def test_user_token_roundtrip(self):
        self.assertIsNone(load_user_token("feishu", "user1"))
        save_user_token(
            "feishu", "user1", {"access_token": "UAT", "refresh_token": "REF"}
        )
        loaded = load_user_token("feishu", "user1")
        self.assertEqual(loaded["access_token"], "UAT")
        # Different user must not collide.
        self.assertIsNone(load_user_token("feishu", "user2"))
        delete_user_token("feishu", "user1")
        self.assertIsNone(load_user_token("feishu", "user1"))


class McpClientUserScopingTest(unittest.TestCase):
    def test_server_ids_collapses_user_keys(self):
        from types import SimpleNamespace

        mgr = mcp_client_module.McpClientManager()
        fake = SimpleNamespace(
            server_id="feishu",
            session=object(),
            tools={"feishu__get-user": {"real_name": "get-user"}},
            cfg={},
        )
        mgr._connections = {("feishu", ""): fake, ("feishu", "user1"): fake}
        self.assertEqual(mgr.server_ids(), ["feishu"])

    def test_resolve_target_prefers_user_connection(self):
        from types import SimpleNamespace

        mgr = mcp_client_module.McpClientManager()
        shared = SimpleNamespace(
            server_id="feishu",
            session="shared-session",
            tools={"feishu__get-user": {"real_name": "get-user"}},
            cfg={},
        )
        user = SimpleNamespace(
            server_id="feishu",
            session="user-session",
            tools={"feishu__get-user": {"real_name": "get-user"}},
            cfg={},
        )
        mgr._connections = {("feishu", ""): shared, ("feishu", "user1"): user}
        target = mgr._resolve_target("feishu__get-user", user_id="user1")
        self.assertEqual(target[1], "user-session")
        fallback = mgr._resolve_target("feishu__get-user")
        self.assertEqual(fallback[1], "shared-session")

    def test_resolve_target_never_borrows_another_users_connection(self):
        from types import SimpleNamespace

        user = SimpleNamespace(
            server_id="feishu",
            session="user-one-session",
            tools={"feishu__get-user": {"real_name": "get-user"}},
            cfg={},
        )
        mgr = mcp_client_module.McpClientManager()
        mgr._connections = {("feishu", "user1"): user}
        self.assertIsNone(
            mgr._resolve_target("feishu__get-user", user_id="user2")
        )

    def test_is_disconnected_recognizes_timeout(self):
        self.assertTrue(mcp_client_module._is_disconnected(TimeoutError("30s")))
        # TimeoutError wrapped in an anyio ExceptionGroup must also be detected.
        group = RuntimeError("unhandled errors in a TaskGroup (1 sub-exception)")
        group.exceptions = [TimeoutError("30s")]
        self.assertTrue(mcp_client_module._is_disconnected(group))


if __name__ == "__main__":
    unittest.main()
