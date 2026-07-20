from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.storage.migration import LegacyDataMigrator
from src.modeling import CanonicalMessage
from src.services.notification import TenantRecipientStore
from src.storage.tenants import ConversationStore, ScheduleStore, TenantRegistry


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_imports_missing_legacy_data_without_overwriting_current_values(self) -> None:
        registry = TenantRegistry(self.data)
        target = registry.resolve("bot", "wechat-user")
        recipients = TenantRecipientStore(registry)
        recipients.update(target, "new-context")
        schedules = ScheduleStore(registry)
        schedules.set_enabled(target.tenant_id, "daily", False)
        conversations = ConversationStore(registry, 10)
        conversations.save_context(
            target.tenant_id,
            [CanonicalMessage("user", "new context")],
        )

        source_id = "00000000-0000-0000-0000-000000000123"
        source = self.data / "users" / source_id
        profile = {
            "tenant_id": source_id,
            "bot_id": "bot",
            "user_id": "wechat-user",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        self.write_json(source / "profile.json", profile)
        self.write_json(
            source / "schedules.json",
            {"subscriptions": {"daily": True, "monthly": True}, "attempts": {}},
        )
        self.write_json(
            source / "recipient.json",
            {
                "user_id": "wechat-user",
                "context_token": "old-context",
                "updated_at": "2025-01-01T00:00:00+00:00",
                "task_attempts": {},
            },
        )
        self.write_json(
            source / "conversation" / "context.json",
            {"messages": [{"role": "user", "content": "old context"}]},
        )
        (source / "conversation" / "transcript.jsonl").write_text(
            json.dumps({"role": "user", "content": "old question"})
            + "\n"
            + json.dumps({"role": "assistant", "content": "old answer"})
            + "\n",
            encoding="utf-8",
        )
        self.write_json(
            source / "integrations.json",
            {
                "integrations": {
                    "example": {
                        "account": "account",
                        "keychain_service": "legacy.example",
                        "keychain_account": "credential",
                        "configured_at": "2026-01-01T00:00:00+00:00",
                    }
                }
            },
        )
        self.write_json(
            source / "scripts" / "todo" / "todos.json",
            {
                "schema_version": 1,
                "next_id": 2,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "items": [
                    {
                        "id": "T0001",
                        "title": "legacy todo",
                        "status": "pending",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "completed_at": None,
                        "archived_at": None,
                    }
                ],
                "archived_items": [],
            },
        )
        image = source / "scripts" / "reports" / "legacy.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"legacy-image")
        self.write_json(
            source / "script_runs" / "legacy-run.json",
            {
                "run_id": "legacy-run",
                "script_id": "example_check",
                "script_name": "legacy script",
                "trigger": "schedule",
                "parameters": {},
                "status": "success",
                "summary": "done",
                "created_at": "2026-01-01T00:00:00+00:00",
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:01+00:00",
                "exit_code": 0,
                "error": None,
                "notification_error": None,
                "artifacts": ["/old/location/legacy.png"],
            },
        )

        results = LegacyDataMigrator(self.data, registry.database).migrate()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["target_tenant_id"], target.tenant_id)
        self.assertFalse(schedules.is_enabled(target.tenant_id, "daily"))
        self.assertTrue(schedules.is_enabled(target.tenant_id, "monthly"))
        self.assertEqual(recipients.load(target.tenant_id).context_token, "new-context")
        self.assertEqual(conversations.load_context(target.tenant_id)[0].content, "new context")

        with registry.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM conversation_events WHERE tenant_id=?",
                    (target.tenant_id,),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM integrations WHERE tenant_id=?",
                    (target.tenant_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM todos WHERE tenant_id=?",
                    (target.tenant_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM script_runs WHERE tenant_id=?",
                    (target.tenant_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM legacy_imports").fetchone()[0], 1)
        self.assertTrue(
            (registry.tenant_root(target.tenant_id) / "scripts" / "reports" / "legacy.png").exists()
        )
        self.assertEqual(LegacyDataMigrator(self.data, registry.database).migrate(), [])

    def test_registry_bootstraps_a_legacy_identity_when_sqlite_is_empty(self) -> None:
        source_id = "00000000-0000-0000-0000-000000000321"
        source = self.data / "users" / source_id
        profile = {
            "tenant_id": source_id,
            "bot_id": "legacy-bot",
            "user_id": "legacy-user",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        self.write_json(source / "profile.json", profile)
        self.write_json(
            self.data / "system" / "users.json",
            {"version": 1, "users": {"legacy": profile}},
        )
        self.write_json(
            source / "schedules.json",
            {"subscriptions": {"daily": True}, "attempts": {}},
        )

        registry = TenantRegistry(self.data)
        contexts = registry.list_contexts()
        self.assertEqual([item.tenant_id for item in contexts], [source_id])
        self.assertTrue(ScheduleStore(registry).is_enabled(source_id, "daily"))
        self.assertEqual(len(registry.legacy_migrations), 1)


if __name__ == "__main__":
    unittest.main()
