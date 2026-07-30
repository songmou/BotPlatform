"""Codex thread management exposed as tenant-aware BotPlatform tools."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from src.core.services.notification import (
    NotificationError,
    NotificationRecipientStaleError,
)

from .base import PluginContext, PluginError, PluginToolDefinition


TERMINAL_STATUSES = {"completed", "failed", "interrupted"}
ACTIVE_STATUSES = {"queued", "running"}
ACTIVE_PHASES = {"queued", "running", "waiting_approval", "waiting_input"}
WAITING_PHASES = {"waiting_approval", "waiting_input"}
DEFAULT_NOTIFY_EVENTS = frozenset(
    {
        "queued",
        "running",
        "waiting_approval",
        "waiting_input",
        "completed",
        "failed",
        "interrupted",
    }
)
SUPPORTED_NOTIFY_EVENTS = DEFAULT_NOTIFY_EVENTS
APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
}
USER_INPUT_METHOD = "item/tool/requestUserInput"
PROJECT_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
HOOK_EVENTS = frozenset(
    {"UserPromptSubmit", "PermissionRequest", "PreToolUse", "PostToolUse", "Stop"}
)
LOGGER = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _object_schema(
    properties: Optional[Dict[str, Any]] = None,
    required: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _enum_text(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "")


def _model_data(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, str, int, float, bool)) or value is None:
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="python", by_alias=False)
    if hasattr(value, "__dict__"):
        return {
            key: _model_data(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def _latest_agent_text(value: Any) -> str:
    """Find the latest accumulated agent message in a read response."""

    found: List[str] = []

    def visit(item: Any) -> None:
        item = _model_data(item)
        if isinstance(item, dict):
            item_type = str(item.get("type", ""))
            text = item.get("text")
            if item_type in {"agentMessage", "agent_message"} and isinstance(text, str):
                if text.strip():
                    found.append(text.strip())
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)
    return found[-1] if found else ""


@dataclass(frozen=True)
class CodexProject:
    id: str
    path: Path


@dataclass(frozen=True)
class CodexTasksConfig:
    admin_tenant_ids: frozenset[str]
    projects: Dict[str, CodexProject]
    default_project: str
    max_concurrent_tasks: int
    notify_on_completion: bool
    monitor_external_tasks: bool
    monitor_tenant_id: str
    external_project_scope: str
    external_poll_interval_seconds: int
    interaction_ttl_seconds: int
    notify_events: frozenset[str]

    @classmethod
    def from_mapping(
        cls, settings: Mapping[str, Any], project_root: Path
    ) -> "CodexTasksConfig":
        CodexTasksPlugin.validate_settings(settings)
        projects: Dict[str, CodexProject] = {}
        for raw in settings.get("projects", []):
            raw_path = Path(str(raw["path"])).expanduser()
            path = raw_path if raw_path.is_absolute() else project_root / raw_path
            projects[str(raw["id"])] = CodexProject(str(raw["id"]), path.resolve())
        admins = list(settings.get("admin_tenant_ids", []))
        monitor_tenant_id = str(settings.get("monitor_tenant_id", "") or "")
        if not monitor_tenant_id and len(admins) == 1:
            monitor_tenant_id = str(admins[0])
        return cls(
            admin_tenant_ids=frozenset(admins),
            projects=projects,
            default_project=str(settings.get("default_project", "")),
            max_concurrent_tasks=int(settings.get("max_concurrent_tasks", 1)),
            notify_on_completion=bool(settings.get("notify_on_completion", True)),
            monitor_external_tasks=bool(settings.get("monitor_external_tasks", True)),
            monitor_tenant_id=monitor_tenant_id,
            external_project_scope=str(
                settings.get("external_project_scope", "configured")
            ),
            external_poll_interval_seconds=int(
                settings.get("external_poll_interval_seconds", 15)
            ),
            interaction_ttl_seconds=int(settings.get("interaction_ttl_seconds", 300)),
            notify_events=frozenset(settings.get("notify_events", DEFAULT_NOTIFY_EVENTS)),
        )


class CodexTaskStore:
    """Transactional task metadata stored in BotPlatform's unified database."""

    def __init__(self, tenant_registry: Any, durable_outbox: bool = False) -> None:
        self.registry = tenant_registry
        self.durable_outbox = durable_outbox

    TASK_COLUMNS = (
        "thread_id, tenant_id, project_id, title, status, phase, origin, created_at, "
        "started_at, finished_at, updated_at, last_seen_at, result_excerpt, error, "
        "notification_status, source_cwd"
    )

    def reconcile_interrupted(
        self, notify_interrupted: bool = True
    ) -> List[Dict[str, Any]]:
        now = _utc_now()
        with self.registry.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT thread_id, tenant_id, title FROM codex_task_runs "
                "WHERE origin='botplatform' AND status IN ('queued', 'running')"
            ).fetchall()
            connection.execute(
                "UPDATE codex_task_runs SET status='interrupted', phase='interrupted', "
                "finished_at=?, updated_at=?, "
                "error=COALESCE(error, 'BotPlatform 重启，任务执行状态已中断') "
                "WHERE origin='botplatform' AND status IN ('queued', 'running')",
                (now, now),
            )
            connection.execute(
                "UPDATE codex_task_interactions SET status='cancelled', resolved_at=? "
                "WHERE status='pending'",
                (now,),
            )
            connection.execute(
                "UPDATE codex_task_events SET delivery_status='retry', next_attempt_at=? "
                "WHERE delivery_status='sending'",
                (now,),
            )
            for row in rows:
                message = (
                    "Codex 开发任务已中断：{}\n任务编号：{}\n"
                    "BotPlatform 重启，原确认通道已关闭；可从微信继续该任务。"
                ).format(row["title"], row["thread_id"])
                connection.execute(
                    "INSERT OR IGNORE INTO codex_task_events("
                    "event_key, thread_id, tenant_id, event_type, message, "
                    "delivery_status, created_at) "
                    "VALUES (?, ?, ?, 'interrupted', ?, ?, ?)",
                    (
                        "restart:{}".format(row["thread_id"]),
                        row["thread_id"],
                        row["tenant_id"],
                        message,
                        "pending" if notify_interrupted else "disabled",
                        now,
                    ),
                )
        return [dict(row) for row in rows]

    def create(
        self,
        thread_id: str,
        tenant_id: str,
        project_id: str,
        title: str,
        notify: bool,
    ) -> Dict[str, Any]:
        created_at = _utc_now()
        notification_status = "pending" if notify else "disabled"
        with self.registry.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT origin FROM codex_task_runs WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if existing is not None and existing["origin"] == "external":
                connection.execute(
                    "UPDATE codex_task_runs SET tenant_id=?, project_id=?, title=?, "
                    "status='queued', phase='queued', origin='botplatform', created_at=?, "
                    "updated_at=?, last_seen_at=?, notification_status=? WHERE thread_id=?",
                    (
                        tenant_id,
                        project_id,
                        title or "未命名 Codex 任务",
                        created_at,
                        created_at,
                        created_at,
                        notification_status,
                        thread_id,
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO codex_task_runs("
                    "thread_id, tenant_id, project_id, title, status, created_at, "
                    "notification_status, phase, origin, updated_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, 'queued', ?, ?, 'queued', 'botplatform', ?, ?)",
                    (
                        thread_id,
                        tenant_id,
                        project_id,
                        title,
                        created_at,
                        notification_status,
                        created_at,
                        created_at,
                    ),
                )
        return self.get(thread_id) or {}

    def adopt(
        self,
        thread_id: str,
        tenant_id: str,
        project_id: str,
        title: str,
        notify: bool,
    ) -> Dict[str, Any]:
        existing = self.get(thread_id)
        if existing:
            return existing
        return self.create(thread_id, tenant_id, project_id, title, notify)

    def upsert_external(
        self,
        task: Mapping[str, Any],
        tenant_id: str,
        seen_at: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        now = seen_at or _utc_now()
        thread_id = str(task["task_id"])
        status = str(task.get("status", "completed"))
        phase = str(task.get("phase", status))
        with self.registry.database.transaction(immediate=True) as connection:
            previous = connection.execute(
                "SELECT phase FROM codex_task_runs WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            if previous is None:
                connection.execute(
                    "INSERT INTO codex_task_runs("
                    "thread_id, tenant_id, project_id, title, status, phase, origin, "
                    "created_at, updated_at, last_seen_at, notification_status, source_cwd) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'external', ?, ?, ?, 'disabled', ?)",
                    (
                        thread_id,
                        tenant_id,
                        str(task.get("project_id", "")),
                        str(task.get("title", "未命名 Codex 任务")),
                        status,
                        phase,
                        str(task.get("created_at") or now),
                        str(task.get("updated_at") or now),
                        now,
                        str(task.get("source_cwd") or "") or None,
                    ),
                )
                previous_phase = None
            else:
                previous_phase = str(previous["phase"])
                connection.execute(
                    "UPDATE codex_task_runs SET project_id=?, title=?, status=?, phase=?, "
                    "updated_at=?, last_seen_at=?, source_cwd=COALESCE(?, source_cwd) "
                    "WHERE thread_id=? AND origin='external'",
                    (
                        str(task.get("project_id", "")),
                        str(task.get("title", "未命名 Codex 任务")),
                        status,
                        phase,
                        str(task.get("updated_at") or now),
                        now,
                        str(task.get("source_cwd") or "") or None,
                        thread_id,
                    ),
                )
        return self.get(thread_id) or {}, previous_phase

    def upsert_external_hook(
        self,
        *,
        thread_id: str,
        tenant_id: str,
        project_id: str,
        source_cwd: str,
        title: str,
        status: str,
        phase: str,
        result_excerpt: str = "",
    ) -> Tuple[Dict[str, Any], Optional[str], bool]:
        """Persist one external hook transition, including terminal-to-running turns."""

        now = _utc_now()
        with self.registry.database.transaction(immediate=True) as connection:
            previous = connection.execute(
                "SELECT origin, phase FROM codex_task_runs WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            if previous is not None and previous["origin"] == "botplatform":
                row = connection.execute(
                    "SELECT {} FROM codex_task_runs WHERE thread_id=?".format(
                        self.TASK_COLUMNS
                    ),
                    (thread_id,),
                ).fetchone()
                return (dict(row) if row is not None else {}), str(previous["phase"]), False

            started_at = now if status == "running" else None
            finished_at = now if status in TERMINAL_STATUSES else None
            notification_status = (
                "pending" if status in TERMINAL_STATUSES else "disabled"
            )
            if previous is None:
                connection.execute(
                    "INSERT INTO codex_task_runs("
                    "thread_id, tenant_id, project_id, title, status, phase, origin, "
                    "created_at, started_at, finished_at, updated_at, last_seen_at, "
                    "result_excerpt, notification_status, source_cwd) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'external', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        thread_id,
                        tenant_id,
                        project_id,
                        title or "未命名 Codex 任务",
                        status,
                        phase,
                        now,
                        started_at,
                        finished_at,
                        now,
                        now,
                        result_excerpt or None,
                        notification_status,
                        source_cwd,
                    ),
                )
                previous_phase = None
            else:
                previous_phase = str(previous["phase"])
                connection.execute(
                    "UPDATE codex_task_runs SET tenant_id=?, project_id=?, source_cwd=?, "
                    "title=CASE WHEN ?<>'' THEN ? ELSE title END, status=?, phase=?, "
                    "started_at=CASE WHEN ?='running' THEN COALESCE(started_at, ?) "
                    "ELSE started_at END, "
                    "finished_at=CASE WHEN ? IN ('completed','failed','interrupted') "
                    "THEN ? ELSE NULL END, "
                    "result_excerpt=CASE WHEN ?='running' THEN NULL "
                    "WHEN ?<>'' THEN ? ELSE result_excerpt END, "
                    "notification_status=?, updated_at=?, last_seen_at=? "
                    "WHERE thread_id=? AND origin='external'",
                    (
                        tenant_id,
                        project_id,
                        source_cwd,
                        title,
                        title,
                        status,
                        phase,
                        status,
                        now,
                        status,
                        now,
                        status,
                        result_excerpt,
                        result_excerpt or None,
                        notification_status,
                        now,
                        now,
                        thread_id,
                    ),
                )
            row = connection.execute(
                "SELECT {} FROM codex_task_runs WHERE thread_id=?".format(
                    self.TASK_COLUMNS
                ),
                (thread_id,),
            ).fetchone()
        return (dict(row) if row is not None else {}), previous_phase, True

    def requeue(self, thread_id: str, notify: bool) -> None:
        now = _utc_now()
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE codex_task_runs SET status='queued', phase='queued', started_at=NULL, "
                "finished_at=NULL, result_excerpt=NULL, error=NULL, notification_status=? "
                ", origin='botplatform', updated_at=?, last_seen_at=? WHERE thread_id=?",
                ("pending" if notify else "disabled", now, now, thread_id),
            )

    def mark_running(self, thread_id: str) -> None:
        now = _utc_now()
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE codex_task_runs SET status='running', phase='running', started_at=?, "
                "finished_at=NULL, error=NULL, updated_at=?, last_seen_at=? WHERE thread_id=?",
                (now, now, now, thread_id),
            )

    def mark_phase(self, thread_id: str, phase: str) -> None:
        if phase not in ACTIVE_PHASES:
            raise ValueError("无效的 Codex 活动阶段")
        now = _utc_now()
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE codex_task_runs SET phase=?, updated_at=?, last_seen_at=? "
                "WHERE thread_id=? AND status IN ('queued', 'running')",
                (phase, now, now, thread_id),
            )

    def finish(
        self,
        thread_id: str,
        status: str,
        result_excerpt: str = "",
        error: str = "",
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError("无效的 Codex 任务终态：{}".format(status))
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE codex_task_runs SET status=?, phase=?, finished_at=?, updated_at=?, "
                "last_seen_at=?, result_excerpt=?, error=? WHERE thread_id=?",
                (
                    status,
                    status,
                    _utc_now(),
                    _utc_now(),
                    _utc_now(),
                    result_excerpt or None,
                    error or None,
                    thread_id,
                ),
            )

    def set_notification(self, thread_id: str, status: str) -> None:
        if status not in {"pending", "sent", "failed", "disabled"}:
            raise ValueError("无效的通知状态")
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE codex_task_runs SET notification_status=? WHERE thread_id=?",
                (status, thread_id),
            )

    def get(self, thread_id: str) -> Optional[Dict[str, Any]]:
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT {} FROM codex_task_runs WHERE thread_id=?".format(
                    self.TASK_COLUMNS
                ),
                (thread_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list(
        self,
        tenant_id: str,
        limit: int,
        statuses: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        selected = list(statuses or [])
        parameters: List[Any] = [tenant_id]
        condition = ""
        if selected:
            condition = " AND status IN ({})".format(",".join("?" for _ in selected))
            parameters.extend(selected)
        parameters.append(limit)
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT {} FROM codex_task_runs WHERE tenant_id=?{} "
                "ORDER BY created_at DESC LIMIT ?".format(
                    self.TASK_COLUMNS, condition
                ),
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_interaction(
        self,
        thread_id: str,
        tenant_id: str,
        method: str,
        payload: Mapping[str, Any],
        ttl_seconds: int,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        turn_id = str(payload.get("turnId", ""))
        item_id = str(payload.get("itemId", ""))
        kind = "user_input" if method == USER_INPUT_METHOD else "approval"
        with self.registry.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM codex_task_interactions WHERE "
                "thread_id=? AND turn_id=? AND item_id=? AND method=?",
                (thread_id, turn_id, item_id, method),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            for _attempt in range(20):
                interaction_id = uuid.uuid4().hex[:8].upper()
                duplicate = connection.execute(
                    "SELECT 1 FROM codex_task_interactions WHERE interaction_id=?",
                    (interaction_id,),
                ).fetchone()
                if duplicate is None:
                    break
            else:
                raise RuntimeError("无法生成 Codex 确认短编号")
            created_at = now.isoformat()
            expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
            connection.execute(
                "INSERT INTO codex_task_interactions("
                "interaction_id, thread_id, tenant_id, turn_id, item_id, kind, method, "
                "payload_json, status, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    interaction_id,
                    thread_id,
                    tenant_id,
                    turn_id,
                    item_id,
                    kind,
                    method,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    created_at,
                    expires_at,
                ),
            )
            connection.execute(
                "UPDATE codex_task_runs SET phase=?, updated_at=?, last_seen_at=? "
                "WHERE thread_id=?",
                (
                    "waiting_input" if kind == "user_input" else "waiting_approval",
                    created_at,
                    created_at,
                    thread_id,
                ),
            )
        return self.get_interaction(interaction_id) or {}

    def get_interaction(self, interaction_id: str) -> Optional[Dict[str, Any]]:
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM codex_task_interactions WHERE interaction_id=?",
                (interaction_id.upper(),),
            ).fetchone()
        return dict(row) if row is not None else None

    def pending_interaction(self, thread_id: str) -> Optional[Dict[str, Any]]:
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT interaction_id, kind, method, created_at, expires_at "
                "FROM codex_task_interactions WHERE thread_id=? AND status='pending' "
                "ORDER BY created_at DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def resolve_interaction(
        self,
        interaction_id: str,
        tenant_id: str,
        status: str,
        response: Mapping[str, Any],
        persist_response: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if status not in {"approved", "declined", "answered", "expired", "cancelled"}:
            raise ValueError("无效的 Codex 交互状态")
        now = _utc_now()
        with self.registry.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM codex_task_interactions WHERE interaction_id=? "
                "AND tenant_id=?",
                (interaction_id.upper(), tenant_id),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return None
            expires_at = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
            current = datetime.now(timezone.utc)
            selected_status = status if current < expires_at else "expired"
            cursor = connection.execute(
                "UPDATE codex_task_interactions SET status=?, response_json=?, resolved_at=? "
                "WHERE interaction_id=? AND status='pending'",
                (
                    selected_status,
                    (
                        json.dumps(response, ensure_ascii=False, sort_keys=True)
                        if persist_response
                        else None
                    ),
                    now,
                    interaction_id.upper(),
                ),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_interaction(interaction_id)

    def enqueue_event(
        self,
        event_key: str,
        thread_id: str,
        tenant_id: str,
        event_type: str,
        message: str,
        enabled: bool = True,
    ) -> Dict[str, Any]:
        created_at = _utc_now()
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO codex_task_events("
                "event_key, thread_id, tenant_id, event_type, message, delivery_status, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event_key,
                    thread_id,
                    tenant_id,
                    event_type,
                    message,
                    "pending" if enabled else "disabled",
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM codex_task_events WHERE event_key=?", (event_key,)
            ).fetchone()
            if (
                row is not None
                and enabled
                and self.durable_outbox
                and row["delivery_status"] in ("pending", "retry")
            ):
                notification_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT OR IGNORE INTO notification_outbox("
                    "notification_id, tenant_id, batch_id, batch_position, "
                    "source_type, source_key, source_ref, kind, text_payload, "
                    "delivery_status, created_at) "
                    "VALUES (?, ?, ?, 0, 'codex', ?, ?, 'text', ?, 'pending', ?)",
                    (
                        notification_id,
                        tenant_id,
                        notification_id,
                        event_key,
                        str(row["event_id"]),
                        message,
                        str(row["created_at"] or created_at),
                    ),
                )
                connection.execute(
                    "UPDATE codex_task_events SET delivery_status='sending', "
                    "next_attempt_at=NULL WHERE event_id=?",
                    (row["event_id"],),
                )
                row = connection.execute(
                    "SELECT * FROM codex_task_events WHERE event_id=?",
                    (row["event_id"],),
                ).fetchone()
        return dict(row) if row is not None else {}

    def claim_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        with self.registry.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE codex_task_events SET delivery_status='sending' "
                "WHERE event_id=? AND delivery_status IN ('pending', 'retry')",
                (event_id,),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM codex_task_events WHERE event_id=?", (event_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def finish_event_delivery(
        self,
        event_id: int,
        success: bool,
        error: str = "",
        retry_delay_seconds: Optional[int] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.registry.database.transaction(immediate=True) as connection:
            if success:
                connection.execute(
                    "UPDATE codex_task_events SET delivery_status='sent', "
                    "attempt_count=attempt_count+1, sent_at=?, next_attempt_at=NULL, "
                    "last_error=NULL WHERE event_id=?",
                    (now.isoformat(), event_id),
                )
            else:
                next_at = (
                    (now + timedelta(seconds=retry_delay_seconds)).isoformat()
                    if retry_delay_seconds is not None
                    else None
                )
                connection.execute(
                    "UPDATE codex_task_events SET delivery_status=?, "
                    "attempt_count=attempt_count+1, next_attempt_at=?, last_error=? "
                    "WHERE event_id=?",
                    (
                        "retry" if retry_delay_seconds is not None else "failed",
                        next_at,
                        error[:1000] or None,
                        event_id,
                    ),
                )

    def wait_event_for_recipient(self, event_id: int, error: str) -> None:
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE codex_task_events SET delivery_status='waiting_recipient', "
                "attempt_count=attempt_count+1, next_attempt_at=NULL, last_error=? "
                "WHERE event_id=?",
                (error[:1000] or None, event_id),
            )

    def requeue_waiting_recipient(self, tenant_id: str) -> int:
        with self.registry.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE codex_task_events SET delivery_status='pending', "
                "next_attempt_at=NULL WHERE tenant_id=? "
                "AND delivery_status='waiting_recipient'",
                (tenant_id,),
            )
            connection.execute(
                "UPDATE codex_task_runs SET notification_status='pending' "
                "WHERE tenant_id=? AND thread_id IN ("
                "SELECT thread_id FROM codex_task_events "
                "WHERE tenant_id=? AND delivery_status='pending' "
                "AND event_type IN ('completed','failed','interrupted'))",
                (tenant_id, tenant_id),
            )
            return int(cursor.rowcount)

    def collapse_pending_legacy_hook_events(self, tenant_id: Optional[str] = None) -> int:
        """Disable old per-tool hook duplicates before they reach WeChat.

        New hook notifications use a phase-level key. This one-time-compatible
        cleanup handles events written by the earlier per-tool key format.
        """

        parameters: List[Any] = []
        condition = ""
        if tenant_id:
            condition = " AND tenant_id=?"
            parameters.append(tenant_id)
        with self.registry.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT event_id, thread_id, event_type, event_key FROM "
                "codex_task_events WHERE delivery_status IN "
                "('pending', 'retry', 'waiting_recipient') "
                "AND event_key LIKE 'hook:%'{} ORDER BY event_id".format(condition),
                tuple(parameters),
            ).fetchall()
            seen: set[Tuple[str, str, str, str]] = set()
            duplicate_ids: List[int] = []
            for row in rows:
                parts = str(row["event_key"]).split(":")
                if len(parts) < 5:
                    continue
                group = (
                    str(row["thread_id"]),
                    parts[1],
                    parts[2],
                    str(row["event_type"]),
                )
                if group in seen:
                    duplicate_ids.append(int(row["event_id"]))
                else:
                    seen.add(group)
            for event_id in duplicate_ids:
                connection.execute(
                    "UPDATE codex_task_events SET delivery_status='disabled', "
                    "next_attempt_at=NULL, last_error='已合并重复 Hook 通知' "
                    "WHERE event_id=?",
                    (event_id,),
                )
                connection.execute(
                    "UPDATE notification_outbox SET delivery_status='cancelled', "
                    "next_attempt_at=NULL, lease_expires_at=NULL, "
                    "last_error='已合并重复 Hook 通知' "
                    "WHERE source_type='codex' AND source_ref=? "
                    "AND delivery_status IN "
                    "('pending','sending','retry','waiting_recipient')",
                    (str(event_id),),
                )
        return len(duplicate_ids)

    def latest_event(self, thread_id: str) -> Optional[Dict[str, Any]]:
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT event_type, delivery_status, attempt_count, last_error, "
                "created_at, sent_at FROM codex_task_events WHERE thread_id=? "
                "ORDER BY event_id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def due_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        now = _utc_now()
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT event.* FROM codex_task_events AS event WHERE ("
                "event.delivery_status='pending' OR "
                "(event.delivery_status='retry' AND event.next_attempt_at<=?)) "
                "AND NOT EXISTS (SELECT 1 FROM codex_task_events AS earlier "
                "WHERE earlier.thread_id=event.thread_id "
                "AND earlier.event_id<event.event_id "
                "AND earlier.delivery_status IN ("
                "'pending', 'sending', 'retry', 'waiting_recipient')) "
                "ORDER BY event.event_id LIMIT ?",
                (now, limit),
            ).fetchall()
        return [dict(row) for row in rows]


class CodexHookInputError(ValueError):
    """Raised for malformed or out-of-scope Codex hook input."""


class CodexHookIngestor:
    """Validate Codex lifecycle hook input and persist notification events."""

    def __init__(self, config: CodexTasksConfig, tenant_registry: Any) -> None:
        self.config = config
        self.store = CodexTaskStore(tenant_registry, durable_outbox=True)

    @staticmethod
    def _required_text(payload: Mapping[str, Any], key: str, maximum: int = 1000) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CodexHookInputError("{} 必须是非空字符串".format(key))
        return value.strip()[:maximum]

    @staticmethod
    def _short_hash(value: str, length: int = 16) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]

    def _project_for_cwd(self, raw_cwd: str) -> Tuple[str, str]:
        cwd_path = Path(raw_cwd).expanduser()
        if not cwd_path.is_absolute():
            raise CodexHookInputError("cwd 必须是绝对路径")
        cwd = cwd_path.resolve(strict=False)
        matches = [
            project
            for project in self.config.projects.values()
            if cwd == project.path or project.path in cwd.parents
        ]
        if matches:
            project = max(matches, key=lambda item: len(str(item.path)))
            return project.id, str(cwd)
        if self.config.external_project_scope != "all":
            raise CodexHookInputError("cwd 不在外部监控范围内")
        return "external-{}".format(self._short_hash(str(cwd), 12)), str(cwd)

    @staticmethod
    def _tool_summary(tool_name: str, tool_input: Any) -> str:
        value = tool_input if isinstance(tool_input, Mapping) else {}
        if tool_name == "request_user_input":
            questions = list(value.get("questions") or [])
            lines = [
                str(_value(question, "question", "") or "").strip()[:300]
                for question in questions
            ]
            return "\n".join(line for line in lines if line) or "Codex 需要补充信息"
        for key in ("description", "reason", "command"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()[:800]
        return "Codex 请求使用工具：{}".format(tool_name or "未知工具")

    @staticmethod
    def _phase_for_event(
        event_name: str, tool_name: str
    ) -> Tuple[str, str]:
        if event_name == "UserPromptSubmit":
            return "running", "running"
        if event_name == "PermissionRequest":
            return "running", "waiting_approval"
        if event_name == "PreToolUse" and tool_name == "request_user_input":
            return "running", "waiting_input"
        if event_name == "PostToolUse" and tool_name == "request_user_input":
            return "running", "running"
        if event_name == "Stop":
            return "completed", "completed"
        raise CodexHookInputError("不支持的 hook 事件或工具")

    def _notification_key(self, session_id: str, turn_id: str, phase: str) -> str:
        """One user-facing notification per lifecycle phase in each turn."""

        return "hook-phase:{}:{}:{}".format(
            self._short_hash(session_id),
            self._short_hash(turn_id),
            phase,
        )

    @staticmethod
    def _message(
        task: Mapping[str, Any],
        phase: str,
        detail: str,
    ) -> str:
        labels = {
            "running": "开始执行",
            "waiting_approval": "等待审批",
            "waiting_input": "等待回答",
            "completed": "已完成",
        }
        suffix = (
            "\n该任务由其他 Codex 客户端发起，请回原 Codex 界面处理；"
            "微信不能代为批准。"
            if phase == "waiting_approval"
            else "\n该任务由其他 Codex 客户端发起，请回原 Codex 界面回答。"
            if phase == "waiting_input"
            else ""
        )
        detail_line = "\n{}".format(detail[:1200]) if detail else ""
        return "Codex 外部任务{}：{}\n项目：{}\n任务编号：{}{}{}".format(
            labels.get(phase, phase),
            task.get("title") or "未命名 Codex 任务",
            task.get("project_id"),
            task.get("thread_id"),
            detail_line,
            suffix,
        )

    def ingest(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise CodexHookInputError("hook 输入必须是 JSON 对象")
        event_name = self._required_text(payload, "hook_event_name", 80)
        if event_name not in HOOK_EVENTS:
            raise CodexHookInputError("不支持的 hook_event_name")
        session_id = self._required_text(payload, "session_id", 200)
        turn_id = self._required_text(payload, "turn_id", 200)
        project_id, source_cwd = self._project_for_cwd(
            self._required_text(payload, "cwd", 4096)
        )
        if not self.config.monitor_tenant_id:
            raise CodexHookInputError("未配置外部任务通知租户")
        tool_name = str(payload.get("tool_name") or "").strip()
        status, phase = self._phase_for_event(event_name, tool_name)

        prompt = str(payload.get("prompt") or "").strip()
        final_message = str(payload.get("last_assistant_message") or "").strip()
        title = (
            prompt.splitlines()[0][:200]
            if prompt
            else source_cwd.rstrip("/").rsplit("/", 1)[-1][:200]
        )
        detail = ""
        if event_name in {"PermissionRequest", "PreToolUse"}:
            detail = self._tool_summary(tool_name, payload.get("tool_input"))
        elif event_name == "Stop":
            detail = final_message[:1200]

        if event_name not in {"UserPromptSubmit", "Stop"}:
            stop_key = self._notification_key(session_id, turn_id, "completed")
            with self.store.registry.database.read() as connection:
                stopped = connection.execute(
                    "SELECT 1 FROM codex_task_events WHERE event_key=?",
                    (stop_key,),
                ).fetchone()
            if stopped is not None:
                return {"accepted": True, "ignored": "turn_already_stopped"}

        task, _previous_phase, accepted = self.store.upsert_external_hook(
            thread_id=session_id,
            tenant_id=self.config.monitor_tenant_id,
            project_id=project_id,
            source_cwd=source_cwd,
            title=title if event_name == "UserPromptSubmit" else "",
            status=status,
            phase=phase,
            result_excerpt=final_message[:6000] if event_name == "Stop" else "",
        )
        if not accepted:
            return {"accepted": True, "ignored": "botplatform_owned"}
        event = self.store.enqueue_event(
            self._notification_key(session_id, turn_id, phase),
            session_id,
            self.config.monitor_tenant_id,
            phase,
            self._message(task, phase, detail),
            # Hooks observe tasks owned by another Codex client. They do not
            # expose that client's live request/response channel, so a
            # waiting interaction cannot be answered safely from WeChat.
            enabled=(
                phase in self.config.notify_events and phase not in WAITING_PHASES
            ),
        )
        return {
            "accepted": True,
            "thread_id": session_id,
            "event_id": event.get("event_id"),
            "delivery_status": event.get("delivery_status"),
        }


@dataclass
class _InteractionWaiter:
    event: threading.Event
    response: Optional[Dict[str, Any]] = None


class _InteractiveCodexSession:
    """Small façade over the pinned SDK client with user-routed approvals."""

    def __init__(self, approval_handler: Callable[[str, Optional[Dict[str, Any]]], Dict[str, Any]]):
        from openai_codex.client import CodexClient

        self._client = CodexClient(approval_handler=approval_handler)
        try:
            self._client.start()
            self._client.initialize()
        except Exception:
            self._client.close()
            raise

    @staticmethod
    def _params(cwd: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "sandbox": "workspace-write",
        }
        if cwd:
            params["cwd"] = cwd
        return params

    def thread_start(self, *, cwd: str, **_kwargs: Any) -> Any:
        from openai_codex import Thread

        started = self._client.thread_start(self._params(cwd))
        return Thread(self._client, started.thread.id)

    def thread_resume(self, thread_id: str, **_kwargs: Any) -> Any:
        from openai_codex import Thread

        resumed = self._client.thread_resume(thread_id, self._params())
        return Thread(self._client, resumed.thread.id)

    def close(self) -> None:
        self._client.close()


class CodexTaskService:
    """Run owned turns, route interactions, and reconcile external Codex threads."""

    SOURCE_KINDS = ["cli", "vscode", "exec", "appServer"]
    RESULT_LIMIT = 6000
    # The WeChat gateway can briefly return ``prepare failed`` when two
    # lifecycle messages follow each other immediately. Retry quickly first;
    # the store keeps later events behind earlier unsent events per task.
    RETRY_DELAYS = (2, 10, 30, 120, 600)

    def __init__(
        self,
        config: CodexTasksConfig,
        context: PluginContext,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.config = config
        self.context = context
        service = context.notification_service
        self.store = CodexTaskStore(
            context.tenant_registry,
            durable_outbox=callable(
                getattr(service, "enqueue_text_to_tenant", None)
            ),
        )
        self.store.reconcile_interrupted(
            config.notify_on_completion and "interrupted" in config.notify_events
        )
        self.store.collapse_pending_legacy_hook_events()
        self._client_factory = client_factory
        self._client: Any = None
        self._client_lock = threading.RLock()
        self._lock = threading.RLock()
        self._active: Dict[str, Any] = {}
        self._task_sessions: Dict[str, Any] = {}
        self._waiters: Dict[str, _InteractionWaiter] = {}
        self._cancelled: set[str] = set()
        self._suppress_notifications: set[str] = set()
        self._notification_lock = threading.Lock()
        self._watchdog_stop = threading.Event()
        self._external_baseline_done = False
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=config.max_concurrent_tasks,
            thread_name_prefix="codex-task",
        )
        self._watchdog = threading.Thread(
            target=self._watchdog_loop,
            name="codex-watchdog",
            daemon=True,
        )
        self._watchdog.start()

    @property
    def sdk_importable(self) -> bool:
        if self._client_factory is not None:
            return True
        try:
            return importlib.util.find_spec("openai_codex") is not None
        except (ImportError, ModuleNotFoundError):
            return False

    def _new_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        try:
            from openai_codex import Codex
        except ImportError as exc:
            raise PluginError("缺少 openai-codex，请先安装项目依赖") from exc
        try:
            return Codex()
        except Exception as exc:
            raise PluginError("无法启动 Codex：{}".format(self._safe_error(exc))) from exc

    def _new_task_session(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        try:
            return _InteractiveCodexSession(self._handle_server_request)
        except ImportError as exc:
            raise PluginError("缺少 openai-codex，请先安装项目依赖") from exc
        except Exception as exc:
            raise PluginError("无法启动 Codex：{}".format(self._safe_error(exc))) from exc

    def _get_client(self) -> Any:
        with self._client_lock:
            if self._closed:
                raise PluginError("Codex 任务插件已经关闭")
            if self._client is None:
                self._client = self._new_client()
            return self._client

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        message = re.sub(
            r"(?i)(authorization\s*:\s*bearer\s+)\S+",
            r"\1<redacted>",
            message,
        )
        message = re.sub(
            r"(?i)((?:context[_-]?token|api[_-]?key|access[_-]?token|token)"
            r"\s*[=:]\s*)[^,\s&]+",
            r"\1<redacted>",
            message,
        )
        return message[:1000]

    @staticmethod
    def _sandbox_option() -> Any:
        try:
            from openai_codex import Sandbox

            return Sandbox.workspace_write
        except ImportError:
            return "workspace_write"

    @staticmethod
    def _thread_id(thread: Any) -> str:
        thread_id = str(_value(thread, "id", "") or "")
        if not thread_id:
            raise PluginError("Codex 没有返回有效的任务编号")
        return thread_id

    def project(self, project_id: Optional[str]) -> CodexProject:
        selected = project_id or self.config.default_project
        project = self.config.projects.get(selected)
        if project is None:
            raise PluginError("未知或未开放的 Codex 项目：{}".format(selected))
        if not project.path.is_dir():
            raise PluginError("Codex 项目目录不存在：{}".format(project.id))
        return project

    def _thread_start(self, project: CodexProject) -> Tuple[Any, Any]:
        session = self._new_task_session()
        try:
            thread = session.thread_start(
                cwd=str(project.path),
            )
            return session, thread
        except PluginError:
            self._close_task_session(session)
            raise
        except Exception as exc:
            self._close_task_session(session)
            raise PluginError("创建 Codex 任务失败：{}".format(self._safe_error(exc))) from exc

    def _close_task_session(self, session: Any) -> None:
        if self._client_factory is not None:
            return
        try:
            session.close()
        except Exception:  # noqa: BLE001 - best effort on session cleanup
            LOGGER.warning("关闭 Codex 任务会话失败", exc_info=True)

    def create_task(
        self,
        tenant_id: str,
        title: str,
        instruction: str,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        project = self.project(project_id)
        session, thread = self._thread_start(project)
        thread_id = self._thread_id(thread)
        try:
            setter = getattr(thread, "set_name", None)
            if callable(setter):
                setter(title)
            task = self.store.create(
                thread_id,
                tenant_id,
                project.id,
                title,
                self.config.notify_on_completion,
            )
            self._notify_phase(thread_id, "queued")
            with self._lock:
                self._task_sessions[thread_id] = session
            self._executor.submit(
                self._execute_turn, session, thread, thread_id, instruction
            )
        except Exception as exc:
            self._close_task_session(session)
            if self.store.get(thread_id):
                self.store.finish(thread_id, "failed", error=self._safe_error(exc))
            raise PluginError("提交 Codex 任务失败：{}".format(self._safe_error(exc))) from exc
        return self._summary(task)

    def _execute_turn(
        self, session: Any, thread: Any, thread_id: str, instruction: str
    ) -> None:
        with self._lock:
            if self._closed or thread_id in self._cancelled:
                self.store.finish(thread_id, "interrupted", error="任务在开始前已被中止")
                self._task_sessions.pop(thread_id, None)
                self._close_task_session(session)
                self._notify(thread_id)
                return
        self.store.mark_running(thread_id)
        self._notify_phase(thread_id, "running")
        handle: Any = None
        result: Any = None
        status = "completed"
        error = ""
        try:
            handle = thread.turn(
                instruction,
                sandbox=self._sandbox_option(),
            )
            with self._lock:
                self._active[thread_id] = handle
                cancelled = thread_id in self._cancelled
            if cancelled:
                handle.interrupt()
            result = handle.run()
            raw_status = _enum_text(_value(result, "status", "completed")).lower()
            raw_error = _value(result, "error")
            with self._lock:
                cancelled = thread_id in self._cancelled
            if cancelled or "interrupt" in raw_status:
                status = "interrupted"
            elif raw_error or "fail" in raw_status:
                status = "failed"
                error = self._safe_error(
                    raw_error if isinstance(raw_error, Exception) else RuntimeError(str(raw_error))
                )
        except Exception as exc:
            with self._lock:
                cancelled = thread_id in self._cancelled
            status = "interrupted" if cancelled else "failed"
            error = self._safe_error(exc)
        finally:
            with self._lock:
                self._active.pop(thread_id, None)
                self._task_sessions.pop(thread_id, None)
            self._close_task_session(session)
        final_response = str(_value(result, "final_response", "") or "")
        self.store.finish(
            thread_id,
            status,
            result_excerpt=final_response[: self.RESULT_LIMIT],
            error=error,
        )
        self._notify(thread_id)

    def _notify_phase(self, thread_id: str, phase: str) -> None:
        if phase not in ACTIVE_PHASES:
            raise ValueError("无效的 Codex 活动阶段")
        task = self.store.get(thread_id)
        if not task or phase not in self.config.notify_events:
            return
        labels = {
            "queued": "已排队",
            "running": "开始执行",
            "waiting_approval": "等待审批",
            "waiting_input": "等待回答",
        }
        message = "Codex 开发任务{}：{}\n项目：{}\n任务编号：{}".format(
            labels.get(phase, phase),
            task["title"],
            task["project_id"],
            thread_id,
        )
        event = self.store.enqueue_event(
            "phase:{}:{}:{}".format(
                thread_id, phase, task.get("updated_at") or _utc_now()
            ),
            thread_id,
            str(task["tenant_id"]),
            phase,
            message,
        )
        self._deliver_event(event)

    def _notify(self, thread_id: str) -> None:
        task = self.store.get(thread_id)
        if not task or task["notification_status"] != "pending":
            return
        with self._lock:
            if thread_id in self._suppress_notifications:
                self.store.set_notification(thread_id, "disabled")
                return
        labels = {
            "completed": "已完成",
            "failed": "失败",
            "interrupted": "已中断",
        }
        details = task.get("result_excerpt") or task.get("error") or "无结果摘要"
        message = "Codex 开发任务{}：{}\n任务编号：{}\n{}".format(
            labels.get(task["status"], task["status"]),
            task["title"],
            thread_id,
            str(details)[:2000],
        )
        event_type = str(task["status"])
        enabled = (
            self.config.notify_on_completion
            and event_type in self.config.notify_events
        )
        event = self.store.enqueue_event(
            "terminal:{}:{}".format(thread_id, task.get("finished_at") or ""),
            thread_id,
            str(task["tenant_id"]),
            event_type,
            message,
            enabled=enabled,
        )
        if not enabled:
            self.store.set_notification(thread_id, "disabled")
            return
        self._deliver_event(event)
        refreshed = self.store.get(thread_id)
        if refreshed:
            with self.context.tenant_registry.database.read() as connection:
                row = connection.execute(
                    "SELECT delivery_status FROM codex_task_events WHERE event_id=?",
                    (event.get("event_id"),),
                ).fetchone()
            self.store.set_notification(
                thread_id,
                (
                    "sent"
                    if row and row["delivery_status"] == "sent"
                    else "failed"
                    if row and row["delivery_status"] == "failed"
                    else "pending"
                ),
            )

    @staticmethod
    def _safe_unknown_response(method: str) -> Dict[str, Any]:
        if method == USER_INPUT_METHOD:
            return {"answers": {}}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        if method.endswith("requestApproval"):
            return {"decision": "decline"}
        return {}

    @staticmethod
    def _request_response(
        method: str,
        payload: Mapping[str, Any],
        action: str,
        answers: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if method == USER_INPUT_METHOD:
            return {"answers": dict(answers or {})}
        if method == "item/permissions/requestApproval":
            return {
                "permissions": dict(payload.get("permissions") or {})
                if action == "approve"
                else {},
                "scope": "turn",
            }
        return {"decision": "accept" if action == "approve" else "decline"}

    @staticmethod
    def _request_summary(method: str, payload: Mapping[str, Any]) -> str:
        if method == USER_INPUT_METHOD:
            questions = list(payload.get("questions") or [])
            lines: List[str] = []
            for index, question in enumerate(questions, 1):
                text = str(_value(question, "question", "") or "")[:400]
                options = list(_value(question, "options", []) or [])
                if options:
                    labels = [str(_value(option, "label", "")) for option in options]
                    text += "（{}）".format(" / ".join(labels))
                lines.append("{}. {}".format(index, text))
            return "\n".join(lines) or "Codex 需要补充信息"
        if method == "item/commandExecution/requestApproval":
            command = str(payload.get("command") or "")
            reason = str(payload.get("reason") or "")
            return (reason + ("\n" if reason and command else "") + command)[:800]
        if method == "item/fileChange/requestApproval":
            return str(payload.get("reason") or "Codex 请求修改项目文件")[:800]
        if method == "item/permissions/requestApproval":
            reason = str(payload.get("reason") or "Codex 请求额外执行权限")
            return reason[:800]
        return "未知 Codex 请求"

    def _interaction_message(
        self, task: Mapping[str, Any], interaction: Mapping[str, Any]
    ) -> str:
        payload = json.loads(str(interaction["payload_json"]))
        code = str(interaction["interaction_id"])
        ttl = self.config.interaction_ttl_seconds
        if interaction["kind"] == "user_input":
            return (
                "Codex 任务等待回答：{}\n项目：{}\n任务编号：{}\n确认编号：{}\n{}\n"
                "请在 {} 秒内回复：/codex answer {} <答案>\n"
                "多问题格式：1=答案;2=答案"
            ).format(
                task["title"],
                task["project_id"],
                task["thread_id"],
                code,
                self._request_summary(str(interaction["method"]), payload),
                ttl,
                code,
            )
        return (
            "Codex 任务等待审批：{}\n项目：{}\n任务编号：{}\n确认编号：{}\n{}\n"
            "请在 {} 秒内回复：\n/codex approve {}\n/codex deny {}"
        ).format(
            task["title"],
            task["project_id"],
            task["thread_id"],
            code,
            self._request_summary(str(interaction["method"]), payload),
            ttl,
            code,
            code,
        )

    def _handle_server_request(
        self, method: str, params: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        payload = dict(params or {})
        thread_id = str(payload.get("threadId", ""))
        if method not in APPROVAL_METHODS | {USER_INPUT_METHOD}:
            if thread_id:
                with self._lock:
                    self._cancelled.add(thread_id)
            return self._safe_unknown_response(method)
        task = self.store.get(thread_id)
        if not task or task.get("origin") != "botplatform":
            return self._safe_unknown_response(method)
        if not str(payload.get("turnId", "")) or not str(payload.get("itemId", "")):
            with self._lock:
                self._cancelled.add(thread_id)
            return self._safe_unknown_response(method)
        interaction = self.store.create_interaction(
            thread_id,
            str(task["tenant_id"]),
            method,
            payload,
            self.config.interaction_ttl_seconds,
        )
        code = str(interaction["interaction_id"])
        if interaction["status"] != "pending":
            with self._lock:
                waiter = self._waiters.get(code)
                if waiter is None:
                    waiter = _InteractionWaiter(threading.Event())
                    self._waiters[code] = waiter
            waiter.event.wait(1.0)
            with self._lock:
                if self._waiters.get(code) is waiter:
                    self._waiters.pop(code, None)
            if waiter.response is not None:
                return waiter.response
            stored_response = interaction.get("response_json")
            if stored_response:
                try:
                    return dict(json.loads(str(stored_response)))
                except (TypeError, ValueError):
                    pass
            return self._safe_unknown_response(method)
        with self._lock:
            waiter = self._waiters.get(code)
            if waiter is None:
                waiter = _InteractionWaiter(threading.Event())
                self._waiters[code] = waiter
        phase = "waiting_input" if method == USER_INPUT_METHOD else "waiting_approval"
        self.store.mark_phase(thread_id, phase)
        event = self.store.enqueue_event(
            "interaction:{}".format(code),
            thread_id,
            str(task["tenant_id"]),
            phase,
            self._interaction_message(task, interaction),
            enabled=phase in self.config.notify_events,
        )
        self._deliver_event(event)
        resolved = waiter.event.wait(self.config.interaction_ttl_seconds)
        if not resolved:
            response = self._request_response(method, payload, "deny")
            expired = self.store.resolve_interaction(
                code, str(task["tenant_id"]), "expired", response
            )
            if expired is not None:
                waiter.response = response
                timeout_message = (
                    "Codex 确认已超时：{}\n确认编号：{}\n{}"
                ).format(
                    task["title"],
                    code,
                    "任务将被中断。" if method == USER_INPUT_METHOD else "已按拒绝处理。",
                )
                expired_event = self.store.enqueue_event(
                    "interaction-expired:{}".format(code),
                    thread_id,
                    str(task["tenant_id"]),
                    "interaction_expired",
                    timeout_message,
                    enabled=True,
                )
                self._deliver_event(expired_event)
                if method == USER_INPUT_METHOD:
                    with self._lock:
                        self._cancelled.add(thread_id)
                    timer = threading.Timer(0.05, self._interrupt_task, args=(thread_id,))
                    timer.daemon = True
                    timer.start()
        with self._lock:
            if self._waiters.get(code) is waiter:
                self._waiters.pop(code, None)
            cancelled = thread_id in self._cancelled
        if not cancelled:
            self.store.mark_phase(thread_id, "running")
        return waiter.response or self._safe_unknown_response(method)

    def _interrupt_task(self, thread_id: str) -> None:
        with self._lock:
            handle = self._active.get(thread_id)
        if handle is not None:
            try:
                handle.interrupt()
            except Exception:  # noqa: BLE001 - task may have already finished
                LOGGER.warning("中断 Codex 任务 %s 失败", thread_id, exc_info=True)

    def _deliver_event(self, event: Mapping[str, Any]) -> None:
        if not event or event.get("delivery_status") == "disabled":
            return
        event_id = int(event["event_id"])
        with self._notification_lock:
            claimed = self.store.claim_event(event_id)
            if claimed is None:
                return
            service = self.context.notification_service
            if service is None:
                attempt = int(claimed.get("attempt_count", 0))
                delay = self.RETRY_DELAYS[
                    min(attempt, len(self.RETRY_DELAYS) - 1)
                ]
                self.store.finish_event_delivery(
                    event_id, False, "微信通知服务不可用", delay
                )
                return
            enqueue = getattr(service, "enqueue_text_to_tenant", None)
            try:
                if callable(enqueue):
                    enqueue(
                        str(claimed["tenant_id"]),
                        str(claimed["message"]),
                        source_type="codex",
                        source_key=str(claimed["event_key"]),
                        source_ref=str(event_id),
                    )
                else:
                    service.send_text_to_tenant(
                        str(claimed["tenant_id"]), str(claimed["message"])
                    )
            except NotificationRecipientStaleError as exc:
                attempt = int(claimed.get("attempt_count", 0))
                safe_error = self._safe_error(exc)
                if attempt < 2:
                    self.store.finish_event_delivery(
                        event_id, False, safe_error, self.RETRY_DELAYS[attempt]
                    )
                else:
                    self.store.wait_event_for_recipient(event_id, safe_error)
            except (NotificationError, OSError, ValueError) as exc:
                attempt = int(claimed.get("attempt_count", 0))
                safe_error = self._safe_error(exc)
                delay = self.RETRY_DELAYS[
                    min(attempt, len(self.RETRY_DELAYS) - 1)
                ]
                self.store.finish_event_delivery(
                    event_id, False, safe_error, delay
                )
                LOGGER.warning(
                    "Codex 通知入队失败 thread=%s event=%s status=retry error=%s",
                    claimed["thread_id"],
                    claimed["event_type"],
                    safe_error,
                )
            else:
                if callable(enqueue):
                    LOGGER.info(
                        "Codex 通知已持久化 thread=%s event=%s status=sending",
                        claimed["thread_id"],
                        claimed["event_type"],
                    )
                else:
                    self.store.finish_event_delivery(event_id, True)
                    if claimed["event_type"] in TERMINAL_STATUSES:
                        self.store.set_notification(
                            str(claimed["thread_id"]), "sent"
                        )

    def on_recipient_refreshed(self, tenant_id: str) -> int:
        """Wake all durable notifications after an inbound WeChat message."""

        self.store.collapse_pending_legacy_hook_events(tenant_id)
        count = self.store.requeue_waiting_recipient(tenant_id)
        if count:
            LOGGER.info(
                "Codex 通知已重新排队 tenant=%s count=%s",
                tenant_id,
                count,
            )
        return count

    def _watchdog_loop(self) -> None:
        next_external_check = datetime.now(timezone.utc)
        while not self._watchdog_stop.is_set():
            try:
                for event in self.store.due_events():
                    self._deliver_event(event)
                now = datetime.now(timezone.utc)
                if self.config.monitor_external_tasks and now >= next_external_check:
                    self._reconcile_external_tasks()
                    next_external_check = now + timedelta(
                        seconds=self.config.external_poll_interval_seconds
                    )
            except Exception as exc:
                LOGGER.exception(
                    "Codex watchdog 异常，线程将继续运行：%s",
                    self._safe_error(exc),
                )
            self._watchdog_stop.wait(1.0)

    def _external_transition_message(
        self, task: Mapping[str, Any], phase: str
    ) -> str:
        labels = {
            "queued": "已排队",
            "running": "开始执行",
            "waiting_approval": "等待审批",
            "waiting_input": "等待回答",
            "completed": "已完成",
            "failed": "失败",
            "interrupted": "已中断",
        }
        suffix = (
            "\n该任务由其他 Codex 客户端发起，请回原 Codex 端处理。"
            if phase in WAITING_PHASES
            else ""
        )
        return "Codex 外部任务{}：{}\n项目：{}\n任务编号：{}{}".format(
            labels.get(phase, phase),
            task.get("title"),
            task.get("project_id"),
            task.get("task_id") or task.get("thread_id"),
            suffix,
        )

    def _reconcile_external_tasks(self) -> None:
        if not self.config.monitor_tenant_id:
            return
        tasks = self._sdk_threads(100)
        baseline = not self._external_baseline_done
        for task in tasks:
            task_id = str(task["task_id"])
            existing = self.store.get(task_id)
            if existing is not None and existing.get("origin") == "botplatform":
                continue
            # App-server state is process-local. Polling therefore supplies
            # discoverable metadata only; hooks own lifecycle transitions.
            # A reported systemError is the sole terminal fallback.
            if task.get("phase") != "failed":
                continue
            stored, previous_phase = self.store.upsert_external(
                task, self.config.monitor_tenant_id
            )
            phase = str(stored.get("phase", task.get("phase", "completed")))
            if baseline or previous_phase == phase:
                continue
            if phase not in self.config.notify_events:
                continue
            event = self.store.enqueue_event(
                "external:{}:{}:{}".format(
                    task_id, phase, task.get("updated_at") or _utc_now()
                ),
                task_id,
                self.config.monitor_tenant_id,
                phase,
                self._external_transition_message(task, phase),
            )
            self._deliver_event(event)
        self._external_baseline_done = True

    def _sdk_threads(self, limit: int) -> List[Dict[str, Any]]:
        client = self._get_client()
        results: Dict[str, Dict[str, Any]] = {}
        for project in self.config.projects.values():
            try:
                response = client.thread_list(
                    cwd=str(project.path),
                    limit=limit,
                    source_kinds=list(self.SOURCE_KINDS),
                    sort_key="updated_at",
                    use_state_db_only=True,
                )
            except Exception as exc:
                raise PluginError("读取 Codex 任务列表失败：{}".format(self._safe_error(exc))) from exc
            for thread in list(_value(response, "data", []) or []):
                thread_id = str(_value(thread, "id", "") or "")
                if not thread_id:
                    continue
                status_value = _value(thread, "status", {})
                status_root = _value(status_value, "root", status_value)
                status_type = _enum_text(_value(status_root, "type", "notLoaded"))
                active_flags = {
                    _enum_text(item)
                    for item in list(
                        _value(status_root, "active_flags", _value(status_root, "activeFlags", []))
                        or []
                    )
                }
                status, phase = self._external_state(status_type, active_flags)
                results[thread_id] = {
                    "task_id": thread_id,
                    "project_id": project.id,
                    "title": str(
                        _value(thread, "name", "")
                        or _value(thread, "preview", "")
                        or "未命名 Codex 任务"
                    )[:200],
                    "status": status,
                    "phase": phase,
                    "created_at": _value(thread, "created_at", _value(thread, "createdAt")),
                    "updated_at": _value(thread, "updated_at", _value(thread, "updatedAt")),
                    "origin": "external",
                    "source_cwd": str(project.path),
                }
        return list(results.values())

    @staticmethod
    def _external_state(status: str, active_flags: Iterable[str]) -> Tuple[str, str]:
        normalized = status.replace("_", "").lower()
        if normalized == "active":
            flags = {value.replace("_", "").lower() for value in active_flags}
            if "waitingonapproval" in flags:
                return "running", "waiting_approval"
            if "waitingonuserinput" in flags:
                return "running", "waiting_input"
            return "running", "running"
        if normalized == "systemerror":
            return "failed", "failed"
        if normalized in {"idle", "notloaded"}:
            return "unknown", "unknown"
        return "unknown", "unknown"

    @classmethod
    def _external_status(cls, status: str) -> str:
        return cls._external_state(status, ())[0]

    @staticmethod
    def _matches_status(task: Mapping[str, Any], requested: str) -> bool:
        status = str(task.get("status", ""))
        phase = str(task.get("phase", status))
        if requested == "all":
            return True
        if requested == "active":
            return status in ACTIVE_STATUSES or phase in ACTIVE_PHASES
        if requested in WAITING_PHASES:
            return phase == requested
        return status == requested

    @staticmethod
    def _summary(task: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "task_id": task.get("thread_id") or task.get("task_id"),
            "project_id": task.get("project_id"),
            "title": task.get("title"),
            "status": task.get("status"),
            "phase": task.get("phase", task.get("status")),
            "created_at": task.get("created_at"),
            "started_at": task.get("started_at"),
            "finished_at": task.get("finished_at"),
            "result": task.get("result_excerpt"),
            "error": task.get("error"),
            "origin": task.get("origin", "botplatform"),
            "source_cwd": task.get("source_cwd"),
        }

    def _summary_with_pending(self, task: Mapping[str, Any]) -> Dict[str, Any]:
        summary = self._summary(task)
        task_id = str(summary.get("task_id") or "")
        pending = self.store.pending_interaction(task_id) if task_id else None
        if pending:
            interaction = self.store.get_interaction(
                str(pending["interaction_id"])
            ) or {}
            try:
                payload = json.loads(str(interaction.get("payload_json") or "{}"))
            except (TypeError, ValueError):
                payload = {}
            summary["pending_request"] = {
                "request_id": pending["interaction_id"],
                "kind": pending["kind"],
                "expires_at": pending["expires_at"],
                "summary": self._request_summary(
                    str(interaction.get("method") or pending["method"]), payload
                ),
            }
        latest_event = self.store.latest_event(task_id) if task_id else None
        if latest_event:
            summary["notification"] = {
                "event_type": latest_event["event_type"],
                "status": latest_event["delivery_status"],
                "attempt_count": latest_event["attempt_count"],
                "last_error": latest_event["last_error"],
                "created_at": latest_event["created_at"],
                "sent_at": latest_event["sent_at"],
            }
        return summary

    def list_tasks(self, tenant_id: str, status: str, limit: int) -> Dict[str, Any]:
        local = self.store.list(tenant_id, max(limit, 20))
        local_by_id = {
            str(item["thread_id"]): self._summary_with_pending(item) for item in local
        }
        merged: Dict[str, Dict[str, Any]] = dict(local_by_id)
        for item in self._sdk_threads(limit):
            task_id = str(item["task_id"])
            owner = self.store.get(task_id)
            if owner is not None and owner["tenant_id"] != tenant_id:
                continue
            merged.setdefault(task_id, item)
        tasks = [item for item in merged.values() if self._matches_status(item, status)]
        tasks.sort(
            key=lambda item: str(
                item.get("created_at") or item.get("updated_at") or ""
            ),
            reverse=True,
        )
        return {"tasks": tasks[:limit], "count": min(len(tasks), limit)}

    def _external_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        for task in self._sdk_threads(20):
            if task["task_id"] == task_id:
                return task
        return None

    def get_task(self, tenant_id: str, task_id: str) -> Dict[str, Any]:
        local = self.store.get(task_id)
        if local is not None:
            if local["tenant_id"] != tenant_id:
                raise PluginError("Codex 任务不存在或不属于当前用户")
            summary = self._summary_with_pending(local)
            if local.get("origin") != "external":
                return summary
            try:
                thread = self._get_client().thread_resume(task_id)
                response = thread.read(include_turns=True)
                latest = _latest_agent_text(response)
                if latest:
                    summary["result"] = latest[: self.RESULT_LIMIT]
            except Exception as exc:
                summary["read_warning"] = self._safe_error(exc)
            return summary
        external = self._external_task(task_id)
        if external is None:
            raise PluginError("Codex 任务不存在或不在开放项目中")
        try:
            thread = self._get_client().thread_resume(task_id)
            response = thread.read(include_turns=True)
            latest = _latest_agent_text(response)
            if latest:
                external["result"] = latest[: self.RESULT_LIMIT]
        except Exception as exc:
            external["read_warning"] = self._safe_error(exc)
        return external

    def continue_task(self, tenant_id: str, task_id: str, instruction: str) -> Dict[str, Any]:
        local = self.store.get(task_id)
        if local is not None and local["tenant_id"] != tenant_id:
            raise PluginError("Codex 任务不存在或不属于当前用户")
        with self._lock:
            if task_id in self._active:
                raise PluginError("Codex 任务仍在运行，请等待完成或先中止")
            self._cancelled.discard(task_id)
        if local is None:
            external = self._external_task(task_id)
            if external is None:
                raise PluginError("Codex 任务不存在或不在开放项目中")
            local = self.store.adopt(
                task_id,
                tenant_id,
                str(external["project_id"]),
                str(external["title"]),
                self.config.notify_on_completion,
            )
        session = self._new_task_session()
        try:
            thread = session.thread_resume(task_id)
        except Exception as exc:
            self._close_task_session(session)
            raise PluginError("恢复 Codex 任务失败：{}".format(self._safe_error(exc))) from exc
        self.store.requeue(task_id, self.config.notify_on_completion)
        self._notify_phase(task_id, "queued")
        with self._lock:
            self._task_sessions[task_id] = session
        self._executor.submit(
            self._execute_turn, session, thread, task_id, instruction
        )
        return self._summary_with_pending(self.store.get(task_id) or local)

    @staticmethod
    def _parse_answers(payload: Mapping[str, Any], raw: str) -> Dict[str, Any]:
        questions = list(payload.get("questions") or [])
        if not questions:
            raise PluginError("该 Codex 请求没有可回答的问题")
        values: Dict[str, str] = {}
        if len(questions) == 1 and "=" not in raw:
            values[str(_value(questions[0], "id", ""))] = raw.strip()
        else:
            for part in raw.split(";"):
                if "=" not in part:
                    raise PluginError("多问题答案格式应为：1=答案;2=答案")
                key, value = part.split("=", 1)
                key = key.strip()
                value = value.strip()
                if not key or not value:
                    raise PluginError("问题编号和答案不能为空")
                if key.isdigit() and 1 <= int(key) <= len(questions):
                    key = str(_value(questions[int(key) - 1], "id", ""))
                values[key] = value
        answers: Dict[str, Any] = {}
        for question in questions:
            question_id = str(_value(question, "id", ""))
            answer = values.get(question_id, "").strip()
            if not answer:
                raise PluginError("缺少问题 {} 的答案".format(question_id))
            options = list(_value(question, "options", []) or [])
            if options:
                labels = [str(_value(option, "label", "")) for option in options]
                if answer.isdigit() and 1 <= int(answer) <= len(labels):
                    answer = labels[int(answer) - 1]
                elif answer not in labels and not bool(_value(question, "isOther", False)):
                    raise PluginError(
                        "问题 {} 仅支持：{}".format(question_id, "、".join(labels))
                    )
            answers[question_id] = {"answers": [answer]}
        return answers

    def resolve_interaction(
        self, tenant_id: str, interaction_id: str, action: str, answer: str = ""
    ) -> str:
        interaction = self.store.get_interaction(interaction_id)
        if interaction is None or interaction["tenant_id"] != tenant_id:
            raise PluginError("Codex 确认编号不存在或不属于当前用户")
        if interaction["status"] != "pending":
            raise PluginError("Codex 确认已经处理或失效")
        payload = json.loads(str(interaction["payload_json"]))
        method = str(interaction["method"])
        if interaction["kind"] == "user_input":
            if action != "answer" or not answer.strip():
                raise PluginError("该请求需要使用 /codex answer <编号> <答案>")
            answers = self._parse_answers(payload, answer)
            response = self._request_response(method, payload, "answer", answers)
            status = "answered"
            persist_response = not any(
                bool(_value(question, "isSecret", False))
                for question in list(payload.get("questions") or [])
            )
        else:
            if action not in {"approve", "deny"}:
                raise PluginError("该请求只能批准或拒绝")
            response = self._request_response(method, payload, action)
            status = "approved" if action == "approve" else "declined"
            persist_response = True
        resolved = self.store.resolve_interaction(
            str(interaction["interaction_id"]),
            tenant_id,
            status,
            response,
            persist_response=persist_response,
        )
        if resolved is None or resolved["status"] == "expired":
            raise PluginError("Codex 确认已经过期")
        code = str(interaction["interaction_id"])
        with self._lock:
            waiter = self._waiters.get(code)
            if waiter is None:
                waiter = _InteractionWaiter(threading.Event())
                self._waiters[code] = waiter
            waiter.response = response
            waiter.event.set()
        labels = {"approved": "已批准", "declined": "已拒绝", "answered": "已提交答案"}
        return "{} Codex 请求 {}。".format(labels[status], code)

    def cancel_task(self, tenant_id: str, task_id: str, notify: bool = True) -> Dict[str, Any]:
        task = self.store.get(task_id)
        if task is None or task["tenant_id"] != tenant_id:
            raise PluginError("Codex 任务不存在或不属于当前用户")
        if task["status"] not in ACTIVE_STATUSES:
            raise PluginError("Codex 任务当前不在运行")
        with self._lock:
            self._cancelled.add(task_id)
            if not notify:
                self._suppress_notifications.add(task_id)
            handle = self._active.get(task_id)
        self._cancel_pending_interaction(task_id, tenant_id)
        if handle is not None:
            try:
                handle.interrupt()
            except Exception as exc:
                raise PluginError("中止 Codex 任务失败：{}".format(self._safe_error(exc))) from exc
        else:
            self.store.finish(task_id, "interrupted", error="任务在开始前已被中止")
            self._notify(task_id)
        return self._summary_with_pending(self.store.get(task_id) or task)

    def _cancel_pending_interaction(self, thread_id: str, tenant_id: str) -> None:
        pending = self.store.pending_interaction(thread_id)
        if not pending:
            return
        interaction = self.store.get_interaction(str(pending["interaction_id"]))
        if not interaction:
            return
        payload = json.loads(str(interaction["payload_json"]))
        response = self._request_response(
            str(interaction["method"]), payload, "deny"
        )
        resolved = self.store.resolve_interaction(
            str(interaction["interaction_id"]), tenant_id, "cancelled", response
        )
        if resolved is None:
            return
        with self._lock:
            code = str(interaction["interaction_id"])
            waiter = self._waiters.get(code)
            if waiter is None:
                waiter = _InteractionWaiter(threading.Event())
                self._waiters[code] = waiter
            waiter.response = response
            waiter.event.set()

    def close_tenant(self, tenant_id: str) -> None:
        for task in self.store.list(tenant_id, 100, ACTIVE_STATUSES):
            try:
                self.cancel_task(tenant_id, str(task["thread_id"]), notify=False)
            except PluginError:
                pass

    def close(self) -> None:
        self._watchdog_stop.set()
        if self._watchdog is not threading.current_thread():
            self._watchdog.join(timeout=2.0)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = list(self._active.items())
            sessions = list(self._task_sessions.values())
            queued = []
            for tenant_id in self.config.admin_tenant_ids:
                queued.extend(self.store.list(tenant_id, 100, ACTIVE_STATUSES))
            for task in queued:
                task_id = str(task["thread_id"])
                self._cancelled.add(task_id)
                self._suppress_notifications.add(task_id)
                self._cancel_pending_interaction(task_id, str(task["tenant_id"]))
        for _task_id, handle in active:
            try:
                handle.interrupt()
            except Exception:  # noqa: BLE001 - best effort on shutdown
                LOGGER.warning("关闭时中断 Codex 任务 %s 失败", _task_id, exc_info=True)
        for task in queued:
            current = self.store.get(str(task["thread_id"]))
            if current and current["status"] in ACTIVE_STATUSES:
                self.store.finish(
                    str(task["thread_id"]),
                    "interrupted",
                    error="BotPlatform 已关闭",
                )
                self.store.set_notification(str(task["thread_id"]), "disabled")
        self._executor.shutdown(wait=True, cancel_futures=True)
        for session in sessions:
            self._close_task_session(session)
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - best effort on shutdown
                LOGGER.warning("关闭 Codex 客户端失败", exc_info=True)


class CodexTasksPlugin:
    id = "codex_tasks"
    TOOL_DEFINITIONS: Dict[str, PluginToolDefinition] = {
        "codex_list_tasks": PluginToolDefinition(
            "列出开放项目中的 Codex 开发任务。status=active 只返回正在排队或运行的任务。",
            _object_schema(
                {
                    "status": {
                        "type": "string",
                        "enum": [
                            "all", "active", "waiting_approval", "waiting_input",
                            "completed", "failed", "interrupted"
                        ],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                }
            ),
        ),
        "codex_get_task": PluginToolDefinition(
            "按任务编号查看 Codex 开发任务状态、结果摘要或错误。",
            _object_schema({"task_id": {"type": "string"}}, ["task_id"]),
        ),
        "codex_create_task": PluginToolDefinition(
            "在预先配置的项目中创建并后台执行新的 Codex 开发任务。",
            _object_schema(
                {
                    "title": {"type": "string", "minLength": 1, "maxLength": 100},
                    "instruction": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "project_id": {"type": "string"},
                },
                ["title", "instruction"],
            ),
            requires_approval=True,
        ),
        "codex_continue_task": PluginToolDefinition(
            "继续一个已结束或中断的 Codex 开发任务并后台执行新的指令。",
            _object_schema(
                {
                    "task_id": {"type": "string"},
                    "instruction": {"type": "string", "minLength": 1, "maxLength": 4000},
                },
                ["task_id", "instruction"],
            ),
            requires_approval=True,
        ),
        "codex_cancel_task": PluginToolDefinition(
            "中止当前用户通过 BotPlatform 启动的 Codex 开发任务。",
            _object_schema({"task_id": {"type": "string"}}, ["task_id"]),
            requires_approval=True,
        ),
    }

    @classmethod
    def validate_settings(cls, settings: Mapping[str, Any]) -> None:
        allowed = {
            "admin_tenant_ids",
            "projects",
            "default_project",
            "max_concurrent_tasks",
            "notify_on_completion",
            "monitor_external_tasks",
            "monitor_tenant_id",
            "external_project_scope",
            "external_poll_interval_seconds",
            "interaction_ttl_seconds",
            "notify_events",
        }
        unknown = sorted(set(settings) - allowed)
        if unknown:
            raise ValueError("codex_tasks 包含未知配置：{}".format("、".join(unknown)))
        admins = settings.get("admin_tenant_ids", [])
        if not isinstance(admins, list) or not all(
            isinstance(item, str) and item.strip() for item in admins
        ):
            raise ValueError("codex_tasks.admin_tenant_ids 必须是非空字符串数组")
        if len(set(admins)) != len(admins):
            raise ValueError("codex_tasks.admin_tenant_ids 不能重复")
        projects = settings.get("projects")
        if not isinstance(projects, list) or not projects:
            raise ValueError("codex_tasks.projects 必须是非空数组")
        project_ids: List[str] = []
        for index, project in enumerate(projects):
            if not isinstance(project, dict) or set(project) != {"id", "path"}:
                raise ValueError(
                    "codex_tasks.projects[{}] 必须只包含 id 和 path".format(index)
                )
            project_id = project.get("id")
            path = project.get("path")
            if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
                raise ValueError("codex_tasks.projects[{}].id 格式无效".format(index))
            if not isinstance(path, str) or not path.strip():
                raise ValueError("codex_tasks.projects[{}].path 必须是非空字符串".format(index))
            project_ids.append(project_id)
        if len(set(project_ids)) != len(project_ids):
            raise ValueError("codex_tasks.projects.id 不能重复")
        default_project = settings.get("default_project")
        if not isinstance(default_project, str) or default_project not in project_ids:
            raise ValueError("codex_tasks.default_project 必须引用已配置项目")
        maximum = settings.get("max_concurrent_tasks", 1)
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 4:
            raise ValueError("codex_tasks.max_concurrent_tasks 必须是 1 到 4 的整数")
        notify = settings.get("notify_on_completion", True)
        if not isinstance(notify, bool):
            raise ValueError("codex_tasks.notify_on_completion 必须是布尔值")
        monitor = settings.get("monitor_external_tasks", True)
        if not isinstance(monitor, bool):
            raise ValueError("codex_tasks.monitor_external_tasks 必须是布尔值")
        monitor_tenant = settings.get("monitor_tenant_id", "")
        if not isinstance(monitor_tenant, str):
            raise ValueError("codex_tasks.monitor_tenant_id 必须是字符串")
        if monitor_tenant and monitor_tenant not in admins:
            raise ValueError("codex_tasks.monitor_tenant_id 必须属于管理员租户")
        if monitor and len(admins) > 1 and not monitor_tenant:
            raise ValueError("多个管理员启用外部监控时必须设置 monitor_tenant_id")
        external_scope = settings.get("external_project_scope", "configured")
        if external_scope not in {"configured", "all"}:
            raise ValueError(
                "codex_tasks.external_project_scope 必须是 configured 或 all"
            )
        poll = settings.get("external_poll_interval_seconds", 15)
        if not isinstance(poll, int) or isinstance(poll, bool) or not 5 <= poll <= 300:
            raise ValueError(
                "codex_tasks.external_poll_interval_seconds 必须是 5 到 300 的整数"
            )
        ttl = settings.get("interaction_ttl_seconds", 300)
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 60 <= ttl <= 3600:
            raise ValueError("codex_tasks.interaction_ttl_seconds 必须是 60 到 3600 的整数")
        notify_events = settings.get("notify_events", list(DEFAULT_NOTIFY_EVENTS))
        if (
            not isinstance(notify_events, list)
            or not all(isinstance(item, str) for item in notify_events)
            or len(set(notify_events)) != len(notify_events)
            or not set(notify_events).issubset(SUPPORTED_NOTIFY_EVENTS)
        ):
            raise ValueError(
                "codex_tasks.notify_events 只能包含 queued、running、waiting_approval、"
                "waiting_input、completed、failed、interrupted，且不能重复"
            )

    def __init__(
        self,
        settings: Mapping[str, Any],
        context: Optional[PluginContext] = None,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        if context is None:
            raise ValueError("codex_tasks 缺少插件运行上下文")
        self.config = CodexTasksConfig.from_mapping(settings, context.project_root)
        self.service = CodexTaskService(self.config, context, client_factory=client_factory)

    @property
    def tool_definitions(self) -> Mapping[str, PluginToolDefinition]:
        return self.TOOL_DEFINITIONS

    def is_available(self, tool_name: str) -> bool:
        return bool(
            tool_name in self.TOOL_DEFINITIONS
            and self.config.admin_tenant_ids
            and self.service.sdk_importable
        )

    def _tenant_id(self, tenant: Any) -> str:
        tenant_id = str(getattr(tenant, "tenant_id", "") or "")
        if not tenant_id or tenant_id not in self.config.admin_tenant_ids:
            raise PluginError("当前用户无权访问 Codex 开发任务")
        return tenant_id

    @staticmethod
    def _text(arguments: Dict[str, Any], name: str, maximum: int) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or not value.strip():
            raise PluginError("{} 必须是非空字符串".format(name))
        value = value.strip()
        if len(value) > maximum:
            raise PluginError("{} 最长为 {} 个字符".format(name, maximum))
        return value

    def execute(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> Any:
        tenant_id = self._tenant_id(tenant)
        if tool_name == "codex_list_tasks":
            status = arguments.get("status", "all")
            limit = arguments.get("limit", 10)
            if status not in {
                "all", "active", "waiting_approval", "waiting_input",
                "completed", "failed", "interrupted"
            }:
                raise PluginError("status 值无效")
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
                raise PluginError("limit 必须是 1 到 20 的整数")
            return self.service.list_tasks(tenant_id, status, limit)
        if tool_name == "codex_get_task":
            return self.service.get_task(tenant_id, self._text(arguments, "task_id", 200))
        if tool_name == "codex_create_task":
            project_id = arguments.get("project_id")
            if project_id is not None and not isinstance(project_id, str):
                raise PluginError("project_id 必须是字符串")
            return self.service.create_task(
                tenant_id,
                self._text(arguments, "title", 100),
                self._text(arguments, "instruction", 4000),
                project_id,
            )
        if tool_name == "codex_continue_task":
            return self.service.continue_task(
                tenant_id,
                self._text(arguments, "task_id", 200),
                self._text(arguments, "instruction", 4000),
            )
        if tool_name == "codex_cancel_task":
            return self.service.cancel_task(
                tenant_id, self._text(arguments, "task_id", 200)
            )
        raise PluginError("未知 Codex 工具：{}".format(tool_name))

    def resolve_channel_command(self, tenant: Any, text: str) -> str:
        tenant_id = self._tenant_id(tenant)
        parts = text.strip().split(maxsplit=3)
        if len(parts) == 1:
            return (
                "Codex 确认命令：\n"
                "/codex approve <编号>\n"
                "/codex deny <编号>\n"
                "/codex answer <编号> <答案>"
            )
        action = parts[1].lower()
        if action not in {"approve", "deny", "answer"}:
            raise PluginError("未知 Codex 命令，请使用 approve、deny 或 answer")
        if (action in {"approve", "deny"} and len(parts) != 3) or (
            action == "answer" and len(parts) != 4
        ):
            raise PluginError("Codex 命令格式不正确，请使用 /codex 查看帮助")
        return self.service.resolve_interaction(
            tenant_id,
            parts[2],
            action,
            parts[3] if action == "answer" else "",
        )

    def resolve_wechat_command(self, tenant: Any, text: str) -> str:
        """Compatibility alias for integrations built before the messaging layer."""
        return self.resolve_channel_command(tenant, text)

    def preview(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> str:
        self._tenant_id(tenant)
        if tool_name == "codex_create_task":
            project = self.service.project(arguments.get("project_id"))
            return "创建 Codex 开发任务：{}\n项目：{}".format(
                self._text(arguments, "title", 100), project.id
            )
        if tool_name == "codex_continue_task":
            return "继续 Codex 开发任务：{}".format(
                self._text(arguments, "task_id", 200)
            )
        if tool_name == "codex_cancel_task":
            return "中止 Codex 开发任务：{}".format(
                self._text(arguments, "task_id", 200)
            )
        return "执行 Codex 工具：{}".format(tool_name)

    def close_tenant(self, tenant_id: str) -> None:
        self.service.close_tenant(tenant_id)

    def on_recipient_refreshed(self, tenant_id: str) -> int:
        return self.service.on_recipient_refreshed(tenant_id)

    def close(self) -> None:
        self.service.close()
