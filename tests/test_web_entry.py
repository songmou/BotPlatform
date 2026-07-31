"""Tests for the combined bot + web panel entry wiring."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.core.modeling import ModelRouter

from tests.test_web_api import FakeClient, _make_config


class OwnsServicesLifespanTest(unittest.TestCase):
    """The lifespan hook must only close services the panel actually owns."""

    def _build_app(self, owns_services):
        from src.api.app import create_app

        scheduler = MagicMock()
        script_service = MagicMock()
        tool_runtime = MagicMock()
        app = create_app(
            _make_config(),
            ModelRouter.single(FakeClient()),
            MagicMock(),
            MagicMock(),
            tool_runtime=tool_runtime,
            scheduler=scheduler,
            script_service=script_service,
            owns_services=owns_services,
        )
        return app, scheduler, script_service, tool_runtime

    def test_shared_services_survive_lifespan_shutdown(self):
        app, scheduler, script_service, tool_runtime = self._build_app(False)
        with TestClient(app):
            pass
        scheduler.shutdown.assert_not_called()
        script_service.shutdown.assert_not_called()
        tool_runtime.close.assert_not_called()

    def test_standalone_panel_still_owns_service_shutdown(self):
        app, scheduler, script_service, tool_runtime = self._build_app(True)
        with TestClient(app):
            pass
        scheduler.shutdown.assert_called_once()
        script_service.shutdown.assert_called_once()
        tool_runtime.close.assert_called_once()


class SharedRuntimeGraphTest(unittest.TestCase):
    """Combined mode must hand the exact bot runtime instances to the panel."""

    def test_web_app_reuses_bot_runtime_instances(self):
        from src.api.app import create_app
        from src.core.application.bootstrap import build_bot_runtime
        from src.core.application.services import CoreServices
        from src.core.services.drive import DriveService
        from src.core.services.knowledge import KnowledgeService
        from src.core.services.notification import TenantRecipientStore
        from src.core.storage.tenants import (
            ConversationStore,
            ScheduleStore,
            TenantRegistry,
        )

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        registry = TenantRegistry(Path(temporary.name) / "data")
        config = _make_config()
        services = CoreServices(
            clients={},
            model_router=ModelRouter.single(FakeClient()),
            tenant_registry=registry,
            conversation_store=ConversationStore(registry, 20),
            embedding_client=None,
            rerank_client=None,
            knowledge_service=KnowledgeService(registry, None),
            recipient_store=TenantRecipientStore(registry),
            schedule_store=ScheduleStore(registry),
            model_analytics_store=MagicMock(),
            drive_service=DriveService(
                registry, Path(temporary.name) / "data" / "public"
            ),
        )
        runtime = build_bot_runtime(config, services)
        self.addCleanup(runtime.shutdown)

        app = create_app(
            config,
            services.model_router,
            registry,
            services.conversation_store,
            tool_runtime=runtime.tool_runtime,
            knowledge_service=services.knowledge_service,
            plugin_context=runtime.plugin_context,
            scheduler=runtime.scheduler,
            script_service=runtime.script_service,
            script_registry=runtime.external_script_registry,
            script_schedule_service=runtime.script_schedule_service,
            owns_services=False,
        )
        self.assertIs(app.state.scheduler, runtime.scheduler)
        self.assertIs(app.state.script_service, runtime.script_service)
        self.assertIs(
            app.state.script_schedule_service, runtime.script_schedule_service
        )
        self.assertIs(app.state.model_router, services.model_router)
        self.assertFalse(app.state.owns_services)
        # This config disables tools, so the shared runtime has none either.
        self.assertIsNone(runtime.tool_runtime)


if __name__ == "__main__":
    unittest.main()
