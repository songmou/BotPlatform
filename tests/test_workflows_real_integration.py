"""Opt-in workflow smoke tests against explicitly configured real services."""

from __future__ import annotations

import os
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from src.core.datasource import DataSourceService
from src.core.integrations.ilink import Credentials
from src.core.config.loader import load_project_config
from src.core.modeling import ModelRouter
from src.core.modeling.factory import create_model_client
from src.core.services.notification import NotificationService, TenantRecipientStore
from src.core.services.script import ScriptService
from src.core.storage.tenants import IntegrationStore, TenantRegistry
from src.core.tooling import ToolRuntime
from src.core.workflows.runtime import WorkflowService


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.environ.get("BOTPLATFORM_WORKFLOW_REAL_INTEGRATION") == "1",
    "需要显式启用工作流真实依赖集成测试",
)
class WorkflowRealIntegrationTests(unittest.TestCase):
    @staticmethod
    def _service(**kwargs):
        organizations = MagicMock()
        organizations.database = MagicMock()
        return WorkflowService(organizations, **kwargs)

    def test_real_https_node(self):
        url = os.environ.get("BOTPLATFORM_WORKFLOW_TEST_HTTPS_URL", "").strip()
        if not url:
            self.skipTest("未配置 BOTPLATFORM_WORKFLOW_TEST_HTTPS_URL")
        outcome = self._service().execute_node_for_test(
            {"run_id": "real-https", "organization_id": "workflow-real", "test_mode": False},
            {"id": "https", "type": "http", "name": "真实 HTTPS"},
            {"method": "GET", "url": url, "timeout_seconds": 30},
        )
        self.assertIn("status_code", outcome.output)
        self.assertIn("body", outcome.output)

    def test_real_model_node(self):
        profile_id = os.environ.get("BOTPLATFORM_WORKFLOW_TEST_MODEL", "").strip()
        if not profile_id:
            self.skipTest("未配置 BOTPLATFORM_WORKFLOW_TEST_MODEL")
        config = load_project_config(ROOT / "config")
        profile = config.models.get(profile_id)
        if profile is None:
            self.fail("真实测试模型档案不存在：{}".format(profile_id))
        service = self._service(
            model_router=ModelRouter.single(create_model_client(profile))
        )
        outcome = service.execute_node_for_test(
            {"run_id": "real-model", "organization_id": "workflow-real", "initiated_by": None},
            {"id": "llm", "type": "llm", "name": "真实模型"},
            {"model": profile_id, "prompt": "仅回复 WORKFLOW_SMOKE_OK"},
        )
        self.assertTrue(str(outcome.output.get("text") or "").strip())

    def test_real_builtin_tool_node(self):
        config = load_project_config(ROOT / "config")
        if not config.tools.enabled:
            self.skipTest("平台工具未启用")
        with tempfile.TemporaryDirectory() as temporary:
            registry = TenantRegistry(Path(temporary) / "data")
            tenant = registry.resolve("workflow-smoke", "tool-user")
            tool_config = replace(
                config.tools,
                default_working_directory=str(Path(temporary) / "workspace"),
                allowed_roots=[str(Path(temporary) / "workspace")],
            )
            runtime = ToolRuntime(
                tool_config, config.app.timezone,
                trash_directory=Path(temporary) / "trash",
                tenant_registry=registry,
                sandbox_available=True,
            )
            self.addCleanup(runtime.close)
            service = self._service(registry=registry, tool_runtime=runtime)
            outcome = service.execute_node_for_test(
                {
                    "run_id": "real-tool", "workflow_id": "workflow-real",
                    "organization_id": tenant.tenant_id,
                    "initiated_by": None, "test_mode": False,
                },
                {"id": "tool", "type": "tool", "name": "真实内置工具"},
                {"tool_name": "get_current_time", "arguments": {}},
            )
            self.assertIn("iso", outcome.output["data"])

    def test_real_script_node(self):
        script_id = os.environ.get("BOTPLATFORM_WORKFLOW_TEST_SCRIPT_ID", "").strip()
        if not script_id:
            self.skipTest("未配置 BOTPLATFORM_WORKFLOW_TEST_SCRIPT_ID")
        config = load_project_config(ROOT / "config")
        definition = config.scripts.get(script_id)
        if definition is None or not definition.enabled:
            self.skipTest("指定脚本不存在或未启用：{}".format(script_id))
        if definition.requires_approval:
            self.skipTest("真实脚本冒烟需选择 requires_approval=false 的隔离测试脚本")
        try:
            parameters = json.loads(
                os.environ.get("BOTPLATFORM_WORKFLOW_TEST_SCRIPT_PARAMETERS", "{}")
            )
        except ValueError as exc:
            self.fail("BOTPLATFORM_WORKFLOW_TEST_SCRIPT_PARAMETERS 必须是 JSON 对象：{}".format(exc))
        if not isinstance(parameters, dict):
            self.fail("BOTPLATFORM_WORKFLOW_TEST_SCRIPT_PARAMETERS 必须是 JSON 对象")
        with tempfile.TemporaryDirectory() as temporary:
            registry = TenantRegistry(Path(temporary) / "data")
            tenant = registry.resolve("workflow-smoke", "script-user")
            script_service = ScriptService(
                config.scripts, None, TenantRecipientStore(registry), ROOT,
                registry, IntegrationStore(registry),
            )
            self.addCleanup(script_service.shutdown)
            service = self._service(registry=registry, script_service=script_service)
            outcome = service.execute_node_for_test(
                {
                    "run_id": "real-script", "workflow_id": "workflow-real",
                    "organization_id": tenant.tenant_id,
                    "initiated_by": None, "test_mode": False,
                },
                {"id": "script", "type": "script", "name": "真实脚本"},
                {"script_id": script_id, "parameters": parameters},
            )
            deadline = time.monotonic() + 120
            result = outcome.output
            while time.monotonic() < deadline and result.get("status") not in {
                "success", "failed", "skipped", "timed_out", "cancelled",
            }:
                time.sleep(0.1)
                result = script_service.get_run(tenant, outcome.output["run_id"])
            self.assertEqual(result.get("status"), "success", result)

    def test_real_datasource_node(self):
        datasource_id = os.environ.get("BOTPLATFORM_WORKFLOW_TEST_DATASOURCE_ID", "").strip()
        if not datasource_id:
            self.skipTest("未配置 BOTPLATFORM_WORKFLOW_TEST_DATASOURCE_ID")
        config = load_project_config(ROOT / "config")
        entry = next((dict(item) for item in config.datasources if item.get("id") == datasource_id), None)
        if entry is None or not entry.get("enabled", True):
            self.skipTest("指定数据源不存在或未启用：{}".format(datasource_id))
        password = os.environ.get("BOTPLATFORM_WORKFLOW_TEST_DATASOURCE_PASSWORD")
        if password is not None:
            entry["password"] = password
        sql = os.environ.get("BOTPLATFORM_WORKFLOW_TEST_DATASOURCE_SQL", "SELECT 1").strip()
        datasource = DataSourceService()
        datasource.reload([entry])
        self.addCleanup(datasource.reload, [])
        outcome = self._service(datasource_service=datasource).execute_node_for_test(
            {"run_id": "real-datasource", "organization_id": "workflow-real"},
            {"id": "datasource", "type": "datasource", "name": "真实数据源"},
            {"datasource_id": datasource_id, "sql": sql, "limit": 10},
        )
        self.assertIn("rows", outcome.output)
        self.assertIn("row_count", outcome.output)

    def test_real_notification_node(self):
        if os.environ.get("BOTPLATFORM_WORKFLOW_TEST_NOTIFICATION_ENABLE_SEND") != "1":
            self.skipTest("未显式设置 BOTPLATFORM_WORKFLOW_TEST_NOTIFICATION_ENABLE_SEND=1")
        names = {
            "token": "BOTPLATFORM_WORKFLOW_TEST_NOTIFICATION_TOKEN",
            "base_url": "BOTPLATFORM_WORKFLOW_TEST_NOTIFICATION_BASE_URL",
            "bot_id": "BOTPLATFORM_WORKFLOW_TEST_NOTIFICATION_BOT_ID",
            "owner_id": "BOTPLATFORM_WORKFLOW_TEST_NOTIFICATION_OWNER_ID",
            "user_id": "BOTPLATFORM_WORKFLOW_TEST_NOTIFICATION_USER_ID",
            "context_token": "BOTPLATFORM_WORKFLOW_TEST_NOTIFICATION_CONTEXT_TOKEN",
        }
        values = {key: os.environ.get(name, "").strip() for key, name in names.items()}
        missing = [names[key] for key, value in values.items() if not value]
        if missing:
            self.skipTest("真实通知缺少配置：{}".format("、".join(missing)))
        with tempfile.TemporaryDirectory() as temporary:
            registry = TenantRegistry(Path(temporary) / "data")
            tenant = registry.resolve(values["bot_id"], values["user_id"])
            recipients = TenantRecipientStore(registry)
            recipients.update(tenant, values["context_token"])
            notifications = NotificationService(
                credentials_loader=lambda: Credentials(
                    values["token"], values["base_url"],
                    values["bot_id"], values["owner_id"],
                ),
                recipient_store=recipients,
            )
            service = self._service(notification_service=notifications)
            service.store.pending_wait_for_node = MagicMock(return_value={"status": "approved"})
            outcome = service.execute_node_for_test(
                {
                    "run_id": "real-notification", "workflow_id": "workflow-real",
                    "organization_id": tenant.tenant_id, "test_mode": False,
                },
                {"id": "notification", "type": "notification", "name": "真实通知"},
                {"message": "BotPlatform 工作流真实依赖冒烟测试"},
            )
            self.assertTrue(outcome.output["notification_ids"])


if __name__ == "__main__":
    unittest.main()
