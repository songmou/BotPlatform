"""Personal channel connection service tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.core.services.connections import (
    PersonalConnectionError,
    PersonalConnectionService,
)
from src.core.services.credentials import CredentialError

from tests._web_api_base import WebApiTestBase


class PersonalConnectionServiceTest(WebApiTestBase):
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
                "username": "conn_" + suffix,
                "password": "password-" + suffix,
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        login = owner.post(
            "/api/auth/login",
            json={"username": "conn_" + suffix, "password": "password-" + suffix},
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
                "username": "pc_" + suffix,
                "password": "password-" + suffix,
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        login = member.post(
            "/api/auth/login",
            json={"username": "pc_" + suffix, "password": "password-" + suffix},
        )
        self.assertEqual(login.status_code, 200, login.text)
        return member

    def _service(self) -> PersonalConnectionService:
        state = self.app.state
        return PersonalConnectionService(
            state.organization_store,
            state.organization_control_store,
            state.credential_service,
        )

    def _user_id(self, client: TestClient) -> int:
        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200, me.text)
        return int(me.json()["user"]["user_id"])

    def test_create_list_and_delete_wechat_connection(self):
        service = self._service()
        org_id, owner = self._create_owner("wechat")
        user_id = self._user_id(owner)

        created = service.create(
            user_id=user_id,
            organization_id=org_id,
            platform="wechat",
            agent_id="general",
        )
        self.assertEqual(created["platform"], "wechat")
        self.assertEqual(created["channel"]["type"], "wechat_ilink")
        self.assertEqual(created["agent_id"], "general")
        self.assertTrue(created["enabled"])
        self.assertFalse(created["credential_configured"])
        channel_id = created["channel"]["id"]
        self.assertTrue(channel_id.startswith("pc-wechat-u{}-".format(user_id)))

        listed = service.list_for_user(user_id)
        self.assertEqual([item["connection_id"] for item in listed],
                         [created["connection_id"]])

        service.delete(created["connection_id"], user_id)
        self.assertEqual(service.list_for_user(user_id), [])
        channels = self.app.state.organization_control_store.list_channels(org_id)
        self.assertNotIn(channel_id, [item["id"] for item in channels])

    def test_wecom_credentials_are_personal_and_runtime_readable(self):
        service = self._service()
        org_id, owner = self._create_owner("wecom")
        user_id = self._user_id(owner)
        other = self._invite_member(owner, org_id, "wecom-other")
        other_id = self._user_id(other)

        created = service.create(
            user_id=user_id,
            organization_id=org_id,
            platform="wecom",
            agent_id="general",
        )
        service.put_wecom_credentials(
            created["connection_id"], user_id, "bot-123", "secret-abc"
        )
        detail = service.get(created["connection_id"], user_id)
        self.assertTrue(detail["credential_configured"])
        self.assertEqual(detail["bot_account_id"], "bot-123")
        self.assertEqual(
            service.current_wecom_secret(detail),
            {"bot_id": "bot-123", "secret": "secret-abc"},
        )

        # Runtime consumption path (bootstrap) reads the personal secret.
        raw = self.app.state.credential_service.secret_for_resource(
            org_id, "channels", detail["channel_instance_id"]
        )
        self.assertIn("secret-abc", raw)

        # The personal credential is invisible to other members.
        visible = self.app.state.credential_service.list_for_user(org_id, other_id)
        self.assertEqual(visible, [])

        # Non-members cannot operate on the connection.
        with self.assertRaises(PersonalConnectionError):
            service.get(created["connection_id"], other_id)
        with self.assertRaises(PersonalConnectionError):
            service.delete(created["connection_id"], other_id)

    def test_save_wechat_credentials_records_bot_and_bumps_revision(self):
        service = self._service()
        org_id, owner = self._create_owner("wxlogin")
        user_id = self._user_id(owner)
        created = service.create(
            user_id=user_id,
            organization_id=org_id,
            platform="wechat",
            agent_id="general",
        )
        before = self.app.state.organization_control_store.runtime_revisions()
        service.save_wechat_credentials(
            created["connection_id"],
            {
                "token": "tok",
                "base_url": "https://example.com",
                "bot_id": "wxbot-1",
                "user_id": "wxid_user",
            },
        )
        detail = service.get(created["connection_id"], user_id)
        self.assertEqual(detail["bot_account_id"], "wxbot-1")
        self.assertTrue(detail["credential_configured"])
        after = self.app.state.organization_control_store.runtime_revisions()
        self.assertGreater(
            int(after.get(org_id, {}).get("channels_revision") or 0),
            int(before.get(org_id, {}).get("channels_revision") or 0),
        )

    def test_change_agent_and_enabled(self):
        service = self._service()
        org_id, owner = self._create_owner("toggle")
        user_id = self._user_id(owner)
        created = service.create(
            user_id=user_id,
            organization_id=org_id,
            platform="wecom",
            agent_id="general",
        )
        disabled = service.set_enabled(created["connection_id"], user_id, False)
        self.assertFalse(disabled["enabled"])
        with self.assertRaises(PersonalConnectionError):
            service.change_agent(created["connection_id"], user_id, "ghost_agent")

    def test_non_member_cannot_create_connection(self):
        service = self._service()
        org_id, owner = self._create_owner("guard")
        viewer_role = self.admin_roles.get_by_code("viewer")
        self.admin_users.create("pc_outsider", "password12345", viewer_role.role_id)
        outsider = self._login("pc_outsider")
        outsider_id = self._user_id(outsider)
        with self.assertRaises(Exception):
            service.create(
                user_id=outsider_id,
                organization_id=org_id,
                platform="wechat",
                agent_id="general",
            )
