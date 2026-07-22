"""Tenant-aware todo management exposed as an in-process platform plugin."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from src.core.storage.database import Database

from .base import PluginContext, PluginError, PluginToolDefinition


SCHEMA_VERSION = 1
TITLE_MAX_CHARACTERS = 200
SUMMARY_MAX_BYTES = 1600
TODO_ID = re.compile(r"^T(0*[1-9][0-9]*)$")
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
        "completed_at", "archived_at",
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
    if status == "pending":
        if completed_at is not None or archived_at is not None:
            raise TodoError("未完成待办 {} 不能包含完成或归档时间".format(todo_id))
        if archived:
            raise TodoError("归档区不能包含未完成待办 {}".format(todo_id))
    else:
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

    def __init__(self, database_path: Path, tenant_id: str) -> None:
        self.database = Database(database_path)
        self.tenant_id = tenant_id

    def _load(self, connection: Any, now: datetime) -> Dict[str, Any]:
        rows = connection.execute(
            "SELECT todo_number, title, status, created_at, updated_at, completed_at, archived_at "
            "FROM todos WHERE tenant_id=? ORDER BY todo_number",
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
                item["completed_at"], item["archived_at"],
            ))
        for item in data["archived_items"]:
            values.append((
                self.tenant_id, int(TODO_ID.fullmatch(item["id"]).group(1)), item["title"],
                "archived", item["created_at"], item["updated_at"],
                item["completed_at"], item["archived_at"],
            ))
        connection.executemany(
            "INSERT INTO todos(tenant_id, todo_number, title, status, created_at, updated_at, "
            "completed_at, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )

    def execute(
        self,
        action: str,
        todo_id: Optional[str] = None,
        title: Optional[str] = None,
        scope: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> OperationResult:
        current = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            data = self._load(connection, current)
            result, changed = apply_action(
                data, action, todo_id=todo_id, title=title, scope=scope, now=current
            )
            if changed:
                data["updated_at"] = isoformat(current)
                self._save(connection, data)
            return result


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


def render_list(data: Dict[str, Any], scope: str) -> str:
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
    for item in items:
        marker = "[ ]" if item["status"] == "pending" else "[x]"
        if item["archived_at"] is not None:
            marker = "[归档]"
        lines.append("- {} {} {}".format(marker, item["id"], item["title"]))
    header = "【待办列表】{}，共 {} 项。".format(labels[scope], len(items))
    return _render_lines(header, lines) if lines else header


def render_reminder(data: Dict[str, Any]) -> str:
    items = [item for item in data["items"] if item["status"] == "pending"]
    if not items:
        return "【待办提醒】当前待办已清空。"
    header = "【待办提醒】当前有 {} 项未完成事项：".format(len(items))
    lines = ["- [ ] {} {}".format(item["id"], item["title"]) for item in items]
    return _render_lines(header, lines)


def apply_action(
    data: Dict[str, Any],
    action: str,
    todo_id: Optional[str],
    title: Optional[str],
    scope: Optional[str],
    now: datetime,
) -> Tuple[OperationResult, bool]:
    if action not in ACTIONS:
        raise TodoError("不支持的待办操作：{}".format(action or "<空>"))
    timestamp = isoformat(now)

    if action == "list":
        _require_absent("todo_id", todo_id)
        _require_absent("title", title)
        selected_scope = scope or "pending"
        if selected_scope not in SCOPES:
            raise TodoError("查询范围仅支持 pending、completed、archived 或 all")
        return OperationResult("success", render_list(data, selected_scope)), False

    if action == "remind":
        _require_absent("todo_id", todo_id)
        _require_absent("title", title)
        _require_absent("scope", scope)
        return OperationResult("success", render_reminder(data)), False

    if action == "add":
        _require_absent("todo_id", todo_id)
        _require_absent("scope", scope)
        normalized_title = normalize_title(title)
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
        })
        return OperationResult("success", "已新增待办：{} {}".format(generated_id, normalized_title)), True

    if action == "archive":
        _require_absent("todo_id", todo_id)
        _require_absent("title", title)
        _require_absent("scope", scope)
        threshold = now.astimezone(timezone.utc) - timedelta(days=30)
        retained = []
        archived = []
        for item in data["items"]:
            if (
                item["status"] == "completed"
                and parse_timestamp(item["completed_at"], item["id"] + ".completed_at") <= threshold
            ):
                item["archived_at"] = timestamp
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
    active = _find(data["items"], normalized_id)
    archived_item = _find(data["archived_items"], normalized_id)
    item = active or archived_item
    if item is None:
        raise TodoError("未找到待办：{}".format(normalized_id))

    if action == "edit":
        normalized_title = normalize_title(title)
        if item["title"] == normalized_title:
            return OperationResult("skipped", "待办 {} 的内容没有变化。".format(normalized_id)), False
        item["title"] = normalized_title
        item["updated_at"] = timestamp
        return OperationResult("success", "已更新待办：{} {}".format(normalized_id, normalized_title)), True

    _require_absent("title", title)
    if action == "complete":
        if item["status"] == "completed":
            location = "，该事项已归档" if archived_item is not None else ""
            return OperationResult("skipped", "待办 {} 已经完成{}。".format(normalized_id, location)), False
        item["status"] = "completed"
        item["completed_at"] = timestamp
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
    now: Optional[datetime] = None,
) -> OperationResult:
    """Convenience wrapper for direct (non-plugin) invocation, e.g. scheduler."""
    return SqliteTodoStore(database_path, tenant_id).execute(
        action, todo_id=todo_id, title=title, scope=scope, now=now
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
                },
                ["action"],
            ),
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

    @property
    def tool_definitions(self) -> Mapping[str, PluginToolDefinition]:
        return self.TOOL_DEFINITIONS

    def is_available(self, tool_name: str) -> bool:
        return tool_name in self.TOOL_DEFINITIONS

    def execute(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> Any:
        if tool_name != "todo_manage":
            raise PluginError("未知待办工具：{}".format(tool_name))
        tenant_id = str(getattr(tenant, "tenant_id", "") or "")
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
        try:
            result = SqliteTodoStore(self._database_path, tenant_id).execute(
                action, todo_id=todo_id, title=title, scope=scope
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
        now: Optional[datetime] = None,
    ) -> OperationResult:
        """Direct invocation for scheduler or internal use (no plugin protocol)."""
        return SqliteTodoStore(self._database_path, tenant_id).execute(
            action, todo_id=todo_id, title=title, scope=scope, now=now
        )

    def preview(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> str:
        action = arguments.get("action", "")
        return "执行待办操作：{}".format(action)

    def close_tenant(self, tenant_id: str) -> None:
        pass

    def close(self) -> None:
        pass
