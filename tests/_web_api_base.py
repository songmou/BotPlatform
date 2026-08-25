"""Shared base class for web panel integration tests.

Not collected by unittest discovery (no ``test`` prefix); imported by the
``test_web_*_api`` modules to build a panel app backed by a temporary
tenant registry and real admin auth stores.
"""

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


class WebApiTestBase(unittest.TestCase):
    """Build the FastAPI panel with real stores rooted in a temp directory."""

    def setUp(self):
        from src.api.app import create_app

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_root = Path(self._tmp.name)

        self.registry = TenantRegistry(self.data_root)
        self.conversation_store = ConversationStore(self.registry, max_messages=20)

        database = self.registry.database
        self.admin_users = AdminUserStore(database)
        self.admin_roles = AdminRoleStore(database)
        self.admin_sessions = AdminSessionStore(database, b"test-secret")
        self.admin_auth = AdminAuthService(
            self.admin_users, self.admin_roles, self.admin_sessions, self.data_root
        )
        admin_role = self.admin_roles.get_by_code("admin")
        viewer_role = self.admin_roles.get_by_code("viewer")
        self.admin_users.create("root", "password12345", admin_role.role_id)
        self.admin_users.create("watcher", "password12345", viewer_role.role_id)

        self.config = _make_config()
        self.fake_client = FakeClient()
        self.model_router = ModelRouter.single(self.fake_client)

        self.app = create_app(
            self.config,
            self.model_router,
            self.registry,
            self.conversation_store,
            admin_auth=self.admin_auth,
            admin_user_store=self.admin_users,
            admin_role_store=self.admin_roles,
            **self.app_kwargs(),
        )
        # Retired endpoint modules remain unit-testable, but normal app
        # construction never enables their mutating routes.
        self.app.state.allow_legacy_config_writes = True
        self.client = self._login("root")
        self.viewer_client = self._login("watcher")

    def app_kwargs(self) -> dict:
        """Extra keyword arguments forwarded to create_app; override in tests."""
        return {}

    def _login(self, username: str, password: str = "password12345") -> TestClient:
        client = TestClient(self.app)
        response = client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        assert response.status_code == 200, response.text
        return client

    def _make_tenant(self, user_id: str = "wxid_demo"):
        return self.registry.resolve("ilink", user_id)
