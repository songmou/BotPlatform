from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.core.services.auth import AdminAuthService, AuthError, PermissionDenied
from src.core.storage.admin_users import (
    AdminRoleStore,
    AdminSessionStore,
    AdminStoreError,
    AdminUserStore,
    hash_password,
    verify_password,
)
from src.core.storage.database import Database


class PasswordHashTests(unittest.TestCase):
    def test_hash_and_verify_roundtrip(self):
        stored = hash_password("正确密码abc123")
        self.assertTrue(stored.startswith("pbkdf2$"))
        self.assertTrue(verify_password("正确密码abc123", stored))
        self.assertFalse(verify_password("错误密码", stored))

    def test_verify_rejects_malformed_hash(self):
        self.assertFalse(verify_password("x", "not-a-valid-hash"))
        self.assertFalse(verify_password("x", ""))


class AdminStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database = Database(Path(self._tmp.name) / "test.sqlite3")
        self.roles = AdminRoleStore(self.database)
        self.users = AdminUserStore(self.database)
        self.sessions = AdminSessionStore(self.database, b"secret")

    def test_builtin_roles_seeded(self):
        codes = {role.code for role in self.roles.list_roles()}
        self.assertEqual(codes, {"admin", "editor", "viewer", "tenant_user"})
        admin = self.roles.get_by_code("admin")
        self.assertTrue(admin.builtin)
        self.assertTrue(admin.allows("anything.at.all"))
        viewer = self.roles.get_by_code("viewer")
        self.assertTrue(viewer.allows("tenants.read"))
        self.assertFalse(viewer.allows("tenants.delete"))

    def test_admin_role_permissions_immutable(self):
        admin = self.roles.get_by_code("admin")
        with self.assertRaises(AdminStoreError):
            self.roles.update_permissions(admin.role_id, ["tenants.read"])

    def test_editor_role_permissions_editable(self):
        editor = self.roles.get_by_code("editor")
        updated = self.roles.update_permissions(editor.role_id, ["panel.read"])
        self.assertEqual(updated.permissions, ["panel.read"])

    def test_user_crud_and_duplicate(self):
        role = self.roles.get_by_code("viewer")
        user = self.users.create("alice", "password12345", role.role_id)
        self.assertEqual(user.username, "alice")
        with self.assertRaises(AdminStoreError):
            self.users.create("alice", "password12345", role.role_id)
        self.users.set_disabled(user.user_id, True)
        self.assertTrue(self.users.get_by_id(user.user_id).disabled)
        self.users.delete(user.user_id)
        self.assertIsNone(self.users.get_by_username("alice"))

    def test_session_lifecycle(self):
        role = self.roles.get_by_code("admin")
        user = self.users.create("root", "password12345", role.role_id)
        token, _ = self.sessions.create(user.user_id)
        self.assertEqual(self.sessions.resolve(token), user.user_id)
        self.assertIsNone(self.sessions.resolve("forged-token"))
        self.sessions.delete(token)
        self.assertIsNone(self.sessions.resolve(token))

    def test_expired_session_rejected(self):
        role = self.roles.get_by_code("admin")
        user = self.users.create("root", "password12345", role.role_id)
        token, _ = self.sessions.create(user.user_id, ttl_seconds=-1)
        self.assertIsNone(self.sessions.resolve(token))

    def test_password_reset_invalidates_sessions(self):
        role = self.roles.get_by_code("admin")
        user = self.users.create("root", "password12345", role.role_id)
        token, _ = self.sessions.create(user.user_id)
        self.users.set_password(user.user_id, "newpassword123")
        self.assertIsNone(self.sessions.resolve(token))
        self.assertTrue(
            verify_password("newpassword123", self.users.password_hash(user.user_id))
        )


class AdminAuthServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database = Database(Path(self._tmp.name) / "test.sqlite3")
        self.auth = AdminAuthService(
            AdminUserStore(self.database),
            AdminRoleStore(self.database),
            AdminSessionStore(self.database, b"secret"),
            Path(self._tmp.name),
        )

    def test_bootstrap_creates_admin_once(self):
        password = self.auth.bootstrap_default_admin()
        self.assertIsNotNone(password)
        self.assertTrue((Path(self._tmp.name) / "admin_initial_password").exists())
        self.assertIsNone(self.auth.bootstrap_default_admin())
        token, principal = self.auth.login("admin", password)
        self.assertEqual(principal.user.username, "admin")
        self.assertTrue(principal.allows("admins.manage"))
        self.assertIsNotNone(self.auth.identify(token))

    def test_login_failures(self):
        self.auth.bootstrap_default_admin()
        with self.assertRaises(AuthError):
            self.auth.login("admin", "wrong-password")
        with self.assertRaises(AuthError):
            self.auth.login("ghost", "whatever-password")

    def test_disabled_user_cannot_login_or_identify(self):
        password = self.auth.bootstrap_default_admin()
        token, principal = self.auth.login("admin", password)
        self.auth.users.set_disabled(principal.user.user_id, True)
        self.assertIsNone(self.auth.identify(token))
        with self.assertRaises(AuthError):
            self.auth.login("admin", password)

    def test_require_permission(self):
        password = self.auth.bootstrap_default_admin()
        _, principal = self.auth.login("admin", password)
        self.auth.require(principal, "tenants.delete")
        with self.assertRaises(PermissionDenied):
            self.auth.require(None, "tenants.read")

    def test_logout(self):
        password = self.auth.bootstrap_default_admin()
        token, _ = self.auth.login("admin", password)
        self.auth.logout(token)
        self.assertIsNone(self.auth.identify(token))


if __name__ == "__main__":
    unittest.main()
