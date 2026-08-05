"""Behavior tests for ScriptService.integration_status.

Scripts backed by a platform integration (ctsehr/ctsoa/autogen) receive their
account/password from the per-tenant integration store and the Keychain. The
method reports whether those credentials are configured for a tenant.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.core.integrations.keychain import KeychainReference, KeychainService
from src.core.services.notification import TenantRecipientStore
from src.core.services.script import ScriptService
from src.core.storage.tenants import IntegrationStore, TenantRegistry


class ScriptIntegrationStatusTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.registry = TenantRegistry(root)
        self.recipient_store = TenantRecipientStore(self.registry)
        self.integration_store = IntegrationStore(self.registry)
        self.keychain = KeychainService(storage_path=root / "keychain.json")
        self.service = ScriptService(
            definitions={},
            credentials=None,
            recipient_store=self.recipient_store,
            project_root=root,
            tenant_registry=self.registry,
            integration_store=self.integration_store,
            keychain_service=self.keychain,
        )
        self.tenant = self.registry.resolve("ilink", "wxid_demo")

    def test_no_integration_returns_none(self):
        self.assertIsNone(self.service.integration_status(self.tenant.tenant_id, "demo"))

    def test_ready_when_account_and_secret_present(self):
        self.integration_store.set(
            self.tenant.tenant_id,
            "ctsehr",
            {"account": "alice", "keychain_service": "ctsehr", "keychain_account": "cred"},
        )
        self.keychain.set_secret(KeychainReference("ctsehr", "cred"), "s3cr3t")
        status = self.service.integration_status(self.tenant.tenant_id, "ctsehr_check")
        self.assertEqual(status["integration_id"], "ctsehr")
        self.assertTrue(status["account_set"])
        self.assertTrue(status["keychain_secret_set"])
        self.assertTrue(status["ready"])

    def test_not_ready_without_keychain_secret(self):
        self.integration_store.set(
            self.tenant.tenant_id,
            "ctsehr",
            {"account": "alice", "keychain_service": "ctsehr", "keychain_account": "cred"},
        )
        status = self.service.integration_status(self.tenant.tenant_id, "ctsehr_check")
        self.assertTrue(status["account_set"])
        self.assertFalse(status["keychain_secret_set"])
        self.assertFalse(status["ready"])

    def test_no_tenant_omits_secret_check(self):
        status = self.service.integration_status(None, "ctsehr_check")
        self.assertEqual(status["integration_id"], "ctsehr")
        self.assertFalse(status["account_set"])
        self.assertFalse(status["keychain_secret_set"])
        self.assertFalse(status["ready"])


if __name__ == "__main__":
    unittest.main()
