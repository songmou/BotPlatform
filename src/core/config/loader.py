"""Load and validate application, agent, and schedule JSON configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from src.core.modeling import ModelCapabilities
from src.core.plugins.registry import (
    known_plugin_ids,
    plugin_tool_names,
    validate_plugin_settings,
)


class ConfigError(RuntimeError):
    """Raised when a configuration file is missing or invalid."""


@dataclass(frozen=True)
class ModelProfile:
    id: str
    enabled: bool
    type: str
    provider: str
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    timeout_seconds: float
    capabilities: ModelCapabilities
    api_key_env: Optional[str] = None
    request_extra: Dict[str, Any] = field(default_factory=dict)
    assistant_passthrough_fields: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class EmbeddingProfile:
    id: str
    enabled: bool
    base_url: str
    model: str
    dimensions: int
    timeout_seconds: float


BUILTIN_TOOL_NAMES = {
    "list_allowed_roots",
    "list_directory",
    "find_files",
    "search_text",
    "read_text_file",
    "get_path_info",
    "get_current_time",
    "get_system_info",
    "get_disk_usage",
    "list_processes",
    "create_directory",
    "write_text_file",
    "replace_text",
    "copy_path",
    "move_path",
    "move_to_trash",
    "run_command",
    "list_scripts",
    "run_script",
    "get_script_run",
    "knowledge_add_text",
    "knowledge_index_file",
    "knowledge_search",
    "knowledge_list",
    "knowledge_delete",
}
KNOWN_TOOL_NAMES = BUILTIN_TOOL_NAMES | plugin_tool_names()

KNOWN_COMMAND_PROFILES = {
    "python",
    "git_readonly",
    "node",
    "npm_script",
    "ollama_readonly",
    "workspace_script",
}


@dataclass(frozen=True)
class ToolConfig:
    enabled: bool
    default_working_directory: str
    allowed_roots: List[str]
    denied_globs: List[str]
    approval_ttl_seconds: int
    max_tool_rounds: int
    max_total_tool_calls: int
    max_read_bytes: int
    max_write_bytes: int
    max_directory_entries: int
    max_search_results: int
    max_command_output_bytes: int
    default_command_timeout_seconds: int
    max_command_timeout_seconds: int
    enabled_command_profiles: List[str]


@dataclass(frozen=True)
class PluginConfig:
    id: str
    enabled: bool
    settings: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelConfig:
    id: str
    type: str
    enabled: bool
    settings: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    default_agent: str
    timezone: str
    history_rounds: int
    image_prompt: str
    active_model: str
    fallback_model: str
    local_model: str
    flash_model: str
    pro_model: str
    vision_model: str
    fallback_cooldown_seconds: int


@dataclass(frozen=True)
class Capability:
    name: str
    description: str


@dataclass(frozen=True)
class AgentPreset:
    id: str
    name: str
    role: str
    description: str
    system_prompt: str
    capabilities: List[Capability]
    image_prompt: Optional[str] = None
    tools: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    mcp_servers: List[str] = field(default_factory=list)
    model: Optional[str] = None
    greeting: Optional[str] = None
    greeting_hints: List[str] = field(default_factory=list)
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


@dataclass(frozen=True)
class TaskAction:
    type: str
    content: Optional[str] = None
    agent_id: Optional[str] = None
    prompt: Optional[str] = None
    image_path: Optional[str] = None
    image_url: Optional[str] = None
    caption: Optional[str] = None
    script_id: Optional[str] = None
    plugin_id: Optional[str] = None
    tool_name: Optional[str] = None
    parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScriptParameter:
    type: str
    required: bool = False
    choices: List[str] = field(default_factory=list)
    positional: bool = False
    flag: Optional[str] = None


@dataclass(frozen=True)
class ScriptDefinition:
    id: str
    name: str
    description: str
    entrypoint: str
    timeout_seconds: int
    requires_approval: bool = True
    data_directory: str = ""
    parameters: Dict[str, ScriptParameter] = field(default_factory=dict)
    artifact_types: List[str] = field(default_factory=list)


def validate_script_parameters(
    definition: ScriptDefinition, raw: Dict[str, Any]
) -> Dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError("脚本参数必须是 JSON 对象")
    unknown = sorted(set(raw) - set(definition.parameters))
    if unknown:
        raise ValueError("包含未知参数：{}".format("、".join(unknown)))
    normalized: Dict[str, str] = {}
    for name, spec in definition.parameters.items():
        if name not in raw:
            if spec.required:
                raise ValueError("缺少必填参数：{}".format(name))
            continue
        value = raw[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("参数 {} 必须是非空字符串".format(name))
        value = value.strip()
        if spec.choices and value not in spec.choices:
            raise ValueError(
                "参数 {} 仅允许：{}".format(name, "、".join(spec.choices))
            )
        if spec.type == "date":
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("参数 {} 必须是 YYYY-MM-DD 日期".format(name)) from exc
        normalized[name] = value
    return normalized


@dataclass(frozen=True)
class TaskCondition:
    type: str
    after_hours: float
    before_hours: float


@dataclass(frozen=True)
class ScheduledTask:
    id: str
    enabled: bool
    cron: str
    target: str
    action: TaskAction
    condition: Optional[TaskCondition] = None
    crons: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProjectConfig:
    app: AppConfig
    models: Dict[str, ModelProfile]
    tools: ToolConfig
    plugins: Dict[str, PluginConfig]
    agents: Dict[str, AgentPreset]
    scripts: Dict[str, ScriptDefinition]
    schedules: List[ScheduledTask]
    embedding: EmbeddingProfile
    skills: List[Dict[str, Any]] = field(default_factory=list)
    mcp_servers: List[Dict[str, Any]] = field(default_factory=list)
    channels: Dict[str, ChannelConfig] = field(default_factory=dict)

    @property
    def active_agent(self) -> AgentPreset:
        return self.agents[self.app.default_agent]

    @property
    def active_model(self) -> ModelProfile:
        return self.models[self.app.active_model]


def _error(path: Path, field: str, message: str) -> ConfigError:
    return ConfigError("{}: 字段 {} {}".format(path, field, message))


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ConfigError("缺少配置文件：{}".format(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "{}: JSON 格式错误（第 {} 行第 {} 列）：{}".format(
                path, exc.lineno, exc.colno, exc.msg
            )
        ) from exc
    except OSError as exc:
        raise ConfigError("读取配置文件失败 {}：{}".format(path, exc)) from exc
    if not isinstance(data, dict):
        raise ConfigError("{}: 顶层必须是 JSON 对象".format(path))
    return data


def _required_string(data: Dict[str, Any], field: str, path: Path) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _error(path, field, "必须是非空字符串")
    return value.strip()


def _required_nested_string(
    data: Dict[str, Any], key: str, field_path: str, path: Path
) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _error(path, field_path, "必须是非空字符串")
    return value.strip()


def _optional_string(data: Dict[str, Any], field: str, path: Path) -> Optional[str]:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _error(path, field, "必须是非空字符串或省略")
    return value.strip()


def _positive_int(data: Dict[str, Any], field: str, path: Path) -> int:
    value = data.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _error(path, field, "必须是大于 0 的整数")
    return value


def _string_list(
    data: Dict[str, Any], field: str, path: Path, allow_empty: bool = False
) -> List[str]:
    value = data.get(field)
    if not isinstance(value, list) or (not allow_empty and not value):
        suffix = "数组" if allow_empty else "非空数组"
        raise _error(path, field, "必须是{}".format(suffix))
    result: List[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise _error(path, "{}[{}]".format(field, index), "必须是非空字符串")
        normalized = item.strip()
        if normalized in result:
            raise _error(path, field, "不能包含重复值：{}".format(normalized))
        result.append(normalized)
    return result


def _is_within(path: Path, roots: List[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _reject_unknown(
    data: Dict[str, Any], allowed: set[str], path: Path, field: str = ""
) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise _error(
            path,
            field or "配置根对象",
            "包含未知字段：{}".format(", ".join(unknown)),
        )


def _load_tools(path: Path) -> ToolConfig:
    data = _load_json(path)
    _reject_unknown(data, {
        "enabled", "default_working_directory", "allowed_roots", "denied_globs",
        "approval_ttl_seconds", "max_tool_rounds", "max_total_tool_calls",
        "max_read_bytes", "max_write_bytes", "max_directory_entries",
        "max_search_results", "max_command_output_bytes",
        "default_command_timeout_seconds", "max_command_timeout_seconds",
        "enabled_command_profiles",
    }, path)
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise _error(path, "enabled", "必须是布尔值")

    tenant_workspace_marker = "$TENANT_WORKSPACE"
    tenant_workspace_placeholder = Path("/__ilinkbot_tenant_workspace__")
    roots: List[Path] = []
    for index, raw_root in enumerate(_string_list(data, "allowed_roots", path)):
        if raw_root == tenant_workspace_marker:
            roots.append(tenant_workspace_placeholder)
            continue
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            raise _error(path, "allowed_roots[{}]".format(index), "必须是绝对路径")
        root = root.resolve()
        if not root.is_dir():
            raise _error(path, "allowed_roots[{}]".format(index), "目录不存在：{}".format(root))
        if root in roots:
            raise _error(path, "allowed_roots", "解析后存在重复目录：{}".format(root))
        roots.append(root)

    raw_default = _required_string(data, "default_working_directory", path)
    if raw_default == tenant_workspace_marker:
        default_directory = tenant_workspace_placeholder
    else:
        default_directory = Path(raw_default).expanduser()
        if not default_directory.is_absolute():
            raise _error(path, "default_working_directory", "必须是绝对路径")
        default_directory = default_directory.resolve()
        if not default_directory.is_dir():
            raise _error(path, "default_working_directory", "目录不存在：{}".format(default_directory))
    if not _is_within(default_directory, roots):
        raise _error(path, "default_working_directory", "必须位于 allowed_roots 中")

    profiles = _string_list(data, "enabled_command_profiles", path, allow_empty=True)
    unknown_profiles = sorted(set(profiles) - KNOWN_COMMAND_PROFILES)
    if unknown_profiles:
        raise _error(
            path,
            "enabled_command_profiles",
            "包含未知命令档案：{}".format(", ".join(unknown_profiles)),
        )

    default_timeout = _positive_int(data, "default_command_timeout_seconds", path)
    max_timeout = _positive_int(data, "max_command_timeout_seconds", path)
    if default_timeout > max_timeout:
        raise _error(
            path,
            "default_command_timeout_seconds",
            "不能大于 max_command_timeout_seconds",
        )

    return ToolConfig(
        enabled=enabled,
        default_working_directory=str(default_directory),
        allowed_roots=[str(root) for root in roots],
        denied_globs=_string_list(data, "denied_globs", path, allow_empty=True),
        approval_ttl_seconds=_positive_int(data, "approval_ttl_seconds", path),
        max_tool_rounds=_positive_int(data, "max_tool_rounds", path),
        max_total_tool_calls=_positive_int(data, "max_total_tool_calls", path),
        max_read_bytes=_positive_int(data, "max_read_bytes", path),
        max_write_bytes=_positive_int(data, "max_write_bytes", path),
        max_directory_entries=_positive_int(data, "max_directory_entries", path),
        max_search_results=_positive_int(data, "max_search_results", path),
        max_command_output_bytes=_positive_int(data, "max_command_output_bytes", path),
        default_command_timeout_seconds=default_timeout,
        max_command_timeout_seconds=max_timeout,
        enabled_command_profiles=profiles,
    )


def _load_app(path: Path) -> AppConfig:
    data = _load_json(path)
    _reject_unknown(data, {
        "default_agent", "timezone", "history_rounds", "image_prompt",
        "active_model", "fallback_model", "local_model", "flash_model",
        "pro_model", "vision_model", "fallback_cooldown_seconds",
    }, path)
    default_agent = _required_string(data, "default_agent", path)
    timezone = _required_string(data, "timezone", path)
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise _error(path, "timezone", "不是有效时区：{}".format(timezone)) from exc

    history_rounds = data.get("history_rounds")
    if not isinstance(history_rounds, int) or isinstance(history_rounds, bool) or history_rounds < 1:
        raise _error(path, "history_rounds", "必须是大于 0 的整数")
    image_prompt = _required_string(data, "image_prompt", path)
    active_model = os.getenv("MODEL_PROFILE") or _required_string(
        data, "active_model", path
    )
    fallback_model = _required_string(data, "fallback_model", path)
    local_model = _required_string(data, "local_model", path)
    flash_model = _required_string(data, "flash_model", path)
    pro_model = _required_string(data, "pro_model", path)
    vision_model = _required_string(data, "vision_model", path)
    fallback_cooldown_seconds = _positive_int(
        data, "fallback_cooldown_seconds", path
    )

    return AppConfig(
        default_agent=default_agent,
        timezone=timezone,
        history_rounds=history_rounds,
        image_prompt=image_prompt,
        active_model=active_model,
        fallback_model=fallback_model,
        local_model=local_model,
        flash_model=flash_model,
        pro_model=pro_model,
        vision_model=vision_model,
        fallback_cooldown_seconds=fallback_cooldown_seconds,
    )


_MODEL_TYPES = {"ollama", "openai_compatible"}
_RESERVED_REQUEST_FIELDS = {
    "model",
    "messages",
    "tools",
    "stream",
    "temperature",
    "max_tokens",
    "max_completion_tokens",
}


def _model_number(
    data: Dict[str, Any], field: str, field_path: str, path: Path
) -> float:
    value = data.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise _error(path, field_path, "必须是数字")
    return float(value)


def _validate_model_url(value: str, field_path: str, path: Path) -> str:
    url = value.rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _error(path, field_path, "必须是有效的 HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise _error(path, field_path, "不能包含凭证、查询参数或片段")
    hostname = parsed.hostname.lower()
    if parsed.scheme != "https" and hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise _error(path, field_path, "远程模型地址必须使用 HTTPS")
    return url


def _load_models(path: Path) -> Dict[str, ModelProfile]:
    data = _load_json(path)
    _reject_unknown(data, {"profiles"}, path)
    raw_profiles = data.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise _error(path, "profiles", "必须是非空 JSON 对象")
    profiles: Dict[str, ModelProfile] = {}
    for profile_id, raw in raw_profiles.items():
        field_base = "profiles.{}".format(profile_id)
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise _error(path, "profiles", "档案名必须是非空字符串")
        if not isinstance(raw, dict):
            raise _error(path, field_base, "必须是 JSON 对象")
        _reject_unknown(raw, {
            "enabled", "type", "provider", "base_url", "api_key_env", "model",
            "temperature", "max_tokens", "timeout_seconds", "capabilities",
            "request_extra", "assistant_passthrough_fields",
        }, path, field_base)
        enabled = raw.get("enabled")
        if not isinstance(enabled, bool):
            raise _error(path, field_base + ".enabled", "必须是布尔值")
        adapter_type = _required_nested_string(raw, "type", field_base + ".type", path)
        if adapter_type not in _MODEL_TYPES:
            raise _error(path, field_base + ".type", "是不支持的适配器类型")
        provider = _required_nested_string(
            raw, "provider", field_base + ".provider", path
        )
        base_url = _validate_model_url(
            _required_nested_string(raw, "base_url", field_base + ".base_url", path),
            field_base + ".base_url",
            path,
        )
        model = _required_nested_string(raw, "model", field_base + ".model", path)
        temperature = _model_number(
            raw, "temperature", field_base + ".temperature", path
        )
        if not 0 <= temperature <= 2:
            raise _error(path, field_base + ".temperature", "必须在 0 到 2 之间")
        max_tokens = raw.get("max_tokens")
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
            raise _error(path, field_base + ".max_tokens", "必须是大于 0 的整数")
        timeout_seconds = _model_number(
            raw, "timeout_seconds", field_base + ".timeout_seconds", path
        )
        if timeout_seconds <= 0 or timeout_seconds > 600:
            raise _error(path, field_base + ".timeout_seconds", "必须在 0 到 600 之间")
        raw_capabilities = raw.get("capabilities")
        if not isinstance(raw_capabilities, dict):
            raise _error(path, field_base + ".capabilities", "必须是 JSON 对象")
        unknown_caps = sorted(
            set(raw_capabilities) - {"tools", "vision", "reasoning"}
        )
        if unknown_caps:
            raise _error(
                path,
                field_base + ".capabilities",
                "包含未知能力：{}".format(", ".join(unknown_caps)),
            )
        for capability in ("tools", "vision", "reasoning"):
            if not isinstance(raw_capabilities.get(capability), bool):
                raise _error(
                    path,
                    field_base + ".capabilities." + capability,
                    "必须是布尔值",
                )
        api_key_env = raw.get("api_key_env")
        if adapter_type == "openai_compatible":
            if not isinstance(api_key_env, str) or not api_key_env.strip():
                raise _error(
                    path, field_base + ".api_key_env", "必须是非空环境变量名"
                )
            api_key_env = api_key_env.strip()
        elif api_key_env is not None:
            raise _error(path, field_base + ".api_key_env", "仅云端兼容档案可配置")

        request_extra = raw.get("request_extra", {})
        if not isinstance(request_extra, dict):
            raise _error(path, field_base + ".request_extra", "必须是 JSON 对象")
        conflicts = sorted(set(request_extra) & _RESERVED_REQUEST_FIELDS)
        if conflicts:
            raise _error(
                path,
                field_base + ".request_extra",
                "不能覆盖核心字段：{}".format(", ".join(conflicts)),
            )
        passthrough = raw.get("assistant_passthrough_fields", [])
        if not isinstance(passthrough, list):
            raise _error(
                path, field_base + ".assistant_passthrough_fields", "必须是数组"
            )
        normalized_fields: List[str] = []
        for index, value in enumerate(passthrough):
            if not isinstance(value, str) or not value.strip():
                raise _error(
                    path,
                    "{}.assistant_passthrough_fields[{}]".format(field_base, index),
                    "必须是非空字符串",
                )
            value = value.strip()
            if value in normalized_fields:
                raise _error(
                    path,
                    field_base + ".assistant_passthrough_fields",
                    "不能包含重复字段：{}".format(value),
                )
            normalized_fields.append(value)

        if adapter_type == "ollama":
            override_url = os.getenv("OLLAMA_BASE_URL")
            if override_url:
                base_url = _validate_model_url(
                    override_url,
                    field_base + ".base_url（OLLAMA_BASE_URL）",
                    path,
                )
            model = os.getenv("OLLAMA_MODEL") or model
        profiles[profile_id] = ModelProfile(
            id=profile_id,
            enabled=enabled,
            type=adapter_type,
            provider=provider,
            base_url=base_url,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            capabilities=ModelCapabilities(**raw_capabilities),
            api_key_env=api_key_env,
            request_extra=dict(request_extra),
            assistant_passthrough_fields=normalized_fields,
        )
    return profiles


def _load_embedding(path: Path) -> EmbeddingProfile:
    data = _load_json(path)
    _reject_unknown(data, {"id", "enabled", "base_url", "model", "dimensions", "timeout_seconds"}, path)
    profile_id = _required_string(data, "id", path)
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise _error(path, "enabled", "必须是布尔值")
    base_url = _validate_model_url(_required_string(data, "base_url", path), "base_url", path)
    model = _required_string(data, "model", path)
    dimensions = _positive_int(data, "dimensions", path)
    timeout_seconds = _model_number(data, "timeout_seconds", "timeout_seconds", path)
    if timeout_seconds <= 0 or timeout_seconds > 600:
        raise _error(path, "timeout_seconds", "必须在 0 到 600 之间")
    override_url = os.getenv("OLLAMA_BASE_URL")
    if override_url:
        base_url = _validate_model_url(override_url, "base_url（OLLAMA_BASE_URL）", path)
    return EmbeddingProfile(profile_id, enabled, base_url, model, dimensions, timeout_seconds)


def _load_agent(path: Path) -> AgentPreset:
    data = _load_json(path)
    _reject_unknown(data, {
        "id", "name", "role", "description", "system_prompt", "image_prompt",
        "capabilities", "tools", "skills", "mcp_servers", "model", "greeting",
        "greeting_hints", "temperature", "max_tokens",
    }, path)
    capabilities_data = data.get("capabilities")
    if not isinstance(capabilities_data, list) or not capabilities_data:
        raise _error(path, "capabilities", "必须是非空数组")
    capabilities: List[Capability] = []
    for index, item in enumerate(capabilities_data):
        if not isinstance(item, dict):
            raise _error(path, "capabilities[{}]".format(index), "必须是 JSON 对象")
        _reject_unknown(item, {"name", "description"}, path, "capabilities[{}]".format(index))
        capabilities.append(
            Capability(
                name=_required_nested_string(
                    item, "name", "capabilities[{}].name".format(index), path
                ),
                description=_required_nested_string(
                    item,
                    "description",
                    "capabilities[{}].description".format(index),
                    path,
                ),
            )
        )
    raw_tools = data.get("tools", [])
    if not isinstance(raw_tools, list):
        raise _error(path, "tools", "必须是数组")
    tools: List[str] = []
    for index, name in enumerate(raw_tools):
        if not isinstance(name, str) or not name.strip():
            raise _error(path, "tools[{}]".format(index), "必须是非空字符串")
        name = name.strip()
        if name not in KNOWN_TOOL_NAMES:
            raise _error(path, "tools[{}]".format(index), "是未知工具：{}".format(name))
        if name in tools:
            raise _error(path, "tools", "不能包含重复工具：{}".format(name))
        tools.append(name)

    raw_skills = data.get("skills", [])
    if not isinstance(raw_skills, list):
        raise _error(path, "skills", "必须是数组")
    skills: List[str] = []
    for index, skill_id in enumerate(raw_skills):
        if not isinstance(skill_id, str) or not skill_id.strip():
            raise _error(path, "skills[{}]".format(index), "必须是非空字符串")
        skill_id = skill_id.strip()
        if skill_id in skills:
            raise _error(path, "skills", "不能包含重复技能：{}".format(skill_id))
        skills.append(skill_id)

    raw_mcp_servers = data.get("mcp_servers", [])
    if not isinstance(raw_mcp_servers, list):
        raise _error(path, "mcp_servers", "必须是数组")
    mcp_servers: List[str] = []
    for index, server_id in enumerate(raw_mcp_servers):
        if not isinstance(server_id, str) or not server_id.strip():
            raise _error(path, "mcp_servers[{}]".format(index), "必须是非空字符串")
        server_id = server_id.strip()
        if server_id in mcp_servers:
            raise _error(path, "mcp_servers", "不能包含重复服务：{}".format(server_id))
        mcp_servers.append(server_id)

    raw_greeting_hints = data.get("greeting_hints", [])
    if not isinstance(raw_greeting_hints, list):
        raise _error(path, "greeting_hints", "必须是数组")
    greeting_hints: List[str] = []
    for index, hint in enumerate(raw_greeting_hints):
        if not isinstance(hint, str) or not hint.strip():
            raise _error(path, "greeting_hints[{}]".format(index), "必须是非空字符串")
        greeting_hints.append(hint.strip())

    temperature = data.get("temperature")
    if temperature is not None:
        if not isinstance(temperature, (int, float)):
            raise _error(path, "temperature", "必须是数字")
        temperature = float(temperature)
        if temperature < 0 or temperature > 2:
            raise _error(path, "temperature", "必须在 0-2 之间")

    max_tokens = data.get("max_tokens")
    if max_tokens is not None:
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            raise _error(path, "max_tokens", "必须是正整数")

    return AgentPreset(
        id=_required_string(data, "id", path),
        name=_required_string(data, "name", path),
        role=_required_string(data, "role", path),
        description=_required_string(data, "description", path),
        system_prompt=_required_string(data, "system_prompt", path),
        image_prompt=_optional_string(data, "image_prompt", path),
        capabilities=capabilities,
        tools=tools,
        skills=skills,
        mcp_servers=mcp_servers,
        model=_optional_string(data, "model", path),
        greeting=_optional_string(data, "greeting", path),
        greeting_hints=greeting_hints,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _load_agents(directory: Path) -> Dict[str, AgentPreset]:
    if not directory.is_dir():
        raise ConfigError("缺少 Agent 配置目录：{}".format(directory))
    files = sorted(directory.glob("*.json"))
    if not files:
        raise ConfigError("Agent 配置目录中没有 JSON 文件：{}".format(directory))
    agents: Dict[str, AgentPreset] = {}
    sources: Dict[str, Path] = {}
    for path in files:
        agent = _load_agent(path)
        if agent.id in agents:
            raise ConfigError(
                "Agent id 重复：{} 同时出现在 {} 和 {}".format(
                    agent.id, sources[agent.id], path
                )
            )
        agents[agent.id] = agent
        sources[agent.id] = path
    return agents


def _load_plugins(path: Path) -> Dict[str, PluginConfig]:
    data = _load_json(path)
    _reject_unknown(data, {"plugins"}, path)
    items = data.get("plugins")
    if not isinstance(items, list):
        raise _error(path, "plugins", "必须是数组")
    plugins: Dict[str, PluginConfig] = {}
    known_ids = known_plugin_ids()
    for index, item in enumerate(items):
        prefix = "plugins[{}]".format(index)
        if not isinstance(item, dict):
            raise _error(path, prefix, "必须是 JSON 对象")
        _reject_unknown(item, {"id", "enabled", "settings"}, path, prefix)
        plugin_id = _required_nested_string(item, "id", prefix + ".id", path)
        if plugin_id not in known_ids:
            raise _error(path, prefix + ".id", "是未知平台插件：{}".format(plugin_id))
        if plugin_id in plugins:
            raise _error(path, prefix + ".id", "不能重复：{}".format(plugin_id))
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise _error(path, prefix + ".enabled", "必须是布尔值")
        settings = item.get("settings", {})
        if not isinstance(settings, dict):
            raise _error(path, prefix + ".settings", "必须是 JSON 对象")
        try:
            validate_plugin_settings(plugin_id, settings)
        except ValueError as exc:
            raise _error(path, prefix + ".settings", str(exc)) from exc
        plugins[plugin_id] = PluginConfig(plugin_id, enabled, dict(settings))
    return plugins


def _load_channels(path: Path) -> Dict[str, ChannelConfig]:
    if not path.exists():
        return {
            "wechat-main": ChannelConfig(
                id="wechat-main",
                type="wechat_ilink",
                enabled=True,
            )
        }
    data = _load_json(path)
    _reject_unknown(data, {"channels"}, path)
    raw_channels = data.get("channels")
    if not isinstance(raw_channels, list):
        raise _error(path, "channels", "必须是数组")
    channels: Dict[str, ChannelConfig] = {}
    for index, raw in enumerate(raw_channels):
        prefix = "channels[{}]".format(index)
        if not isinstance(raw, dict):
            raise _error(path, prefix, "必须是 JSON 对象")
        _reject_unknown(raw, {"id", "type", "enabled", "settings"}, path, prefix)
        channel_id = _required_nested_string(raw, "id", prefix + ".id", path)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", channel_id):
            raise _error(path, prefix + ".id", "格式无效")
        if channel_id in channels:
            raise _error(path, prefix + ".id", "不能重复")
        channel_type = _required_nested_string(
            raw, "type", prefix + ".type", path
        )
        if channel_type != "wechat_ilink":
            raise _error(path, prefix + ".type", "首期仅支持 wechat_ilink")
        enabled = raw.get("enabled")
        if not isinstance(enabled, bool):
            raise _error(path, prefix + ".enabled", "必须是布尔值")
        settings = raw.get("settings", {})
        if not isinstance(settings, dict):
            raise _error(path, prefix + ".settings", "必须是 JSON 对象")
        forbidden = {
            key
            for key in settings
            if any(word in str(key).lower() for word in ("token", "secret", "password"))
        }
        if forbidden:
            raise _error(
                path,
                prefix + ".settings",
                "不得包含凭证字段：{}".format("、".join(sorted(forbidden))),
            )
        channels[channel_id] = ChannelConfig(
            id=channel_id,
            type=channel_type,
            enabled=enabled,
            settings=dict(settings),
        )
    if not any(channel.enabled for channel in channels.values()):
        raise _error(path, "channels", "至少需要启用一个消息渠道")
    return channels


def _load_scripts(path: Path, project_root: Path) -> Dict[str, ScriptDefinition]:
    data = _load_json(path)
    _reject_unknown(data, {"scripts"}, path)
    items = data.get("scripts")
    if not isinstance(items, list):
        raise _error(path, "scripts", "必须是数组")
    scripts: Dict[str, ScriptDefinition] = {}
    jobs_root = (project_root / "src" / "core" / "jobs").resolve()
    for index, item in enumerate(items):
        prefix = "scripts[{}]".format(index)
        if not isinstance(item, dict):
            raise _error(path, prefix, "必须是 JSON 对象")
        _reject_unknown(item, {
            "id", "name", "description", "entrypoint", "timeout_seconds",
            "requires_approval", "data_directory", "parameters", "artifact_types",
        }, path, prefix)
        script_id = _required_nested_string(item, "id", prefix + ".id", path)
        if script_id in scripts:
            raise ConfigError("{}: 脚本 id 重复：{}".format(path, script_id))
        name = _required_nested_string(item, "name", prefix + ".name", path)
        description = _required_nested_string(
            item, "description", prefix + ".description", path
        )
        raw_entrypoint = _required_nested_string(
            item, "entrypoint", prefix + ".entrypoint", path
        )
        candidate = Path(raw_entrypoint).expanduser()
        if not candidate.is_absolute():
            candidate = project_root / candidate
        try:
            entrypoint = candidate.resolve(strict=True)
        except OSError as exc:
            raise _error(path, prefix + ".entrypoint", "不存在") from exc
        if not entrypoint.is_file() or not (
            entrypoint == jobs_root or jobs_root in entrypoint.parents
        ):
            raise _error(path, prefix + ".entrypoint", "必须是 src/core/jobs 目录内的文件")
        timeout = item.get("timeout_seconds")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise _error(path, prefix + ".timeout_seconds", "必须是 1 到 3600 的整数")
        requires_approval = item.get("requires_approval", True)
        if not isinstance(requires_approval, bool):
            raise _error(path, prefix + ".requires_approval", "必须是布尔值")
        data_directory = item.get("data_directory", script_id)
        if (
            not isinstance(data_directory, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", data_directory)
        ):
            raise _error(
                path,
                prefix + ".data_directory",
                "必须是小写字母、数字、下划线或连字符组成的单层目录名",
            )
        raw_parameters = item.get("parameters", {})
        if not isinstance(raw_parameters, dict):
            raise _error(path, prefix + ".parameters", "必须是 JSON 对象")
        parameters: Dict[str, ScriptParameter] = {}
        for parameter_name, parameter_data in raw_parameters.items():
            field_name = prefix + ".parameters." + str(parameter_name)
            if (
                not isinstance(parameter_name, str)
                or not parameter_name
                or not isinstance(parameter_data, dict)
            ):
                raise _error(path, field_name, "必须是具名 JSON 对象")
            parameter_type = parameter_data.get("type")
            if parameter_type not in {"string", "date"}:
                raise _error(path, field_name + ".type", "仅支持 string 或 date")
            required = parameter_data.get("required", False)
            positional = parameter_data.get("positional", False)
            flag = parameter_data.get("flag")
            choices = parameter_data.get("choices", [])
            if not isinstance(required, bool) or not isinstance(positional, bool):
                raise _error(path, field_name, "required 和 positional 必须是布尔值")
            if flag is not None and (
                not isinstance(flag, str) or not flag.startswith("--")
            ):
                raise _error(path, field_name + ".flag", "必须是 -- 开头的字符串")
            if positional == (flag is not None):
                raise _error(path, field_name, "必须且只能设置 positional=true 或 flag")
            if not isinstance(choices, list) or any(
                not isinstance(choice, str) or not choice for choice in choices
            ):
                raise _error(path, field_name + ".choices", "必须是字符串数组")
            parameters[parameter_name] = ScriptParameter(
                type=parameter_type,
                required=required,
                choices=list(choices),
                positional=positional,
                flag=flag,
            )
        artifact_types = item.get("artifact_types", [])
        if not isinstance(artifact_types, list) or any(
            value != "image" for value in artifact_types
        ):
            raise _error(path, prefix + ".artifact_types", "首版仅支持 image")
        scripts[script_id] = ScriptDefinition(
            id=script_id,
            name=name,
            description=description,
            entrypoint=str(entrypoint),
            timeout_seconds=timeout,
            requires_approval=requires_approval,
            data_directory=data_directory,
            parameters=parameters,
            artifact_types=list(artifact_types),
        )
    return scripts


def _load_schedules(
    path: Path,
    timezone: str,
    agents: Dict[str, AgentPreset],
    scripts: Dict[str, ScriptDefinition],
) -> List[ScheduledTask]:
    data = _load_json(path)
    _reject_unknown(data, {"tasks"}, path)
    tasks_data = data.get("tasks")
    if not isinstance(tasks_data, list):
        raise _error(path, "tasks", "必须是数组")
    tasks: List[ScheduledTask] = []
    task_ids = set()
    for index, item in enumerate(tasks_data):
        prefix = "tasks[{}]".format(index)
        if not isinstance(item, dict):
            raise _error(path, prefix, "必须是 JSON 对象")
        _reject_unknown(item, {
            "id", "enabled", "cron", "crons", "target", "condition", "action",
        }, path, prefix)
        task_id = _required_nested_string(item, "id", prefix + ".id", path)
        if task_id in task_ids:
            raise ConfigError("{}: 定时任务 id 重复：{}".format(path, task_id))
        task_ids.add(task_id)
        enabled = item.get("enabled")
        if not isinstance(enabled, bool):
            raise _error(path, prefix + ".enabled", "必须是布尔值")
        raw_cron = item.get("cron")
        raw_crons = item.get("crons")
        if (raw_cron is None) == (raw_crons is None):
            raise _error(path, prefix, "cron 与 crons 必须且只能提供一个")
        if raw_cron is not None:
            if not isinstance(raw_cron, str) or not raw_cron.strip():
                raise _error(path, prefix + ".cron", "必须是非空字符串")
            crons = [raw_cron.strip()]
        else:
            if (
                not isinstance(raw_crons, list)
                or not 1 <= len(raw_crons) <= 8
                or any(not isinstance(value, str) or not value.strip() for value in raw_crons)
            ):
                raise _error(path, prefix + ".crons", "必须是 1 到 8 个非空 cron 字符串")
            crons = [value.strip() for value in raw_crons]
            if len(set(crons)) != len(crons):
                raise _error(path, prefix + ".crons", "不能包含重复时间")
        for cron_index, cron_value in enumerate(crons):
            try:
                CronTrigger.from_crontab(cron_value, timezone=timezone)
            except (TypeError, ValueError) as exc:
                field_name = prefix + (".cron" if raw_cron is not None else ".crons[{}]".format(cron_index))
                raise _error(
                    path,
                    field_name,
                    "不是有效的五段 cron：{}".format(cron_value),
                ) from exc
        cron = crons[0]
        target = _required_nested_string(item, "target", prefix + ".target", path)
        if target != "last_active_user":
            raise _error(path, prefix + ".target", "首版只支持 last_active_user")

        condition = None
        condition_data = item.get("condition")
        if condition_data is not None:
            if not isinstance(condition_data, dict):
                raise _error(path, prefix + ".condition", "必须是 JSON 对象或省略")
            condition_type = _required_nested_string(
                condition_data, "type", prefix + ".condition.type", path
            )
            if condition_type != "inactivity_once":
                raise _error(
                    path,
                    prefix + ".condition.type",
                    "仅支持 inactivity_once",
                )
            after_hours = _model_number(
                condition_data,
                "after_hours",
                prefix + ".condition.after_hours",
                path,
            )
            before_hours = _model_number(
                condition_data,
                "before_hours",
                prefix + ".condition.before_hours",
                path,
            )
            if not 0 < after_hours < before_hours <= 24:
                raise _error(
                    path,
                    prefix + ".condition",
                    "必须满足 0 < after_hours < before_hours <= 24",
                )
            condition = TaskCondition(
                type=condition_type,
                after_hours=after_hours,
                before_hours=before_hours,
            )

        action_data = item.get("action")
        if not isinstance(action_data, dict):
            raise _error(path, prefix + ".action", "必须是 JSON 对象")
        action_type = _required_nested_string(
            action_data, "type", prefix + ".action.type", path
        )
        if action_type == "text":
            action = TaskAction(
                type=action_type,
                content=_required_nested_string(
                    action_data, "content", prefix + ".action.content", path
                ),
            )
        elif action_type == "agent_prompt":
            agent_id = _required_nested_string(
                action_data, "agent_id", prefix + ".action.agent_id", path
            )
            if agent_id not in agents:
                raise _error(
                    path,
                    prefix + ".action.agent_id",
                    "引用了不存在的 Agent：{}".format(agent_id),
                )
            action = TaskAction(
                type=action_type,
                agent_id=agent_id,
                prompt=_required_nested_string(
                    action_data, "prompt", prefix + ".action.prompt", path
                ),
            )
        elif action_type == "image":
            raw_image_path = action_data.get("image_path")
            raw_image_url = action_data.get("image_url")
            if raw_image_path is not None and (
                not isinstance(raw_image_path, str) or not raw_image_path.strip()
            ):
                raise _error(
                    path,
                    prefix + ".action.image_path",
                    "必须是非空字符串或省略",
                )
            if raw_image_url is not None and (
                not isinstance(raw_image_url, str) or not raw_image_url.strip()
            ):
                raise _error(
                    path,
                    prefix + ".action.image_url",
                    "必须是非空字符串或省略",
                )
            has_path = isinstance(raw_image_path, str) and bool(raw_image_path.strip())
            has_url = isinstance(raw_image_url, str) and bool(raw_image_url.strip())
            if has_path == has_url:
                raise _error(
                    path,
                    prefix + ".action",
                    "image_path 与 image_url 必须且只能提供一个",
                )
            image_path = None
            image_url = None
            if has_path:
                candidate = Path(raw_image_path.strip()).expanduser()
                if not candidate.is_absolute():
                    candidate = path.parent.parent / candidate
                candidate = candidate.resolve()
                project_root = path.parent.parent.resolve()
                if candidate != project_root and project_root not in candidate.parents:
                    raise _error(
                        path,
                        prefix + ".action.image_path",
                        "必须位于项目目录内",
                    )
                image_path = str(candidate)
            else:
                image_url = raw_image_url.strip()
                parsed_url = urlparse(image_url)
                if (
                    parsed_url.scheme.lower() not in {"http", "https"}
                    or not parsed_url.hostname
                ):
                    raise _error(
                        path,
                        prefix + ".action.image_url",
                        "必须是有效的 HTTP(S) URL",
                    )
                if parsed_url.username is not None or parsed_url.password is not None:
                    raise _error(
                        path,
                        prefix + ".action.image_url",
                        "不能包含用户名或密码",
                    )
            raw_caption = action_data.get("caption")
            caption = None
            if raw_caption is not None:
                if not isinstance(raw_caption, str) or not raw_caption.strip():
                    raise _error(
                        path,
                        prefix + ".action.caption",
                        "必须是非空字符串或省略",
                    )
                caption = raw_caption
            action = TaskAction(
                type=action_type,
                image_path=image_path,
                image_url=image_url,
                caption=caption,
            )
        elif action_type == "script":
            script_id = _required_nested_string(
                action_data, "script_id", prefix + ".action.script_id", path
            )
            if script_id not in scripts:
                raise _error(
                    path,
                    prefix + ".action.script_id",
                    "引用了不存在的脚本：{}".format(script_id),
                )
            parameters = action_data.get("parameters", {})
            if not isinstance(parameters, dict):
                raise _error(path, prefix + ".action.parameters", "必须是 JSON 对象")
            try:
                normalized = validate_script_parameters(scripts[script_id], parameters)
            except ValueError as exc:
                raise _error(
                    path, prefix + ".action.parameters", str(exc)
                ) from exc
            action = TaskAction(
                type=action_type,
                script_id=script_id,
                parameters=normalized,
            )
        elif action_type == "plugin":
            plugin_id = _required_nested_string(
                action_data, "plugin_id", prefix + ".action.plugin_id", path
            )
            if plugin_id not in known_plugin_ids():
                raise _error(
                    path,
                    prefix + ".action.plugin_id",
                    "引用了不存在的插件：{}".format(plugin_id),
                )
            tool_name = _required_nested_string(
                action_data, "tool_name", prefix + ".action.tool_name", path
            )
            available_tools = plugin_tool_names({plugin_id})
            if tool_name not in available_tools:
                raise _error(
                    path,
                    prefix + ".action.tool_name",
                    "插件 {} 不包含工具：{}".format(plugin_id, tool_name),
                )
            parameters = action_data.get("parameters", {})
            if not isinstance(parameters, dict):
                raise _error(path, prefix + ".action.parameters", "必须是 JSON 对象")
            action = TaskAction(
                type=action_type,
                plugin_id=plugin_id,
                tool_name=tool_name,
                parameters=parameters,
            )
        else:
            raise _error(
                path,
                prefix + ".action.type",
                "仅支持 text、agent_prompt、image、script 或 plugin",
            )
        tasks.append(
            ScheduledTask(
                id=task_id,
                enabled=enabled,
                cron=cron,
                target=target,
                action=action,
                condition=condition,
                crons=crons,
            )
        )
    return tasks


def _load_skills(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    data = _load_json(path)
    skills = data.get("skills", [])
    if not isinstance(skills, list):
        raise _error(path, "skills", "必须是数组")
    return skills


def _load_mcp_servers(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    data = _load_json(path)
    servers = data.get("servers", [])
    if not isinstance(servers, list):
        raise _error(path, "servers", "必须是数组")
    return servers


def load_project_config(config_dir: Path) -> ProjectConfig:
    config_dir = config_dir.resolve()
    app = _load_app(config_dir / "app.json")
    models = _load_models(config_dir / "models.json")
    embedding = _load_embedding(config_dir / "embeddings.json")
    tools = _load_tools(config_dir / "tools.json")
    plugins = _load_plugins(config_dir / "plugins.json")
    channels = _load_channels(config_dir / "channels.json")
    agents = _load_agents(config_dir / "agents")
    skills = _load_skills(config_dir / "skills.json")
    mcp_servers = _load_mcp_servers(config_dir / "mcp_servers.json")
    configured_plugin_tools = plugin_tool_names(plugins)
    skill_ids = {s.get("id") for s in skills if isinstance(s, dict)}
    server_ids = {s.get("id") for s in mcp_servers if isinstance(s, dict)}
    for agent in agents.values():
        unknown = sorted(
            set(agent.tools) - BUILTIN_TOOL_NAMES - configured_plugin_tools
        )
        if unknown:
            raise ConfigError(
                "Agent {} 引用了未知工具：{}".format(
                    agent.id, "、".join(unknown)
                )
            )
        unknown_skills = sorted(set(agent.skills) - skill_ids)
        if unknown_skills:
            raise ConfigError(
                "Agent {} 引用了未知技能：{}".format(
                    agent.id, "、".join(unknown_skills)
                )
            )
        unknown_servers = sorted(set(agent.mcp_servers) - server_ids)
        if unknown_servers:
            raise ConfigError(
                "Agent {} 引用了未知 MCP 服务：{}".format(
                    agent.id, "、".join(unknown_servers)
                )
            )
    if app.default_agent not in agents:
        raise ConfigError(
            "{}: default_agent 引用了不存在的 Agent：{}".format(
                config_dir / "app.json", app.default_agent
            )
        )
    if app.active_model not in models:
        raise ConfigError(
            "{}: active_model 引用了不存在的模型档案：{}".format(
                config_dir / "app.json", app.active_model
            )
        )
    for field_name in (
        "fallback_model", "local_model", "flash_model", "pro_model", "vision_model"
    ):
        profile_id = getattr(app, field_name)
        if profile_id not in models:
            raise ConfigError(
                "{}: {} 引用了不存在的模型档案：{}".format(
                    config_dir / "app.json", field_name, profile_id
                )
            )
    active_model = models[app.active_model]
    if not active_model.enabled:
        raise ConfigError(
            "{}: 活动模型档案 {} 必须启用".format(
                config_dir / "models.json",
                active_model.id,
            )
        )
    if not models[app.fallback_model].enabled:
        raise ConfigError(
            "{}: 兜底模型档案 {} 必须启用".format(
                config_dir / "models.json", app.fallback_model
            )
        )
    scripts = _load_scripts(config_dir / "scripts.json", config_dir.parent)
    schedules = _load_schedules(
        config_dir / "schedules.json", app.timezone, agents, scripts
    )
    return ProjectConfig(
        app=app,
        models=models,
        tools=tools,
        plugins=plugins,
        agents=agents,
        scripts=scripts,
        schedules=schedules,
        embedding=embedding,
        skills=skills,
        mcp_servers=mcp_servers,
        channels=channels,
    )
