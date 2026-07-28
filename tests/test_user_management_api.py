from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.core.modeling import ModelRouter
from src.core.services.auth import AdminAuthService
from src.core.storage.admin_users import (
    AdminRoleStore,
    AdminSessionStore,
    AdminUserStore,
)
from src.core.storage.tenants import ConversationStore, TenantRegistry

from tests.test_web_api import FakeClient, _make_config


class UserManagementApiTest(unittest.TestCase):
    def setUp(self):
        from src.api.app import create_app

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        data_root = Path(self._tmp.name)

        self.registry = TenantRegistry(data_root)
        self.conversation_store = ConversationStore(self.registry, max_messages=20)

        database = self.registry.database
        self.admin_users = AdminUserStore(database)
        self.admin_roles = AdminRoleStore(database)
        self.admin_sessions = AdminSessionStore(database, b"test-secret")
        self.admin_auth = AdminAuthService(
            self.admin_users, self.admin_roles, self.admin_sessions, data_root
        )

        admin_role = self.admin_roles.get_by_code("admin")
        viewer_role = self.admin_roles.get_by_code("viewer")
        self.admin_users.create("root", "password12345", admin_role.role_id)
        self.admin_users.create("watcher", "password12345", viewer_role.role_id)

        self.app = create_app(
            _make_config(),
            ModelRouter.single(FakeClient()),
            self.registry,
            self.conversation_store,
            admin_auth=self.admin_auth,
            admin_user_store=self.admin_users,
            admin_role_store=self.admin_roles,
        )
        self.client = self._login("root")
        self.viewer_client = self._login("watcher")

    def _login(self, username: str, password: str = "password12345") -> TestClient:
        client = TestClient(self.app)
        response = client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        assert response.status_code == 200, response.text
        return client

    def _make_tenant(self, user_id: str = "wxid_demo"):
        context = self.registry.resolve("ilink", user_id)
        self.conversation_store.append_transcript(context.tenant_id, "user", "你好")
        self.conversation_store.append_transcript(context.tenant_id, "assistant", "你好！")
        return context

    # ---- tenants ----

    def test_tenant_list_detail_and_delete(self):
        context = self._make_tenant()
        response = self.client.get("/api/tenants")
        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tenant_id"], context.tenant_id)
        self.assertEqual(items[0]["message_count"], 2)

        response = self.client.get("/api/tenants/" + context.tenant_id)
        self.assertEqual(response.status_code, 200)
        detail = response.json()
        self.assertEqual(len(detail["recent_events"]), 2)

        response = self.client.delete("/api/tenants/" + context.tenant_id)
        self.assertEqual(response.status_code, 200)
        response = self.client.get("/api/tenants")
        self.assertEqual(response.json(), [])

    def test_tenant_detail_not_found(self):
        response = self.client.get(
            "/api/tenants/00000000-0000-0000-0000-000000000009"
        )
        self.assertEqual(response.status_code, 404)

    def test_viewer_can_read_but_not_delete_tenant(self):
        context = self._make_tenant()
        response = self.viewer_client.get("/api/tenants")
        self.assertEqual(response.status_code, 200)
        response = self.viewer_client.delete("/api/tenants/" + context.tenant_id)
        self.assertEqual(response.status_code, 403)

    # ---- admins ----

    def test_viewer_cannot_manage_admins(self):
        response = self.viewer_client.get("/api/admins")
        self.assertEqual(response.status_code, 403)

    def test_admin_crud(self):
        editor_role = self.admin_roles.get_by_code("editor")
        response = self.client.post(
            "/api/admins",
            json={"username": "bob", "role_id": editor_role.role_id},
        )
        self.assertEqual(response.status_code, 201)
        user_id = response.json()["user_id"]

        response = self.client.post(
            "/api/admins",
            json={"username": "bob", "role_id": editor_role.role_id},
        )
        self.assertEqual(response.status_code, 409)

        response = self.client.post(
            "/api/admins",
            json={"username": "x", "role_id": editor_role.role_id},
        )
        self.assertEqual(response.status_code, 400)

        # New accounts get the fixed initial password 12345.
        self._login("bob", "12345")

        viewer_role = self.admin_roles.get_by_code("viewer")
        response = self.client.put(
            "/api/admins/{}".format(user_id), json={"role_id": viewer_role.role_id}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"]["code"], "viewer")

        response = self.client.post("/api/admins/{}/password".format(user_id))
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.json()["new_password"]), 10)

        response = self.client.delete("/api/admins/{}".format(user_id))
        self.assertEqual(response.status_code, 200)

    def test_cannot_delete_self_or_last_admin(self):
        root = self.admin_users.get_by_username("root")
        response = self.client.delete("/api/admins/{}".format(root.user_id))
        self.assertEqual(response.status_code, 400)

        viewer_role = self.admin_roles.get_by_code("viewer")
        response = self.client.put(
            "/api/admins/{}".format(root.user_id),
            json={"role_id": viewer_role.role_id},
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.put(
            "/api/admins/{}".format(root.user_id), json={"disabled": True}
        )
        self.assertEqual(response.status_code, 400)

    # ---- roles ----

    def test_role_listing_and_update(self):
        response = self.client.get("/api/admins/roles")
        self.assertEqual(response.status_code, 200)
        roles = {role["code"]: role for role in response.json()}
        self.assertEqual(set(roles), {"admin", "editor", "viewer"})

        editor = roles["editor"]
        response = self.client.put(
            "/api/admins/roles/{}".format(editor["role_id"]),
            json={"permissions": ["tenants.read", "panel.read"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["permissions"], ["panel.read", "tenants.read"]
        )

        response = self.client.put(
            "/api/admins/roles/{}".format(editor["role_id"]),
            json={"permissions": ["made.up.permission"]},
        )
        self.assertEqual(response.status_code, 400)

        admin = roles["admin"]
        response = self.client.put(
            "/api/admins/roles/{}".format(admin["role_id"]),
            json={"permissions": ["tenants.read"]},
        )
        self.assertEqual(response.status_code, 400)

    def test_users_page_renders(self):
        response = self.client.get("/users")
        self.assertEqual(response.status_code, 200)
        self.assertIn("用户管理", response.text)


if __name__ == "__main__":
    unittest.main()
