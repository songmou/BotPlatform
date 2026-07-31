"""Minimal tenant-scoped Codex task plugin.

The host only sees five regular plugin tools. Codex approvals, user-input
requests, external thread monitoring, lifecycle hooks, and notifications are
intentionally unsupported.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .base import PluginContext, PluginError, PluginToolDefinition


PROJECT_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"completed", "failed", "interrupted"}
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
USER_INPUT_METHOD = "item/tool/requestUserInput"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _enum_text(value: Any) -> str:
    raw = _value(value, "value", value)
    return str(raw or "")


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


@dataclass(frozen=True)
class CodexProject:
    id: str
    path: Path


@dataclass(frozen=True)
class CodexTasksConfig:
    allowed_tenant_ids: Tuple[str, ...]
    projects: Dict[str, CodexProject]
    default_project: str
    max_concurrent_tasks: int = 1

    @classmethod
    def from_mapping(
        cls,
        settings: Mapping[str, Any],
        project_root: Path,
    ) -> "CodexTasksConfig":
        CodexTasksPlugin.validate_settings(settings)
        projects: Dict[str, CodexProject] = {}
        for item in settings["projects"]:
            raw_path = Path(str(item["path"])).expanduser()
            path = raw_path if raw_path.is_absolute() else project_root / raw_path
            projects[str(item["id"])] = CodexProject(
                str(item["id"]), path.resolve()
            )
        return cls(
            allowed_tenant_ids=tuple(
                str(item) for item in settings.get("allowed_tenant_ids", [])
            ),
            projects=projects,
            default_project=str(settings["default_project"]),
            max_concurrent_tasks=int(settings.get("max_concurrent_tasks", 1)),
        )


class CodexTaskStore:
    """Small SQLite repository stored inside one tenant's plugin directory."""

    def __init__(self, database_path: Path) -> None:
        self.path = database_path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'queued', 'running', 'completed',
                            'failed', 'interrupted'
                        )
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    result_excerpt TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_tasks_status_created
                    ON tasks(status, created_at DESC);
                """
            )
            connection.execute(
                "UPDATE tasks SET status='interrupted', finished_at=?, updated_at=?, "
                "error=COALESCE(error, '服务重启，任务已中断') "
                "WHERE status IN ('queued','running')",
                (_utc_now(), _utc_now()),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    def create(self, task_id: str, project_id: str, title: str) -> Dict[str, Any]:
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id, project_id, title, status, "
                "created_at, updated_at) VALUES (?, ?, ?, 'queued', ?, ?)",
                (task_id, project_id, title, now, now),
            )
        return self.get(task_id) or {}

    def requeue(self, task_id: str) -> Dict[str, Any]:
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE tasks SET status='queued', updated_at=?, started_at=NULL, "
                "finished_at=NULL, result_excerpt=NULL, error=NULL WHERE task_id=?",
                (now, task_id),
            )
        return self.get(task_id) or {}

    def mark_running(self, task_id: str) -> None:
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE tasks SET status='running', started_at=?, updated_at=? "
                "WHERE task_id=?",
                (now, now, task_id),
            )

    def finish(
        self,
        task_id: str,
        status: str,
        result_excerpt: str = "",
        error: str = "",
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError("无效的任务终态")
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE tasks SET status=?, result_excerpt=?, error=?, "
                "finished_at=?, updated_at=? WHERE task_id=?",
                (
                    status,
                    result_excerpt or None,
                    error or None,
                    now,
                    now,
                    task_id,
                ),
            )

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._dict(row)

    def list(self, status: str, limit: int) -> List[Dict[str, Any]]:
        where = ""
        params: List[Any] = []
        if status == "active":
            where = " WHERE status IN ('queued','running')"
        elif status != "all":
            where = " WHERE status=?"
            params.append(status)
        params.append(limit)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks{} ORDER BY created_at DESC LIMIT ?".format(
                    where
                ),
                params,
            ).fetchall()
        return [dict(row) for row in rows]


class _SafeCodexSession:
    """Pinned SDK façade with fail-closed interaction handling."""

    def __init__(
        self,
        request_handler: Callable[[str, Optional[Dict[str, Any]]], Dict[str, Any]],
    ) -> None:
        from openai_codex.client import CodexClient

        self._client = CodexClient(approval_handler=request_handler)
        try:
            self._client.start()
            self._client.initialize()
        except Exception:
            self._client.close()
            raise

    @staticmethod
    def _params(cwd: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "approvalPolicy": "never",
            "sandbox": "workspace-write",
        }
        if cwd:
            params["cwd"] = cwd
        return params

    def thread_start(self, *, cwd: str, **_kwargs: Any) -> Any:
        from openai_codex import Thread

        response = self._client.thread_start(self._params(cwd))
        return Thread(self._client, response.thread.id)

    def thread_resume(
        self, thread_id: str, *, cwd: Optional[str] = None, **_kwargs: Any
    ) -> Any:
        from openai_codex import Thread

        response = self._client.thread_resume(thread_id, self._params(cwd))
        return Thread(self._client, response.thread.id)

    def close(self) -> None:
        self._client.close()


class CodexTaskService:
    RESULT_LIMIT = 6000

    def __init__(
        self,
        config: CodexTasksConfig,
        context: PluginContext,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.config = config
        self.context = context
        self._client_factory = client_factory
        self._executor = ThreadPoolExecutor(
            max_workers=config.max_concurrent_tasks,
            thread_name_prefix="codex-task",
        )
        self._slots = threading.BoundedSemaphore(
            config.max_concurrent_tasks
        )
        self._lock = threading.RLock()
        self._active: Dict[str, Any] = {}
        self._sessions: Dict[str, Any] = {}
        self._stores: Dict[str, CodexTaskStore] = {}
        self._task_tenants: Dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._blocked_interactions: set[str] = set()
        self._closed = False

    @property
    def sdk_importable(self) -> bool:
        try:
            return importlib.util.find_spec("openai_codex") is not None
        except (ImportError, ModuleNotFoundError):
            return False

    def _store(self, tenant_id: str) -> CodexTaskStore:
        with self._lock:
            store = self._stores.get(tenant_id)
            if store is None:
                root = self.context.tenant_data_dir("codex_tasks", tenant_id)
                store = CodexTaskStore(root / "tasks.sqlite3")
                self._stores[tenant_id] = store
            return store

    def _new_session(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        try:
            return _SafeCodexSession(self._handle_server_request)
        except ImportError as exc:
            raise PluginError("缺少 openai-codex 插件依赖") from exc
        except Exception as exc:
            raise PluginError("无法启动 Codex：{}".format(self._safe_error(exc))) from exc

    @staticmethod
    def _safe_error(exc: Any) -> str:
        text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        text = re.sub(
            r"(?i)((?:api[_-]?key|access[_-]?token|token)\s*[=:]\s*)[^,\s&]+",
            r"\1<redacted>",
            text,
        )
        return text[:1000]

    def project(self, project_id: Optional[str]) -> CodexProject:
        selected = project_id or self.config.default_project
        project = self.config.projects.get(selected)
        if project is None:
            raise PluginError("未知或未开放的 Codex 项目：{}".format(selected))
        if not project.path.is_dir():
            raise PluginError("Codex 项目目录不存在：{}".format(project.id))
        return project

    @staticmethod
    def _task_id(thread: Any) -> str:
        task_id = str(_value(thread, "id", "") or "")
        if not task_id:
            raise PluginError("Codex 没有返回有效的任务编号")
        return task_id

    @staticmethod
    def _summary(task: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "task_id": str(task["task_id"]),
            "project_id": str(task["project_id"]),
            "title": str(task["title"]),
            "status": str(task["status"]),
            "created_at": str(task["created_at"]),
            "updated_at": str(task["updated_at"]),
            "result": str(task.get("result_excerpt") or ""),
            "error": str(task.get("error") or ""),
        }

    def create_task(
        self,
        tenant_id: str,
        title: str,
        instruction: str,
        project_id: Optional[str],
    ) -> Dict[str, Any]:
        if not self._slots.acquire(blocking=False):
            raise PluginError("Codex 任务已达到并发上限，请等待现有任务结束")
        session = None
        try:
            project = self.project(project_id)
            session = self._new_session()
            thread = session.thread_start(cwd=str(project.path))
            task_id = self._task_id(thread)
            setter = getattr(thread, "set_name", None)
            if callable(setter):
                setter(title)
            task = self._store(tenant_id).create(task_id, project.id, title)
            with self._lock:
                if self._closed:
                    raise PluginError("Codex 任务插件已经关闭")
                self._sessions[task_id] = session
                self._task_tenants[task_id] = tenant_id
            self._executor.submit(
                self._execute_turn,
                tenant_id,
                project,
                session,
                thread,
                task_id,
                instruction,
            )
            return self._summary(task)
        except Exception:
            if session is not None:
                self._close_session(session)
            self._slots.release()
            raise

    def continue_task(
        self, tenant_id: str, task_id: str, instruction: str
    ) -> Dict[str, Any]:
        store = self._store(tenant_id)
        task = store.get(task_id)
        if task is None:
            raise PluginError("Codex 任务不存在或不属于当前用户")
        if task["status"] in ACTIVE_STATUSES:
            raise PluginError("Codex 任务仍在运行，请等待完成或先中止")
        if not self._slots.acquire(blocking=False):
            raise PluginError("Codex 任务已达到并发上限，请等待现有任务结束")
        session = None
        try:
            project = self.project(str(task["project_id"]))
            session = self._new_session()
            thread = session.thread_resume(task_id, cwd=str(project.path))
            task = store.requeue(task_id)
            with self._lock:
                if self._closed:
                    raise PluginError("Codex 任务插件已经关闭")
                self._sessions[task_id] = session
                self._task_tenants[task_id] = tenant_id
                self._cancelled.discard(task_id)
                self._blocked_interactions.discard(task_id)
            self._executor.submit(
                self._execute_turn,
                tenant_id,
                project,
                session,
                thread,
                task_id,
                instruction,
            )
            return self._summary(task)
        except Exception:
            if session is not None:
                self._close_session(session)
            self._slots.release()
            raise

    def _execute_turn(
        self,
        tenant_id: str,
        _project: CodexProject,
        session: Any,
        thread: Any,
        task_id: str,
        instruction: str,
    ) -> None:
        store = self._store(tenant_id)
        store.mark_running(task_id)
        result: Any = None
        status = "completed"
        error = ""
        try:
            handle = thread.turn(instruction, sandbox=self._sandbox_option())
            with self._lock:
                self._active[task_id] = handle
                cancelled = task_id in self._cancelled
            if cancelled:
                handle.interrupt()
            result = handle.run()
            raw_status = _enum_text(_value(result, "status", "completed")).lower()
            raw_error = _value(result, "error")
            with self._lock:
                cancelled = task_id in self._cancelled
                blocked = task_id in self._blocked_interactions
            if blocked:
                status = "failed"
                error = "任务请求了额外审批、权限或补充信息；精简模式已安全拒绝，请调整指令后继续"
            elif cancelled or "interrupt" in raw_status:
                status = "interrupted"
            elif raw_error or "fail" in raw_status:
                status = "failed"
                error = self._safe_error(raw_error or raw_status)
        except Exception as exc:
            with self._lock:
                cancelled = task_id in self._cancelled
                blocked = task_id in self._blocked_interactions
            status = "interrupted" if cancelled else "failed"
            error = (
                "任务请求了额外审批、权限或补充信息；精简模式已安全拒绝，请调整指令后继续"
                if blocked
                else self._safe_error(exc)
            )
        finally:
            with self._lock:
                self._active.pop(task_id, None)
                self._sessions.pop(task_id, None)
                self._task_tenants.pop(task_id, None)
                self._cancelled.discard(task_id)
                self._blocked_interactions.discard(task_id)
            self._close_session(session)
        final_response = str(_value(result, "final_response", "") or "")
        try:
            store.finish(
                task_id,
                status,
                final_response[: self.RESULT_LIMIT],
                error,
            )
        finally:
            self._slots.release()

    @staticmethod
    def _sandbox_option() -> Any:
        try:
            from openai_codex import Sandbox

            return Sandbox.workspace_write
        except ImportError:
            return "workspace_write"

    def _handle_server_request(
        self, method: str, params: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        payload = dict(params or {})
        task_id = str(payload.get("threadId") or "")
        if task_id:
            with self._lock:
                self._blocked_interactions.add(task_id)
                handle = self._active.get(task_id)
            if handle is not None:
                timer = threading.Timer(0.01, handle.interrupt)
                timer.daemon = True
                timer.start()
        if method == USER_INPUT_METHOD:
            return {"answers": {}}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        return {"decision": "decline"}

    def list_tasks(self, tenant_id: str, status: str, limit: int) -> Dict[str, Any]:
        return {
            "tasks": [
                self._summary(item)
                for item in self._store(tenant_id).list(status, limit)
            ]
        }

    def get_task(self, tenant_id: str, task_id: str) -> Dict[str, Any]:
        task = self._store(tenant_id).get(task_id)
        if task is None:
            raise PluginError("Codex 任务不存在或不属于当前用户")
        return self._summary(task)

    def cancel_task(self, tenant_id: str, task_id: str) -> Dict[str, Any]:
        store = self._store(tenant_id)
        task = store.get(task_id)
        if task is None:
            raise PluginError("Codex 任务不存在或不属于当前用户")
        if task["status"] not in ACTIVE_STATUSES:
            raise PluginError("Codex 任务当前不在运行")
        with self._lock:
            self._cancelled.add(task_id)
            handle = self._active.get(task_id)
        if handle is not None:
            try:
                handle.interrupt()
            except Exception as exc:
                raise PluginError(
                    "中止 Codex 任务失败：{}".format(self._safe_error(exc))
                ) from exc
        return self._summary(store.get(task_id) or task)

    def close_tenant(self, tenant_id: str) -> None:
        with self._lock:
            task_ids = [
                task_id
                for task_id, owner in self._task_tenants.items()
                if owner == tenant_id
            ]
        for task_id in task_ids:
            try:
                self.cancel_task(tenant_id, task_id)
            except PluginError:
                pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handles = list(self._active.values())
            sessions = list(self._sessions.values())
            self._cancelled.update(self._active)
        for handle in handles:
            try:
                handle.interrupt()
            except Exception:
                pass
        self._executor.shutdown(wait=True)
        for session in sessions:
            self._close_session(session)

    @staticmethod
    def _close_session(session: Any) -> None:
        closer = getattr(session, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass


class CodexTasksPlugin:
    id = "codex_tasks"
    TOOL_DEFINITIONS: Dict[str, PluginToolDefinition] = {
        "codex_list_tasks": PluginToolDefinition(
            "列出当前用户通过本插件创建的 Codex 开发任务。",
            _object_schema(
                {
                    "status": {
                        "type": "string",
                        "enum": [
                            "all", "active", "completed", "failed", "interrupted"
                        ],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                }
            ),
        ),
        "codex_get_task": PluginToolDefinition(
            "查看 Codex 开发任务状态、结果摘要或错误。",
            _object_schema({"task_id": {"type": "string"}}, ["task_id"]),
        ),
        "codex_create_task": PluginToolDefinition(
            "创建并后台执行新的 Codex 开发任务。",
            _object_schema(
                {
                    "title": {"type": "string"},
                    "instruction": {"type": "string"},
                    "project_id": {"type": "string"},
                },
                ["title", "instruction"],
            ),
            requires_approval=True,
        ),
        "codex_continue_task": PluginToolDefinition(
            "继续一个已结束或中断的插件自有 Codex 开发任务。",
            _object_schema(
                {
                    "task_id": {"type": "string"},
                    "instruction": {"type": "string"},
                },
                ["task_id", "instruction"],
            ),
            requires_approval=True,
        ),
        "codex_cancel_task": PluginToolDefinition(
            "中止当前用户通过本插件启动的 Codex 开发任务。",
            _object_schema({"task_id": {"type": "string"}}, ["task_id"]),
            requires_approval=True,
        ),
    }

    @classmethod
    def validate_settings(cls, settings: Mapping[str, Any]) -> None:
        allowed = {
            "allowed_tenant_ids",
            "projects",
            "default_project",
            "max_concurrent_tasks",
        }
        unknown = sorted(set(settings) - allowed)
        if unknown:
            raise ValueError("codex_tasks 包含未知配置：{}".format("、".join(unknown)))
        tenants = settings.get("allowed_tenant_ids")
        if not isinstance(tenants, list) or not all(
            isinstance(item, str) and item.strip() for item in tenants
        ):
            raise ValueError("codex_tasks.allowed_tenant_ids 必须是字符串数组")
        if len(set(tenants)) != len(tenants):
            raise ValueError("codex_tasks.allowed_tenant_ids 不能重复")
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
        if default_project not in project_ids:
            raise ValueError("codex_tasks.default_project 必须引用已配置项目")
        maximum = settings.get("max_concurrent_tasks", 1)
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 4:
            raise ValueError("codex_tasks.max_concurrent_tasks 必须是 1 到 4 的整数")

    def __init__(
        self,
        settings: Mapping[str, Any],
        context: Optional[PluginContext] = None,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        if context is None:
            raise ValueError("codex_tasks 缺少插件运行上下文")
        self.config = CodexTasksConfig.from_mapping(settings, context.project_root)
        self.service = CodexTaskService(
            self.config, context, client_factory=client_factory
        )

    @property
    def tool_definitions(self) -> Mapping[str, PluginToolDefinition]:
        return self.TOOL_DEFINITIONS

    def start(self) -> None:
        pass

    def is_available(self, tool_name: str, tenant: Any = None) -> bool:
        if tool_name not in self.TOOL_DEFINITIONS or not self.service.sdk_importable:
            return False
        if tenant is None:
            return bool(self.config.allowed_tenant_ids)
        tenant_id = str(getattr(tenant, "tenant_id", "") or "")
        return tenant_id in self.config.allowed_tenant_ids

    def _tenant_id(self, tenant: Any) -> str:
        tenant_id = str(getattr(tenant, "tenant_id", "") or "")
        if not tenant_id or tenant_id not in self.config.allowed_tenant_ids:
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
            if status not in {"all", "active", "completed", "failed", "interrupted"}:
                raise PluginError("status 值无效")
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
                raise PluginError("limit 必须是 1 到 20 的整数")
            return self.service.list_tasks(tenant_id, status, limit)
        if tool_name == "codex_get_task":
            return self.service.get_task(
                tenant_id, self._text(arguments, "task_id", 200)
            )
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

    def close(self) -> None:
        self.service.close()
