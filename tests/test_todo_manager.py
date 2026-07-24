from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from src.core.plugins.todo import (
    SUMMARY_MAX_BYTES,
    SqliteTodoStore,
    TodoError,
    execute_action,
)
from src.core.storage.tenants import TenantRegistry


def moment(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class TodoManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = TenantRegistry(Path(self.temp.name) / "data")
        self.tenant = self.registry.resolve("bot", "todo-user")
        self.db_path = self.registry.database_path
        self.tenant_id = self.tenant.tenant_id

    def _execute(self, action, **kwargs):
        return execute_action(self.db_path, self.tenant_id, action, **kwargs)

    def load(self):
        store = SqliteTodoStore(self.db_path, self.tenant_id)
        with store.database.read() as connection:
            return store._load(connection, moment("2026-01-01T00:00:00"))

    def test_initialization_ids_and_full_lifecycle(self) -> None:
        first = self._execute("add", title=" 编写待办测试 ", now=moment("2026-01-01T00:00:00"))
        second = self._execute("add", title="补充文档", now=moment("2026-01-01T01:00:00"))
        self.assertIn("T0001", first.summary)
        self.assertIn("T0002", second.summary)
        self.assertEqual(os.stat(self.db_path).st_mode & 0o777, 0o600)

        edited = self._execute(
            "edit",
            todo_id="t0001",
            title="编写完整待办测试",
            now=moment("2026-01-02T00:00:00"),
        )
        self.assertIn("编写完整待办测试", edited.summary)
        completed = self._execute(
            "complete", todo_id="T0001", now=moment("2026-01-03T00:00:00")
        )
        self.assertEqual(completed.status, "success")
        self.assertNotIn("T0001", self._execute("list", scope="pending").summary)
        self.assertIn("T0001", self._execute("list", scope="completed").summary)

        repeated = self._execute("complete", todo_id="T0001")
        self.assertEqual(repeated.status, "skipped")
        reopened = self._execute(
            "reopen", todo_id="T0001", now=moment("2026-01-04T00:00:00")
        )
        self.assertEqual(reopened.status, "success")
        data = self.load()
        item = next(item for item in data["items"] if item["id"] == "T0001")
        self.assertEqual(item["status"], "pending")
        self.assertIsNone(item["completed_at"])

    def test_invalid_inputs_do_not_change_database(self) -> None:
        with self.assertRaisesRegex(TodoError, "不能为空"):
            self._execute("add", title="   ")
        self.assertEqual(self.load()["items"], [])

        self._execute("list", now=moment("2026-01-01T00:00:00"))
        with self.assertRaisesRegex(TodoError, "未找到"):
            self._execute("complete", todo_id="T9999")
        with self.assertRaisesRegex(TodoError, "不支持"):
            self._execute("delete")
        with self.assertRaisesRegex(TodoError, "不接受"):
            self._execute("remind", title="unexpected")

        self.assertEqual(self.load()["items"], [])

    def test_archive_after_thirty_days_and_restore_from_archive(self) -> None:
        self._execute("add", title="旧事项", now=moment("2026-01-01T00:00:00"))
        self._execute("complete", todo_id="T0001", now=moment("2026-01-02T00:00:00"))
        self._execute("add", title="新事项", now=moment("2026-01-20T00:00:00"))
        self._execute("complete", todo_id="T0002", now=moment("2026-01-20T00:00:00"))

        archived = self._execute("archive", now=moment("2026-02-01T00:00:00"))
        self.assertIn("T0001", archived.summary)
        data = self.load()
        self.assertEqual([item["id"] for item in data["archived_items"]], ["T0001"])
        self.assertEqual([item["id"] for item in data["items"]], ["T0002"])

        reopened = self._execute(
            "reopen", todo_id="T0001", now=moment("2026-02-02T00:00:00")
        )
        self.assertEqual(reopened.status, "success")
        data = self.load()
        self.assertEqual(data["archived_items"], [])
        restored = next(item for item in data["items"] if item["id"] == "T0001")
        self.assertEqual(restored["status"], "pending")
        self.assertIsNone(restored["archived_at"])

    def test_reminder_only_lists_pending_and_truncates_safely(self) -> None:
        self._execute("add", title="已完成事项", now=moment("2026-01-01T00:00:00"))
        self._execute("complete", todo_id="T0001", now=moment("2026-01-02T00:00:00"))
        for index in range(20):
            self._execute(
                "add",
                title="第 {} 项 ".format(index) + "长内容" * 40,
                now=moment("2026-01-03T00:00:00"),
            )
        reminder = self._execute("remind").summary
        self.assertNotIn("已完成事项", reminder)
        self.assertIn("当前有 20 项", reminder)
        self.assertIn("未显示", reminder)
        self.assertLessEqual(len(reminder.encode("utf-8")), SUMMARY_MAX_BYTES)

    def test_concurrent_threads_do_not_lose_updates_or_duplicate_ids(self) -> None:
        def add(index: int):
            return execute_action(
                self.db_path,
                self.tenant_id,
                "add",
                title="并发事项 {}".format(index),
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(add, range(24)))
        self.assertTrue(all(result.status == "success" for result in results))
        data = self.load()
        ids = [item["id"] for item in data["items"]]
        self.assertEqual(len(ids), 24)
        self.assertEqual(len(set(ids)), 24)
        self.assertEqual(data["next_id"], 25)

    def test_empty_reminder_is_clear(self) -> None:
        result = self._execute("remind", now=moment("2026-01-01T00:00:00"))
        self.assertEqual(result.summary, "【待办提醒】当前待办已清空。")

    def test_one_off_reminder_is_persisted_claimed_once_and_cancelled_on_complete(self) -> None:
        created = moment("2026-01-01T00:00:00")
        self._execute(
            "add", title="五分钟后提醒", remind_at="5分钟后", now=created
        )
        store = SqliteTodoStore(self.db_path, self.tenant_id)
        with store.database.read() as connection:
            event = connection.execute(
                "SELECT due_at, delivery_status FROM todo_reminder_events "
                "WHERE tenant_id=? AND todo_number=1",
                (self.tenant_id,),
            ).fetchone()
        self.assertEqual(event["due_at"], "2026-01-01T00:05:00+00:00")
        self.assertEqual(event["delivery_status"], "pending")

        due = store.claim_due_reminders(moment("2026-01-01T00:05:00"))
        self.assertEqual([(item["todo_number"], item["title"]) for item in due], [(1, "五分钟后提醒")])
        self.assertEqual(store.claim_due_reminders(moment("2026-01-01T00:06:00")), [])
        store.finish_reminder(1, True, now=moment("2026-01-01T00:05:01"))

        self._execute(
            "edit", todo_id="T0001", remind_at="2026-01-01T00:10:00+00:00",
            update_reminder=True, now=moment("2026-01-01T00:06:00"),
        )
        self._execute("complete", todo_id="T0001", now=moment("2026-01-01T00:07:00"))
        with store.database.read() as connection:
            event = connection.execute(
                "SELECT delivery_status FROM todo_reminder_events "
                "WHERE tenant_id=? AND todo_number=1",
                (self.tenant_id,),
            ).fetchone()
        self.assertEqual(event["delivery_status"], "cancelled")

    def test_delivered_one_off_task_is_automatically_completed(self) -> None:
        created = moment("2026-01-01T00:00:00")
        self._execute(
            "add",
            title="今晚 10 点发布部署更新",
            remind_at="5分钟后",
            is_one_off=True,
            now=created,
        )
        store = SqliteTodoStore(self.db_path, self.tenant_id)
        due = store.claim_due_reminders(moment("2026-01-01T00:05:00"))
        self.assertEqual(len(due), 1)
        store.finish_reminder(1, True, now=moment("2026-01-01T00:05:01"))

        self.assertNotIn("T0001", self._execute("list", scope="pending").summary)
        completed = self._execute("list", scope="completed").summary
        self.assertIn("T0001", completed)
        self.assertIn("今晚 10 点发布部署更新", completed)
        self.assertIn("已清空", self._execute("remind").summary)

    def test_delivered_regular_reminder_remains_pending(self) -> None:
        created = moment("2026-01-01T00:00:00")
        self._execute("add", title="需要后续跟进", remind_at="5分钟后", now=created)
        store = SqliteTodoStore(self.db_path, self.tenant_id)
        store.claim_due_reminders(moment("2026-01-01T00:05:00"))
        store.finish_reminder(1, True, now=moment("2026-01-01T00:05:01"))

        self.assertIn("需要后续跟进", self._execute("list", scope="pending").summary)

    def test_restart_recovers_claimed_reminder_for_redelivery(self) -> None:
        self._execute(
            "add", title="恢复提醒", remind_at="2026-01-01T00:01:00+00:00",
            now=moment("2026-01-01T00:00:00"),
        )
        store = SqliteTodoStore(self.db_path, self.tenant_id)
        self.assertEqual(len(store.claim_due_reminders(moment("2026-01-01T00:01:00"))), 1)
        store.recover_inflight_reminders(moment("2026-01-01T00:02:00"))
        self.assertEqual(len(store.claim_due_reminders(moment("2026-01-01T00:02:00"))), 1)


if __name__ == "__main__":
    unittest.main()
