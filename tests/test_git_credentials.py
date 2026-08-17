"""Tests for per-tenant, per-host git credentials (store, API, runner injection)."""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.core.config.git_credentials as credentials_module
from src.core.config.git_credentials import (
    delete_token,
    list_credentials,
    list_hosts,
    load_token,
    save_token,
)
from src.core.tooling.git_runner import GitRunner

from tests._web_api_base import WebApiTestBase


class GitCredentialsStoreTests(unittest.TestCase):
    """Token storage must be isolated by tenant AND by host."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.credentials_file = Path(self._tmp.name) / "git_credentials.json"
        self.patcher = patch.object(
            credentials_module, "GIT_CREDENTIALS_FILE", self.credentials_file
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_save_and_load_token(self) -> None:
        save_token("tenant_a", "github.com", "ghp_test123")
        self.assertEqual(load_token("tenant_a", "github.com"), "ghp_test123")

    def test_load_missing_token_returns_empty(self) -> None:
        self.assertEqual(load_token("tenant_a", "github.com"), "")

    def test_load_empty_tenant_or_host_returns_empty(self) -> None:
        self.assertEqual(load_token("", "github.com"), "")
        self.assertEqual(load_token("tenant_a", ""), "")

    def test_save_empty_token_deletes(self) -> None:
        save_token("tenant_a", "github.com", "ghp_test123")
        save_token("tenant_a", "github.com", "")
        self.assertEqual(load_token("tenant_a", "github.com"), "")

    def test_delete_token(self) -> None:
        save_token("tenant_a", "gitlab.com", "glpat_abc")
        delete_token("tenant_a", "gitlab.com")
        self.assertEqual(load_token("tenant_a", "gitlab.com"), "")

    def test_hosts_isolated_by_domain(self) -> None:
        save_token("tenant_a", "github.com", "ghp_1")
        save_token("tenant_a", "gitlab.com", "glpat_2")
        save_token("tenant_a", "gitee.com", "gitee_3")
        self.assertEqual(load_token("tenant_a", "github.com"), "ghp_1")
        self.assertEqual(load_token("tenant_a", "gitlab.com"), "glpat_2")
        self.assertEqual(load_token("tenant_a", "gitee.com"), "gitee_3")

    def test_tenants_are_isolated(self) -> None:
        """租户 A 的 token 不能影响/读取租户 B 的 token。"""
        save_token("tenant_a", "github.com", "ghp_secret_a")
        save_token("tenant_b", "github.com", "ghp_secret_b")

        self.assertEqual(load_token("tenant_a", "github.com"), "ghp_secret_a")
        self.assertEqual(load_token("tenant_b", "github.com"), "ghp_secret_b")

        # 删除租户 A 的 token 不影响租户 B
        delete_token("tenant_a", "github.com")
        self.assertEqual(load_token("tenant_a", "github.com"), "")
        self.assertEqual(load_token("tenant_b", "github.com"), "ghp_secret_b")

    def test_list_hosts_is_tenant_scoped(self) -> None:
        save_token("tenant_a", "github.com", "t1")
        save_token("tenant_a", "gitlab.com", "t2")
        save_token("tenant_b", "gitee.com", "t3")
        self.assertEqual(list_hosts("tenant_a"), ["github.com", "gitlab.com"])
        self.assertEqual(list_hosts("tenant_b"), ["gitee.com"])
        self.assertEqual(list_hosts(""), [])
        self.assertEqual(list_hosts("tenant_c"), [])

    def test_list_hosts_never_exposes_tokens(self) -> None:
        save_token("tenant_a", "github.com", "ghp_secret")
        for host in list_hosts("tenant_a"):
            self.assertNotIn("secret", host)
        for entry in list_credentials("tenant_a"):
            self.assertNotIn("token", entry)

    def test_list_empty(self) -> None:
        self.assertEqual(list_hosts("tenant_a"), [])


class GitCredentialInjectionTests(unittest.TestCase):
    """GitRunner must inject only the current tenant's token for the host."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()

    def _runner(self, tenant_id: str | None = "tenant_a") -> GitRunner:
        return GitRunner(
            git_binary="git",
            git_root=self.root,
            author_name="Test",
            author_email="test@test.local",
            tenant_id=tenant_id,
        )

    @staticmethod
    def _expected_header(token: str) -> str:
        encoded = base64.b64encode("oauth2:{}".format(token).encode("utf-8"))
        return "Authorization: Basic {}".format(encoded.decode("ascii"))

    @patch("src.core.tooling.git_runner.load_token", return_value="ghp_token123")
    def test_clone_injects_extra_header_for_host(self, mock_token) -> None:
        env: dict[str, str] = {}
        secrets = self._runner("tenant_a")._apply_credentials(
            "clone", ["https://github.com/owner/repo.git"], self.root, env
        )
        self.assertEqual(secrets, ["ghp_token123"])
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(
            env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraHeader"
        )
        self.assertEqual(
            env["GIT_CONFIG_VALUE_0"], self._expected_header("ghp_token123")
        )
        mock_token.assert_called_once_with("tenant_a", "github.com")

    @patch("src.core.tooling.git_runner.load_token", return_value="ghp_token123")
    def test_token_never_lands_in_argv(self, _mock_token) -> None:
        """Token 只能走环境变量，不能出现在 URL 或命令行参数里。"""
        args = ["https://github.com/owner/repo.git"]
        env: dict[str, str] = {}
        self._runner("tenant_a")._apply_credentials("clone", args, self.root, env)
        self.assertEqual(args, ["https://github.com/owner/repo.git"])

    @patch("src.core.tooling.git_runner.load_token", return_value="")
    def test_no_token_injects_nothing(self, _mock_token) -> None:
        env: dict[str, str] = {}
        secrets = self._runner("tenant_a")._apply_credentials(
            "clone", ["https://github.com/owner/repo.git"], self.root, env
        )
        self.assertEqual(secrets, [])
        self.assertEqual(env, {})

    @patch("src.core.tooling.git_runner.load_token")
    def test_without_tenant_uses_anonymous(self, mock_token) -> None:
        """未绑定租户时不做凭据注入（匿名访问）。"""
        env: dict[str, str] = {}
        secrets = self._runner(None)._apply_credentials(
            "clone", ["https://github.com/owner/repo.git"], self.root, env
        )
        self.assertEqual(secrets, [])
        self.assertEqual(env, {})
        mock_token.assert_not_called()

    @patch("src.core.tooling.git_runner.load_token", return_value="ghp_token123")
    def test_ssh_url_ignored(self, mock_token) -> None:
        env: dict[str, str] = {}
        secrets = self._runner("tenant_a")._apply_credentials(
            "clone", ["git@github.com:owner/repo.git"], self.root, env
        )
        self.assertEqual(secrets, [])
        self.assertEqual(env, {})
        mock_token.assert_not_called()

    @patch("src.core.tooling.git_runner.load_token", return_value="ghp_token123")
    def test_non_network_command_ignored(self, mock_token) -> None:
        env: dict[str, str] = {}
        secrets = self._runner("tenant_a")._apply_credentials(
            "commit", ["-m", "hi"], self.root, env
        )
        self.assertEqual(secrets, [])
        self.assertEqual(env, {})
        mock_token.assert_not_called()

    @patch("src.core.tooling.git_runner.load_token", return_value="ghp_token123")
    def test_push_reads_host_from_remote_config(self, mock_token) -> None:
        """push/pull 不带 URL，域名必须从仓库 remote 配置里读出来。"""
        runner = self._runner("tenant_a")
        env: dict[str, str] = {}
        with patch.object(
            GitRunner,
            "_remote_hosts",
            return_value=["gitlab.example.com"],
        ):
            secrets = runner._apply_credentials("push", ["origin", "main"], self.root, env)
        self.assertEqual(secrets, ["ghp_token123"])
        self.assertEqual(
            env["GIT_CONFIG_KEY_0"],
            "http.https://gitlab.example.com/.extraHeader",
        )
        mock_token.assert_called_once_with("tenant_a", "gitlab.example.com")


class GitCredentialsApiTest(WebApiTestBase):
    """Integration tests for /api/tenants/{id}/git/credentials endpoints."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.credentials_file = Path(self._tmp.name) / "git_credentials.json"
        self.patcher = patch.object(
            credentials_module, "GIT_CREDENTIALS_FILE", self.credentials_file
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        super().setUp()
        self.tenant_a = self._make_tenant("wxid_tenant_a")
        self.tenant_b = self._make_tenant("wxid_tenant_b")

    def test_save_list_delete_flow(self) -> None:
        tenant_id = self.tenant_a.tenant_id
        # 保存
        response = self.client.post(
            "/api/tenants/{}/git/credentials".format(tenant_id),
            json={"host": "github.com", "token": "ghp_secret123"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["configured"])
        # 列表（不泄露 token）
        response = self.client.get(
            "/api/tenants/{}/git/credentials".format(tenant_id)
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data, [{"host": "github.com", "configured": True}])
        self.assertNotIn("secret", str(data))
        # 删除
        response = self.client.delete(
            "/api/tenants/{}/git/credentials/github.com".format(tenant_id)
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["configured"])
        response = self.client.get(
            "/api/tenants/{}/git/credentials".format(tenant_id)
        )
        self.assertEqual(response.json(), [])

    def test_tenants_isolated_via_api(self) -> None:
        """租户 A 保存的凭据对租户 B 不可见。"""
        a_id = self.tenant_a.tenant_id
        b_id = self.tenant_b.tenant_id
        response = self.client.post(
            "/api/tenants/{}/git/credentials".format(a_id),
            json={"host": "github.com", "token": "ghp_a"},
        )
        self.assertEqual(response.status_code, 200, response.text)

        # 租户 B 列表里看不到
        data = self.client.get(
            "/api/tenants/{}/git/credentials".format(b_id)
        ).json()
        self.assertEqual(data, [])
        # 租户 A 能看到
        data = self.client.get(
            "/api/tenants/{}/git/credentials".format(a_id)
        ).json()
        self.assertEqual([entry["host"] for entry in data], ["github.com"])

    def test_host_normalized_to_lowercase(self) -> None:
        tenant_id = self.tenant_a.tenant_id
        response = self.client.post(
            "/api/tenants/{}/git/credentials".format(tenant_id),
            json={"host": "GitHub.COM", "token": "ghp_x"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["host"], "github.com")

    def test_multiple_hosts(self) -> None:
        tenant_id = self.tenant_a.tenant_id
        for host, token in (("github.com", "t1"), ("gitlab.com", "t2"), ("gitee.com", "t3")):
            response = self.client.post(
                "/api/tenants/{}/git/credentials".format(tenant_id),
                json={"host": host, "token": token},
            )
            self.assertEqual(response.status_code, 200, response.text)
        data = self.client.get(
            "/api/tenants/{}/git/credentials".format(tenant_id)
        ).json()
        self.assertEqual(
            [entry["host"] for entry in data],
            ["gitee.com", "github.com", "gitlab.com"],
        )

    def test_unknown_tenant_returns_404(self) -> None:
        response = self.client.get("/api/tenants/no_such_tenant/git/credentials")
        self.assertEqual(response.status_code, 404)

    def test_requires_login(self) -> None:
        import fastapi.testclient

        anonymous = fastapi.testclient.TestClient(self.app)
        response = anonymous.post(
            "/api/tenants/{}/git/credentials".format(self.tenant_a.tenant_id),
            json={"host": "github.com", "token": "x"},
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
