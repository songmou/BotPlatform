"""Tool schemas and safe local filesystem/system execution."""

from __future__ import annotations

import difflib
import copy
import fnmatch
import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from src.core.config.loader import ToolConfig
from src.core.plugins.base import PlatformPlugin, PluginError, PluginToolDefinition
from .commands import CommandRunner
from .models import ToolAuditContext, ToolError, ToolResult
from src.core.storage.tenants import TenantContext, TenantRegistry

if TYPE_CHECKING:
    from src.core.services.script import ScriptService
    from src.core.services.knowledge import KnowledgeService
    from src.core.tooling.mcp_client import McpClientManager


APPROVAL_TOOLS = {
    "create_directory",
    "write_text_file",
    "replace_text",
    "copy_path",
    "move_path",
    "move_to_trash",
    "run_command",
    "run_script",
    "knowledge_add_text",
    "knowledge_index_file",
    "knowledge_delete",
}


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


TOOL_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "knowledge_add_text": {
        "description": "把用户明确提供的纯文本保存到当前用户的私人知识库。",
        "parameters": _object_schema(
            {"name": {"type": "string"}, "content": {"type": "string"}},
            ["name", "content"],
        ),
    },
    "knowledge_index_file": {
        "description": "索引当前用户 workspace 内的 UTF-8 TXT 或 Markdown 文件。",
        "parameters": _object_schema({"path": {"type": "string"}}, ["path"]),
    },
    "knowledge_search": {
        "description": "检索当前用户的私人知识库。",
        "parameters": _object_schema(
            {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
            ["query"],
        ),
    },
    "knowledge_list": {
        "description": "列出当前用户的知识来源及索引状态。",
        "parameters": _object_schema(),
    },
    "knowledge_delete": {
        "description": "按来源编号删除当前用户的一项知识及其索引。",
        "parameters": _object_schema({"source_id": {"type": "string"}}, ["source_id"]),
    },
    "list_allowed_roots": {
        "description": "显示本机工具允许访问的根目录和当前默认工作目录。",
        "parameters": _object_schema(),
    },
    "list_directory": {
        "description": "列出目录中的文件和子目录；相对路径基于默认工作目录。",
        "parameters": _object_schema(
            {
                "path": {"type": "string", "description": "目录路径，默认 ."},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1},
            }
        ),
    },
    "find_files": {
        "description": "在目录中递归按文件名查找文件或目录，支持 * 和 ? 通配符。",
        "parameters": _object_schema(
            {
                "query": {"type": "string"},
                "path": {"type": "string", "description": "默认 ."},
                "max_results": {"type": "integer", "minimum": 1},
            },
            ["query"],
        ),
    },
    "search_text": {
        "description": "在开放目录的 UTF-8 文本文件中搜索文字。",
        "parameters": _object_schema(
            {
                "query": {"type": "string"},
                "path": {"type": "string", "description": "默认 ."},
                "glob": {"type": "string", "description": "可选文件名模式，如 *.py"},
                "case_sensitive": {"type": "boolean"},
                "max_results": {"type": "integer", "minimum": 1},
            },
            ["query"],
        ),
    },
    "read_text_file": {
        "description": "按行读取开放目录中的 UTF-8 文本文件。",
        "parameters": _object_schema(
            {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 400},
            },
            ["path"],
        ),
    },
    "get_path_info": {
        "description": "获取文件或目录的类型、大小和修改时间。",
        "parameters": _object_schema({"path": {"type": "string"}}, ["path"]),
    },
    "get_current_time": {
        "description": "获取机器人配置时区中的当前日期和时间。",
        "parameters": _object_schema(),
    },
    "get_system_info": {
        "description": "获取本机操作系统、架构和主机名，不返回环境变量。",
        "parameters": _object_schema(),
    },
    "get_disk_usage": {
        "description": "获取开放目录所在磁盘的容量和可用空间。",
        "parameters": _object_schema({"path": {"type": "string"}}),
    },
    "list_processes": {
        "description": "列出本机进程的 PID 和程序名，不包含完整命令参数。",
        "parameters": _object_schema(
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            }
        ),
    },
    "create_directory": {
        "description": "新建目录，需要用户确认。",
        "parameters": _object_schema(
            {"path": {"type": "string"}, "parents": {"type": "boolean"}}, ["path"]
        ),
    },
    "write_text_file": {
        "description": "新建或覆盖 UTF-8 文本文件，需要用户确认。",
        "parameters": _object_schema(
            {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["create", "overwrite"]},
            },
            ["path", "content", "mode"],
        ),
    },
    "replace_text": {
        "description": "在文本文件中精确替换内容，需要用户确认。",
        "parameters": _object_schema(
            {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "expected_count": {"type": "integer", "minimum": 1},
            },
            ["path", "old_text", "new_text"],
        ),
    },
    "copy_path": {
        "description": "复制文件或目录到一个不存在的目标路径，需要用户确认。",
        "parameters": _object_schema(
            {"source": {"type": "string"}, "destination": {"type": "string"}},
            ["source", "destination"],
        ),
    },
    "move_path": {
        "description": "移动文件或目录到一个不存在的目标路径，需要用户确认。",
        "parameters": _object_schema(
            {"source": {"type": "string"}, "destination": {"type": "string"}},
            ["source", "destination"],
        ),
    },
    "move_to_trash": {
        "description": "把文件或目录移到 iLinkBot 专用废纸篓，不会永久删除，需要用户确认。",
        "parameters": _object_schema({"path": {"type": "string"}}, ["path"]),
    },
    "run_command": {
        "description": "使用白名单档案在 macOS 沙箱中运行命令，需要用户确认；不支持 shell 字符串。",
        "parameters": _object_schema(
            {
                "profile": {
                    "type": "string",
                    "enum": [
                        "python", "git_readonly", "node", "npm_script",
                        "ollama_readonly", "workspace_script"
                    ],
                },
                "args": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            ["profile", "args"],
        ),
    },
    "list_scripts": {
        "description": "列出 iLinkBot 已注册、可由模型请求运行的固定脚本及其参数。",
        "parameters": _object_schema(),
    },
    "run_script": {
        "description": (
            "提交已注册的固定脚本到后台异步运行，立即返回任务编号（状态通常为 running）。"
            "脚本在后台执行，完成后其结果摘要和产物会自动推送给用户，无需你在对话中等待。"
            "提交成功后应直接告知用户“已提交，结果将在完成后自动发送”，"
            "不要反复调用 get_script_run 轮询等待完成（会耗尽工具调用轮次）。"
        ),
        "parameters": _object_schema(
            {
                "script_id": {"type": "string"},
                "parameters": {
                    "type": "object",
                    "description": "脚本的具名参数；先用 list_scripts 查看允许值。",
                    "additionalProperties": True,
                },
            },
            ["script_id"],
        ),
    },
    "get_script_run": {
        "description": (
            "仅在用户明确要求查询某个任务编号的当前状态时，做一次性状态查询；"
            "不要用它轮询等待脚本完成，脚本结果会在完成后自动推送。"
        ),
        "parameters": _object_schema(
            {"run_id": {"type": "string"}}, ["run_id"]
        ),
    },
}


class ToolRuntime:
    def __init__(
        self,
        config: ToolConfig,
        timezone_name: str,
        trash_directory: Optional[Path] = None,
        audit_logger: Optional[
            Callable[[ToolAuditContext, str, str, float, int], None]
        ] = None,
        sandbox_available: Any = None,
        script_service: Optional["ScriptService"] = None,
        tenant_registry: Optional[TenantRegistry] = None,
        knowledge_service: Optional["KnowledgeService"] = None,
        plugins: Optional[Iterable[PlatformPlugin]] = None,
        tool_audit_store: Optional[Any] = None,
        tool_states: Optional[Dict[str, Dict[str, Any]]] = None,
        mcp_manager: Optional["McpClientManager"] = None,
    ) -> None:
        self.base_config = config
        self.config = config
        self.timezone = ZoneInfo(timezone_name)
        self.roots = [Path(item).resolve() for item in config.allowed_roots]
        self.default_directory = Path(config.default_working_directory).resolve()
        self.trash_directory = trash_directory or (Path.home() / ".Trash" / "iLinkBot")
        self.audit_logger = audit_logger
        self.script_service = script_service
        self.tenant_registry = tenant_registry
        self.knowledge_service = knowledge_service
        self.tenant: Optional[TenantContext] = None
        self.plugins = list(plugins or [])
        self._plugin_tools: Dict[str, PlatformPlugin] = {}
        for plugin in self.plugins:
            for tool_name in plugin.tool_definitions:
                if tool_name in TOOL_DEFINITIONS or tool_name in self._plugin_tools:
                    raise ValueError("平台插件工具名称重复：{}".format(tool_name))
                self._plugin_tools[tool_name] = plugin
        self._sandbox_available = sandbox_available
        self.command_runner = CommandRunner(
            config, self.resolve_path, sandbox_available=sandbox_available
        )
        self.tool_audit_store = tool_audit_store
        self._tool_states: Dict[str, Dict[str, Any]] = tool_states or {}
        self.mcp_manager = mcp_manager

    def is_tool_enabled(self, name: str) -> bool:
        state = self._tool_states.get(name)
        if state is not None:
            return state.get("enabled", True)
        return True

    def get_tool_state(self, name: str) -> Dict[str, Any]:
        state = self._tool_states.get(name, {})
        return {
            "enabled": state.get("enabled", True),
            "require_approval": state.get(
                "require_approval", name in APPROVAL_TOOLS
            ),
        }

    def reload_tool_states(self, states: Dict[str, Dict[str, Any]]) -> None:
        self._tool_states = states

    def bind_tenant(self, tenant: TenantContext) -> None:
        """Fail-closed binding of all filesystem and script tools to one tenant."""
        if self.tenant_registry is None:
            return
        registered = self.tenant_registry.get(tenant.tenant_id)
        if registered != tenant:
            raise ToolError("租户身份不匹配")
        workspace = self.tenant_registry.tenant_root(tenant.tenant_id) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(workspace), 0o700)
        self.tenant = tenant
        self.roots = [workspace.resolve()]
        self.default_directory = workspace.resolve()
        self.trash_directory = self.tenant_registry.tenant_root(tenant.tenant_id) / ".trash"
        self.config = replace(
            self.base_config,
            allowed_roots=[str(self.default_directory)],
            default_working_directory=str(self.default_directory),
        )
        self.command_runner = CommandRunner(
            self.config,
            self.resolve_path,
            sandbox_available=self._sandbox_available,
        )

    def _require_tenant(self) -> Optional[TenantContext]:
        if self.tenant_registry is not None and self.tenant is None:
            raise ToolError("工具尚未绑定用户工作区")
        return self.tenant

    def is_available(self, name: str) -> bool:
        if not self.is_tool_enabled(name):
            return False
        plugin = self._plugin_tools.get(name)
        if plugin is not None:
            return plugin.is_available(name)
        if self.mcp_manager is not None and self.mcp_manager.has_tool(name):
            return self.mcp_manager.is_available(name)
        if name not in TOOL_DEFINITIONS:
            return False
        if name == "run_command":
            return self.command_runner.available
        if name in {"list_scripts", "run_script", "get_script_run"}:
            return self.script_service is not None
        if name.startswith("knowledge_"):
            return self.knowledge_service is not None
        return True

    def _definition(self, name: str) -> Optional[Dict[str, Any]]:
        definition = TOOL_DEFINITIONS.get(name)
        if definition is not None:
            return definition
        plugin = self._plugin_tools.get(name)
        if plugin is None:
            if self.mcp_manager is not None:
                mcp_definition = self.mcp_manager.tool_schema(name)
                if mcp_definition is not None:
                    return mcp_definition
            return None
        plugin_definition = plugin.tool_definitions.get(name)
        if plugin_definition is None:
            return None
        return {
            "description": plugin_definition.description,
            "parameters": plugin_definition.parameters,
        }

    def schemas(self, names: Iterable[str]) -> List[Dict[str, Any]]:
        schemas: List[Dict[str, Any]] = []
        for name in names:
            definition = self._definition(name)
            if not definition or not self.is_available(name):
                continue
            parameters = copy.deepcopy(definition["parameters"])
            description = definition["description"]
            if name == "run_command":
                parameters["properties"]["profile"]["enum"] = list(
                    self.config.enabled_command_profiles
                )
            if name == "run_script" and self.script_service:
                parameters["properties"]["script_id"]["enum"] = list(
                    self.script_service.script_ids
                )
                catalog = "；".join(
                    "{}＝{}（{}）".format(item.id, item.name, item.description)
                    if item.description
                    else "{}＝{}".format(item.id, item.name)
                    for item in sorted(
                        self.script_service.definitions.values(),
                        key=lambda entry: entry.id,
                    )
                )
                if catalog:
                    description = "{}可用脚本：{}。".format(description, catalog)
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": parameters,
                    },
                }
            )
        return schemas

    def requires_approval(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> bool:
        plugin = self._plugin_tools.get(name)
        if plugin is not None:
            definition = plugin.tool_definitions.get(name)
            return bool(definition and definition.requires_approval)
        if name == "run_script" and self.script_service is not None:
            if arguments is None:
                return self.script_service.has_approval_required_scripts()
            if not isinstance(arguments, dict):
                return True
            return self.script_service.requires_approval(arguments.get("script_id"))
        state = self._tool_states.get(name)
        if state is not None and "require_approval" in state:
            return state["require_approval"]
        return name in APPROVAL_TOOLS

    def direct_response_text(
        self, name: str, result: Optional[ToolResult]
    ) -> Optional[str]:
        """Return a trusted plugin summary that should bypass model rewriting."""

        plugin = self._plugin_tools.get(name)
        definition = plugin.tool_definitions.get(name) if plugin is not None else None
        if not definition or not definition.direct_response or not result or not result.ok:
            return None
        if not isinstance(result.data, dict):
            return None
        summary = result.data.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return None
        return summary.strip()

    def script_approval_groups(self) -> tuple[List[str], List[str]]:
        if self.script_service is None:
            return [], []
        automatic: List[str] = []
        approval: List[str] = []
        for script in self.script_service.list_scripts():
            label = "{}（{}）".format(script["name"], script["id"])
            target = approval if script.get("requires_approval", True) else automatic
            target.append(label)
        return automatic, approval

    def _is_within_roots(self, path: Path) -> bool:
        return any(path == root or root in path.parents for root in self.roots)

    def _relative_to_root(self, path: Path) -> str:
        for root in self.roots:
            if path == root or root in path.parents:
                return path.relative_to(root).as_posix()
        return path.as_posix()

    def _is_denied(self, path: Path) -> bool:
        relative = self._relative_to_root(path)
        parts = path.parts
        if ".git" in parts:
            return True
        name = path.name
        if name == ".env" or name.startswith(".env.") or path.suffix.lower() in {".pem", ".key"}:
            return True
        for pattern in self.config.denied_globs:
            if fnmatch.fnmatch(relative, pattern) or Path(relative).match(pattern):
                return True
        return False

    def resolve_path(
        self,
        raw_path: str,
        base: Optional[Path] = None,
        must_exist: bool = False,
    ) -> Path:
        self._require_tenant()
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolError("路径必须是非空字符串")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = (base or self.default_directory) / candidate
        try:
            resolved = candidate.resolve(strict=must_exist)
        except (OSError, RuntimeError) as exc:
            raise ToolError("无法解析路径 {}：{}".format(raw_path, exc)) from exc
        if not self._is_within_roots(resolved):
            raise ToolError("路径超出允许目录：{}".format(resolved))
        if self._is_denied(resolved):
            raise ToolError("安全策略禁止访问该路径：{}".format(resolved))
        if must_exist and not resolved.exists():
            raise ToolError("路径不存在：{}".format(resolved))
        return resolved

    def _string(self, arguments: Dict[str, Any], name: str, default: Optional[str] = None) -> str:
        value = arguments.get(name, default)
        if not isinstance(value, str) or (default is None and not value):
            raise ToolError("{} 必须是字符串".format(name))
        return value

    def _integer(
        self, arguments: Dict[str, Any], name: str, default: int, minimum: int, maximum: int
    ) -> int:
        value = arguments.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise ToolError("{} 必须是 {} 到 {} 之间的整数".format(name, minimum, maximum))
        return value

    def _read_bytes(self, path: Path) -> bytes:
        if not path.is_file():
            raise ToolError("目标不是普通文件：{}".format(path))
        size = path.stat().st_size
        if size > self.config.max_read_bytes:
            raise ToolError(
                "文件大小 {} 字节，超过单次读取上限 {} 字节".format(
                    size, self.config.max_read_bytes
                )
            )
        data = path.read_bytes()
        if b"\x00" in data:
            raise ToolError("目标看起来是二进制文件，首版只支持文本")
        return data

    def _read_text(self, path: Path) -> str:
        try:
            return self._read_bytes(path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("文件不是有效的 UTF-8 文本：{}".format(path)) from exc

    def _validate_arguments(self, name: str, arguments: Dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ToolError("工具参数必须是 JSON 对象")
        definition = self._definition(name)
        if not definition:
            raise ToolError("未知工具：{}".format(name))
        parameters = definition["parameters"]
        properties = set(parameters.get("properties") or {})
        unknown = sorted(set(arguments) - properties)
        if unknown:
            raise ToolError("工具 {} 收到未知参数：{}".format(name, ", ".join(unknown)))
        missing = [field for field in parameters.get("required") or [] if field not in arguments]
        if missing:
            raise ToolError("工具 {} 缺少参数：{}".format(name, ", ".join(missing)))

    def preview(self, name: str, arguments: Dict[str, Any]) -> str:
        self._require_tenant()
        self._validate_arguments(name, arguments)
        if name not in APPROVAL_TOOLS:
            plugin = self._plugin_tools.get(name)
            plugin_definition = plugin.tool_definitions.get(name) if plugin else None
            if not plugin_definition or not plugin_definition.requires_approval:
                raise ToolError("该工具不需要审批：{}".format(name))
            try:
                return plugin.preview(name, arguments, self.tenant)
            except PluginError as exc:
                raise ToolError(str(exc)) from exc
        if name == "create_directory":
            path = self.resolve_path(self._string(arguments, "path"))
            if path.exists():
                raise ToolError("目标路径已存在：{}".format(path))
            return "新建目录：{}".format(path)
        if name == "write_text_file":
            return self._preview_write(arguments)
        if name == "replace_text":
            return self._preview_replace(arguments)
        if name in {"copy_path", "move_path"}:
            source = self.resolve_path(self._string(arguments, "source"), must_exist=True)
            destination = self.resolve_path(self._string(arguments, "destination"))
            if destination.exists():
                raise ToolError("目标路径已存在：{}".format(destination))
            action = "复制" if name == "copy_path" else "移动"
            return "{}：{}\n到：{}".format(action, source, destination)
        if name == "move_to_trash":
            path = self.resolve_path(self._string(arguments, "path"), must_exist=True)
            if path in self.roots:
                raise ToolError("不能把开放根目录本身移到废纸篓")
            return "移到废纸篓：{}".format(path)
        if name == "run_command":
            return self.command_runner.prepare(arguments).preview()
        if name == "run_script":
            if not self.script_service:
                raise ToolError("固定脚本服务不可用")
            try:
                return self.script_service.preview(
                    self._string(arguments, "script_id"),
                    arguments.get("parameters", {}),
                )
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
        if name == "knowledge_add_text":
            return "保存私人知识：{}\n内容长度：{} 字符".format(
                self._string(arguments, "name"), len(self._string(arguments, "content"))
            )
        if name == "knowledge_index_file":
            path = self.resolve_path(self._string(arguments, "path"), must_exist=True)
            return "索引私人知识文件：{}".format(path)
        if name == "knowledge_delete":
            return "删除私人知识来源：{}".format(self._string(arguments, "source_id"))
        raise ToolError("工具缺少审批预览：{}".format(name))

    def execute(
        self,
        name: str,
        arguments: Dict[str, Any],
        audit_context: Optional[ToolAuditContext] = None,
    ) -> ToolResult:
        if not self.is_tool_enabled(name):
            return ToolResult(False, error="工具已被禁用")
        try:
            self._require_tenant()
        except ToolError as exc:
            return ToolResult(False, error=str(exc))
        started = time.monotonic()
        status = "失败"
        output_size = 0
        error_msg = ""
        try:
            self._validate_arguments(name, arguments)
            plugin = self._plugin_tools.get(name)
            if plugin is not None:
                data = plugin.execute(name, arguments, self.tenant)
            elif self.mcp_manager is not None and self.mcp_manager.has_tool(name):
                data = self.mcp_manager.call_tool(name, arguments)
            else:
                handler = getattr(self, "_tool_{}".format(name), None)
                if name not in TOOL_DEFINITIONS or not handler:
                    raise ToolError("未知工具：{}".format(name))
                data = handler(arguments)
            status = "成功"
            output_size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            return ToolResult(True, data=data)
        except (ToolError, PluginError, OSError, ValueError, subprocess.SubprocessError) as exc:
            error_msg = str(exc)
            return ToolResult(False, error=error_msg)
        finally:
            duration = time.monotonic() - started
            if self.audit_logger:
                self.audit_logger(
                    audit_context or ToolAuditContext(),
                    name,
                    status,
                    duration,
                    output_size,
                )
            if self.tool_audit_store is not None:
                try:
                    import hashlib
                    args_hash = hashlib.sha256(
                        json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest()[:16]
                    ctx = audit_context or ToolAuditContext()
                    self.tool_audit_store.record(
                        tenant_id=self.tenant.tenant_id if self.tenant else None,
                        session_id=ctx.session_id,
                        agent_id=ctx.agent_id,
                        tool_name=name,
                        status=status,
                        duration_ms=int(duration * 1000),
                        output_bytes=output_size,
                        args_hash=args_hash,
                        error=error_msg or None,
                    )
                except Exception:
                    pass

    def close_tenant(self, tenant_id: str) -> None:
        for plugin in self.plugins:
            plugin.close_tenant(tenant_id)

    def close(self) -> None:
        for plugin in reversed(self.plugins):
            try:
                plugin.close()
            except Exception:
                pass
        if self.mcp_manager is not None:
            try:
                self.mcp_manager.close()
            except Exception:
                pass

    def reload_plugins(self, plugins: Iterable[PlatformPlugin]) -> None:
        for plugin in reversed(self.plugins):
            try:
                plugin.close()
            except Exception:
                pass
        self.plugins = list(plugins)
        self._plugin_tools = {}
        for plugin in self.plugins:
            for tool_name in plugin.tool_definitions:
                if tool_name in TOOL_DEFINITIONS or tool_name in self._plugin_tools:
                    raise ValueError("平台插件工具名称重复：{}".format(tool_name))
                self._plugin_tools[tool_name] = plugin

    def _tool_list_allowed_roots(self, _arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "default_working_directory": str(self.default_directory),
            "allowed_roots": [str(root) for root in self.roots],
        }

    def _tool_list_scripts(self, _arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not self.script_service:
            raise ToolError("固定脚本服务不可用")
        return {"scripts": self.script_service.list_scripts()}

    def _tool_run_script(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not self.script_service:
            raise ToolError("固定脚本服务不可用")
        tenant = self._require_tenant()
        if tenant is None:
            raise ToolError("脚本工具需要租户身份")
        try:
            return self.script_service.submit_for_tenant(
                tenant,
                self._string(arguments, "script_id"),
                arguments.get("parameters", {}),
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    def _tool_get_script_run(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not self.script_service:
            raise ToolError("固定脚本服务不可用")
        tenant = self._require_tenant()
        if tenant is None:
            raise ToolError("脚本工具需要租户身份")
        try:
            return self.script_service.get_run(
                tenant, self._string(arguments, "run_id")
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    def _knowledge_tenant(self) -> TenantContext:
        tenant = self._require_tenant()
        if tenant is None or self.knowledge_service is None:
            raise ToolError("知识库服务需要租户身份")
        return tenant

    def _tool_knowledge_add_text(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        tenant = self._knowledge_tenant()
        return self.knowledge_service.add_text(
            tenant.tenant_id,
            self._string(arguments, "name"),
            self._string(arguments, "content"),
        )

    def _tool_knowledge_index_file(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        tenant = self._knowledge_tenant()
        path = self.resolve_path(self._string(arguments, "path"), must_exist=True)
        return self.knowledge_service.index_file(tenant, path)

    def _tool_knowledge_search(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        tenant = self._knowledge_tenant()
        limit = self._integer(arguments, "limit", 6, 1, 20)
        return {"results": self.knowledge_service.search(
            tenant.tenant_id, self._string(arguments, "query"), limit
        )}

    def _tool_knowledge_list(self, _arguments: Dict[str, Any]) -> Dict[str, Any]:
        tenant = self._knowledge_tenant()
        return {"sources": self.knowledge_service.list(tenant.tenant_id)}

    def _tool_knowledge_delete(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        tenant = self._knowledge_tenant()
        deleted = self.knowledge_service.delete(
            tenant.tenant_id, self._string(arguments, "source_id")
        )
        if not deleted:
            raise ToolError("未找到知识来源")
        return {"deleted": True}

    def _tool_list_directory(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        path = self.resolve_path(self._string(arguments, "path", "."), must_exist=True)
        if not path.is_dir():
            raise ToolError("目标不是目录：{}".format(path))
        depth = self._integer(arguments, "depth", 1, 1, 3)
        offset = self._integer(arguments, "offset", 0, 0, 1_000_000)
        requested_limit = self._integer(
            arguments, "limit", min(100, self.config.max_directory_entries), 1,
            self.config.max_directory_entries,
        )
        items: List[Dict[str, Any]] = []

        def visit(directory: Path, level: int) -> None:
            if len(items) >= offset + requested_limit + 1:
                return
            try:
                children = sorted(directory.iterdir(), key=lambda item: item.name.lower())
            except OSError as exc:
                raise ToolError("无法读取目录 {}：{}".format(directory, exc)) from exc
            for child in children:
                try:
                    resolved = self.resolve_path(str(child), must_exist=True)
                except ToolError:
                    continue
                stat = child.lstat()
                kind = "symlink" if child.is_symlink() else "directory" if resolved.is_dir() else "file"
                items.append(
                    {
                        "path": str(child.relative_to(path)),
                        "type": kind,
                        "size": stat.st_size if kind == "file" else None,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    }
                )
                if level < depth and kind == "directory":
                    visit(resolved, level + 1)
                if len(items) >= offset + requested_limit + 1:
                    return

        visit(path, 1)
        page = items[offset : offset + requested_limit]
        return {
            "directory": str(path),
            "items": page,
            "offset": offset,
            "has_more": len(items) > offset + requested_limit,
        }

    def _walk(self, root: Path) -> Iterable[Path]:
        for directory, directory_names, file_names in os.walk(str(root), followlinks=False):
            directory_path = Path(directory)
            allowed_directories: List[str] = []
            for name in directory_names:
                candidate = directory_path / name
                try:
                    resolved = self.resolve_path(str(candidate), must_exist=True)
                except ToolError:
                    continue
                if not candidate.is_symlink() and resolved.is_dir():
                    allowed_directories.append(name)
                yield candidate
            directory_names[:] = allowed_directories
            for name in file_names:
                candidate = directory_path / name
                try:
                    self.resolve_path(str(candidate), must_exist=True)
                except ToolError:
                    continue
                yield candidate

    def _tool_find_files(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        query = self._string(arguments, "query")
        root = self.resolve_path(self._string(arguments, "path", "."), must_exist=True)
        if not root.is_dir():
            raise ToolError("搜索起点不是目录：{}".format(root))
        limit = self._integer(
            arguments, "max_results", min(100, self.config.max_search_results), 1,
            self.config.max_search_results,
        )
        pattern = query if any(character in query for character in "*?[]") else "*{}*".format(query)
        results: List[Dict[str, str]] = []
        for candidate in self._walk(root):
            if fnmatch.fnmatch(candidate.name.lower(), pattern.lower()):
                results.append(
                    {
                        "path": str(candidate),
                        "type": "directory" if candidate.is_dir() else "file",
                    }
                )
                if len(results) >= limit:
                    break
        return {"root": str(root), "query": query, "results": results, "truncated": len(results) >= limit}

    def _tool_search_text(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        query = self._string(arguments, "query")
        if not query:
            raise ToolError("query 不能为空")
        root = self.resolve_path(self._string(arguments, "path", "."), must_exist=True)
        if not root.is_dir():
            raise ToolError("搜索起点不是目录：{}".format(root))
        file_glob = self._string(arguments, "glob", "*")
        case_sensitive = arguments.get("case_sensitive", False)
        if not isinstance(case_sensitive, bool):
            raise ToolError("case_sensitive 必须是布尔值")
        limit = self._integer(
            arguments, "max_results", min(100, self.config.max_search_results), 1,
            self.config.max_search_results,
        )
        needle = query if case_sensitive else query.lower()
        results: List[Dict[str, Any]] = []
        for candidate in self._walk(root):
            if not candidate.is_file() or not fnmatch.fnmatch(candidate.name, file_glob):
                continue
            try:
                if candidate.stat().st_size > self.config.max_read_bytes:
                    continue
                text = candidate.read_text(encoding="utf-8")
                if "\x00" in text:
                    continue
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                haystack = line if case_sensitive else line.lower()
                if needle in haystack:
                    results.append(
                        {"path": str(candidate), "line": line_number, "text": line[:500]}
                    )
                    if len(results) >= limit:
                        return {"root": str(root), "query": query, "results": results, "truncated": True}
        return {"root": str(root), "query": query, "results": results, "truncated": False}

    def _tool_read_text_file(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        path = self.resolve_path(self._string(arguments, "path"), must_exist=True)
        start_line = self._integer(arguments, "start_line", 1, 1, 10_000_000)
        max_lines = self._integer(arguments, "max_lines", 200, 1, 400)
        lines = self._read_text(path).splitlines()
        selected = lines[start_line - 1 : start_line - 1 + max_lines]
        return {
            "path": str(path),
            "start_line": start_line,
            "end_line": start_line + len(selected) - 1 if selected else start_line - 1,
            "total_lines": len(lines),
            "content": "\n".join(selected),
            "has_more": start_line - 1 + len(selected) < len(lines),
        }

    def _tool_get_path_info(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        path = self.resolve_path(self._string(arguments, "path"), must_exist=True)
        stat = path.stat()
        return {
            "path": str(path),
            "type": "directory" if path.is_dir() else "file" if path.is_file() else "other",
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "readable": os.access(str(path), os.R_OK),
            "writable": os.access(str(path), os.W_OK),
        }

    def _tool_get_current_time(self, _arguments: Dict[str, Any]) -> Dict[str, Any]:
        now = datetime.now(self.timezone)
        return {"timezone": str(self.timezone), "iso": now.isoformat(timespec="seconds")}

    def _tool_get_system_info(self, _arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.mac_ver()[0] or platform.version(),
            "architecture": platform.machine(),
            "hostname": socket.gethostname(),
            "python": platform.python_version(),
        }

    def _tool_get_disk_usage(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        path = self.resolve_path(self._string(arguments, "path", "."), must_exist=True)
        usage = shutil.disk_usage(str(path))
        return {"path": str(path), "total": usage.total, "used": usage.used, "free": usage.free}

    def _tool_list_processes(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        query = self._string(arguments, "query", "").lower()
        limit = self._integer(arguments, "limit", 50, 1, 200)
        try:
            completed = subprocess.run(
                ["/bin/ps", "-axo", "pid=,comm="],
                check=True,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolError("读取进程列表失败：{}".format(exc)) from exc
        processes: List[Dict[str, Any]] = []
        for raw_line in completed.stdout.decode("utf-8", errors="replace").splitlines():
            parts = raw_line.strip().split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            name = Path(parts[1]).name
            if query and query not in name.lower():
                continue
            processes.append({"pid": int(parts[0]), "name": name})
            if len(processes) >= limit:
                break
        return {"processes": processes, "truncated": len(processes) >= limit}

    def _tool_create_directory(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        path = self.resolve_path(self._string(arguments, "path"))
        if path.exists():
            raise ToolError("目标路径已存在：{}".format(path))
        parents = arguments.get("parents", False)
        if not isinstance(parents, bool):
            raise ToolError("parents 必须是布尔值")
        path.mkdir(parents=parents, exist_ok=False)
        return {"path": str(path), "created": True}

    def _validate_write(self, arguments: Dict[str, Any]) -> tuple[Path, str, str]:
        path = self.resolve_path(self._string(arguments, "path"))
        content = self._string(arguments, "content", "")
        mode = self._string(arguments, "mode")
        if mode not in {"create", "overwrite"}:
            raise ToolError("mode 仅支持 create 或 overwrite")
        if len(content.encode("utf-8")) > self.config.max_write_bytes:
            raise ToolError("写入内容超过 {} 字节上限".format(self.config.max_write_bytes))
        if mode == "create" and path.exists():
            raise ToolError("create 模式下目标必须不存在：{}".format(path))
        if mode == "overwrite" and not path.is_file():
            raise ToolError("overwrite 模式下目标必须是已有普通文件：{}".format(path))
        if not path.parent.is_dir():
            raise ToolError("目标父目录不存在：{}".format(path.parent))
        return path, content, mode

    def _preview_write(self, arguments: Dict[str, Any]) -> str:
        path, content, mode = self._validate_write(arguments)
        old = self._read_text(path) if path.exists() else ""
        diff = _limited_diff(old, content, str(path))
        return "{}文本文件：{}\n内容大小：{} 字节\n{}".format(
            "新建" if mode == "create" else "覆盖",
            path,
            len(content.encode("utf-8")),
            diff,
        )

    def _atomic_write(self, path: Path, content: str) -> None:
        existing_mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        descriptor, temp_name = tempfile.mkstemp(prefix=".ilinkbot-", dir=str(path.parent))
        try:
            os.fchmod(descriptor, existing_mode)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, str(path))
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _tool_write_text_file(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        path, content, mode = self._validate_write(arguments)
        self._atomic_write(path, content)
        return {"path": str(path), "mode": mode, "bytes_written": len(content.encode("utf-8"))}

    def _validate_replace(self, arguments: Dict[str, Any]) -> tuple[Path, str, str, str, int]:
        path = self.resolve_path(self._string(arguments, "path"), must_exist=True)
        old_text = self._string(arguments, "old_text")
        new_text = self._string(arguments, "new_text", "")
        expected = self._integer(arguments, "expected_count", 1, 1, 1_000_000)
        current = self._read_text(path)
        actual = current.count(old_text)
        if actual != expected:
            raise ToolError("预期匹配 {} 次，实际匹配 {} 次".format(expected, actual))
        updated = current.replace(old_text, new_text)
        if len(updated.encode("utf-8")) > self.config.max_write_bytes:
            raise ToolError("替换后的内容超过 {} 字节上限".format(self.config.max_write_bytes))
        return path, current, updated, old_text, expected

    def _preview_replace(self, arguments: Dict[str, Any]) -> str:
        path, current, updated, _old, count = self._validate_replace(arguments)
        return "修改文本文件：{}\n替换次数：{}\n{}".format(
            path, count, _limited_diff(current, updated, str(path))
        )

    def _tool_replace_text(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        path, _current, updated, _old, count = self._validate_replace(arguments)
        self._atomic_write(path, updated)
        return {"path": str(path), "replacements": count, "bytes_written": len(updated.encode("utf-8"))}

    def _source_destination(self, arguments: Dict[str, Any]) -> tuple[Path, Path]:
        source = self.resolve_path(self._string(arguments, "source"), must_exist=True)
        destination = self.resolve_path(self._string(arguments, "destination"))
        if source in self.roots:
            raise ToolError("不能移动或复制开放根目录本身")
        if destination.exists():
            raise ToolError("目标路径已存在：{}".format(destination))
        if not destination.parent.is_dir():
            raise ToolError("目标父目录不存在：{}".format(destination.parent))
        return source, destination

    def _tool_copy_path(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        source, destination = self._source_destination(arguments)
        if source.is_dir():
            shutil.copytree(str(source), str(destination), symlinks=True)
        elif source.is_file():
            shutil.copy2(str(source), str(destination), follow_symlinks=False)
        else:
            raise ToolError("只支持复制普通文件或目录")
        return {"source": str(source), "destination": str(destination)}

    def _tool_move_path(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        source, destination = self._source_destination(arguments)
        shutil.move(str(source), str(destination))
        return {"source": str(source), "destination": str(destination)}

    def _tool_move_to_trash(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        source = self.resolve_path(self._string(arguments, "path"), must_exist=True)
        if source in self.roots:
            raise ToolError("不能把开放根目录本身移到废纸篓")
        self.trash_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = self.trash_directory / "{}-{}-{}".format(
            stamp, uuid.uuid4().hex[:8], source.name
        )
        shutil.move(str(source), str(destination))
        return {"source": str(source), "trash_path": str(destination)}

    def _tool_run_command(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        prepared = self.command_runner.prepare(arguments)
        return self.command_runner.execute(prepared)


def _limited_diff(old: str, new: str, label: str, limit: int = 8192) -> str:
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=label,
            tofile=label,
        )
    )
    if not diff:
        diff = "（内容没有变化）"
    encoded = diff.encode("utf-8")
    if len(encoded) > limit:
        diff = encoded[:limit].decode("utf-8", errors="ignore") + "\n……差异已截断"
    return diff
