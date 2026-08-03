"""Tool schemas and safe local filesystem/system execution."""

from __future__ import annotations

import difflib
import copy
import fnmatch
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from src.core.config.loader import ToolConfig
from src.core.plugins.base import PlatformPlugin, PluginError
from .commands import CommandRunner
# APPROVAL_TOOLS/TOOL_DEFINITIONS are re-exported here for backward
# compatibility: existing callers import them from ``runtime``.
from .definitions import APPROVAL_TOOLS, TOOL_DEFINITIONS, _object_schema  # noqa: F401
from .models import ToolAuditContext, ToolError, ToolResult
from src.core.storage.tenants import TenantContext, TenantRegistry

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.core.services.script import ScriptService
    from src.core.services.script_schedule import ScriptScheduleService
    from src.core.services.knowledge import KnowledgeService
    from src.core.services.drive import DriveService
    from src.core.tooling.mcp_client import McpClientManager
    from src.core.plugins.manager import PluginManager
    from src.core.services.resources import ScopedResourceStore


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
        plugin_manager: Optional["PluginManager"] = None,
        tool_audit_store: Optional[Any] = None,
        tool_states: Optional[Dict[str, Dict[str, Any]]] = None,
        mcp_manager: Optional["McpClientManager"] = None,
        script_schedule_service: Optional["ScriptScheduleService"] = None,
        drive_service: Optional["DriveService"] = None,
        drive_audit_store: Optional[Any] = None,
        resource_store: Optional["ScopedResourceStore"] = None,
    ) -> None:
        self.base_config = config
        self.timezone = ZoneInfo(timezone_name)
        self._default_roots = [
            Path(item).resolve() for item in config.allowed_roots
        ]
        self._default_directory = Path(
            config.default_working_directory
        ).resolve()
        self._default_trash_directory = trash_directory or (
            Path.home() / ".Trash" / "iLinkBot"
        )
        self._binding = threading.local()
        self.audit_logger = audit_logger
        self.script_service = script_service
        self.script_schedule_service = script_schedule_service
        self.tenant_registry = tenant_registry
        self.knowledge_service = knowledge_service
        self.plugin_manager = plugin_manager
        self.plugins = list(plugins or []) if plugin_manager is None else []
        self._plugin_tools: Dict[str, PlatformPlugin] = {}
        if plugin_manager is not None:
            for tool_name in plugin_manager.tool_names:
                if tool_name in TOOL_DEFINITIONS:
                    raise ValueError("平台插件工具名称重复：{}".format(tool_name))
        else:
            for plugin in self.plugins:
                for tool_name in plugin.tool_definitions:
                    if tool_name in TOOL_DEFINITIONS or tool_name in self._plugin_tools:
                        raise ValueError("平台插件工具名称重复：{}".format(tool_name))
                    self._plugin_tools[tool_name] = plugin
        self._sandbox_available = sandbox_available
        self._default_command_runner = CommandRunner(
            config, self.resolve_path, sandbox_available=sandbox_available
        )
        self.tool_audit_store = tool_audit_store
        self._tool_states: Dict[str, Dict[str, Any]] = tool_states or {}
        self.mcp_manager = mcp_manager
        self.drive_service = drive_service
        self.drive_audit_store = drive_audit_store
        self.resource_store = resource_store
    @property
    def tenant(self) -> Optional[TenantContext]:
        return getattr(self._binding, "tenant", None)

    @property
    def config(self) -> ToolConfig:
        return getattr(self._binding, "config", self.base_config)

    @property
    def roots(self) -> List[Path]:
        return getattr(self._binding, "roots", self._default_roots)

    @property
    def default_directory(self) -> Path:
        return getattr(
            self._binding, "default_directory", self._default_directory
        )

    @property
    def trash_directory(self) -> Path:
        return getattr(
            self._binding,
            "trash_directory",
            self._default_trash_directory,
        )

    @property
    def command_runner(self) -> CommandRunner:
        return getattr(
            self._binding, "command_runner", self._default_command_runner
        )

    @property
    def _audit_context(self) -> ToolAuditContext:
        return getattr(self._binding, "audit_context", ToolAuditContext())

    @_audit_context.setter
    def _audit_context(self, value: ToolAuditContext) -> None:
        self._binding.audit_context = value

    def is_tool_enabled(self, name: str) -> bool:
        state = self._tool_states.get(name)
        if state is not None and not state.get("enabled", True):
            return False
        policy = self._organization_tool_policy()
        if name in set(policy.get("disabled_tools") or []):
            return False
        allowed = policy.get("allowed_tools")
        if isinstance(allowed, list) and allowed and name not in allowed:
            return False
        return True

    def _organization_tool_policy(self) -> Dict[str, Any]:
        if self.resource_store is None or self.tenant is None:
            return {}
        try:
            item = self.resource_store.get_effective(
                self.tenant.tenant_id, "tools", "platform"
            )
            payload = item.get("payload")
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

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

    def reload_config(self, config: ToolConfig) -> None:
        """Atomically replace the platform tool policy for new bindings."""
        roots = [Path(item).resolve() for item in config.allowed_roots]
        default_directory = Path(config.default_working_directory).resolve()
        runner = CommandRunner(
            config,
            self.resolve_path,
            sandbox_available=self._sandbox_available,
        )
        self.base_config = config
        self._default_roots = roots
        self._default_directory = default_directory
        self._default_command_runner = runner

    def bind_tenant(self, tenant: TenantContext) -> None:
        """Fail-closed binding of all filesystem and script tools to one tenant."""
        if self.tenant_registry is None:
            return
        registered = self.tenant_registry.get(tenant.tenant_id)
        if registered != tenant:
            raise ToolError("租户身份不匹配")
        workspace = self.tenant_registry.tenant_root(tenant.tenant_id) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        # POSIX permission bits are meaningless on Windows (it uses ACLs) and
        # chmod there can even raise WinError 5; only enforce on POSIX.
        if os.name != "nt":
            os.chmod(str(workspace), 0o700)
        default_directory = workspace.resolve()
        config = replace(
            self.base_config,
            allowed_roots=[str(default_directory)],
            default_working_directory=str(default_directory),
        )
        self._binding.tenant = tenant
        self._binding.roots = [default_directory]
        self._binding.default_directory = default_directory
        self._binding.trash_directory = (
            self.tenant_registry.tenant_root(tenant.tenant_id) / ".trash"
        )
        self._binding.config = config
        self._binding.command_runner = CommandRunner(
            config,
            self.resolve_path,
            sandbox_available=self._sandbox_available,
        )

    def clear_tenant(self) -> None:
        """Clear the current thread's tenant binding after a request."""
        for name in (
            "tenant",
            "roots",
            "default_directory",
            "trash_directory",
            "config",
            "command_runner",
            "audit_context",
        ):
            if hasattr(self._binding, name):
                delattr(self._binding, name)

    def _require_tenant(self) -> Optional[TenantContext]:
        if self.tenant_registry is not None and self.tenant is None:
            raise ToolError("工具尚未绑定用户工作区")
        return self.tenant

    def is_available(self, name: str) -> bool:
        if not self.is_tool_enabled(name):
            return False
        if (
            self.plugin_manager is not None
            and self.plugin_manager.manifest_for_tool(name) is not None
        ):
            return self.plugin_manager.is_available(name, self.tenant)
        plugin = self._plugin_tools.get(name)
        if plugin is not None:
            return plugin.is_available(name)
        if self.mcp_manager is not None and self.mcp_manager.has_tool(name):
            return self.mcp_manager.is_available(name)
        if name not in TOOL_DEFINITIONS:
            return False
        if name == "run_command":
            return self.command_runner.available
        if name in {
            "list_scripts", "run_script", "get_script_run", "cancel_script_run"
        }:
            return self.script_service is not None
        if name in {"list_script_schedules", "manage_script_schedule"}:
            return self.script_schedule_service is not None
        if name.startswith("knowledge_"):
            return self.knowledge_service is not None
        return True

    def _definition(self, name: str) -> Optional[Dict[str, Any]]:
        definition = TOOL_DEFINITIONS.get(name)
        if definition is not None:
            return definition
        if self.plugin_manager is not None:
            managed_definition = self.plugin_manager.definition(name)
            if managed_definition is not None:
                return {
                    "description": managed_definition.description,
                    "parameters": managed_definition.parameters,
                }
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
            if name in {"run_script", "manage_script_schedule"} and self.script_service:
                parameters["properties"]["script_id"]["enum"] = list(
                    self.script_service.script_ids
                )
            if name == "run_script" and self.script_service:
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
        if name in set(
            self._organization_tool_policy().get("require_approval_tools") or []
        ):
            return True
        definition = (
            self.plugin_manager.definition(name)
            if self.plugin_manager is not None
            else None
        )
        plugin = self._plugin_tools.get(name)
        if definition is not None or plugin is not None:
            if definition is None and plugin is not None:
                definition = plugin.tool_definitions.get(name)
            if definition and definition.approval_policy == "required":
                return True
            state = self._tool_states.get(name)
            if state is not None and "require_approval" in state:
                return bool(state["require_approval"])
            return bool(definition and definition.requires_approval)
        if name == "run_script" and self.script_service is not None:
            if arguments is None:
                return self.script_service.has_approval_required_scripts()
            if not isinstance(arguments, dict):
                return True
            return self.script_service.requires_approval(arguments.get("script_id"))
        if name in {"cancel_script_run", "manage_script_schedule"}:
            return True
        state = self._tool_states.get(name)
        if state is not None and "require_approval" in state:
            return state["require_approval"]
        return name in APPROVAL_TOOLS

    def direct_response_text(
        self, name: str, result: Optional[ToolResult]
    ) -> Optional[str]:
        """Return a trusted plugin summary that should bypass model rewriting."""

        definition = (
            self.plugin_manager.definition(name)
            if self.plugin_manager is not None
            else None
        )
        plugin = self._plugin_tools.get(name)
        if definition is None and plugin is not None:
            definition = plugin.tool_definitions.get(name)
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
            plugin_definition = (
                self.plugin_manager.definition(name)
                if self.plugin_manager is not None
                else None
            )
            plugin = self._plugin_tools.get(name)
            if plugin_definition is None and plugin is not None:
                plugin_definition = plugin.tool_definitions.get(name)
            if not plugin_definition or not plugin_definition.requires_approval:
                raise ToolError("该工具不需要审批：{}".format(name))
            try:
                if self.plugin_manager is not None:
                    return self.plugin_manager.preview(name, arguments, self.tenant)
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
        if name == "cancel_script_run":
            if not self.script_service:
                raise ToolError("固定脚本服务不可用")
            tenant = self._require_tenant()
            if tenant is None:
                raise ToolError("脚本工具需要租户身份")
            run_id = self._string(arguments, "run_id")
            current = self.script_service.get_run(tenant, run_id)
            return "取消脚本任务：{}\n脚本：{}\n当前状态：{}".format(
                run_id, current["script_name"], current["status"]
            )
        if name == "manage_script_schedule":
            tenant = self._require_tenant()
            if tenant is None or self.script_schedule_service is None:
                raise ToolError("脚本计划服务不可用")
            try:
                return self.script_schedule_service.preview(tenant, arguments)
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
        if name == "drive_delete_file":
            return "删除个人网盘文件：{}".format(self._string(arguments, "path"))
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
        self._audit_context = audit_context or ToolAuditContext()
        try:
            self._validate_arguments(name, arguments)
            if (
                self.plugin_manager is not None
                and self.plugin_manager.manifest_for_tool(name) is not None
            ):
                data = self.plugin_manager.execute(name, arguments, self.tenant)
            elif (plugin := self._plugin_tools.get(name)) is not None:
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
                        user_id=(
                            ctx.member_user_id
                            if ctx.member_user_id is not None
                            else (
                                self.tenant.member_user_id
                                if self.tenant is not None
                                else None
                            )
                        ),
                    )
                except Exception:  # noqa: BLE001 - audit must never break tool calls
                    logger.warning("写入工具审计记录失败：工具=%s", name, exc_info=True)

    def close_tenant(self, tenant_id: str) -> None:
        if self.plugin_manager is not None:
            self.plugin_manager.close_tenant(tenant_id)
            return
        for plugin in self.plugins:
            plugin.close_tenant(tenant_id)

    def close(self) -> None:
        if self.plugin_manager is not None:
            self.plugin_manager.close()
        else:
            for plugin in reversed(self.plugins):
                try:
                    plugin.close()
                except Exception:  # noqa: BLE001 - best effort on shutdown
                    logger.warning("关闭插件 %s 失败", plugin.id, exc_info=True)
        if self.mcp_manager is not None:
            try:
                self.mcp_manager.close()
            except Exception:  # noqa: BLE001 - best effort on shutdown
                logger.warning("关闭 MCP 管理器失败", exc_info=True)

    def reload_plugins(self, plugins: Iterable[PlatformPlugin]) -> None:
        if self.plugin_manager is not None:
            raise ValueError("插件配置需要重启后生效")
        for plugin in reversed(self.plugins):
            try:
                plugin.close()
            except Exception:  # noqa: BLE001 - keep reload going
                logger.warning("重载前关闭插件 %s 失败", plugin.id, exc_info=True)
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

    def _tool_cancel_script_run(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not self.script_service:
            raise ToolError("固定脚本服务不可用")
        tenant = self._require_tenant()
        if tenant is None:
            raise ToolError("脚本工具需要租户身份")
        try:
            return self.script_service.cancel_run(
                tenant, self._string(arguments, "run_id")
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    def _tool_list_script_schedules(
        self, _arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        tenant = self._require_tenant()
        if tenant is None or self.script_schedule_service is None:
            raise ToolError("脚本计划服务不可用")
        return {
            "schedules": self.script_schedule_service.list_for_tenant(tenant)
        }

    def _tool_manage_script_schedule(
        self, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        tenant = self._require_tenant()
        if tenant is None or self.script_schedule_service is None:
            raise ToolError("脚本计划服务不可用")
        try:
            return self.script_schedule_service.manage(
                tenant, arguments, authorized_by="chat"
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    def _knowledge_tenant(self) -> TenantContext:
        tenant = self._require_tenant()
        if tenant is None or self.knowledge_service is None:
            raise ToolError("知识库服务需要租户身份")
        return tenant

    # ---- drive tools ----

    def _drive_tenant(self) -> TenantContext:
        tenant = self._require_tenant()
        if tenant is None or self.drive_service is None:
            raise ToolError("网盘服务需要租户身份")
        return tenant

    def _drive_scope(self, arguments: Dict[str, Any]) -> str:
        scope = self._string(arguments, "scope", "tenant")
        if scope not in ("tenant", "public"):
            raise ToolError("scope 仅支持 tenant 或 public")
        return scope

    def _drive_record(
        self,
        scope: str,
        tenant_id: Optional[str],
        action: str,
        path: str,
        size_bytes: int = 0,
        status: str = "成功",
        error: Optional[str] = None,
    ) -> None:
        if self.drive_audit_store is None:
            return
        try:
            self.drive_audit_store.record(
                operator="agent:{}".format(self._audit_context.agent_id or "unknown"),
                source="agent",
                scope=scope,
                tenant_id=tenant_id,
                action=action,
                path=path,
                size_bytes=size_bytes,
                status=status,
                error=error,
            )
        except Exception:  # noqa: BLE001 - audit must never break tool calls
            logger.warning("写入网盘审计记录失败", exc_info=True)

    def _tool_drive_list_files(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        tenant = self._drive_tenant()
        scope = self._drive_scope(arguments)
        tenant_id = tenant.tenant_id if scope == "tenant" else None
        path = self._string(arguments, "path", "")
        try:
            result = self.drive_service.list_entries(scope, tenant_id, path)
        except ValueError as exc:
            self._drive_record(scope, tenant_id, "list", path, status="失败", error=str(exc))
            raise ToolError(str(exc)) from exc
        self._drive_record(scope, tenant_id, "list", path)
        return result

    def _tool_drive_read_file(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        tenant = self._drive_tenant()
        scope = self._drive_scope(arguments)
        tenant_id = tenant.tenant_id if scope == "tenant" else None
        path = self._string(arguments, "path")
        max_lines = self._integer(arguments, "max_lines", 200, 1, 400)
        try:
            preview = self.drive_service.read_text(scope, tenant_id, path)
        except ValueError as exc:
            self._drive_record(scope, tenant_id, "preview", path, status="失败", error=str(exc))
            raise ToolError(str(exc)) from exc
        lines = preview["content"].splitlines()
        truncated = preview["truncated"] or len(lines) > max_lines
        self._drive_record(scope, tenant_id, "preview", path, size_bytes=preview["size"])
        return {
            "path": path,
            "size": preview["size"],
            "truncated": truncated,
            "content": "\n".join(lines[:max_lines]),
        }

    def _tool_drive_save_file(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        tenant = self._drive_tenant()
        path = self._string(arguments, "path")
        content = self._string(arguments, "content")
        overwrite = bool(arguments.get("overwrite", False))
        directory, _, filename = path.replace("\\", "/").strip("/").rpartition("/")
        try:
            result = self.drive_service.save_file(
                "tenant",
                tenant.tenant_id,
                directory,
                filename,
                content.encode("utf-8"),
                overwrite=overwrite,
            )
        except ValueError as exc:
            self._drive_record(
                "tenant", tenant.tenant_id, "upload", path, status="失败", error=str(exc)
            )
            raise ToolError(str(exc)) from exc
        self._drive_record(
            "tenant", tenant.tenant_id, "upload", result["path"], size_bytes=result["size"]
        )
        return result

    def _tool_drive_delete_file(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        tenant = self._drive_tenant()
        path = self._string(arguments, "path")
        try:
            result = self.drive_service.delete("tenant", tenant.tenant_id, path)
        except ValueError as exc:
            self._drive_record(
                "tenant", tenant.tenant_id, "delete", path, status="失败", error=str(exc)
            )
            raise ToolError(str(exc)) from exc
        self._drive_record("tenant", tenant.tenant_id, "delete", path)
        return result

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
        category_ids = arguments.get("category_ids")
        if category_ids is not None and (
            not isinstance(category_ids, list)
            or any(not isinstance(value, str) for value in category_ids)
        ):
            raise ToolError("category_ids 必须是字符串数组")
        return {"results": self.knowledge_service.search(
            tenant.tenant_id,
            self._string(arguments, "query"),
            limit,
            agent_id=self._audit_context.agent_id or None,
            category_ids=category_ids,
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
