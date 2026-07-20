"""One-time, idempotent import of pre-SQLite tenant data."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from database import Database


TODO_ID = re.compile(r"^T(0*[1-9][0-9]*)$")


class LegacyMigrationError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_file(path: Path, optional: bool = True) -> Optional[Any]:
    if not path.exists():
        if optional:
            return None
        raise LegacyMigrationError("旧数据文件不存在：{}".format(path))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LegacyMigrationError("旧数据文件无法读取：{}".format(path)) from exc


def _timestamp(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return fallback


def _file_timestamp(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return _utc_now()


class LegacyDataMigrator:
    """Import legacy JSON state without overwriting newer SQLite values."""

    def __init__(self, data_root: Path, database: Database) -> None:
        self.data_root = data_root.resolve()
        self.system_root = self.data_root / "system"
        self.users_root = self.data_root / "users"
        self.database = database

    def migrate(self) -> List[Dict[str, Any]]:
        results = []
        for source_id, profile, source_root in self._legacy_profiles():
            target_id = self._target_tenant(source_id, profile)
            result = self._migrate_source(source_id, target_id, profile, source_root)
            if result is not None:
                results.append(result)
        return results

    def _legacy_profiles(self) -> List[Tuple[str, Dict[str, Any], Path]]:
        profiles: Dict[str, Dict[str, Any]] = {}
        index = _json_file(self.system_root / "users.json")
        if isinstance(index, dict) and isinstance(index.get("users"), dict):
            for raw in index["users"].values():
                if isinstance(raw, dict) and isinstance(raw.get("tenant_id"), str):
                    profiles[raw["tenant_id"]] = dict(raw)
        if self.users_root.exists():
            for path in sorted(self.users_root.glob("*/profile.json")):
                raw = _json_file(path, optional=False)
                if isinstance(raw, dict) and isinstance(raw.get("tenant_id"), str):
                    profiles[raw["tenant_id"]] = dict(raw)

        resolved = []
        for source_id, profile in sorted(profiles.items()):
            try:
                if str(uuid.UUID(source_id)) != source_id:
                    continue
            except (ValueError, TypeError, AttributeError):
                continue
            if not all(
                isinstance(profile.get(field), str) and profile[field]
                for field in ("bot_id", "user_id")
            ):
                continue
            source_root = (self.users_root / source_id).resolve()
            if source_root.exists() and source_root.parent == self.users_root:
                resolved.append((source_id, profile, source_root))
        return resolved

    def _target_tenant(self, source_id: str, profile: Dict[str, Any]) -> str:
        bot_id = str(profile["bot_id"])
        user_id = str(profile["user_id"])
        created_at = _timestamp(profile.get("created_at"), _utc_now())
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT tenant_id FROM tenants WHERE bot_id=? AND user_id=?",
                (bot_id, user_id),
            ).fetchone()
            if row is not None:
                return str(row["tenant_id"])
            collision = connection.execute(
                "SELECT 1 FROM tenants WHERE tenant_id=?", (source_id,)
            ).fetchone()
            target_id = source_id if collision is None else str(uuid.uuid4())
            connection.execute(
                "INSERT INTO tenants(tenant_id, bot_id, user_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (target_id, bot_id, user_id, created_at),
            )
            return target_id

    def _migrate_source(
        self,
        source_id: str,
        target_id: str,
        profile: Dict[str, Any],
        source_root: Path,
    ) -> Optional[Dict[str, Any]]:
        with self.database.read() as connection:
            if connection.execute(
                "SELECT 1 FROM legacy_imports WHERE source_tenant_id=?", (source_id,)
            ).fetchone():
                return None

        target_root = (self.users_root / target_id).resolve()
        target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(target_root, 0o700)
        copied = self._copy_user_files(source_root, target_root)

        details: Dict[str, Any] = {
            "subscriptions": 0,
            "conversation_events": 0,
            "context_messages": 0,
            "integrations": 0,
            "todos": 0,
            "script_runs": 0,
            "artifacts": 0,
            "files": copied,
        }
        with self.database.transaction(immediate=True) as connection:
            self._import_settings(connection, source_root, target_id)
            details["subscriptions"] = self._import_schedules(
                connection, source_root, target_id
            )
            self._import_recipient(connection, source_root, target_id)
            events, context = self._import_conversation(
                connection, source_root, target_id, profile
            )
            details["conversation_events"] = events
            details["context_messages"] = context
            details["integrations"] = self._import_integrations(
                connection, source_root, target_id
            )
            details["todos"] = self._import_todos(connection, source_root, target_id)
            runs, artifacts = self._import_script_runs(
                connection, source_root, target_root, target_id
            )
            details["script_runs"] = runs
            details["artifacts"] = artifacts
            connection.execute(
                "INSERT INTO legacy_imports"
                "(source_tenant_id, target_tenant_id, imported_at, details_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    source_id,
                    target_id,
                    _utc_now(),
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        return {"source_tenant_id": source_id, "target_tenant_id": target_id, **details}

    @staticmethod
    def _copy_user_files(source_root: Path, target_root: Path) -> int:
        if source_root == target_root:
            return 0
        copied = 0
        for name in ("workspace", "scripts"):
            source = source_root / name
            if not source.exists():
                continue
            for path in source.rglob("*"):
                if path.is_symlink():
                    continue
                relative = path.relative_to(source_root)
                destination = target_root / relative
                if path.is_dir():
                    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if os.name != "nt":
                        os.chmod(destination, 0o700)
                elif path.is_file() and not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copy2(path, destination)
                    if os.name != "nt":
                        os.chmod(destination, 0o600)
                    copied += 1
        return copied

    @staticmethod
    def _import_settings(connection: Any, source_root: Path, target_id: str) -> None:
        raw = _json_file(source_root / "settings.json")
        if not isinstance(raw, dict):
            return
        mode = raw.get("model_mode")
        if isinstance(mode, str) and mode:
            connection.execute(
                "INSERT INTO tenant_settings(tenant_id, model_mode) VALUES (?, ?) "
                "ON CONFLICT(tenant_id) DO NOTHING",
                (target_id, mode),
            )

    @staticmethod
    def _import_schedules(connection: Any, source_root: Path, target_id: str) -> int:
        raw = _json_file(source_root / "schedules.json")
        if not isinstance(raw, dict):
            return 0
        subscriptions = raw.get("subscriptions")
        count = 0
        if isinstance(subscriptions, dict):
            for task_id, enabled in subscriptions.items():
                if not isinstance(task_id, str) or not task_id or not isinstance(enabled, bool):
                    continue
                cursor = connection.execute(
                    "INSERT INTO schedule_subscriptions(tenant_id, task_id, enabled) "
                    "VALUES (?, ?, ?) ON CONFLICT(tenant_id, task_id) DO NOTHING",
                    (target_id, task_id, int(enabled)),
                )
                count += cursor.rowcount
        attempts = raw.get("attempts")
        if isinstance(attempts, dict):
            for task_id, interaction_at in attempts.items():
                if isinstance(task_id, str) and isinstance(interaction_at, str) and interaction_at:
                    connection.execute(
                        "INSERT INTO schedule_attempts(tenant_id, task_id, interaction_at) "
                        "VALUES (?, ?, ?) ON CONFLICT(tenant_id, task_id) DO UPDATE SET "
                        "interaction_at=CASE WHEN excluded.interaction_at > interaction_at "
                        "THEN excluded.interaction_at ELSE interaction_at END",
                        (target_id, task_id, interaction_at),
                    )
        return count

    @staticmethod
    def _import_recipient(connection: Any, source_root: Path, target_id: str) -> None:
        raw = _json_file(source_root / "recipient.json")
        if not isinstance(raw, dict):
            return
        values = [raw.get(key) for key in ("user_id", "context_token", "updated_at")]
        if all(isinstance(value, str) and value for value in values):
            connection.execute(
                "INSERT INTO recipients(tenant_id, user_id, context_token, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(tenant_id) DO UPDATE SET "
                "user_id=excluded.user_id, context_token=excluded.context_token, "
                "updated_at=excluded.updated_at WHERE excluded.updated_at > recipients.updated_at",
                (target_id, *values),
            )
        attempts = raw.get("task_attempts")
        if isinstance(attempts, dict):
            for task_id, interaction_at in attempts.items():
                if isinstance(task_id, str) and isinstance(interaction_at, str) and interaction_at:
                    connection.execute(
                        "INSERT INTO recipient_task_attempts(tenant_id, task_id, interaction_at) "
                        "VALUES (?, ?, ?) ON CONFLICT(tenant_id, task_id) DO UPDATE SET "
                        "interaction_at=CASE WHEN excluded.interaction_at > interaction_at "
                        "THEN excluded.interaction_at ELSE interaction_at END",
                        (target_id, task_id, interaction_at),
                    )

    @staticmethod
    def _conversation_items(path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        items = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    if (
                        isinstance(raw, dict)
                        and raw.get("role") in {"user", "assistant", "system"}
                        and isinstance(raw.get("content"), str)
                    ):
                        items.append(raw)
        except (OSError, ValueError) as exc:
            raise LegacyMigrationError("旧会话记录无法读取：{}".format(path)) from exc
        return items

    def _import_conversation(
        self,
        connection: Any,
        source_root: Path,
        target_id: str,
        profile: Dict[str, Any],
    ) -> Tuple[int, int]:
        transcript_path = source_root / "conversation" / "transcript.jsonl"
        items = self._conversation_items(transcript_path)
        base_text = _timestamp(profile.get("created_at"), _file_timestamp(transcript_path))
        base = datetime.fromisoformat(base_text.replace("Z", "+00:00"))
        row = connection.execute(
            "SELECT COALESCE(MIN(event_id), 1) AS minimum FROM conversation_events "
            "WHERE tenant_id=?",
            (target_id,),
        ).fetchone()
        first_id = int(row["minimum"]) - len(items)
        for index, item in enumerate(items):
            created_at = _timestamp(
                item.get("created_at"),
                (base + timedelta(microseconds=index)).astimezone(timezone.utc).isoformat(),
            )
            connection.execute(
                "INSERT INTO conversation_events"
                "(event_id, tenant_id, role, content, image, event_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'message', ?)",
                (
                    first_id + index,
                    target_id,
                    item["role"],
                    item["content"],
                    int(bool(item.get("image", False))),
                    created_at,
                ),
            )

        context_count = 0
        existing = connection.execute(
            "SELECT 1 FROM conversation_context_messages WHERE tenant_id=? LIMIT 1",
            (target_id,),
        ).fetchone()
        context = _json_file(source_root / "conversation" / "context.json")
        messages = context.get("messages") if isinstance(context, dict) else None
        if existing is None and isinstance(messages, list):
            for index, item in enumerate(messages):
                if (
                    isinstance(item, dict)
                    and item.get("role") in {"user", "assistant"}
                    and isinstance(item.get("content"), str)
                ):
                    connection.execute(
                        "INSERT INTO conversation_context_messages"
                        "(tenant_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                        (
                            target_id,
                            item["role"],
                            item["content"],
                            (base + timedelta(microseconds=index)).isoformat(),
                        ),
                    )
                    context_count += 1
        return len(items), context_count

    @staticmethod
    def _import_integrations(connection: Any, source_root: Path, target_id: str) -> int:
        raw = _json_file(source_root / "integrations.json")
        integrations = raw.get("integrations") if isinstance(raw, dict) else None
        if not isinstance(integrations, dict):
            return 0
        count = 0
        fallback = _file_timestamp(source_root / "integrations.json")
        for integration_id, metadata in integrations.items():
            if not isinstance(integration_id, str) or not isinstance(metadata, dict):
                continue
            updated_at = _timestamp(metadata.get("configured_at"), fallback)
            cursor = connection.execute(
                "INSERT INTO integrations(tenant_id, integration_id, metadata_json, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(tenant_id, integration_id) DO NOTHING",
                (
                    target_id,
                    integration_id,
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    updated_at,
                ),
            )
            count += cursor.rowcount
        return count

    @staticmethod
    def _import_todos(connection: Any, source_root: Path, target_id: str) -> int:
        raw = _json_file(source_root / "scripts" / "todo" / "todos.json")
        if not isinstance(raw, dict):
            return 0
        count = 0
        groups: Iterable[Tuple[str, Any]] = (
            ("active", raw.get("items")),
            ("archived", raw.get("archived_items")),
        )
        for group, items in groups:
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                match = TODO_ID.fullmatch(str(item.get("id", "")))
                title = item.get("title")
                status = "archived" if group == "archived" else item.get("status")
                if not match or not isinstance(title, str) or status not in {
                    "pending", "completed", "archived"
                }:
                    continue
                created = _timestamp(item.get("created_at"), _utc_now())
                updated = _timestamp(item.get("updated_at"), created)
                cursor = connection.execute(
                    "INSERT INTO todos"
                    "(tenant_id, todo_number, title, status, created_at, updated_at, "
                    "completed_at, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(tenant_id, todo_number) DO NOTHING",
                    (
                        target_id,
                        int(match.group(1)),
                        title,
                        status,
                        created,
                        updated,
                        item.get("completed_at"),
                        item.get("archived_at"),
                    ),
                )
                count += cursor.rowcount
        return count

    @staticmethod
    def _artifact_relative(source_root: Path, raw_path: Any) -> Optional[Path]:
        if not isinstance(raw_path, str) or not raw_path:
            return None
        candidate = Path(raw_path).expanduser()
        try:
            resolved = candidate.resolve()
            return resolved.relative_to(source_root)
        except (OSError, ValueError):
            pass
        matches = [
            item
            for item in (source_root / "scripts").rglob(candidate.name)
            if item.is_file() and not item.is_symlink()
        ]
        return matches[0].relative_to(source_root) if len(matches) == 1 else None

    def _import_script_runs(
        self,
        connection: Any,
        source_root: Path,
        target_root: Path,
        target_id: str,
    ) -> Tuple[int, int]:
        runs_root = source_root / "script_runs"
        if not runs_root.exists():
            return 0, 0
        run_count = 0
        artifact_count = 0
        for path in sorted(runs_root.glob("*.json")):
            raw = _json_file(path, optional=False)
            if not isinstance(raw, dict):
                continue
            required = ("run_id", "script_id", "script_name", "status", "created_at")
            if not all(isinstance(raw.get(key), str) and raw[key] for key in required):
                continue
            parameters = raw.get("parameters")
            if not isinstance(parameters, dict):
                parameters = {}
            cursor = connection.execute(
                "INSERT INTO script_runs"
                "(run_id, tenant_id, script_id, script_name, trigger, parameters_json, "
                "status, summary, created_at, started_at, finished_at, exit_code, error, "
                "notification_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO NOTHING",
                (
                    raw["run_id"],
                    target_id,
                    raw["script_id"],
                    raw["script_name"],
                    raw.get("trigger") if isinstance(raw.get("trigger"), str) else "legacy",
                    json.dumps(parameters, ensure_ascii=False, separators=(",", ":")),
                    raw["status"],
                    raw.get("summary") if isinstance(raw.get("summary"), str) else "",
                    raw["created_at"],
                    raw.get("started_at"),
                    raw.get("finished_at"),
                    raw.get("exit_code"),
                    raw.get("error"),
                    raw.get("notification_error"),
                ),
            )
            if cursor.rowcount == 0:
                continue
            run_count += 1
            artifacts = raw.get("artifacts")
            if not isinstance(artifacts, list):
                continue
            for position, artifact in enumerate(artifacts):
                relative = self._artifact_relative(source_root, artifact)
                if relative is None:
                    continue
                target = target_root / relative
                if not target.is_file():
                    continue
                try:
                    digest = hashlib.sha256(target.read_bytes()).hexdigest()
                except OSError:
                    digest = None
                connection.execute(
                    "INSERT INTO script_run_artifacts"
                    "(run_id, position, relative_path, content_hash) VALUES (?, ?, ?, ?)",
                    (raw["run_id"], position, str(relative), digest),
                )
                artifact_count += 1
        return run_count, artifact_count
