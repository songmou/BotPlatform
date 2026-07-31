"""Tenant-scoped SQLite repositories and filesystem workspace boundaries."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.core.storage.database import Database, DatabaseError
from src.core.modeling import CanonicalMessage


SCHEMA_VERSION = 1


class TenantStoreError(RuntimeError):
    """Raised when tenant data is missing, corrupt, or unsafe to access."""


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    bot_id: str
    user_id: str
    member_user_id: Optional[int] = field(default=None, compare=False)
    personal_tenant_id: Optional[str] = field(default=None, compare=False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(str(path), 0o700)


class TenantRegistry:
    """Manage canonical tenant UUIDs and legacy bot/user identity keys."""

    def __init__(self, data_root: Path, database: Optional[Database] = None) -> None:
        self.data_root = data_root.resolve()
        self.system_root = self.data_root / "system"
        self.users_root = self.data_root / "users"
        _secure_directory(self.data_root)
        _secure_directory(self.system_root)
        _secure_directory(self.users_root)
        try:
            self.database = database or Database(self.system_root / "botplatform.sqlite3")
        except DatabaseError as exc:
            raise TenantStoreError(str(exc)) from exc
        self._retry_cleanup_jobs()

    @property
    def database_path(self) -> Path:
        return self.database.path

    @staticmethod
    def _key(bot_id: str, user_id: str) -> None:
        if not bot_id or not user_id:
            raise TenantStoreError("bot_id 和 user_id 不能为空")

    def _initialize_tenant(self, context: TenantContext) -> None:
        root = self.tenant_root(context.tenant_id)
        for path in (root, root / "workspace", root / "scripts"):
            _secure_directory(path)

    @staticmethod
    def _from_row(row: Any) -> TenantContext:
        return TenantContext(
            tenant_id=str(row["tenant_id"]),
            bot_id=str(row["bot_id"]),
            user_id=str(row["user_id"]),
        )

    def resolve(self, bot_id: str, user_id: str) -> TenantContext:
        self._key(bot_id, user_id)
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT tenant_id, bot_id, user_id FROM tenants "
                "WHERE bot_id=? AND user_id=? AND deleting=0",
                (bot_id, user_id),
            ).fetchone()
            if row is None:
                tenant_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO tenants(tenant_id, bot_id, user_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (tenant_id, bot_id, user_id, _utc_now()),
                )
                row = connection.execute(
                    "SELECT tenant_id, bot_id, user_id FROM tenants WHERE tenant_id=?",
                    (tenant_id,),
                ).fetchone()
        context = self._from_row(row)
        self._validate_context(context)
        self._initialize_tenant(context)
        organizations = getattr(self, "organization_store", None)
        if (
            organizations is not None
            and not bot_id.startswith("member-personal:")
        ):
            organizations.ensure_legacy_organization(context.tenant_id)
        return context

    def member_personal_context(
        self, organization_id: str, member_user_id: int
    ) -> TenantContext:
        """Return the private storage subject for one member in an organization."""
        self.get(organization_id)
        return self.resolve(
            "member-personal:{}".format(organization_id),
            str(member_user_id),
        )

    def get(self, tenant_id: str) -> TenantContext:
        self._validate_tenant_id(tenant_id)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT tenant_id, bot_id, user_id FROM tenants "
                "WHERE tenant_id=? AND deleting=0",
                (tenant_id,),
            ).fetchone()
        if row is None:
            raise TenantStoreError("未找到租户：{}".format(tenant_id))
        return self._from_row(row)

    def list_contexts(
        self, include_internal: bool = False
    ) -> List[TenantContext]:
        internal_clause = (
            "" if include_internal else "AND bot_id NOT LIKE 'member-personal:%' "
        )
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT tenant_id, bot_id, user_id FROM tenants "
                "WHERE deleting=0 {}ORDER BY created_at".format(internal_clause)
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_overviews(
        self, include_internal: bool = False
    ) -> List[Dict[str, Any]]:
        internal_clause = (
            ""
            if include_internal
            else "AND t.bot_id NOT LIKE 'member-personal:%' "
        )
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT t.tenant_id, t.bot_id, t.user_id, t.created_at, "
                "COALESCE(s.model_mode, 'auto') AS model_mode, "
                "COALESCE(e.message_count, 0) AS message_count, e.last_active_at "
                "FROM tenants t "
                "LEFT JOIN tenant_settings s ON s.tenant_id = t.tenant_id "
                "LEFT JOIN (SELECT tenant_id, COUNT(*) AS message_count, "
                "MAX(created_at) AS last_active_at FROM conversation_events "
                "GROUP BY tenant_id) e ON e.tenant_id = t.tenant_id "
                "WHERE t.deleting=0 {}ORDER BY t.created_at".format(
                    internal_clause
                )
            ).fetchall()
        return [
            {
                "tenant_id": str(row["tenant_id"]),
                "bot_id": str(row["bot_id"]),
                "user_id": str(row["user_id"]),
                "created_at": str(row["created_at"]),
                "model_mode": str(row["model_mode"]),
                "message_count": int(row["message_count"]),
                "last_active_at": (
                    str(row["last_active_at"])
                    if row["last_active_at"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    def delete(self, context: TenantContext) -> None:
        self._validate_context(context)
        requested_at = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT bot_id, user_id FROM tenants WHERE tenant_id=?",
                (context.tenant_id,),
            ).fetchone()
            if row is None or row["bot_id"] != context.bot_id or row["user_id"] != context.user_id:
                raise TenantStoreError("租户身份已经失效")
            connection.execute(
                "UPDATE tenants SET deleting=1 WHERE tenant_id=?", (context.tenant_id,)
            )
            connection.execute(
                "INSERT INTO tenant_cleanup_jobs(tenant_id, requested_at, last_error) "
                "VALUES (?, ?, NULL) ON CONFLICT(tenant_id) DO NOTHING",
                (context.tenant_id, requested_at),
            )
        try:
            self._finish_cleanup(context.tenant_id, requested_at)
        except OSError as exc:
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE tenant_cleanup_jobs SET last_error=? WHERE tenant_id=?",
                    (str(exc)[:1000], context.tenant_id),
                )
            raise

    def _finish_cleanup(self, tenant_id: str, requested_at: str) -> None:
        root = self.tenant_root(tenant_id)
        if root.exists():
            shutil.rmtree(str(root))
        with self.database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM knowledge_fts WHERE tenant_id=?", (tenant_id,))
            connection.execute(
                "INSERT INTO deletion_audit(tenant_id, deleted_at, status) VALUES (?, ?, 'complete')",
                (tenant_id, _utc_now()),
            )
            connection.execute("DELETE FROM tenants WHERE tenant_id=?", (tenant_id,))
            connection.execute("DELETE FROM tenant_cleanup_jobs WHERE tenant_id=?", (tenant_id,))

    def _retry_cleanup_jobs(self) -> None:
        with self.database.read() as connection:
            jobs = connection.execute(
                "SELECT tenant_id, requested_at FROM tenant_cleanup_jobs ORDER BY requested_at"
            ).fetchall()
        for job in jobs:
            try:
                self._finish_cleanup(str(job["tenant_id"]), str(job["requested_at"]))
            except OSError as exc:
                with self.database.transaction(immediate=True) as connection:
                    connection.execute(
                        "UPDATE tenant_cleanup_jobs SET last_error=? WHERE tenant_id=?",
                        (str(exc)[:1000], str(job["tenant_id"])),
                    )

    def tenant_root(self, tenant_id: str) -> Path:
        self._validate_tenant_id(tenant_id)
        return self.users_root / tenant_id

    @staticmethod
    def _validate_tenant_id(tenant_id: str) -> None:
        try:
            parsed = uuid.UUID(tenant_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise TenantStoreError("租户编号格式无效") from exc
        if str(parsed) != tenant_id:
            raise TenantStoreError("租户编号格式无效")

    def _validate_context(self, context: TenantContext) -> None:
        self._validate_tenant_id(context.tenant_id)
        if not context.bot_id or not context.user_id:
            raise TenantStoreError("租户身份记录不完整")


class ConversationStore:
    def __init__(self, registry: TenantRegistry, max_messages: int) -> None:
        self.registry = registry
        self.max_messages = max_messages
        self._locks: Dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    @staticmethod
    def _lock_key(tenant_id: str, session_key: str = "direct") -> str:
        return "{}\x1f{}".format(tenant_id, session_key or "direct")

    def lock_for(
        self,
        tenant_id: str,
        session_key: str = "direct",
    ) -> threading.RLock:
        """Share one tenant lock across chat turns and proactive deliveries."""
        key = self._lock_key(tenant_id, session_key)
        with self._locks_guard:
            return self._locks.setdefault(key, threading.RLock())

    def load_context(
        self,
        tenant_id: str,
        session_key: str = "direct",
        user_id: Optional[int] = None,
    ) -> List[CanonicalMessage]:
        with self.lock_for(tenant_id, session_key):
            with self.registry.database.read() as connection:
                if user_id is None:
                    rows = connection.execute(
                        "SELECT role, content FROM conversation_context_messages "
                        "WHERE tenant_id=? AND session_key=? "
                        "ORDER BY message_id DESC LIMIT ?",
                        (tenant_id, session_key, self.max_messages),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT role, content FROM conversation_context_messages "
                        "WHERE tenant_id=? AND session_key=? AND user_id=? "
                        "ORDER BY message_id DESC LIMIT ?",
                        (tenant_id, session_key, user_id, self.max_messages),
                    ).fetchall()
        return [CanonicalMessage(str(row["role"]), str(row["content"])) for row in reversed(rows)]

    def save_context(
        self,
        tenant_id: str,
        messages: Iterable[CanonicalMessage],
        session_key: str = "direct",
        user_id: Optional[int] = None,
    ) -> None:
        kept = list(messages)[-self.max_messages :]
        now = _utc_now()
        with self.lock_for(tenant_id, session_key):
            with self.registry.database.transaction(immediate=True) as connection:
                if user_id is None:
                    connection.execute(
                        "DELETE FROM conversation_context_messages "
                        "WHERE tenant_id=? AND session_key=?",
                        (tenant_id, session_key),
                    )
                else:
                    connection.execute(
                        "DELETE FROM conversation_context_messages "
                        "WHERE tenant_id=? AND session_key=? AND user_id=?",
                        (tenant_id, session_key, user_id),
                    )
                connection.executemany(
                    "INSERT INTO conversation_context_messages("
                    "tenant_id, role, content, created_at, session_key, user_id"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            tenant_id,
                            item.role,
                            item.content,
                            now,
                            session_key,
                            user_id,
                        )
                        for item in kept
                    ],
                )

    def record_outbound_message(
        self,
        tenant_id: str,
        content: str,
        *,
        image: bool = False,
        delivery_key: str = "",
    ) -> bool:
        """Add a delivered proactive message to short-term and durable history."""
        if not isinstance(content, str) or not content.strip():
            raise TenantStoreError("主动消息上下文格式无效")
        now = _utc_now()
        with self.lock_for(tenant_id):
            with self.registry.database.transaction(immediate=True) as connection:
                if delivery_key:
                    inserted = connection.execute(
                        "INSERT OR IGNORE INTO conversation_delivery_receipts"
                        "(delivery_key, tenant_id, recorded_at) VALUES (?, ?, ?)",
                        (delivery_key, tenant_id, now),
                    )
                    if inserted.rowcount == 0:
                        return False
                connection.execute(
                    "INSERT INTO conversation_context_messages"
                    "(tenant_id, role, content, created_at, session_key) "
                    "VALUES (?, 'assistant', ?, ?, 'direct')",
                    (tenant_id, content, now),
                )
                connection.execute(
                    "DELETE FROM conversation_context_messages "
                    "WHERE tenant_id=? AND session_key='direct' "
                    "AND message_id NOT IN ("
                    "SELECT message_id FROM conversation_context_messages "
                    "WHERE tenant_id=? AND session_key='direct' "
                    "ORDER BY message_id DESC LIMIT ?"
                    ")",
                    (tenant_id, tenant_id, self.max_messages),
                )
                connection.execute(
                    "INSERT INTO conversation_events"
                    "(tenant_id, role, content, image, event_type, created_at, "
                    "session_key) VALUES (?, 'assistant', ?, ?, 'notification', "
                    "?, 'direct')",
                    (tenant_id, content, int(image), now),
                )
        return True

    def append_transcript(
        self,
        tenant_id: str,
        role: str,
        content: str,
        image: bool = False,
        session_key: str = "direct",
        user_id: Optional[int] = None,
    ) -> None:
        if role not in {"user", "assistant", "system"} or not isinstance(content, str):
            raise TenantStoreError("永久对话记录格式无效")
        with self.lock_for(tenant_id, session_key):
            with self.registry.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO conversation_events"
                    "(tenant_id, role, content, image, event_type, created_at, "
                    "session_key, user_id) VALUES (?, ?, ?, ?, 'message', ?, ?, ?)",
                    (
                        tenant_id,
                        role,
                        content,
                        int(image),
                        _utc_now(),
                        session_key,
                        user_id,
                    ),
                )

    def clear_context(
        self,
        tenant_id: str,
        session_key: str = "direct",
        user_id: Optional[int] = None,
    ) -> None:
        with self.lock_for(tenant_id, session_key):
            with self.registry.database.transaction(immediate=True) as connection:
                if user_id is None:
                    connection.execute(
                        "DELETE FROM conversation_context_messages "
                        "WHERE tenant_id=? AND session_key=?",
                        (tenant_id, session_key),
                    )
                else:
                    connection.execute(
                        "DELETE FROM conversation_context_messages "
                        "WHERE tenant_id=? AND session_key=? AND user_id=?",
                        (tenant_id, session_key, user_id),
                    )
                connection.execute(
                    "INSERT INTO conversation_events"
                    "(tenant_id, role, content, image, event_type, created_at, "
                    "session_key, user_id) VALUES (?, 'system', ?, 0, "
                    "'context_cleared', ?, ?, ?)",
                    (
                        tenant_id,
                        "用户清除了当前对话上下文。",
                        _utc_now(),
                        session_key,
                        user_id,
                    ),
                )


class SettingsStore:
    def __init__(self, registry: TenantRegistry) -> None:
        self.registry = registry

    def model_mode(self, tenant_id: str) -> str:
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT model_mode FROM tenant_settings WHERE tenant_id=?", (tenant_id,)
            ).fetchone()
        return str(row["model_mode"]) if row else "auto"

    def set_model_mode(self, tenant_id: str, mode: str) -> None:
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO tenant_settings(tenant_id, model_mode) VALUES (?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET model_mode=excluded.model_mode",
                (tenant_id, mode),
            )


class ScheduleStore:
    def __init__(self, registry: TenantRegistry) -> None:
        self.registry = registry

    def is_enabled(self, tenant_id: str, task_id: str) -> bool:
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT enabled FROM schedule_subscriptions WHERE tenant_id=? AND task_id=?",
                (tenant_id, task_id),
            ).fetchone()
        return bool(row and row["enabled"])

    def set_enabled(self, tenant_id: str, task_id: str, enabled: bool) -> None:
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO schedule_subscriptions(tenant_id, task_id, enabled) VALUES (?, ?, ?) "
                "ON CONFLICT(tenant_id, task_id) DO UPDATE SET enabled=excluded.enabled",
                (tenant_id, task_id, int(enabled)),
            )

    def enabled_tenants(self, task_id: str) -> List[TenantContext]:
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT t.tenant_id, t.bot_id, t.user_id FROM tenants t "
                "JOIN schedule_subscriptions s ON s.tenant_id=t.tenant_id "
                "WHERE s.task_id=? AND s.enabled=1 AND t.deleting=0 ORDER BY t.created_at",
                (task_id,),
            ).fetchall()
        return [TenantRegistry._from_row(row) for row in rows]

    def claim_attempt(self, tenant_id: str, task_id: str, interaction_at: str) -> bool:
        with self.registry.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT interaction_at FROM schedule_attempts WHERE tenant_id=? AND task_id=?",
                (tenant_id, task_id),
            ).fetchone()
            if row and row["interaction_at"] == interaction_at:
                return False
            connection.execute(
                "INSERT INTO schedule_attempts(tenant_id, task_id, interaction_at) VALUES (?, ?, ?) "
                "ON CONFLICT(tenant_id, task_id) DO UPDATE SET interaction_at=excluded.interaction_at",
                (tenant_id, task_id, interaction_at),
            )
            return True


class IntegrationStore:
    """Store non-secret integration metadata; secrets remain outside SQLite."""

    def __init__(self, registry: TenantRegistry) -> None:
        self.registry = registry

    def get(self, tenant_id: str, integration_id: str) -> Optional[Dict[str, Any]]:
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM integrations WHERE tenant_id=? AND integration_id=?",
                (tenant_id, integration_id),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["metadata_json"]))
        return dict(value) if isinstance(value, dict) else None

    def set(self, tenant_id: str, integration_id: str, metadata: Dict[str, Any]) -> None:
        payload = json.dumps(dict(metadata), ensure_ascii=False, separators=(",", ":"))
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO integrations(tenant_id, integration_id, metadata_json, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(tenant_id, integration_id) DO UPDATE SET "
                "metadata_json=excluded.metadata_json, updated_at=excluded.updated_at",
                (tenant_id, integration_id, payload, _utc_now()),
            )

    def delete(self, tenant_id: str, integration_id: str) -> None:
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM integrations WHERE tenant_id=? AND integration_id=?",
                (tenant_id, integration_id),
            )


def new_confirmation_code() -> str:
    return "{:06d}".format(secrets.randbelow(1_000_000))
