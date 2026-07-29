from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.application.services import build_core_services
from src.core.modeling import ModelError

from tests.test_web_api import FakeClient, _make_config

FACTORY_TARGET = "src.core.application.services.create_model_client"


class BuildCoreServicesTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.data_dir = Path(self._dir.name)
        self.config = _make_config()

    def _config_with_broken_primary(self):
        """Add a second profile that the fake factory refuses to build."""
        broken = dataclasses.replace(self.config.models["test_model"], id="broken_model")
        models = dict(self.config.models)
        models["broken_model"] = broken
        app = dataclasses.replace(
            self.config.app, active_model="broken_model", fallback_model="broken_model"
        )
        return dataclasses.replace(self.config, app=app, models=models)

    @staticmethod
    def _fake_factory(created):
        def factory(profile, logger=None):
            if profile.id == "broken_model":
                raise ModelError("boom")
            client = FakeClient(profile.id, profile.provider, profile.model)
            created.append(client)
            return client

        return factory

    def test_strict_build_wires_graph_and_close_releases_clients(self):
        created = []
        with patch(FACTORY_TARGET, self._fake_factory(created)):
            services = build_core_services(self.config, self.data_dir)
        self.assertEqual(services.model_router.primary_profile_id, "test_model")
        self.assertEqual(services.model_warnings, [])
        self.assertIsNone(services.embedding_client)
        self.assertEqual(
            services.tenant_registry.data_root, self.data_dir.resolve()
        )
        services.close()
        self.assertTrue(all(client.closed for client in created))

    def test_strict_mode_aborts_and_cleans_up_on_broken_profile(self):
        config = self._config_with_broken_primary()
        created = []
        with patch(FACTORY_TARGET, self._fake_factory(created)):
            with self.assertRaises(ModelError):
                build_core_services(config, self.data_dir)
        self.assertTrue(all(client.closed for client in created))

    def test_non_strict_skips_broken_profile_with_warning(self):
        config = self._config_with_broken_primary()
        created = []
        with patch(FACTORY_TARGET, self._fake_factory(created)):
            services = build_core_services(config, self.data_dir, strict_models=False)
        # The broken primary falls back to the first working client.
        self.assertEqual(services.model_router.primary_profile_id, "test_model")
        self.assertEqual(services.model_router.fallback_profile_id, "test_model")
        self.assertEqual(len(services.model_warnings), 2)
        self.assertIn("broken_model", services.model_warnings[0])
        services.close()

    def test_non_strict_without_any_usable_model_raises(self):
        config = self._config_with_broken_primary()
        models = {"broken_model": config.models["broken_model"]}
        config = dataclasses.replace(config, models=models)
        with patch(FACTORY_TARGET, self._fake_factory([])):
            with self.assertRaisesRegex(ModelError, "没有可用的模型档案"):
                build_core_services(config, self.data_dir, strict_models=False)


if __name__ == "__main__":
    unittest.main()
