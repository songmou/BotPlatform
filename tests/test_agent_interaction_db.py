"""数据库 DataSources 集成测试（真实 PG 连接）。

以真实 Postgres（Docker 供给或本机已有 PG）走 ``AgentService.chat()`` 完整验证：
db_list_tables / db_describe_table / db_query（只读）与 db_execute（审批写）。
环境不可用时（Docker 守护进程未启动且无 ``BOTPLATFORM_TEST_PG_DSN``）整体 skip。

运行：
    ./.venv/bin/python -m unittest tests.test_agent_interaction_db -v
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from src.core.datasource.errors import DataSourceError
from src.core.modeling import CanonicalMessage
from src.core.datasource.service import DataSourceService
from src.core.tooling import ApprovalRequired, FinalAnswer, ToolRuntime

from tests._pg_docker import require_postgres
from tests.test_agent_interaction import (
    FakeToolOllama,
    InteractionTestBase,
    tool_call,
)

SOURCE_CONFIG = Path(__file__).resolve().parents[1] / "config"


class DatabaseInteractionTests(InteractionTestBase):
    def setUp(self) -> None:
        super().setUp()
        # 环境不可用时此处抛出 SkipTest
        self.fixture = require_postgres()
        self.addCleanup(self.fixture.stop)

        self.ds = DataSourceService()
        self.ds.reload(
            [
                self.fixture.datasource_config("ds_ro", True),
                self.fixture.datasource_config("ds_rw", False),
            ]
        )
        self.addCleanup(lambda: self.ds.reload([]))

        self.runtime = ToolRuntime(
            self.tool_config,
            "Asia/Shanghai",
            trash_directory=Path(self.temp.name) / "trash",
            sandbox_available=True,
            datasource_service=self.ds,
        )
        general = self.config.agents[self.config.app.default_agent]
        self.ro_agent = replace(general, id="db_ro", datasources=["ds_ro"])
        self.rw_agent = replace(
            general,
            id="db_rw",
            datasources=["ds_rw"],
            tools=list(dict.fromkeys(list(general.tools) + ["db_execute"])),
        )

    # ---- 只读工具（走 chat 真实往返）----
    def test_db_list_tables_via_chat(self) -> None:
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call("db_list_tables", {"datasource_id": "ds_ro"}),
                    CanonicalMessage("assistant", "已列出表。"),
                ]
            ),
            runtime=self.runtime,
            agents={**self.config.agents, "db_ro": self.ro_agent},
        )
        outcome = service.chat("user", "这个库有哪些表？", agent_id="db_ro")
        self.assertIsInstance(outcome, FinalAnswer)
        self.assertTrue(
            any("customers" in m.content for m in self.tool_messages(ollama))
        )

    def test_db_describe_table_via_chat(self) -> None:
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call(
                        "db_describe_table",
                        {"datasource_id": "ds_ro", "table": "customers"},
                    ),
                    CanonicalMessage("assistant", "已查看表结构。"),
                ]
            ),
            runtime=self.runtime,
            agents={**self.config.agents, "db_ro": self.ro_agent},
        )
        outcome = service.chat("user", "customers 表结构", agent_id="db_ro")
        self.assertIsInstance(outcome, FinalAnswer)
        joined = "\n".join(m.content for m in self.tool_messages(ollama))
        self.assertIn("id", joined)
        self.assertIn("name", joined)
        self.assertIn("city", joined)

    def test_db_query_via_chat(self) -> None:
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call(
                        "db_query",
                        {
                            "datasource_id": "ds_ro",
                            "sql": "SELECT name, city FROM public.customers "
                            "WHERE city='Shanghai'",
                        },
                    ),
                    CanonicalMessage("assistant", "已查询上海客户。"),
                ]
            ),
            runtime=self.runtime,
            agents={**self.config.agents, "db_ro": self.ro_agent},
        )
        outcome = service.chat("user", "上海的客户有哪些？", agent_id="db_ro")
        self.assertIsInstance(outcome, FinalAnswer)
        joined = "\n".join(m.content for m in self.tool_messages(ollama))
        self.assertIn("Alice", joined)
        self.assertIn("Carol", joined)

    # ---- 写工具（审批 + 真实副作用）----
    def test_db_execute_write_via_chat(self) -> None:
        service, ollama = self.agent_service(
            FakeToolOllama(
                [
                    tool_call(
                        "db_execute",
                        {
                            "datasource_id": "ds_rw",
                            "sql": "UPDATE public.customers SET city='Shanghai' "
                            "WHERE id=2",
                            "reason": "集成测试：将 2 号客户城市改为上海",
                        },
                    ),
                    CanonicalMessage("assistant", "已更新。"),
                ]
            ),
            runtime=self.runtime,
            agents={**self.config.agents, "db_rw": self.rw_agent},
        )
        pending = service.chat("user", "把 2 号客户的城市改成上海", agent_id="db_rw")
        self.assertIsInstance(pending, ApprovalRequired)
        final = service.resolve_pending_approval("user", True)
        self.assertIsInstance(final, FinalAnswer)
        # 真实回查 DB 验证行被修改（2 号原为 Beijing，应变为 Shanghai）
        result = self.ds.query(
            "ds_rw", "SELECT city FROM public.customers WHERE id=2"
        )
        self.assertEqual(result["rows"][0][0], "Shanghai")

    # ---- 网关 / 只读强制（服务层，真实 gateway 校验）----
    def test_readonly_source_rejects_write(self) -> None:
        with self.assertRaises(DataSourceError):
            self.ds.execute_write(
                "ds_ro", "UPDATE public.customers SET city='Z' WHERE id=1"
            )

    def test_query_unauthorized_table_rejected(self) -> None:
        with self.assertRaises(DataSourceError):
            self.ds.query("ds_ro", "SELECT * FROM public.orders")

    def test_query_non_select_rejected(self) -> None:
        with self.assertRaises(DataSourceError):
            self.ds.query(
                "ds_ro",
                "INSERT INTO public.customers (name, city) VALUES ('x', 'y')",
            )


if __name__ == "__main__":
    unittest.main()
