"""Web API tests for the agent publish endpoints."""

from __future__ import annotations

from unittest import mock

from src.core.integrations.wecom_bot import WeComVerifyError
from src.core.services.publish import PublishStore
from src.core.services.wechat_login import WeChatLoginManager

from tests._web_api_base import WebApiTestBase

_VERIFY = "src.api.routers.publish.verify_wecom_credentials"


class PublishApiTests(WebApiTestBase):
    def app_kwargs(self) -> dict:
        self.publish_store = PublishStore(self.data_root / "publish.json")
        return {"publish_store": self.publish_store}

    def _install_login_manager(self, connected: bool = False) -> WeChatLoginManager:
        cred_path = self.data_root / "wechat-credentials.json"
        if connected:
            cred_path.write_text("{}", encoding="utf-8")
        manager = WeChatLoginManager(
            client_factory=lambda: (_ for _ in ()).throw(RuntimeError("不应触发")),
            credentials_path=cred_path,
        )
        self.app.state.wechat_login_manager = manager
        return manager

    def test_list_reports_all_platforms(self):
        resp = self.client.get("/api/publish")
        self.assertEqual(resp.status_code, 200)
        platforms = {p["platform"]: p for p in resp.json()["platforms"]}
        self.assertTrue(platforms["wechat"]["supported"])
        self.assertTrue(platforms["wecom"]["supported"])
        self.assertFalse(platforms["dingtalk"]["supported"])
        self.assertFalse(platforms["feishu"]["supported"])

    def test_publish_and_list_binding(self):
        resp = self.client.put(
            "/api/publish/wechat/agents",
            json={"agent_id": "general"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        bound = self.publish_store.bound_agent("wechat")
        self.assertEqual(bound["agent_id"], "general")
        self.assertTrue(bound["enabled"])
        listed = self.client.get("/api/publish").json()["platforms"]
        wechat = next(p for p in listed if p["platform"] == "wechat")
        self.assertEqual(wechat["agent"]["agent_id"], "general")

    def test_publish_unknown_agent_404(self):
        resp = self.client.put(
            "/api/publish/wechat/agents", json={"agent_id": "ghost"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_placeholder_platform_rejected(self):
        for platform in ("dingtalk", "feishu"):
            resp = self.client.put(
                "/api/publish/" + platform + "/agents",
                json={"agent_id": "general"},
            )
            self.assertEqual(resp.status_code, 400, platform)
            self.assertEqual(resp.json()["detail"], "该平台暂未开放")

    def test_enable_disable_and_delete(self):
        self.client.put(
            "/api/publish/wechat/agents", json={"agent_id": "general"}
        )
        resp = self.client.put(
            "/api/publish/wechat/agents/general/enabled",
            json={"enabled": False},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.publish_store.bound_agent("wechat")["enabled"])

        resp = self.client.delete("/api/publish/wechat/agents/general")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.publish_store.bound_agent("wechat"))

    def test_delete_missing_binding_404(self):
        resp = self.client.delete("/api/publish/wechat/agents/general")
        self.assertEqual(resp.status_code, 404)

    def test_viewer_cannot_publish(self):
        resp = self.viewer_client.put(
            "/api/publish/wechat/agents", json={"agent_id": "general"}
        )
        self.assertEqual(resp.status_code, 403)

    def test_wecom_config_requires_fields(self):
        resp = self.client.put(
            "/api/publish/wecom/config",
            json={"bot_id": "", "secret": ""},
        )
        self.assertEqual(resp.status_code, 400)

    def test_wecom_config_rejects_invalid_credentials(self):
        with mock.patch(_VERIFY, side_effect=WeComVerifyError("机器人不存在")):
            resp = self.client.put(
                "/api/publish/wecom/config",
                json={"bot_id": "bad", "secret": "bad"},
            )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("机器人不存在", resp.json()["detail"])
        listed = self.client.get("/api/publish").json()["platforms"]
        wecom = next(p for p in listed if p["platform"] == "wecom")
        self.assertFalse(wecom["config"]["configured"])

    def test_wecom_config_hides_secret(self):
        with mock.patch(_VERIFY, return_value=None):
            resp = self.client.put(
                "/api/publish/wecom/config",
                json={"bot_id": "bot123", "secret": "sec456"},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        listed = self.client.get("/api/publish").json()["platforms"]
        wecom = next(p for p in listed if p["platform"] == "wecom")
        self.assertTrue(wecom["config"]["configured"])
        self.assertEqual(wecom["config"]["bot_id"], "bot123")
        self.assertNotIn("secret", wecom["config"])

    def test_wecom_publish_requires_config(self):
        resp = self.client.put(
            "/api/publish/wecom/agents", json={"agent_id": "general"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Bot ID", resp.json()["detail"])

    def test_wecom_publish_succeeds_after_config(self):
        with mock.patch(_VERIFY, return_value=None):
            self.client.put(
                "/api/publish/wecom/config",
                json={"bot_id": "bot123", "secret": "sec456"},
            )
        resp = self.client.put(
            "/api/publish/wecom/agents", json={"agent_id": "general"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_wechat_status_disconnected(self):
        self._install_login_manager(connected=False)
        resp = self.client.get("/api/publish/wechat/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["connected"])
        self.assertEqual(body["state"], "idle")

    def test_wechat_status_connected(self):
        self._install_login_manager(connected=True)
        resp = self.client.get("/api/publish/wechat/status")
        self.assertTrue(resp.json()["connected"])

    def test_wechat_login_allowed_when_connected(self):
        # Re-scanning must be allowed even when connected, so a kicked bot
        # can reclaim the connection by scanning again.
        self._install_login_manager(connected=True)
        resp = self.client.post("/api/publish/wechat/login")
        self.assertEqual(resp.status_code, 200)

    def test_wechat_login_requires_permission(self):
        self._install_login_manager(connected=False)
        resp = self.viewer_client.post("/api/publish/wechat/login")
        self.assertEqual(resp.status_code, 403)
