"""Organization identity, resource inheritance, and isolation tests."""

from __future__ import annotations

import threading
import unittest
import json
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

from src.core.modeling import CanonicalMessage
from src.core.storage.organizations import OrganizationError
from src.core.tooling.runtime import ToolRuntime
from tests._web_api_base import WebApiTestBase


class OrganizationResourceApiTest(WebApiTestBase):
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
        owner_org_id = owner.get("/api/v2/me").json()["active_organization_id"]
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
            "/api/v2/orgs/{}/resources/agents/private_agent".format(org_a),
            json={"payload": {"name": "组织 A 助手"}},
        )
        self.assertEqual(created.status_code, 200, created.text)
        listed = owner_a.get(
            "/api/v2/orgs/{}/resources/agents".format(org_a)
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        ids = {item["resource_id"] for item in listed.json()["items"]}
        self.assertIn("private_agent", ids)
        denied = owner_a.get(
            "/api/v2/orgs/{}/resources/agents".format(org_b)
        )
        self.assertEqual(denied.status_code, 403)

    def test_public_override_inherits_unmodified_fields_and_can_reset(self):
        org_id, owner = self._create_owner("override")
        public = self.client.put(
            "/api/v2/platform/catalog/agents/shared_helper",
            json={
                "payload": {
                    "name": "公共助手",
                    "description": "v1",
                    "tools": ["read_text_file"],
                }
            },
        )
        self.assertEqual(public.status_code, 200, public.text)
        override = owner.put(
            "/api/v2/orgs/{}/resources/agents/shared_helper/override".format(
                org_id
            ),
            json={
                "patch": {"name": "组织助手"},
                "list_modes": {"tools": "disable"},
            },
        )
        self.assertEqual(override.status_code, 200, override.text)
        self.assertEqual(override.json()["payload"]["name"], "组织助手")
        self.assertEqual(override.json()["payload"]["description"], "v1")
        self.assertEqual(override.json()["payload"]["tools"], [])

        updated = self.client.put(
            "/api/v2/platform/catalog/agents/shared_helper",
            json={
                "payload": {
                    "name": "公共助手 2",
                    "description": "v2",
                    "tools": ["read_text_file"],
                }
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        effective = owner.get(
            "/api/v2/orgs/{}/resources/agents/shared_helper".format(org_id)
        ).json()
        self.assertEqual(effective["payload"]["name"], "组织助手")
        self.assertEqual(effective["payload"]["description"], "v2")

        reset = owner.delete(
            "/api/v2/orgs/{}/resources/agents/shared_helper/override".format(
                org_id
            )
        )
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(reset.json()["payload"]["name"], "公共助手 2")

    def test_tenant_user_cannot_publish_public_resource(self):
        _org_id, owner = self._create_owner("publish")
        response = owner.put(
            "/api/v2/platform/catalog/agents/not_allowed",
            json={"payload": {"name": "越权"}},
        )
        self.assertEqual(response.status_code, 403)

    def test_web_conversations_are_private_to_member(self):
        org_id, owner = self._create_owner("chat")
        member = self._invite_member(owner, org_id, "chat")
        conversation = owner.post(
            "/api/v2/orgs/{}/conversations".format(org_id)
        )
        self.assertEqual(conversation.status_code, 201, conversation.text)
        conversation_id = conversation.json()["id"]
        self.assertEqual(
            member.get(
                "/api/v2/orgs/{}/conversations".format(org_id)
            ).json(),
            [],
        )
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
        self.assertEqual(history.status_code, 404)

    def test_legacy_web_conversation_migration_is_idempotent(self):
        path = self.data_root / "legacy-web.json"
        conversation_id = "legacy-conversation"
        legacy = self.registry.resolve("web", conversation_id)
        self.conversation_store.save_context(
            legacy.tenant_id,
            [
                CanonicalMessage("user", "旧问题")
            ],
        )
        path.write_text(
            json.dumps(
                {
                    "conversations": [
                        {
                            "id": conversation_id,
                            "title": "旧会话",
                            "created_at": "2026-01-01T00:00:00+00:00",
                            "updated_at": "2026-01-01T00:00:00+00:00",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        root = self.admin_users.get_by_username("root")
        store = self.app.state.organization_store
        self.assertEqual(
            store.migrate_legacy_web_conversations(
                path, root.user_id, root.username
            ),
            1,
        )
        self.assertEqual(
            store.migrate_legacy_web_conversations(
                path, root.user_id, root.username
            ),
            0,
        )
        conversation = store.get_conversation(root.user_id, conversation_id)
        messages = self.conversation_store.load_context(
            conversation["organization_id"],
            session_key="web:{}:{}".format(root.user_id, conversation_id),
            user_id=root.user_id,
        )
        self.assertEqual([item.content for item in messages], ["旧问题"])

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
        secret = owner.put(
            "/api/v2/orgs/{}/resources/models/private".format(org_id),
            json={
                "payload": {
                    "name": "私有模型",
                    "base_url": "https://models.example.com",
                    "api_key": "should-not-be-stored",
                }
            },
        )
        self.assertEqual(secret.status_code, 400)
        local_mcp = owner.put(
            "/api/v2/orgs/{}/resources/mcp/local".format(org_id),
            json={
                "payload": {
                    "name": "本机 MCP",
                    "transport": "stdio",
                    "command": "python",
                }
            },
        )
        self.assertEqual(local_mcp.status_code, 400)
        remote_mcp = owner.put(
            "/api/v2/orgs/{}/resources/mcp/remote".format(org_id),
            json={
                "payload": {
                    "name": "远程 MCP",
                    "transport": "streamablehttp",
                    "url": "https://mcp.example.com",
                }
            },
        )
        self.assertEqual(remote_mcp.status_code, 200, remote_mcp.text)

    def test_credentials_are_write_only_and_member_scoped(self):
        org_id, owner = self._create_owner("credential")
        member = self._invite_member(owner, org_id, "credential")
        saved = owner.put(
            "/api/v2/orgs/{}/credentials/model_service".format(org_id),
            json={
                "scope": "organization",
                "resource_type": "models",
                "resource_id": "private_model",
                "label": "模型服务密钥",
                "secret": "top-secret-value",
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
                "resource_type": "channels",
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
                "resource_type": "models",
                "resource_id": "private_model",
                "secret": "other-organization-secret",
            },
        )
        self.assertEqual(same_local_id.status_code, 200, same_local_id.text)
        self.assertEqual(
            same_local_id.json()["organization_id"], other_org_id
        )

    def test_organization_delete_backs_up_metadata_before_removing_secrets(self):
        org_id, owner = self._create_owner("delete-backup")
        saved = owner.put(
            "/api/v2/orgs/{}/credentials/model_service".format(org_id),
            json={
                "scope": "organization",
                "resource_type": "models",
                "resource_id": "private_model",
                "secret": "delete-after-backup-secret",
            },
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
                "FROM credential_metadata WHERE credential_id='model_service'"
            ).fetchone()
        self.assertEqual(row, (org_id, "private_model"))
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
            "/api/v2/orgs/{}/resources/agents/audited".format(org_id),
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
