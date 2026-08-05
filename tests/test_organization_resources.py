"""Organization identity, resource inheritance, and isolation tests."""

from __future__ import annotations

import threading
import unittest
import json
import sqlite3
from contextlib import closing
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.core.modeling import CanonicalMessage
from src.core.storage.organizations import OrganizationError
from src.core.tooling.runtime import ToolRuntime
from tests._web_api_base import WebApiTestBase


class OrganizationResourceApiTest(WebApiTestBase):
    def _publish_catalog(self, resource_type: str, resource_id: str, payload: dict):
        saved = self.client.put(
            "/api/v2/platform/catalog/{}/{}".format(
                resource_type, resource_id
            ),
            json={"payload": payload},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        return saved

    def _create_owner(self, suffix: str):
        created = self.client.post(
            "/api/v2/platform/organizations",
            json={"name": "组织 " + suffix},
        )
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        anonymous = TestClient(self.app)
        accepted = anonymous.post(
            "/api/v2/invitations/accept",
            json={
                "token": payload["owner_invitation_token"],
                "username": "owner_" + suffix,
                "password": "password-" + suffix,
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        login = anonymous.post(
            "/api/auth/login",
            json={
                "username": "owner_" + suffix,
                "password": "password-" + suffix,
            },
        )
        self.assertEqual(login.status_code, 200, login.text)
        return (
            payload["organization"]["organization_id"],
            anonymous,
        )

    def test_platform_catalog_mcp_headers_stored_in_keychain(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from src.core.config import mcp_headers as mh

        keychain_file = Path(tempfile.mkdtemp()) / "mcp_headers.json"
        with patch.object(mh, "MCP_HEADERS_FILE", keychain_file):
            saved = self.client.put(
                "/api/v2/platform/catalog/mcp/secret_svc",
                json={"payload": {
                    "id": "secret_svc",
                    "name": "带鉴权 MCP",
                    "transport": "streamablehttp",
                    "url": "https://mcp.example.com",
                    "headers": {"Authorization": "Bearer topsecret"},
                }},
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            # Secret must NOT be echoed back in the stored catalog payload.
            got = self.client.get("/api/v2/platform/catalog/mcp/secret_svc")
            self.assertEqual(got.status_code, 200, got.text)
            self.assertEqual(got.json()["payload"].get("headers"), {})
            # Secret lives in the keychain, merged back at read time.
            self.assertEqual(
                mh.load_headers("secret_svc"),
                {"Authorization": "Bearer topsecret"},
            )
            # Deleting the resource cleans up its keychain entry.
            deleted = self.client.delete(
                "/api/v2/platform/catalog/mcp/secret_svc"
            )
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertEqual(mh.load_headers("secret_svc"), {})

    def _invite_member(
        self, owner: TestClient, organization_id: str, suffix: str, role="member"
    ):
        invitation = owner.post(
            "/api/v2/orgs/{}/invitations".format(organization_id),
            json={"role": role},
        )
        self.assertEqual(invitation.status_code, 201, invitation.text)
        member = TestClient(self.app)
        accepted = member.post(
            "/api/v2/invitations/accept",
            json={
                "token": invitation.json()["invitation_token"],
                "username": "member_" + suffix,
                "password": "password-" + suffix,
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        login = member.post(
            "/api/auth/login",
            json={
                "username": "member_" + suffix,
                "password": "password-" + suffix,
            },
        )
        self.assertEqual(login.status_code, 200, login.text)
        return member

    def test_legacy_tenant_is_bootstrapped_as_unclaimed_organization(self):
        tenant = self._make_tenant("legacy-user")
        organization = self.app.state.organization_store.get(tenant.tenant_id)
        self.assertTrue(organization["legacy"])
        members = self.app.state.organization_store.list_members(tenant.tenant_id)
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["role"], "owner")
        self.assertIsNone(members[0]["user_id"])

    def test_platform_organization_management_lists_claim_state_and_members(self):
        legacy = self._make_tenant("legacy-platform-list")
        created = self.client.post(
            "/api/v2/platform/organizations", json={"name": "平台组织"}
        )
        self.assertEqual(created.status_code, 201, created.text)
        organization_id = created.json()["organization"]["organization_id"]

        organizations = self.client.get("/api/v2/platform/organizations")
        self.assertEqual(organizations.status_code, 200, organizations.text)
        items = {
            item["organization_id"]: item for item in organizations.json()["items"]
        }
        self.assertTrue(items[legacy.tenant_id]["legacy"])
        self.assertFalse(items[legacy.tenant_id]["legacy_claimed"])
        self.assertFalse(items[organization_id]["legacy"])
        self.assertIsNone(items[organization_id]["legacy_claimed"])

        members = self.client.get(
            "/api/v2/platform/organizations/{}/members".format(organization_id)
        )
        self.assertEqual(members.status_code, 200, members.text)
        self.assertEqual(members.json()["items"], [])

        owner = self._create_owner("platform-members")[1]
        owner_org_id = owner.get("/api/v2/me").json()["organizations"][0][
            "organization_id"
        ]
        owner_members = self.client.get(
            "/api/v2/platform/organizations/{}/members".format(owner_org_id)
        )
        self.assertEqual(owner_members.status_code, 200, owner_members.text)
        self.assertEqual(len(owner_members.json()["items"]), 1)
        self.assertEqual(
            self.viewer_client.get(
                "/api/v2/platform/organizations/{}/members".format(owner_org_id)
            ).status_code,
            403,
        )
        self.assertEqual(
            owner.get(
                "/api/v2/platform/organizations/{}/members".format(owner_org_id)
            ).status_code,
            403,
        )

    def test_platform_administrator_can_update_status_and_issue_legacy_claim(self):
        legacy = self._make_tenant("legacy-platform-claim")
        renamed = self.client.put(
            "/api/v2/platform/organizations/{}".format(legacy.tenant_id),
            json={"name": "已改名的个人空间"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["name"], "已改名的个人空间")
        paused = self.client.put(
            "/api/v2/platform/organizations/{}".format(legacy.tenant_id),
            json={"status": "suspended"},
        )
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertEqual(paused.json()["status"], "suspended")
        restored = self.client.put(
            "/api/v2/platform/organizations/{}".format(legacy.tenant_id),
            json={"status": "active"},
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        claim = self.client.post(
            "/api/v2/platform/organizations/{}/claim-token".format(
                legacy.tenant_id
            )
        )
        self.assertEqual(claim.status_code, 200, claim.text)
        self.assertTrue(claim.json()["claim_token"])
        self.assertEqual(
            self.viewer_client.put(
                "/api/v2/platform/organizations/{}".format(legacy.tenant_id),
                json={"status": "suspended"},
            ).status_code,
            403,
        )

    def test_owner_can_use_resources_but_cannot_cross_organization(self):
        org_a, owner_a = self._create_owner("a")
        org_b, _owner_b = self._create_owner("b")
        created = owner_a.put(
            "/api/v2/orgs/{}/agents/private_agent".format(org_a),
            json={"payload": {"name": "组织 A 助手"}},
        )
        self.assertEqual(created.status_code, 200, created.text)
        listed = owner_a.get(
            "/api/v2/orgs/{}/agents".format(org_a)
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        ids = {item["resource_id"] for item in listed.json()["items"]}
        self.assertIn("private_agent", ids)
        denied = owner_a.get(
            "/api/v2/orgs/{}/agents".format(org_b)
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(
            owner_a.get(
                "/api/v2/orgs/{}/resources/agents".format(org_a)
            ).status_code,
            404,
        )

    def test_public_agent_is_read_only_and_copy_is_a_snapshot(self):
        org_id, owner = self._create_owner("override")
        public = self._publish_catalog(
            "agents",
            "shared_helper",
            {
                "name": "公共助手",
                "description": "v1",
                "tools": [],
            },
        )
        override = owner.put(
            "/api/v2/orgs/{}/resources/agents/shared_helper/override".format(
                org_id
            ),
            json={
                "patch": {"name": "组织助手"},
                "list_modes": {"tools": "disable"},
            },
        )
        self.assertEqual(override.status_code, 404, override.text)
        copied = owner.post(
            "/api/v2/orgs/{}/agents/shared_helper/copy".format(org_id),
            json={"id": "shared_helper_copy", "name": "组织助手"},
        )
        self.assertEqual(copied.status_code, 201, copied.text)

        updated = self._publish_catalog(
            "agents",
            "shared_helper",
            {
                "name": "公共助手 2",
                "description": "v2",
                "tools": [],
            },
        )
        agents = owner.get(
            "/api/v2/orgs/{}/agents".format(org_id)
        ).json()["items"]
        by_id = {item["resource_id"]: item for item in agents}
        self.assertNotIn("shared_helper", by_id)
        self.assertEqual(by_id["shared_helper_copy"]["payload"]["description"], "v1")
        self.assertEqual(by_id["shared_helper_copy"]["base_resource_id"], "shared_helper")

    def test_organization_editor_options_expose_templates_and_schedule_dependencies(self):
        org_id, owner = self._create_owner("editor-options")
        published = self._publish_catalog(
            "agents",
            "editor_template",
            {
                "name": "编辑模板",
                "description": "可编辑模板",
                "system_prompt": "模板提示词",
                "tools": [],
                "plugin_tools": {},
                "skills": [],
                "mcp_servers": [],
            },
        )
        self.assertEqual(published.status_code, 200, published.text)
        agent_options = owner.get(
            "/api/v2/orgs/{}/agent-editor-options".format(org_id)
        )
        self.assertEqual(agent_options.status_code, 200, agent_options.text)
        templates = {
            item["id"]: item for item in agent_options.json()["templates"]
        }
        self.assertIn("editor_template", templates)
        self.assertEqual(
            templates["editor_template"]["payload"]["system_prompt"],
            "模板提示词",
        )
        self.assertNotIn("entrypoint", agent_options.text)

        schedule_options = owner.get(
            "/api/v2/orgs/{}/schedule-editor-options".format(org_id)
        )
        self.assertEqual(schedule_options.status_code, 200, schedule_options.text)
        self.assertIn("timezone", schedule_options.json())
        self.assertTrue(schedule_options.json()["agents"])
        self.assertIsInstance(schedule_options.json()["scripts"], list)

    def test_organization_agent_editor_payload_and_plugin_tool_validation(self):
        org_id, owner = self._create_owner("editor-save")
        published = self._publish_catalog(
            "agents",
            "editor_template_save",
            {
                "name": "保存模板",
                "system_prompt": "原始提示词",
                "tools": [],
            },
        )
        self.assertEqual(published.status_code, 200, published.text)
        created = owner.put(
            "/api/v2/orgs/{}/agents/edited_agent".format(org_id),
            json={
                "base_resource_id": "editor_template_save",
                "payload": {
                    "name": "组织编辑助手",
                    "system_prompt": "组织提示词",
                    "tools": [],
                    "plugin_tools": {},
                    "skills": [],
                    "mcp_servers": [],
                },
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["base_resource_id"], "editor_template_save")
        self.assertEqual(created.json()["payload"]["system_prompt"], "组织提示词")

        invalid_schedule = owner.put(
            "/api/v2/orgs/{}/schedules/bad-plugin-tool".format(org_id),
            json={
                "enabled": False,
                "crons": ["0 9 * * *"],
                "action": {
                    "type": "plugin",
                    "plugin_id": "todo",
                    "tool_name": "web_search_duckduckgo",
                    "parameters": {},
                },
            },
        )
        self.assertEqual(invalid_schedule.status_code, 400, invalid_schedule.text)

    def test_tenant_user_cannot_publish_public_resource(self):
        _org_id, owner = self._create_owner("publish")
        response = owner.put(
            "/api/v2/platform/catalog/agents/not_allowed",
            json={"payload": {"name": "越权"}},
        )
        self.assertEqual(response.status_code, 403)

    def test_tenant_platform_catalog_is_read_only_and_redacted(self):
        org_id, owner = self._create_owner("safe-catalog")
        published = self._publish_catalog(
            "scripts",
            "maintenance",
            {
                "id": "maintenance",
                "name": "维护脚本",
                "description": "清理临时文件",
                "entrypoint": "/private/platform/maintenance.py",
                "command": ["python", "maintenance.py"],
                "environment": {"MODE": "production"},
                "enabled": True,
            },
        )
        self.assertEqual(published.json()["activation_state"], "restart_required")
        catalog = owner.get("/api/v2/catalog/scripts")
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertNotIn("/private/platform", catalog.text)
        self.assertNotIn("environment", catalog.text)
        self.assertNotIn("production", catalog.text)
        self.assertFalse(
            any(
                value["resource_id"] == "maintenance"
                for value in catalog.json()["items"]
            )
        )
        full = owner.get("/api/v2/platform/catalog/scripts")
        self.assertEqual(full.status_code, 403)
        me = owner.get("/api/v2/me").json()
        self.assertNotIn("selected_organization_id", me)
        organization = next(
            item for item in me["organizations"]
            if item["organization_id"] == org_id
        )
        self.assertTrue(organization["permissions"]["collaborate"])
        self.assertTrue(organization["permissions"]["manage_sensitive"])

    def test_platform_governance_pages_are_admin_only(self):
        _org_id, owner = self._create_owner("governance-page")
        for path in (
            "/platform/organizations",
            "/platform/access",
            "/platform/analytics",
            "/platform/audit",
        ):
            self.assertEqual(owner.get(path).status_code, 403, path)
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_menus_are_server_rendered_by_role_and_org_urls_are_authorized(self):
        org_id, owner = self._create_owner("menu")
        other_org_id, _other_owner = self._create_owner("menu-other")

        organization_page = owner.get(
            "/organization/overview?organization_id={}".format(org_id)
        )
        self.assertEqual(organization_page.status_code, 200, organization_page.text)
        self.assertIn("组织工作台", organization_page.text)
        self.assertIn('id="organization-page-switch"', organization_page.text)
        self.assertNotIn('<div class="nav-section-label">平台管理</div>', organization_page.text)
        self.assertNotIn('href="/platform"', organization_page.text)

        forbidden = owner.get(
            "/organization/overview?organization_id={}".format(other_org_id)
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.text)

        platform_page = self.client.get("/platform")
        self.assertEqual(platform_page.status_code, 200, platform_page.text)
        self.assertIn('<div class="nav-section-label">平台管理</div>', platform_page.text)
        self.assertIn("组织工作台", platform_page.text)
        self.assertNotIn('id="organization-page-switch"', platform_page.text)

    def test_platform_admin_can_manage_platform_without_organization_membership(self):
        """Platform configuration must not depend on a selected organization."""
        initial = self.client.get("/api/v2/me")
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertNotIn("selected_organization_id", initial.json())
        self.assertNotIn("active_organization_id", initial.json())

        configured = self._publish_catalog(
            "agents",
            "platform_only",
            {"name": "平台助手", "tools": []},
        )
        self.assertEqual(configured.json()["activation_state"], "active")
        self.assertEqual(self.client.get("/platform/agent-templates").status_code, 200)
        self.assertEqual(self.client.get("/platform/analytics").status_code, 200)

    def test_organization_picker_is_url_scoped_and_not_persisted(self):
        first = self.client.post(
            "/api/v2/platform/organizations", json={"name": "组织一"}
        ).json()["organization"]["organization_id"]
        second = self.client.post(
            "/api/v2/platform/organizations", json={"name": "组织二"}
        ).json()["organization"]["organization_id"]
        selected = self.client.put(
            "/api/v2/me/active-organization", json={"organization_id": second}
        )
        self.assertEqual(selected.status_code, 404, selected.text)
        me = self.client.get("/api/v2/me").json()
        self.assertNotIn("selected_organization_id", me)
        page = self.client.get(
            "/organization/agents?organization_id={}".format(second)
        )
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('id="organization-page-switch"', page.text)
        self.assertNotEqual(first, second)

    def test_url_context_pages_and_platform_delegation_are_audited(self):
        org_id, owner = self._create_owner("context")
        switched = self.client.put(
            "/api/v2/me/context",
            json={"scope": "organization", "organization_id": org_id},
        )
        self.assertEqual(switched.status_code, 404, switched.text)
        me = self.client.get("/api/v2/me").json()
        delegated = next(
            item for item in me["organizations"]
            if item["organization_id"] == org_id
        )
        self.assertEqual(delegated["role"], "platform_delegation")
        page = self.client.get(
            "/organization/agents?organization_id={}".format(org_id)
        )
        self.assertIn('data-module="agents"', page.text)
        changed = self.client.put(
            "/api/v2/orgs/{}/agents/delegated".format(org_id),
            json={"payload": {"id": "delegated", "name": "代管助手", "tools": []}},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        audit = self.client.get(
            "/api/v2/orgs/{}/audit".format(org_id)
        ).json()["items"]
        self.assertTrue(any(item["source"] == "platform_delegation" for item in audit))
        self.assertEqual(
            owner.get(
                "/organization/agents?organization_id={}".format(org_id)
            ).status_code,
            200,
        )
        self.assertEqual(owner.get("/platform").status_code, 403)
        app_redirect = owner.get("/", follow_redirects=False)
        self.assertEqual(app_redirect.status_code, 302)
        self.assertEqual(app_redirect.headers["location"], "/organization/overview")

    def test_typed_channels_are_isolated_and_credentials_never_echo(self):
        org_a, owner_a = self._create_owner("channel-a")
        org_b, owner_b = self._create_owner("channel-b")
        body = {
            "type": "wecom_aibot",
            "agent_id": "general",
            "enabled": False,
            "settings": {"group_policy": "private_only"},
        }
        channel_a = owner_a.put(
            "/api/v2/orgs/{}/channels/main".format(org_a), json=body
        )
        channel_b = owner_b.put(
            "/api/v2/orgs/{}/channels/main".format(org_b), json=body
        )
        self.assertEqual(channel_a.status_code, 200, channel_a.text)
        self.assertEqual(channel_b.status_code, 200, channel_b.text)
        self.assertNotEqual(
            channel_a.json()["channel_instance_id"],
            channel_b.json()["channel_instance_id"],
        )
        saved = owner_a.put(
            "/api/v2/orgs/{}/channels/main/credentials".format(org_a),
            json={"credentials": {"bot_id": "bot", "secret": "never-echo"}},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotIn("never-echo", saved.text)
        listed = owner_a.get(
            "/api/v2/orgs/{}/channels".format(org_a)
        )
        self.assertTrue(listed.json()["items"][0]["credential_configured"])
        self.assertNotIn("never-echo", listed.text)

    def test_external_channel_user_is_fixed_to_channel_organization(self):
        from src.core.messaging import ChannelAddressStore, InboundMessage

        org_a, owner_a = self._create_owner("route-a")
        org_b, owner_b = self._create_owner("route-b")
        body = {
            "type": "wecom_aibot",
            "agent_id": "general",
            "enabled": True,
            "settings": {"group_policy": "private_only"},
        }
        first = owner_a.put(
            "/api/v2/orgs/{}/channels/main".format(org_a), json=body
        ).json()
        second = owner_b.put(
            "/api/v2/orgs/{}/channels/main".format(org_b), json=body
        ).json()
        store = ChannelAddressStore(self.registry)

        def message(channel_id, event_id):
            return InboundMessage(
                event_id=event_id,
                channel_id=channel_id,
                platform="wecom_aibot",
                account_id="bot",
                sender_id="external-same-user",
                conversation_type="direct",
                conversation_id="external-same-user",
                text="你好",
            )

        inbound_a = message(first["channel_instance_id"], "event-a")
        tenant_a = store.resolve(inbound_a)
        self.assertEqual(tenant_a.tenant_id, org_a)
        self.assertIsNotNone(tenant_a.personal_tenant_id)
        conversation_a = store.ensure_organization_conversation(
            inbound_a, tenant_a
        )
        store.record_endpoint(tenant_a, inbound_a)
        inbound_b = message(second["channel_instance_id"], "event-b")
        tenant_b = store.resolve(inbound_b)
        self.assertEqual(tenant_b.tenant_id, org_b)
        self.assertNotEqual(tenant_a.personal_tenant_id, tenant_b.personal_tenant_id)
        self.assertIsNotNone(conversation_a)
        self.assertEqual(
            len(self.app.state.organization_store.list_members(org_a)), 1
        )
        shared = owner_a.get(
            "/api/v2/orgs/{}/conversations".format(org_a)
        ).json()
        self.assertEqual(shared[0]["source"], "channel")

    def test_member_collaborates_but_sensitive_governance_stays_restricted(self):
        org_id, owner = self._create_owner("controls")
        member = self._invite_member(owner, org_id, "controls")
        created = owner.put(
            "/api/v2/orgs/{}/schedules/morning".format(org_id),
            json={
                "enabled": True,
                "crons": ["0 9 * * *"],
                "action": {"type": "text", "content": "早上好"},
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(
            member.get("/api/v2/orgs/{}/schedules".format(org_id)).status_code,
            200,
        )
        updated = member.patch(
            "/api/v2/orgs/{}/schedules/morning/status".format(org_id),
            json={"enabled": False},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        denied = member.post(
            "/api/v2/orgs/{}/invitations".format(org_id),
            json={"role": "member"},
        )
        self.assertEqual(denied.status_code, 403)

    def test_organization_schedule_skips_without_org_recipient(self):
        from unittest.mock import MagicMock

        from src.core.services.scheduler import SchedulerService
        from src.core.storage.tenants import ScheduleStore

        org_id, owner = self._create_owner("schedule-run")
        created = owner.put(
            "/api/v2/orgs/{}/schedules/morning".format(org_id),
            json={
                "enabled": True,
                "crons": ["0 9 * * *"],
                "action": {"type": "text", "content": "早上好"},
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        notification = MagicMock()
        notification.address_store.latest_endpoint.return_value = None
        scheduler = SchedulerService(
            tasks=[],
            tenant_registry=self.registry,
            schedule_store=ScheduleStore(self.registry),
            notification_service=notification,
            organization_control_store=self.app.state.organization_control_store,
        )
        self.assertFalse(scheduler.run_organization_schedule(org_id, "morning"))
        notification.enqueue_text_to_tenant.assert_not_called()
        runs = owner.get(
            "/api/v2/orgs/{}/schedule-runs".format(org_id)
        ).json()["items"]
        self.assertEqual(runs[0]["status"], "skipped")
        self.assertIn("没有有效", runs[0]["detail"])

    def test_member_can_trigger_schedule_run_now(self):
        # The "execute" button on the schedules page is gated by
        # canWriteOrganization() (collaborate permission), so a regular member
        # must be allowed to POST the run-now endpoint (no admin role required).
        org_id, owner = self._create_owner("schedule-run-member")
        owner.put(
            "/api/v2/orgs/{}/schedules/morning".format(org_id),
            json={
                "enabled": True,
                "crons": ["0 9 * * *"],
                "action": {"type": "text", "content": "早上好"},
            },
        )
        member = self._invite_member(owner, org_id, "runner")
        scheduler = MagicMock()
        scheduler.run_organization_schedule.return_value = True
        self.app.state.scheduler = scheduler
        triggered = member.post(
            "/api/v2/orgs/{}/schedules/morning/run".format(org_id)
        )
        self.assertEqual(triggered.status_code, 200, triggered.text)
        self.assertTrue(triggered.json()["ok"])

    def test_last_enabled_agent_is_preserved_and_default_moves(self):
        org_id, owner = self._create_owner("agents-invariant")
        last = owner.patch(
            "/api/v2/orgs/{}/agents/general/status".format(org_id),
            json={"enabled": False},
        )
        self.assertEqual(last.status_code, 400, last.text)
        created = owner.put(
            "/api/v2/orgs/{}/agents/secondary".format(org_id),
            json={"payload": {"id": "secondary", "name": "备用助手", "tools": []}},
        )
        self.assertEqual(created.status_code, 200, created.text)
        paused = owner.patch(
            "/api/v2/orgs/{}/agents/general/status".format(org_id),
            json={"enabled": False},
        )
        self.assertEqual(paused.status_code, 200, paused.text)
        agents = owner.get("/api/v2/orgs/{}/agents".format(org_id)).json()
        self.assertEqual(agents["default_agent_id"], "secondary")
        disabled_public = next(
            item for item in agents["items"] if item["resource_id"] == "general"
        )
        self.assertFalse(disabled_public["payload"]["enabled"])
        cannot_pause = owner.patch(
            "/api/v2/orgs/{}/agents/secondary/status".format(org_id),
            json={"enabled": False},
        )
        self.assertEqual(cannot_pause.status_code, 400, cannot_pause.text)

    def test_shared_organization_content_is_collaboratively_managed(self):
        from src.core.services.knowledge import KnowledgeService

        self.app.state.knowledge_service = KnowledgeService(
            self.registry, None, None
        )
        org_id, owner = self._create_owner("content")
        creator = self._invite_member(owner, org_id, "content-creator")
        other = self._invite_member(owner, org_id, "content-other")
        added = creator.post(
            "/api/v2/orgs/{}/knowledge/text".format(org_id),
            json={"name": "共享规则", "content": "组织知识"},
        )
        self.assertEqual(added.status_code, 200, added.text)
        source_id = added.json()["source_id"]
        deleted = other.delete(
            "/api/v2/orgs/{}/knowledge/sources/{}".format(org_id, source_id)
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        missing = creator.delete(
            "/api/v2/orgs/{}/knowledge/sources/{}".format(org_id, source_id)
        )
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_web_conversations_are_shared_but_lifecycle_is_restricted(self):
        org_id, owner = self._create_owner("chat")
        member = self._invite_member(owner, org_id, "chat")
        conversation = owner.post(
            "/api/v2/orgs/{}/conversations".format(org_id)
        )
        self.assertEqual(conversation.status_code, 201, conversation.text)
        conversation_id = conversation.json()["id"]
        visible = member.get(
            "/api/v2/orgs/{}/conversations".format(org_id)
        ).json()
        self.assertEqual([item["id"] for item in visible], [conversation_id])
        denied = member.delete(
            "/api/v2/orgs/{}/conversations/{}".format(
                org_id, conversation_id
            )
        )
        self.assertEqual(denied.status_code, 404)
        history = member.get(
            "/api/v2/orgs/{}/conversations/{}/history".format(
                org_id, conversation_id
            )
        )
        self.assertEqual(history.status_code, 200)

    def test_owner_transfer_and_member_exit_lifecycle(self):
        org_id, owner = self._create_owner("lifecycle")
        member = self._invite_member(owner, org_id, "lifecycle")
        member_user_id = member.get("/api/v2/me").json()["user"]["user_id"]
        forbidden = member.put(
            "/api/v2/orgs/{}/members/{}".format(org_id, member_user_id),
            json={"role": "owner"},
        )
        self.assertEqual(forbidden.status_code, 403)
        transferred = owner.put(
            "/api/v2/orgs/{}/ownership".format(org_id),
            json={"new_owner_user_id": member_user_id},
        )
        self.assertEqual(transferred.status_code, 200, transferred.text)
        self.assertEqual(transferred.json()["role"], "owner")
        left = owner.delete(
            "/api/v2/orgs/{}/members/{}".format(
                org_id, owner.get("/api/v2/me").json()["user"]["user_id"]
            )
        )
        self.assertEqual(left.status_code, 200, left.text)

    def test_resource_payload_rejects_secrets_and_local_mcp(self):
        org_id, owner = self._create_owner("secure-resource")
        secret = self.client.put(
            "/api/v2/platform/catalog/models/private",
            json={
                "payload": {
                    "name": "私有模型",
                    "base_url": "https://models.example.com",
                    "api_key": "should-not-be-stored",
                }
            },
        )
        self.assertEqual(secret.status_code, 400)
        removed = owner.put(
            "/api/v2/orgs/{}/resources/mcp/remote".format(org_id),
            json={
                "payload": {
                    "name": "远程 MCP",
                    "transport": "streamablehttp",
                    "url": "https://mcp.example.com",
                }
            },
        )
        self.assertEqual(removed.status_code, 404, removed.text)
        self.assertEqual(
            owner.put(
                "/api/v2/platform/catalog/mcp/remote",
                json={"payload": {"name": "越权 MCP"}},
            ).status_code,
            403,
        )

    def test_credentials_are_write_only_and_member_scoped(self):
        org_id, owner = self._create_owner("credential")
        member = self._invite_member(owner, org_id, "credential")
        channel = owner.put(
            "/api/v2/orgs/{}/channels/main".format(org_id),
            json={
                "type": "wecom_aibot",
                "agent_id": "general",
                "enabled": False,
                "settings": {"group_policy": "private_only"},
            },
        )
        self.assertEqual(channel.status_code, 200, channel.text)
        saved = owner.put(
            "/api/v2/orgs/{}/channels/main/credentials".format(org_id),
            json={
                "credentials": {
                    "bot_id": "credential-bot",
                    "secret": "top-secret-value",
                }
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["configured"])
        self.assertNotIn("secret", saved.text)
        visible = member.get(
            "/api/v2/orgs/{}/credentials".format(org_id)
        )
        self.assertEqual(visible.status_code, 200, visible.text)
        self.assertNotIn("top-secret-value", visible.text)

        personal = member.put(
            "/api/v2/orgs/{}/credentials/my_channel".format(org_id),
            json={
                "scope": "personal",
                "resource_type": "integrations",
                "resource_id": "my_channel",
                "secret": "member-only-secret",
            },
        )
        self.assertEqual(personal.status_code, 200, personal.text)
        owner_list = owner.get(
            "/api/v2/orgs/{}/credentials".format(org_id)
        )
        self.assertNotIn("my_channel", owner_list.text)
        denied = member.put(
            "/api/v2/orgs/{}/credentials/another_service".format(org_id),
            json={
                "scope": "organization",
                "resource_type": "mcp",
                "resource_id": "remote",
                "secret": "forbidden",
            },
        )
        self.assertEqual(denied.status_code, 400)

        other_org_id, other_owner = self._create_owner("credential-other")
        same_local_id = other_owner.put(
            "/api/v2/orgs/{}/credentials/model_service".format(
                other_org_id
            ),
            json={
                "scope": "organization",
                "resource_type": "plugins",
                "resource_id": "shared_service",
                "secret": '{"api_token":"other-organization-secret"}',
            },
        )
        self.assertEqual(same_local_id.status_code, 400, same_local_id.text)

    def test_organization_delete_backs_up_metadata_before_removing_secrets(self):
        org_id, owner = self._create_owner("delete-backup")
        channel = owner.put(
            "/api/v2/orgs/{}/channels/main".format(org_id),
            json={
                "type": "wecom_aibot",
                "agent_id": "general",
                "enabled": False,
                "settings": {"group_policy": "private_only"},
            },
        )
        self.assertEqual(channel.status_code, 200, channel.text)
        saved = owner.put(
            "/api/v2/orgs/{}/channels/main/credentials".format(org_id),
            json={"credentials": {"bot_id": "bot", "secret": "delete-after-backup-secret"}},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        legacy_keychain = (
            self.app.state.credential_service.legacy_integration_keychain
        )
        legacy_reference = legacy_keychain.reference(org_id, "ehr")
        legacy_keychain.set_secret(legacy_reference, "legacy-secret")
        from src.core.storage.tenants import IntegrationStore

        IntegrationStore(self.registry).set(
            org_id, "ehr", {"username": "demo"}
        )

        deleted = owner.delete(
            "/api/v2/orgs/{}".format(org_id),
            headers={"x-request-id": "delete-organization-audit"},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        backup_root = (
            self.registry.system_root
            / "organization_backups"
            / deleted.json()["backup_id"]
        )
        self.assertTrue((backup_root / "manifest.json").is_file())
        database_path = backup_root / "botplatform.sqlite3"
        with closing(sqlite3.connect(str(database_path))) as connection:
            row = connection.execute(
                "SELECT organization_id, resource_id "
                "FROM credential_metadata WHERE resource_type='channels'"
            ).fetchone()
        self.assertEqual(row, (org_id, channel.json()["channel_instance_id"]))
        self.assertNotIn(
            b"delete-after-backup-secret", database_path.read_bytes()
        )
        credential_path = (
            self.registry.system_root / "organization_credentials.json"
        )
        if credential_path.exists():
            self.assertNotIn(
                "delete-after-backup-secret",
                credential_path.read_text(encoding="utf-8"),
            )
        self.assertFalse(legacy_keychain.exists(legacy_reference))
        with self.assertRaises(OrganizationError):
            self.app.state.organization_store.get(org_id)
        audit = self.client.get("/api/v2/platform/audit").json()["items"]
        deletion_audit = next(
            item
            for item in audit
            if item["request_id"] == "delete-organization-audit"
        )
        self.assertEqual(deletion_audit["organization_id"], org_id)
        self.assertEqual(
            deletion_audit["resource"], "/api/v2/orgs/{}".format(org_id)
        )

    def test_mutating_v2_requests_are_audited_without_request_body(self):
        org_id, owner = self._create_owner("audit")
        changed = owner.put(
            "/api/v2/orgs/{}/agents/audited".format(org_id),
            headers={"x-request-id": "request-audit-1"},
            json={"payload": {"name": "审计助手"}},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        audit = owner.get(
            "/api/v2/orgs/{}/audit".format(org_id)
        )
        self.assertEqual(audit.status_code, 200, audit.text)
        item = next(
            value
            for value in audit.json()["items"]
            if value["request_id"] == "request-audit-1"
        )
        self.assertEqual(item["action"], "PUT")
        self.assertEqual(item["status_code"], 200)
        self.assertNotIn("审计助手", json.dumps(item, ensure_ascii=False))

    def test_organization_analytics_and_budget_are_tenant_scoped(self):
        from src.core.storage.model_analytics import ModelAnalyticsStore

        org_a, owner_a = self._create_owner("analytics-a")
        member_a = self._invite_member(owner_a, org_a, "analytics-a")
        org_b, owner_b = self._create_owner("analytics-b")
        self.app.state.model_analytics_store = ModelAnalyticsStore(
            self.registry, self.config
        )
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO model_runs("
                "run_id, tenant_id, source, status, started_at"
                ") VALUES ('run-a', ?, 'web', 'success', ?)",
                (org_a, "2026-07-31T00:00:00+00:00"),
            )

        detail = owner_a.get(
            "/api/v2/orgs/{}/analytics/runs/run-a".format(org_a)
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        hidden = owner_b.get(
            "/api/v2/orgs/{}/analytics/runs/run-a".format(org_b)
        )
        self.assertEqual(hidden.status_code, 404, hidden.text)
        forbidden = member_a.put(
            "/api/v2/orgs/{}/analytics/budget".format(org_a),
            json={"monthly_limit_micros": 1_000_000},
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.text)
        saved = owner_a.put(
            "/api/v2/orgs/{}/analytics/budget".format(org_a),
            json={"monthly_limit_micros": 1_000_000},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["scope_id"], org_a)

    def _make_channel(self, org_id, owner, suffix):
        channel = owner.put(
            "/api/v2/orgs/{}/channels/main".format(org_id),
            json={
                "type": "wecom_aibot",
                "agent_id": "general",
                "enabled": False,
                "settings": {"group_policy": "private_only"},
            },
        )
        self.assertEqual(channel.status_code, 200, channel.text)
        return channel.json()["channel_instance_id"]

    def _configure_channel_credentials(self, org_id, owner):
        saved = owner.put(
            "/api/v2/orgs/{}/channels/main/credentials".format(org_id),
            json={"credentials": {"bot_id": "bot", "secret": "test-secret" + org_id}},
        )
        self.assertEqual(saved.status_code, 200, saved.text)

    def test_channel_test_without_credentials_returns_400(self):
        org_id, owner = self._create_owner("channel-test-nocred")
        self._make_channel(org_id, owner, "channel-test-nocred")
        response = owner.post(
            "/api/v2/orgs/{}/channels/main/test".format(org_id)
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_channel_test_sends_message_to_latest_endpoint(self):
        from unittest.mock import MagicMock, patch

        from src.core.messaging.contracts import DeliveryEndpoint

        org_id, owner = self._create_owner("channel-test-send")
        channel_instance_id = self._make_channel(org_id, owner, "channel-test-send")
        self._configure_channel_credentials(org_id, owner)

        fake_adapter = MagicMock()
        endpoint = DeliveryEndpoint(
            channel_id=channel_instance_id,
            platform="wecom_aibot",
            account_id="bot",
            conversation_type="direct",
            conversation_id="user-1",
            recipient_id="user-1",
        )
        address_store = MagicMock()
        address_store.latest_endpoint.return_value = endpoint
        self.app.state.notification_service = MagicMock()
        self.app.state.notification_service.address_store = address_store

        with patch(
            "src.api.routers.v2.build_channel_adapter", return_value=fake_adapter
        ):
            response = owner.post(
                "/api/v2/orgs/{}/channels/main/test".format(org_id)
            )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["state"], "message_sent")
        self.assertIn("user-1", payload["detail"])
        fake_adapter.send.assert_called_once()
        sent_endpoint, sent_message = fake_adapter.send.call_args.args
        self.assertEqual(sent_endpoint.recipient_id, "user-1")
        self.assertIn("BotPlatform", sent_message.text)
        fake_adapter.close.assert_called_once()

    def test_channel_test_without_recipient_reports_valid_credentials(self):
        from unittest.mock import MagicMock, patch

        org_id, owner = self._create_owner("channel-test-norecip")
        self._make_channel(org_id, owner, "channel-test-norecip")
        self._configure_channel_credentials(org_id, owner)

        fake_adapter = MagicMock()
        address_store = MagicMock()
        address_store.latest_endpoint.return_value = None
        self.app.state.notification_service = MagicMock()
        self.app.state.notification_service.address_store = address_store

        with patch(
            "src.api.routers.v2.build_channel_adapter", return_value=fake_adapter
        ):
            response = owner.post(
                "/api/v2/orgs/{}/channels/main/test".format(org_id)
            )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["state"], "credentials_valid")
        self.assertIn("收件人", payload["detail"])
        fake_adapter.send.assert_not_called()
        fake_adapter.close.assert_called_once()


class ToolRuntimeThreadBindingTest(unittest.TestCase):
    def test_parallel_bindings_do_not_share_workspace(self):
        from pathlib import Path
        import tempfile

        from src.core.config.loader import ToolConfig
        from src.core.storage.tenants import TenantRegistry

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = TenantRegistry(root / "data")
            left = registry.resolve("bot", "left")
            right = registry.resolve("bot", "right")
            config = ToolConfig(
                enabled=True,
                default_working_directory=str(root),
                allowed_roots=[str(root)],
                denied_globs=[],
                approval_ttl_seconds=60,
                max_tool_rounds=4,
                max_total_tool_calls=8,
                max_read_bytes=1024,
                max_write_bytes=1024,
                max_directory_entries=20,
                max_search_results=20,
                max_command_output_bytes=1024,
                default_command_timeout_seconds=5,
                max_command_timeout_seconds=10,
                enabled_command_profiles=[],
            )
            runtime = ToolRuntime(
                config,
                "UTC",
                tenant_registry=registry,
                sandbox_available=False,
            )
            barrier = threading.Barrier(2)
            results = {}

            def bind(name, tenant):
                runtime.bind_tenant(tenant)
                barrier.wait()
                results[name] = (
                    runtime.tenant.tenant_id,
                    str(runtime.default_directory),
                )

            first = threading.Thread(target=bind, args=("left", left))
            second = threading.Thread(target=bind, args=("right", right))
            first.start()
            second.start()
            first.join()
            second.join()
            self.assertEqual(results["left"][0], left.tenant_id)
            self.assertEqual(results["right"][0], right.tenant_id)
            self.assertNotEqual(results["left"][1], results["right"][1])
