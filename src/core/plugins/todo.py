"""Tenant-aware todo management exposed as an in-process platform plugin."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from src.core.storage.database import Database

from .base import PluginContext, PluginError, PluginJobDefinition, PluginToolDefinition


SCHEMA_VERSION = 1
TITLE_MAX_CHARACTERS = 200
SUMMARY_MAX_BYTES = 1600
TODO_ID = re.compile(r"^T(0*[1-9][0-9]*)$")
RELATIVE_REMINDER = re.compile(r"^(?:in\s+)?(\d+)\s*(?:minutes?|mins?|分钟)\s*(?:later|后)?$", re.I)
SCOPES = {"pending", "completed", "archived", "all"}
ACTIONS = {"list", "add", "edit", "complete", "reopen", "remind", "archive"}


class TodoError(RuntimeError):
    """A user-readable todo storage or validation error."""


@dataclass(frozen=True)
class OperationResult:
    status: str
    summary: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        raise TodoError("时间必须包含时区")
    return value.astimezone(timezone.utc).isoformat()


def parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise TodoError("字段 {} 必须是带时区的时间字符串".format(field))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TodoError("字段 {} 不是有效时间".format(field)) from exc
    if parsed.tzinfo is None:
        raise TodoError("字段 {} 必须包含时区".format(field))
    return parsed.astimezone(timezone.utc)


def parse_reminder_time(value: object, now: datetime) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TodoError("提醒时间必须是带时区的时间字符串或“5分钟后”")
    raw = value.strip()
    relative = RELATIVE_REMINDER.fullmatch(raw)
    if relative:
        minutes = int(relative.group(1))
        if minutes < 1:
            raise TodoError("提醒延迟至少为 1 分钟")
        return now.astimezone(timezone.utc) + timedelta(minutes=minutes)
    parsed = parse_timestamp(raw, "remind_at")
    if parsed <= now.astimezone(timezone.utc):
        raise TodoError("提醒时间必须晚于当前时间")
    return parsed


def normalize_title(value: Optional[str]) -> str:
    if not isinstance(value, str):
        raise TodoError("待办内容不能为空")
    title = value.strip()
    if not title:
        raise TodoError("待办内容不能为空")
    if len(title) > TITLE_MAX_CHARACTERS:
        raise TodoError("待办内容不能超过 {} 个字符".format(TITLE_MAX_CHARACTERS))
    if any(ord(character) < 32 or ord(character) == 127 for character in title):
        raise TodoError("待办内容必须是单行可见文字")
    return title


def normalize_todo_id(value: Optional[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TodoError("必须提供待办编号")
    todo_id = value.strip().upper()
    if not TODO_ID.fullmatch(todo_id):
        raise TodoError("待办编号格式无效，应类似 T0001")
    return todo_id


def new_store(now: datetime) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "next_id": 1,
        "updated_at": isoformat(now),
        "items": [],
        "archived_items": [],
    }


def validate_item(raw: object, archived: bool) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise TodoError("待办项必须是 JSON 对象")
    required = {
        "id", "title", "status", "created_at", "updated_at",
        "completed_at", "archived_at", "reminder_at", "is_one_off",
    }
    if set(raw) != required:
        raise TodoError("待办项字段不完整或包含未知字段")
    todo_id = normalize_todo_id(raw.get("id"))
    title = normalize_title(raw.get("title"))
    status = raw.get("status")
    if status not in {"pending", "completed"}:
        raise TodoError("待办 {} 的状态无效".format(todo_id))
    parse_timestamp(raw.get("created_at"), todo_id + ".created_at")
    parse_timestamp(raw.get("updated_at"), todo_id + ".updated_at")
    completed_at = raw.get("completed_at")
    archived_at = raw.get("archived_at")
    reminder_at = raw.get("reminder_at")
    is_one_off = raw.get("is_one_off")
    if not isinstance(is_one_off, bool):
        raise TodoError("待办 {} 的一次性标识无效".format(todo_id))
    if reminder_at is not None:
        parse_timestamp(reminder_at, todo_id + ".reminder_at")
    if status == "pending":
        if completed_at is not None or archived_at is not None:
            raise TodoError("未完成待办 {} 不能包含完成或归档时间".format(todo_id))
        if archived:
            raise TodoError("归档区不能包含未完成待办 {}".format(todo_id))
    else:
        if reminder_at is not None:
            raise TodoError("已完成待办 {} 不能包含提醒时间".format(todo_id))
        parse_timestamp(completed_at, todo_id + ".completed_at")
        if archived:
            parse_timestamp(archived_at, todo_id + ".archived_at")
        elif archived_at is not None:
            raise TodoError("活动区待办 {} 不能包含归档时间".format(todo_id))
    return {
        "id": todo_id,
        "title": title,
        "status": status,
        "created_at": raw["created_at"],
        "updated_at": raw["updated_at"],
        "completed_at": completed_at,
        "archived_at": archived_at,
        "reminder_at": reminder_at,
        "is_one_off": is_one_off,
    }


def validate_store(raw: object) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise TodoError("待办文件必须是 JSON 对象")
    required = {"schema_version", "next_id", "updated_at", "items", "archived_items"}
    if set(raw) != required:
        raise TodoError("待办文件字段不完整或包含未知字段")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise TodoError("不支持的待办数据版本")
    next_id = raw.get("next_id")
    if not isinstance(next_id, int) or isinstance(next_id, bool) or next_id < 1:
        raise TodoError("next_id 必须是正整数")
    parse_timestamp(raw.get("updated_at"), "updated_at")
    if not isinstance(raw.get("items"), list) or not isinstance(raw.get("archived_items"), list):
        raise TodoError("items 和 archived_items 必须是数组")
    items = [validate_item(item, archived=False) for item in raw["items"]]
    archived_items = [validate_item(item, archived=True) for item in raw["archived_items"]]
    ids = [item["id"] for item in [*items, *archived_items]]
    if len(ids) != len(set(ids)):
        raise TodoError("待办编号不能重复")
    highest = max((int(TODO_ID.fullmatch(item_id).group(1)) for item_id in ids), default=0)
    if next_id <= highest:
        raise TodoError("next_id 必须大于已有待办编号")
    return {
        "schema_version": SCHEMA_VERSION,
        "next_id": next_id,
        "updated_at": raw["updated_at"],
        "items": items,
        "archived_items": archived_items,
    }


class SqliteTodoStore:
    """Transactional tenant todo repository."""

    def __init__(
        self, database_path: Path, tenant_id: str, timezone_name: str = "UTC"
    ) -> None:
        self.database = Database(database_path)
        self.tenant_id = tenant_id
        self.timezone_name = str(ZoneInfo(timezone_name))

    def _load(self, connection: Any, now: datetime) -> Dict[str, Any]:
        rows = connection.execute(
            "SELECT todo.todo_number, todo.title, todo.status, todo.created_at, "
            "todo.updated_at, todo.completed_at, todo.archived_at, "
            "todo.reminder_at, todo.is_one_off, "
            "event.delivery_status AS reminder_delivery_status "
            "FROM todos AS todo LEFT JOIN todo_reminder_events AS event "
            "ON event.tenant_id=todo.tenant_id "
            "AND event.todo_number=todo.todo_number "
            "WHERE todo.tenant_id=? ORDER BY todo.todo_number",
            (self.tenant_id,),
        ).fetchall()
        items = []
        archived_items = []
        for row in rows:
            archived = row["status"] == "archived"
            item = {
                "id": "T{:04d}".format(int(row["todo_number"])),
                "title": str(row["title"]),
                "status": "completed" if archived else str(row["status"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "completed_at": row["completed_at"],
                "archived_at": row["archived_at"],
                "reminder_at": row["reminder_at"],
                "is_one_off": bool(row["is_one_off"]),
                "reminder_delivery_status": row["reminder_delivery_status"],
            }
            (archived_items if archived else items).append(item)
        highest = max((int(row["todo_number"]) for row in rows), default=0)
        updated_at = max((str(row["updated_at"]) for row in rows), default=isoformat(now))
        return {
            "schema_version": SCHEMA_VERSION,
            "next_id": highest + 1,
            "updated_at": updated_at,
            "items": items,
            "archived_items": archived_items,
        }

    def _save(self, connection: Any, data: Dict[str, Any]) -> None:
        connection.execute("DELETE FROM todos WHERE tenant_id=?", (self.tenant_id,))
        values = []
        for item in data["items"]:
            values.append((
                self.tenant_id, int(TODO_ID.fullmatch(item["id"]).group(1)), item["title"],
                item["status"], item["created_at"], item["updated_at"],
                item["completed_at"], item["archived_at"], item["reminder_at"], int(item["is_one_off"]),
            ))
        for item in data["archived_items"]:
            values.append((
                self.tenant_id, int(TODO_ID.fullmatch(item["id"]).group(1)), item["title"],
                "archived", item["created_at"], item["updated_at"],
                item["completed_at"], item["archived_at"], item["reminder_at"], int(item["is_one_off"]),
            ))
        connection.executemany(
            "INSERT INTO todos(tenant_id, todo_number, title, status, created_at, updated_at, "
            "completed_at, archived_at, reminder_at, is_one_off) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )

    def execute(
        self,
        action: str,
        todo_id: Optional[str] = None,
        title: Optional[str] = None,
        scope: Optional[str] = None,
        remind_at: Optional[str] = None,
        update_reminder: bool = False,
        is_one_off: Optional[bool] = None,
        update_one_off: bool = False,
        now: Optional[datetime] = None,
    ) -> OperationResult:
        current = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            data = self._load(connection, current)
            result, changed = apply_action(
                data, action, todo_id=todo_id, title=title, scope=scope,
                remind_at=remind_at, update_reminder=update_reminder,
                is_one_off=is_one_off, update_one_off=update_one_off, now=current,
                timezone_name=self.timezone_name,
            )
            if changed:
                data["updated_at"] = isoformat(current)
                self._save(connection, data)
                self._sync_reminder_events(connection, data, current)
            return result

    def _sync_reminder_events(
        self, connection: Any, data: Dict[str, Any], now: datetime
    ) -> None:
        timestamp = isoformat(now)
        active = set()
        for item in data["items"]:
            reminder_at = item["reminder_at"]
            if item["status"] != "pending" or reminder_at is None:
                continue
            number = int(TODO_ID.fullmatch(item["id"]).group(1))
            active.add(number)
            connection.execute(
                "INSERT INTO todo_reminder_events(tenant_id, todo_number, due_at, "
                "delivery_status, attempt_count, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', 0, ?, ?) "
                "ON CONFLICT(tenant_id, todo_number) DO UPDATE SET "
                "due_at=excluded.due_at, delivery_status='pending', attempt_count=0, "
                "sent_at=NULL, last_error=NULL, updated_at=excluded.updated_at "
                "WHERE todo_reminder_events.due_at<>excluded.due_at "
                "OR todo_reminder_events.delivery_status='cancelled'",
                (self.tenant_id, number, reminder_at, timestamp, timestamp),
            )
        statement = (
            "UPDATE todo_reminder_events SET delivery_status='cancelled', updated_at=? "
            "WHERE tenant_id=? AND delivery_status IN ('pending', 'sending') "
        )
        parameters: Tuple[Any, ...] = (timestamp, self.tenant_id)
        if active:
            statement += "AND todo_number NOT IN ({})".format(
                ",".join("?" for _ in active)
            )
            parameters += tuple(sorted(active))
        connection.execute(statement, parameters)

    def recover_inflight_reminders(self, now: Optional[datetime] = None) -> None:
        current = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE todo_reminder_events SET delivery_status='pending', updated_at=? "
                "WHERE delivery_status='sending' AND NOT EXISTS ("
                "SELECT 1 FROM notification_outbox AS outbox "
                "WHERE outbox.source_type='todo' "
                "AND outbox.tenant_id=todo_reminder_events.tenant_id "
                "AND outbox.source_ref=CAST(todo_reminder_events.todo_number AS TEXT) "
                "AND outbox.delivery_status IN "
                "('pending','sending','retry','waiting_recipient'))",
                (isoformat(current),),
            )

    def due_reminders(
        self, now: Optional[datetime] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """List due reminders; the notification Outbox performs the atomic claim."""

        current = now or utc_now()
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT event.tenant_id, event.todo_number, todo.title, event.due_at "
                "FROM todo_reminder_events AS event JOIN todos AS todo "
                "ON todo.tenant_id=event.tenant_id AND todo.todo_number=event.todo_number "
                "WHERE event.delivery_status='pending' AND event.due_at<=? "
                "AND todo.status='pending' AND todo.reminder_at=event.due_at "
                "ORDER BY event.due_at, event.tenant_id, event.todo_number LIMIT ?",
                (isoformat(current), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_due_reminders(
        self, now: Optional[datetime] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        current = now or utc_now()
        timestamp = isoformat(current)
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT event.tenant_id, event.todo_number, todo.title, event.due_at "
                "FROM todo_reminder_events AS event JOIN todos AS todo "
                "ON todo.tenant_id=event.tenant_id AND todo.todo_number=event.todo_number "
                "WHERE event.delivery_status='pending' AND event.due_at<=? "
                "AND todo.status='pending' AND todo.reminder_at=event.due_at "
                "ORDER BY event.due_at, event.tenant_id, event.todo_number LIMIT ?",
                (timestamp, limit),
            ).fetchall()
            claimed = []
            for row in rows:
                updated = connection.execute(
                    "UPDATE todo_reminder_events SET delivery_status='sending', "
                    "attempt_count=attempt_count+1, updated_at=? "
                    "WHERE tenant_id=? AND todo_number=? AND delivery_status='pending'",
                    (timestamp, row["tenant_id"], row["todo_number"]),
                ).rowcount
                if updated:
                    claimed.append(dict(row))
            return claimed

    def finish_reminder(
        self, todo_number: int, delivered: bool, error: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        current = now or utc_now()
        timestamp = isoformat(current)
        with self.database.transaction(immediate=True) as connection:
            updated = connection.execute(
                "UPDATE todo_reminder_events SET delivery_status=?, sent_at=?, "
                "last_error=?, updated_at=? WHERE tenant_id=? AND todo_number=? "
                "AND delivery_status='sending'",
                (
                    "sent" if delivered else "pending",
                    timestamp if delivered else None,
                    None if delivered else (error or "投递失败"),
                    timestamp, self.tenant_id, todo_number,
                ),
            ).rowcount
            if delivered and updated:
                connection.execute(
                    "UPDATE todos SET status='completed', completed_at=?, reminder_at=NULL, "
                    "updated_at=? WHERE tenant_id=? AND todo_number=? "
                    "AND status='pending' AND is_one_off=1",
                    (timestamp, timestamp, self.tenant_id, todo_number),
                )


def _require_absent(name: str, value: Optional[str]) -> None:
    if value is not None:
        raise TodoError("当前操作不接受参数 {}".format(name))


def _find(items: Iterable[Dict[str, Any]], todo_id: str) -> Optional[Dict[str, Any]]:
    return next((item for item in items if item["id"] == todo_id), None)


def _render_lines(header: str, lines: List[str], limit: int = SUMMARY_MAX_BYTES) -> str:
    if len(header.encode("utf-8")) > limit:
        return header.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
    selected: List[str] = []
    for index, line in enumerate(lines):
        omitted = len(lines) - index - 1
        candidate = "\n".join([header, *selected, line])
        suffix = "\n……另有 {} 项未显示。".format(omitted) if omitted else ""
        if len((candidate + suffix).encode("utf-8")) <= limit:
            selected.append(line)
            continue
        break
    omitted = len(lines) - len(selected)
    parts = [header, *selected]
    if omitted:
        parts.append("……另有 {} 项未显示。".format(omitted))
    return "\n".join(parts)


def _format_local_time(value: str, timezone_name: str) -> str:
    local = parse_timestamp(value, "reminder_at").astimezone(ZoneInfo(timezone_name))
    return "{}（{}）".format(
        local.strftime("%Y-%m-%d %H:%M"),
        timezone_name,
    )


def _reminder_state(item: Dict[str, Any], now: datetime) -> Tuple[str, str]:
    reminder_at = item.get("reminder_at")
    if reminder_at is None:
        return "none", "无提醒"
    due = parse_timestamp(reminder_at, item["id"] + ".reminder_at")
    delivery_status = item.get("reminder_delivery_status")
    if delivery_status == "sent":
        return "sent", "已提醒，待办仍未完成"
    if due > now.astimezone(timezone.utc):
        return "upcoming", "尚未到提醒时间"
    if delivery_status == "pending":
        return "pending_delivery", "已到提醒时间，等待投递"
    if delivery_status == "sending":
        return "sending", "提醒投递中"
    return "past", "提醒时间已过"


def _reminder_overview(states: List[str]) -> str:
    labels = (
        ("none", "无提醒"),
        ("upcoming", "尚未到"),
        ("pending_delivery", "等待投递"),
        ("sending", "投递中"),
        ("sent", "已提醒"),
        ("past", "时间已过"),
    )
    parts = [
        "{} {} 项".format(label, states.count(state))
        for state, label in labels
        if state in states
    ]
    return "提醒概况：{}。".format("，".join(parts)) if parts else ""


def render_list(
    data: Dict[str, Any],
    scope: str,
    now: datetime,
    timezone_name: str = "UTC",
    reminder_mode: bool = False,
) -> str:
    labels = {
        "pending": "未完成",
        "completed": "近期已完成",
        "archived": "已归档",
        "all": "全部",
    }
    if scope == "pending":
        items = [item for item in data["items"] if item["status"] == "pending"]
    elif scope == "completed":
        items = [item for item in data["items"] if item["status"] == "completed"]
    elif scope == "archived":
        items = list(data["archived_items"])
    else:
        items = [*data["items"], *data["archived_items"]]
    lines = []
    reminder_states: List[str] = []
    for item in items:
        marker = "[ ]" if item["status"] == "pending" else "[x]"
        if item["archived_at"] is not None:
            marker = "[归档]"
        reminder = ""
        if item["status"] == "pending":
            state, state_label = _reminder_state(item, now)
            reminder_states.append(state)
            reminder = "（{}）".format(state_label)
            if item["reminder_at"] is not None:
                reminder = "（提醒：{}；{}）".format(
                    _format_local_time(item["reminder_at"], timezone_name),
                    state_label,
                )
        kind = "（一次性）" if item["is_one_off"] else ""
        lines.append("- {} {} {}{}{}".format(marker, item["id"], item["title"], kind, reminder))
    local_now = now.astimezone(ZoneInfo(timezone_name))
    if reminder_mode:
        title = "【待办提醒】当前有 {} 项未完成事项：".format(len(items))
    else:
        title = "【待办列表】{}，共 {} 项。".format(labels[scope], len(items))
    header = "{}\n查询时间：{}（{}）。".format(
        title,
        local_now.strftime("%Y-%m-%d %H:%M:%S"),
        timezone_name,
    )
    overview = _reminder_overview(reminder_states)
    if overview:
        header = "{}\n{}".format(header, overview)
    return _render_lines(header, lines) if lines else header


def render_reminder(
    data: Dict[str, Any], now: datetime, timezone_name: str = "UTC"
) -> str:
    items = [item for item in data["items"] if item["status"] == "pending"]
    if not items:
        local_now = now.astimezone(ZoneInfo(timezone_name))
        return "【待办提醒】当前待办已清空。\n查询时间：{}（{}）。".format(
            local_now.strftime("%Y-%m-%d %H:%M:%S"),
            timezone_name,
        )
    return render_list(
        data,
        "pending",
        now,
        timezone_name=timezone_name,
        reminder_mode=True,
    )


def apply_action(
    data: Dict[str, Any],
    action: str,
    todo_id: Optional[str],
    title: Optional[str],
    scope: Optional[str],
    remind_at: Optional[str],
    update_reminder: bool,
    now: datetime,
    is_one_off: Optional[bool] = None,
    update_one_off: bool = False,
    timezone_name: str = "UTC",
) -> Tuple[OperationResult, bool]:
    if action not in ACTIONS:
        raise TodoError("不支持的待办操作：{}".format(action or "<空>"))
    timestamp = isoformat(now)

    if action == "list":
        _require_absent("todo_id", todo_id)
        _require_absent("title", title)
        _require_absent("remind_at", remind_at)
        _require_absent("is_one_off", is_one_off)
        selected_scope = scope or "pending"
        if selected_scope not in SCOPES:
            raise TodoError("查询范围仅支持 pending、completed、archived 或 all")
        return OperationResult(
            "success",
            render_list(data, selected_scope, now, timezone_name=timezone_name),
        ), False

    if action == "remind":
        _require_absent("todo_id", todo_id)
        _require_absent("title", title)
        _require_absent("scope", scope)
        _require_absent("remind_at", remind_at)
        _require_absent("is_one_off", is_one_off)
        return OperationResult(
            "success", render_reminder(data, now, timezone_name=timezone_name)
        ), False

    if action == "add":
        _require_absent("todo_id", todo_id)
        _require_absent("scope", scope)
        if is_one_off is not None and not isinstance(is_one_off, bool):
            raise TodoError("一次性标识必须是布尔值")
        normalized_title = normalize_title(title)
        parsed_reminder = (
            isoformat(parse_reminder_time(remind_at, now)) if remind_at is not None else None
        )
        effective_one_off = (
            is_one_off if is_one_off is not None else parsed_reminder is not None
        )
        generated_id = "T{:04d}".format(data["next_id"])
        data["next_id"] += 1
        data["items"].append({
            "id": generated_id,
            "title": normalized_title,
            "status": "pending",
            "created_at": timestamp,
            "updated_at": timestamp,
            "completed_at": None,
            "archived_at": None,
            "reminder_at": parsed_reminder,
            "is_one_off": effective_one_off,
        })
        kind = "，一次性任务" if effective_one_off else ""
        suffix = (
            "，提醒时间：{}".format(_format_local_time(parsed_reminder, timezone_name))
            if parsed_reminder else ""
        )
        return OperationResult(
            "success",
            "已新增待办：{} {}{}{}".format(
                generated_id, normalized_title, kind, suffix
            ),
        ), True

    if action == "archive":
        _require_absent("todo_id", todo_id)
        _require_absent("title", title)
        _require_absent("scope", scope)
        _require_absent("remind_at", remind_at)
        _require_absent("is_one_off", is_one_off)
        threshold = now.astimezone(timezone.utc) - timedelta(days=30)
        retained = []
        archived = []
        for item in data["items"]:
            if (
                item["status"] == "completed"
                and parse_timestamp(item["completed_at"], item["id"] + ".completed_at") <= threshold
            ):
                item["archived_at"] = timestamp
                item["reminder_at"] = None
                item["updated_at"] = timestamp
                archived.append(item)
            else:
                retained.append(item)
        if not archived:
            return OperationResult("success", "没有完成满 30 天的待办需要归档。"), False
        data["items"] = retained
        data["archived_items"].extend(archived)
        return OperationResult(
            "success",
            "已归档 {} 项待办：{}".format(
                len(archived), "、".join(item["id"] for item in archived)
            ),
        ), True

    normalized_id = normalize_todo_id(todo_id)
    _require_absent("scope", scope)
    if action != "edit":
        _require_absent("remind_at", remind_at)
        _require_absent("is_one_off", is_one_off)
    active = _find(data["items"], normalized_id)
    archived_item = _find(data["archived_items"], normalized_id)
    item = active or archived_item
    if item is None:
        raise TodoError("未找到待办：{}".format(normalized_id))

    if action == "edit":
        if title is None and not update_reminder and not update_one_off:
            raise TodoError("编辑待办时必须提供内容、提醒时间或一次性标识")
        changed = False
        if title is not None:
            normalized_title = normalize_title(title)
            if item["title"] != normalized_title:
                item["title"] = normalized_title
                changed = True
        if update_reminder:
            if item["status"] != "pending" and remind_at is not None:
                raise TodoError("已完成待办不能设置提醒时间")
            had_reminder = item["reminder_at"] is not None
            parsed_reminder = (
                isoformat(parse_reminder_time(remind_at, now)) if remind_at is not None else None
            )
            if item["reminder_at"] != parsed_reminder:
                item["reminder_at"] = parsed_reminder
                changed = True
            if not update_one_off:
                if not had_reminder and parsed_reminder is not None:
                    item["is_one_off"] = True
                elif had_reminder and parsed_reminder is None:
                    item["is_one_off"] = False
        if update_one_off:
            if not isinstance(is_one_off, bool):
                raise TodoError("一次性标识必须是布尔值")
            if item["is_one_off"] != is_one_off:
                item["is_one_off"] = is_one_off
                changed = True
        if not changed:
            return OperationResult("skipped", "待办 {} 的内容、提醒和类型没有变化。".format(normalized_id)), False
        item["updated_at"] = timestamp
        suffix = (
            "，提醒时间：{}".format(
                _format_local_time(item["reminder_at"], timezone_name)
            )
            if item["reminder_at"] is not None else "，已清除提醒"
        )
        kind = "，一次性任务" if item["is_one_off"] else ""
        return OperationResult(
            "success",
            "已更新待办：{} {}{}{}".format(
                normalized_id, item["title"], kind, suffix
            ),
        ), True

    _require_absent("title", title)
    if action == "complete":
        if item["status"] == "completed":
            location = "，该事项已归档" if archived_item is not None else ""
            return OperationResult("skipped", "待办 {} 已经完成{}。".format(normalized_id, location)), False
        item["status"] = "completed"
        item["completed_at"] = timestamp
        item["reminder_at"] = None
        item["updated_at"] = timestamp
        return OperationResult("success", "已完成待办：{} {}".format(normalized_id, item["title"])), True

    if item["status"] == "pending":
        return OperationResult("skipped", "待办 {} 已经是未完成状态。".format(normalized_id)), False
    if archived_item is not None:
        data["archived_items"].remove(archived_item)
        data["items"].append(archived_item)
    item["status"] = "pending"
    item["completed_at"] = None
    item["archived_at"] = None
    item["updated_at"] = timestamp
    return OperationResult("success", "已恢复待办：{} {}".format(normalized_id, item["title"])), True


def execute_action(
    database_path: Path,
    tenant_id: str,
    action: str,
    todo_id: Optional[str] = None,
    title: Optional[str] = None,
    scope: Optional[str] = None,
    remind_at: Optional[str] = None,
    update_reminder: bool = False,
    is_one_off: Optional[bool] = None,
    update_one_off: bool = False,
    now: Optional[datetime] = None,
    timezone_name: str = "UTC",
) -> OperationResult:
    """Convenience wrapper for direct (non-plugin) invocation, e.g. scheduler."""
    return SqliteTodoStore(database_path, tenant_id, timezone_name).execute(
        action, todo_id=todo_id, title=title, scope=scope, remind_at=remind_at,
        update_reminder=update_reminder, is_one_off=is_one_off,
        update_one_off=update_one_off, now=now,
    )


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


class TodoPlugin:
    """In-process todo management plugin with per-tenant SQLite isolation."""

    id = "todo"
    TOOL_DEFINITIONS: Dict[str, PluginToolDefinition] = {
        "todo_manage": PluginToolDefinition(
            "管理当前用户的私人待办：查询、新增、编辑、完成、恢复、提醒和归档。",
            _object_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": sorted(ACTIONS),
                        "description": "待办操作类型",
                    },
                    "todo_id": {
                        "type": "string",
                        "description": "待办编号，如 T0001；edit/complete/reopen 时必填",
                    },
                    "title": {
                        "type": "string",
                        "description": "待办内容；add/edit 时必填",
                    },
                    "scope": {
                        "type": "string",
                        "enum": sorted(SCOPES),
                        "description": "list 查询范围，默认 pending",
                    },
                    "remind_at": {
                        "type": ["string", "null"],
                        "description": (
                            "add/edit 的一次性提醒时间；使用带时区 ISO 时间，"
                            "或“5分钟后”。edit 传 null 清除提醒。"
                        ),
                    },
                    "is_one_off": {
                        "type": "boolean",
                        "description": (
                            "是否为一次性任务。设置 remind_at 时默认 true；"
                            "到期提醒成功送达后自动完成。"
                            "仅当提醒后仍需继续跟进时显式设为 false。"
                        ),
                    },
                },
                ["action"],
            ),
            direct_response=True,
        ),
    }

    @classmethod
    def validate_settings(cls, settings: Mapping[str, Any]) -> None:
        allowed: set = set()
        unknown = sorted(set(settings) - allowed)
        if unknown:
            raise ValueError("todo 包含未知配置：{}".format("、".join(unknown)))

    def __init__(
        self,
        settings: Mapping[str, Any],
        context: Optional[PluginContext] = None,
    ) -> None:
        if context is None:
            raise ValueError("todo 缺少插件运行上下文")
        self._database_path: Path = context.tenant_registry.database_path
        self._timezone_name = context.timezone
        self._notification_service = context.notification_service

    @property
    def background_jobs(self) -> List[PluginJobDefinition]:
        return [PluginJobDefinition("due_reminders", 30)]

    def start(self) -> None:
        self.recover_inflight_reminders()

    def run_background_job(
        self, job_id: str, now: Optional[datetime] = None
    ) -> bool:
        if job_id != "due_reminders":
            raise PluginError("未知待办后台任务：{}".format(job_id))
        if self._notification_service is None:
            return False
        any_success = False
        for event in self.claim_due_reminders(now):
            tenant_id = str(event["tenant_id"])
            number = int(event["todo_number"])
            try:
                enqueue = getattr(
                    self._notification_service, "enqueue_todo_reminder", None
                )
                if callable(enqueue):
                    enqueue(
                        tenant_id,
                        number,
                        str(event["due_at"]),
                        str(event["title"]),
                    )
                else:
                    self._notification_service.enqueue_text_to_tenant(
                        tenant_id,
                        "【待办提醒】T{:04d} {}".format(number, event["title"]),
                        source_type="todo",
                        source_key="{}:{}".format(number, event["due_at"]),
                        source_ref=str(number),
                    )
                any_success = True
            except Exception as exc:
                self.finish_reminder(
                    tenant_id, number, False, str(exc), now=now
                )
        return any_success

    @property
    def tool_definitions(self) -> Mapping[str, PluginToolDefinition]:
        return self.TOOL_DEFINITIONS

    def is_available(self, tool_name: str) -> bool:
        return tool_name in self.TOOL_DEFINITIONS

    def execute(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> Any:
        if tool_name != "todo_manage":
            raise PluginError("未知待办工具：{}".format(tool_name))
        tenant_id = str(
            getattr(tenant, "personal_tenant_id", None)
            or getattr(tenant, "tenant_id", "")
            or ""
        )
        if not tenant_id:
            raise PluginError("待办工具需要租户身份")
        action = arguments.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            raise PluginError(
                "action 必须是：{}".format("、".join(sorted(ACTIONS)))
            )
        todo_id = arguments.get("todo_id")
        if todo_id is not None and not isinstance(todo_id, str):
            raise PluginError("todo_id 必须是字符串")
        title = arguments.get("title")
        if title is not None and not isinstance(title, str):
            raise PluginError("title 必须是字符串")
        scope = arguments.get("scope")
        if scope is not None and (not isinstance(scope, str) or scope not in SCOPES):
            raise PluginError("scope 仅支持 pending、completed、archived 或 all")
        remind_at = arguments.get("remind_at")
        update_reminder = "remind_at" in arguments
        if remind_at is not None and not isinstance(remind_at, str):
            raise PluginError("remind_at 必须是字符串或 null")
        is_one_off = arguments.get("is_one_off")
        update_one_off = "is_one_off" in arguments
        if is_one_off is not None and not isinstance(is_one_off, bool):
            raise PluginError("is_one_off 必须是布尔值")
        try:
            result = SqliteTodoStore(
                self._database_path, tenant_id, self._timezone_name
            ).execute(
                action, todo_id=todo_id, title=title, scope=scope,
                remind_at=remind_at, update_reminder=update_reminder,
                is_one_off=is_one_off, update_one_off=update_one_off,
            )
        except TodoError as exc:
            raise PluginError(str(exc)) from exc
        return {"status": result.status, "summary": result.summary}

    def execute_for_tenant(
        self,
        tenant_id: str,
        action: str,
        todo_id: Optional[str] = None,
        title: Optional[str] = None,
        scope: Optional[str] = None,
        remind_at: Optional[str] = None,
        update_reminder: bool = False,
        is_one_off: Optional[bool] = None,
        update_one_off: bool = False,
        now: Optional[datetime] = None,
    ) -> OperationResult:
        """Direct invocation for scheduler or internal use (no plugin protocol)."""
        return SqliteTodoStore(
            self._database_path, tenant_id, self._timezone_name
        ).execute(
            action, todo_id=todo_id, title=title, scope=scope, remind_at=remind_at,
            update_reminder=update_reminder, is_one_off=is_one_off,
            update_one_off=update_one_off, now=now,
        )

    def recover_inflight_reminders(self, now: Optional[datetime] = None) -> None:
        SqliteTodoStore(self._database_path, "").recover_inflight_reminders(now)

    def claim_due_reminders(
        self, now: Optional[datetime] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        return SqliteTodoStore(self._database_path, "").claim_due_reminders(now, limit)

    def due_reminders(
        self, now: Optional[datetime] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        return SqliteTodoStore(self._database_path, "").due_reminders(now, limit)

    def finish_reminder(
        self,
        tenant_id: str,
        todo_number: int,
        delivered: bool,
        error: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        SqliteTodoStore(self._database_path, tenant_id).finish_reminder(
            todo_number, delivered, error, now
        )

    def preview(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> str:
        action = arguments.get("action", "")
        return "执行待办操作：{}".format(action)

    def close_tenant(self, tenant_id: str) -> None:
        pass

    def close(self) -> None:
        pass
