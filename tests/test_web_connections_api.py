"""Web API tests for personal channel connections."""

from __future__ import annotations

from unittest import mock

from fastapi.testclient import TestClient

from src.core.services.wechat_login import WeChatLoginManager

from tests._web_api_base import WebApiTestBase

_VERIFY = "src.api.routers.connections.verify_wecom_credentials"


class ConnectionsApiTests(WebApiTestBase):
    def _create_owner(self, suffix: str):
        created = self.client.post(
            "/api/v2/platform/organizations", json={"name": "连接组织 " + suffix}
        )
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        owner = TestClient(self.app)
        accepted = owner.post(
            "/api/v2/invitations/accept",
            json={
                "token": payload["owner_invitation_token"],
                "username": "cx_" + suffix,
                "password": "password-" + suffix,
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        login = owner.post(
            "/api/auth/login",
            json={"username": "cx_" + suffix, "password": "password-" + suffix},
        )
        self.assertEqual(login.status_code, 200, login.text)
        return payload["organization"]["organization_id"], owner

    def _invite_member(self, owner: TestClient, organization_id: str, suffix: str):
        invitation = owner.post(
            "/api/v2/orgs/{}/invitations".format(organization_id),
            json={"role": "member"},
        )
        self.assertEqual(invitation.status_code, 201, invitation.text)
        member = TestClient(self.app)
        accepted = member.post(
            "/api/v2/invitations/accept",
            json={
                "token": invitation.json()["invitation_token"],
                "username": "cxm_" + suffix,
                "password": "password-" + suffix,
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        login = member.post(
            "/api/auth/login",
            json={"username": "cxm_" + suffix, "password": "password-" + suffix},
        )
        self.assertEqual(login.status_code, 200, login.text)
        return member

    def test_wecom_connection_create_verify_and_hide_secret(self):
        org_id, owner = self._create_owner("wecom-api")
        with mock.patch(_VERIFY) as verify:
            created = owner.post(
                "/api/connections",
                json={
                    "platform": "wecom",
                    "organization_id": org_id,
                    "agent_id": "general",
                    "bot_id": "bot-1",
                    "secret": "secret-1",
                },
            )
        self.assertEqual(created.status_code, 201, created.text)
        verify.assert_called_once_with("bot-1", "secret-1")
        payload = created.json()
        self.assertEqual(payload["platform"], "wecom")
        self.assertEqual(payload["bot_account_id"], "bot-1")
        self.assertTrue(payload["credential_configured"])

        listed = owner.get("/api/connections").json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertNotIn("secret-1", str(listed))

        # Organization credential list must not expose the personal secret row.
        org_credentials = owner.get(
            "/api/v2/orgs/{}/credentials".format(org_id)
        )
        if org_credentials.status_code == 200:
            self.assertNotIn("secret-1", org_credentials.text)

    def test_wecom_invalid_credentials_roll_back_connection(self):
        from src.core.integrations.wecom_verify import WeComVerifyError

        org_id, owner = self._create_owner("wecom-bad")
        with mock.patch(
            _VERIFY, side_effect=WeComVerifyError("握手失败")
        ):
            created = owner.post(
                "/api/connections",
                json={
                    "platform": "wecom",
                    "organization_id": org_id,
                    "agent_id": "general",
                    "bot_id": "bot-x",
                    "secret": "secret-x",
                },
            )
        self.assertEqual(created.status_code, 400, created.text)
        self.assertEqual(owner.get("/api/connections").json()["items"], [])

    def test_wechat_connection_login_flow_with_injected_manager(self):
        org_id, owner = self._create_owner("wechat-api")
        created = owner.post(
            "/api/connections",
            json={
                "platform": "wechat",
                "organization_id": org_id,
                "agent_id": "general",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        connection_id = created.json()["connection_id"]
        self.assertFalse(created.json()["credential_configured"])

        status = owner.get(
            "/api/connections/{}/wechat/status".format(connection_id)
        )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["state"], "idle")
        self.assertFalse(status.json()["connected"])

        class _FakeCredentials:
            bot_id = "wx-bot-9"

            def to_dict(self):
                return {
                    "token": "tok",
                    "base_url": "https://example.com",
                    "bot_id": self.bot_id,
                    "user_id": "wxid_9",
                }

        class _FakeClient:
            def login(self, show_qr, status_changed=None):
                return _FakeCredentials()

            def close(self):
                pass

        from src.core.services.connections import PersonalConnectionService

        service = PersonalConnectionService(
            self.app.state.organization_store,
            self.app.state.organization_control_store,
            self.app.state.credential_service,
        )
        holder = {}
        manager = WeChatLoginManager(
            client_factory=_FakeClient,
            credentials_saver=lambda creds, _path: holder.update(
                pending=creds.to_dict()
            ),
            connected_checker=lambda: True,
        )
        manager.pending_holder = holder
        self.app.state.wechat_login_managers[connection_id] = manager
        started = owner.post(
            "/api/connections/{}/wechat/login".format(connection_id)
        )
        self.assertEqual(started.status_code, 200, started.text)
        for _ in range(100):
            state = owner.get(
                "/api/connections/{}/wechat/status".format(connection_id)
            ).json()
            if state["state"] in {"success", "failed"}:
                break
        self.assertEqual(state["state"], "success", state)
        # Credentials are staged but not yet active until confirmed.
        detail = owner.get("/api/connections").json()["items"][0]
        self.assertFalse(detail["credential_configured"])

        confirm = owner.post(
            "/api/connections/{}/wechat/confirm".format(connection_id)
        )
        self.assertEqual(confirm.status_code, 200, confirm.text)
        detail = owner.get("/api/connections").json()["items"][0]
        self.assertEqual(detail["bot_account_id"], "wx-bot-9")
        self.assertTrue(detail["credential_configured"])

        # Confirming again without a new scan is rejected.
        manager.pending_holder["pending"] = None
        rejected = owner.post(
            "/api/connections/{}/wechat/confirm".format(connection_id)
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)

    def test_ownership_is_enforced(self):
        org_id, owner = self._create_owner("owner-check")
        other = self._invite_member(owner, org_id, "other-user")
        created = owner.post(
            "/api/connections",
            json={
                "platform": "wechat",
                "organization_id": org_id,
                "agent_id": "general",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        connection_id = created.json()["connection_id"]

        self.assertEqual(other.get("/api/connections").json()["items"], [])
        self.assertEqual(
            other.delete("/api/connections/{}".format(connection_id)).status_code,
            404,
        )
        self.assertEqual(
            other.get(
                "/api/connections/{}/wechat/status".format(connection_id)
            ).status_code,
            404,
        )
        self.assertEqual(
            owner.delete("/api/connections/{}".format(connection_id)).status_code,
            200,
        )

    def test_organization_channel_endpoints_hide_personal_connections(self):
        org_id, owner = self._create_owner("hide-personal")
        created = owner.post(
            "/api/connections",
            json={
                "platform": "wechat",
                "organization_id": org_id,
                "agent_id": "general",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        channel_id = created.json()["channel"]["id"]

        listed = owner.get("/api/v2/orgs/{}/channels".format(org_id)).json()
        self.assertNotIn(channel_id, [item["id"] for item in listed["items"]])

        blocked = owner.put(
            "/api/v2/orgs/{}/channels/{}".format(org_id, channel_id),
            json={
                "type": "wechat_ilink",
                "agent_id": "general",
                "enabled": True,
                "settings": {"group_policy": "private_only"},
            },
        )
        self.assertEqual(blocked.status_code, 400, blocked.text)
        blocked_delete = owner.delete(
            "/api/v2/orgs/{}/channels/{}".format(org_id, channel_id)
        )
        self.assertEqual(blocked_delete.status_code, 400, blocked_delete.text)

    def test_status_agent_and_options(self):
        org_id, owner = self._create_owner("ops")
        created = owner.post(
            "/api/connections",
            json={
                "platform": "wechat",
                "organization_id": org_id,
                "agent_id": "general",
            },
        )
        connection_id = created.json()["connection_id"]

        disabled = owner.patch(
            "/api/connections/{}/status".format(connection_id),
            json={"enabled": False},
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertFalse(disabled.json()["enabled"])

        bad_agent = owner.put(
            "/api/connections/{}/agent".format(connection_id),
            json={"agent_id": "ghost"},
        )
        self.assertEqual(bad_agent.status_code, 400, bad_agent.text)
        good_agent = owner.put(
            "/api/connections/{}/agent".format(connection_id),
            json={"agent_id": "general"},
        )
        self.assertEqual(good_agent.status_code, 200, good_agent.text)

        options = owner.get("/api/connections/options").json()
        self.assertIn(
            org_id, [item["organization_id"] for item in options["organizations"]]
        )

    def test_org_wecom_channel_credentials_are_verified(self):
        from src.core.integrations.wecom_verify import WeComVerifyError

        org_id, owner = self._create_owner("org-wecom")
        created = owner.put(
            "/api/v2/orgs/{}/channels/wecom_1".format(org_id),
            json={
                "type": "wecom_aibot",
                "agent_id": "general",
                "enabled": True,
                "settings": {"group_policy": "private_only"},
            },
        )
        self.assertEqual(created.status_code, 200, created.text)

        with mock.patch("src.api.routers.v2.verify_wecom_credentials") as verify:
            saved = owner.put(
                "/api/v2/orgs/{}/channels/wecom_1/credentials".format(org_id),
                json={"credentials": {"bot_id": "bot-1", "secret": "secret-1"}},
            )
        self.assertEqual(saved.status_code, 200, saved.text)
        verify.assert_called_once_with("bot-1", "secret-1")

        with mock.patch("src.api.routers.v2.verify_wecom_credentials") as verify:
            saved_again = owner.put(
                "/api/v2/orgs/{}/channels/wecom_1/credentials".format(org_id),
                json={"credentials": {"bot_id": "bot-1", "secret": "secret-1"}},
            )
        self.assertEqual(saved_again.status_code, 200, saved_again.text)
        verify.assert_not_called()

        with mock.patch(
            "src.api.routers.v2.verify_wecom_credentials",
            side_effect=WeComVerifyError("握手失败"),
        ):
            rejected = owner.put(
                "/api/v2/orgs/{}/channels/wecom_1/credentials".format(org_id),
                json={"credentials": {"bot_id": "bot-x", "secret": "secret-x"}},
            )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertIn("企业微信凭证校验失败", rejected.text)

    def test_org_wechat_channel_qr_login_flow(self):
        org_id, owner = self._create_owner("org-wechat")
        created = owner.put(
            "/api/v2/orgs/{}/channels/wx_1".format(org_id),
            json={
                "type": "wechat_ilink",
                "agent_id": "general",
                "enabled": True,
                "settings": {"group_policy": "private_only"},
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        channel_id = created.json()["id"]

        class _FakeCredentials:
            bot_id = "wx-org-bot"

            def to_dict(self):
                return {
                    "token": "tok",
                    "base_url": "https://example.com",
                    "bot_id": self.bot_id,
                    "user_id": "wxid_org",
                }

        class _FakeClient:
            def login(self, show_qr, status_changed=None):
                return _FakeCredentials()

            def close(self):
                pass

        from src.core.services.connections import PersonalConnectionService

        service = PersonalConnectionService(
            self.app.state.organization_store,
            self.app.state.organization_control_store,
            self.app.state.credential_service,
        )

        owner_me = owner.get("/api/auth/me").json()
        owner_user_id = int(owner_me["user"]["user_id"])

        holder = {}
        manager = WeChatLoginManager(
            client_factory=_FakeClient,
            credentials_saver=lambda creds, _path: holder.update(pending=creds.to_dict()),
            connected_checker=lambda: False,
        )
        manager.pending_holder = holder
        self.app.state.wechat_login_managers["org-channel:{}".format(channel_id)] = manager

        status = owner.get(
            "/api/v2/orgs/{}/channels/{}/wechat/status".format(org_id, channel_id)
        )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["state"], "idle")

        started = owner.post(
            "/api/v2/orgs/{}/channels/{}/wechat/login".format(org_id, channel_id)
        )
        self.assertEqual(started.status_code, 200, started.text)
        for _ in range(100):
            state = owner.get(
                "/api/v2/orgs/{}/channels/{}/wechat/status".format(org_id, channel_id)
            ).json()
            if state["state"] in {"success", "failed"}:
                break
        self.assertEqual(state["state"], "success", state)
        # Credentials are staged; the robot is not active until confirmed.
        channel = owner.get("/api/v2/orgs/{}/channels".format(org_id)).json()
        wechat = next(item for item in channel["items"] if item["id"] == channel_id)
        self.assertFalse(wechat["credential_configured"])

        confirm = owner.post(
            "/api/v2/orgs/{}/channels/{}/wechat/confirm".format(org_id, channel_id)
        )
        self.assertEqual(confirm.status_code, 200, confirm.text)
        channel = owner.get("/api/v2/orgs/{}/channels".format(org_id)).json()
        wechat = next(item for item in channel["items"] if item["id"] == channel_id)
        self.assertTrue(wechat["credential_configured"])

        holder["pending"] = None
        rejected = owner.post(
            "/api/v2/orgs/{}/channels/{}/wechat/confirm".format(org_id, channel_id)
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)

    def test_admin_options_include_all_organizations(self):
        org_a, _owner_a = self._create_owner("admin-opt-a")
        org_b, _owner_b = self._create_owner("admin-opt-b")
        admin = self._login("root")
        options = admin.get("/api/connections/options").json()
        org_ids = [item["organization_id"] for item in options["organizations"]]
        self.assertIn(org_a, org_ids)
        self.assertIn(org_b, org_ids)
