from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from scripts.todo.todo_manager import (
    SUMMARY_MAX_BYTES,
    SqliteTodoStore,
    TodoError,
    execute_action,
)
from src.storage.tenants import TenantRegistry


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "todo" / "todo_manager.py"


def moment(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class TodoManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "todo"
        self.registry = TenantRegistry(Path(self.temp.name) / "data")
        self.tenant = self.registry.resolve("bot", "todo-user")
        self.environment = patch.dict(
            os.environ,
            {
                "ILINKBOT_DATABASE_PATH": str(self.registry.database_path),
                "ILINKBOT_TENANT_ID": self.tenant.tenant_id,
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def load(self):
        store = SqliteTodoStore(self.registry.database_path, self.tenant.tenant_id)
        with store.database.read() as connection:
            return store._load(connection, moment("2026-01-01T00:00:00"))

    def test_initialization_ids_and_full_lifecycle(self) -> None:
        first = execute_action(
            self.root, "add", title=" 编写待办测试 ", now=moment("2026-01-01T00:00:00")
        )
        second = execute_action(
            self.root, "add", title="补充文档", now=moment("2026-01-01T01:00:00")
        )
        self.assertIn("T0001", first.summary)
        self.assertIn("T0002", second.summary)
        self.assertEqual(os.stat(self.registry.database_path).st_mode & 0o777, 0o600)

        edited = execute_action(
            self.root,
            "edit",
            todo_id="t0001",
            title="编写完整待办测试",
            now=moment("2026-01-02T00:00:00"),
        )
        self.assertIn("编写完整待办测试", edited.summary)
        completed = execute_action(
            self.root, "complete", todo_id="T0001", now=moment("2026-01-03T00:00:00")
        )
        self.assertEqual(completed.status, "success")
        self.assertNotIn("T0001", execute_action(self.root, "list", scope="pending").summary)
        self.assertIn("T0001", execute_action(self.root, "list", scope="completed").summary)

        repeated = execute_action(self.root, "complete", todo_id="T0001")
        self.assertEqual(repeated.status, "skipped")
        reopened = execute_action(
            self.root, "reopen", todo_id="T0001", now=moment("2026-01-04T00:00:00")
        )
        self.assertEqual(reopened.status, "success")
        data = self.load()
        item = next(item for item in data["items"] if item["id"] == "T0001")
        self.assertEqual(item["status"], "pending")
        self.assertIsNone(item["completed_at"])

    def test_invalid_inputs_do_not_change_database(self) -> None:
        with self.assertRaisesRegex(TodoError, "不能为空"):
            execute_action(self.root, "add", title="   ")
        self.assertEqual(self.load()["items"], [])

        execute_action(self.root, "list", now=moment("2026-01-01T00:00:00"))
        with self.assertRaisesRegex(TodoError, "未找到"):
            execute_action(self.root, "complete", todo_id="T9999")
        with self.assertRaisesRegex(TodoError, "不支持"):
            execute_action(self.root, "delete")
        with self.assertRaisesRegex(TodoError, "不接受"):
            execute_action(self.root, "remind", title="unexpected")

        self.assertEqual(self.load()["items"], [])

    def test_archive_after_thirty_days_and_restore_from_archive(self) -> None:
        execute_action(self.root, "add", title="旧事项", now=moment("2026-01-01T00:00:00"))
        execute_action(self.root, "complete", todo_id="T0001", now=moment("2026-01-02T00:00:00"))
        execute_action(self.root, "add", title="新事项", now=moment("2026-01-20T00:00:00"))
        execute_action(self.root, "complete", todo_id="T0002", now=moment("2026-01-20T00:00:00"))

        archived = execute_action(
            self.root, "archive", now=moment("2026-02-01T00:00:00")
        )
        self.assertIn("T0001", archived.summary)
        data = self.load()
        self.assertEqual([item["id"] for item in data["archived_items"]], ["T0001"])
        self.assertEqual([item["id"] for item in data["items"]], ["T0002"])

        reopened = execute_action(
            self.root, "reopen", todo_id="T0001", now=moment("2026-02-02T00:00:00")
        )
        self.assertEqual(reopened.status, "success")
        data = self.load()
        self.assertEqual(data["archived_items"], [])
        restored = next(item for item in data["items"] if item["id"] == "T0001")
        self.assertEqual(restored["status"], "pending")
        self.assertIsNone(restored["archived_at"])

    def test_reminder_only_lists_pending_and_truncates_safely(self) -> None:
        execute_action(self.root, "add", title="已完成事项", now=moment("2026-01-01T00:00:00"))
        execute_action(self.root, "complete", todo_id="T0001", now=moment("2026-01-02T00:00:00"))
        for index in range(20):
            execute_action(
                self.root,
                "add",
                title="第 {} 项 ".format(index) + "长内容" * 40,
                now=moment("2026-01-03T00:00:00"),
            )
        reminder = execute_action(self.root, "remind").summary
        self.assertNotIn("已完成事项", reminder)
        self.assertIn("当前有 20 项", reminder)
        self.assertIn("未显示", reminder)
        self.assertLessEqual(len(reminder.encode("utf-8")), SUMMARY_MAX_BYTES)

    def test_concurrent_processes_do_not_lose_updates_or_duplicate_ids(self) -> None:
        def add(index: int):
            environment = dict(os.environ)
            environment["ILINKBOT_SCRIPT_DATA_ROOT"] = str(self.root)
            environment["ILINKBOT_SCRIPT_RESULT_FILE"] = str(
                Path(self.temp.name) / "results" / "{}.json".format(index)
            )
            return subprocess.run(
                [sys.executable, str(SCRIPT), "add", "--title", "并发事项 {}".format(index)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(add, range(24)))
        self.assertEqual(
            [(result.returncode, result.stderr) for result in results],
            [(0, "")] * 24,
        )
        data = self.load()
        ids = [item["id"] for item in data["items"]]
        self.assertEqual(len(ids), 24)
        self.assertEqual(len(set(ids)), 24)
        self.assertEqual(data["next_id"], 25)

    def test_empty_reminder_is_clear(self) -> None:
        result = execute_action(self.root, "remind", now=moment("2026-01-01T00:00:00"))
        self.assertEqual(result.summary, "【待办提醒】当前待办已清空。")


if __name__ == "__main__":
    unittest.main()
