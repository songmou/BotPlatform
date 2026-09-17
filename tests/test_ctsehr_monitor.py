from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from src.core.jobs.ctsehr.monitor import AuthenticationError, OAClient


class _Response:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.text = text


class CtsehrLoginTests(unittest.TestCase):
    def test_login_uses_dedicated_password_endpoint(self) -> None:
        client = OAClient.__new__(OAClient)
        client.logger = MagicMock()
        client.client = MagicMock()
        client.client.cookies = {".FormsAuthCookie": "session"}
        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "GET":
                return _Response(
                    "https://ehr.example/ZDSHRS/account/logon.aspx",
                    '<script>var enc2k = "key";</script>'
                    '<form id="_Logon" action="logon.aspx">'
                    '<input type="hidden" name="state" value="one">'
                    "</form>",
                )
            return _Response(
                "https://ehr.example/ZDSHRS/frame2021/default.aspx", ""
            )

        client._request = request
        client.login("account", "password")

        self.assertEqual(calls[0][:2], ("GET", "/account/logon.aspx"))
        self.assertEqual(calls[1][0], "POST")
        self.assertEqual(calls[1][2]["data"]["txtLoginID"], "account")
        self.assertTrue(calls[1][2]["data"]["txtLoginPswd"])

    def test_login_reports_feishu_sso_redirect(self) -> None:
        client = OAClient.__new__(OAClient)
        client._request = lambda *_args, **_kwargs: _Response(
            "https://accounts.feishu.cn/accounts/page/login", "<html></html>"
        )

        with self.assertRaisesRegex(AuthenticationError, "飞书单点登录"):
            client.login("account", "password")


if __name__ == "__main__":
    unittest.main()
