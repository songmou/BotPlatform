"""Side-effect-free plugin manifest parsing and settings validation."""

from __future__ import annotations

import importlib.util
import json
import re
from importlib import metadata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .base import PluginJobDefinition, PluginToolDefinition
from src.core.services.env_resolver import normalize_allowlist


PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,127}$")
MODULE_SEGMENT_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
SUPPORTED_SERVICES = {
    "background_executor",
    "notification",
    "project_paths",
    "tenant_storage",
}


class PluginManifestError(ValueError):
    """Raised when a plugin package manifest is invalid."""


@dataclass(frozen=True)
class PluginDependency:
    distribution: str
    import_name: str
    version: str = ""


@dataclass(frozen=True)
class PluginManifest:
    schema_version: int
    id: str
    name: str
    version: str
    description: str
    entrypoint: str
    core_api: str
    source: str
    package_root: Path
    tools: Mapping[str, PluginToolDefinition]
    settings_schema: Dict[str, Any] = field(default_factory=dict)
    instructions: str = ""
    icon: str = ""
    color: str = "#6b7280"
    dependencies: Tuple[PluginDependency, ...] = ()
    services: Tuple[str, ...] = ()
    jobs: Tuple[PluginJobDefinition, ...] = ()
    settings_aliases: Mapping[str, str] = field(default_factory=dict)
    discard_unknown_settings: bool = False
    prepare: str = ""
    env_allowlist: Tuple[str, ...] = ()

    @property
    def missing_dependencies(self) -> List[str]:
        missing: List[str] = []
        for dependency in self.dependencies:
            try:
                available = importlib.util.find_spec(dependency.import_name) is not None
            except (ImportError, ModuleNotFoundError, ValueError):
                available = False
            if not available:
                label = dependency.distribution
                if dependency.version:
                    label += dependency.version
                missing.append(label)
                continue
            if dependency.version:
                try:
                    installed = metadata.version(dependency.distribution)
                    compatible = Version(installed) in SpecifierSet(
                        dependency.version
                    )
                except (
                    metadata.PackageNotFoundError,
                    InvalidSpecifier,
                    InvalidVersion,
                ):
                    compatible = False
                if not compatible:
                    missing.append(
                        "{}{}（版本不兼容）".format(
                            dependency.distribution,
                            dependency.version,
                        )
                    )
        return missing

    def validate_settings(self, settings: Mapping[str, Any]) -> None:
        validate_json_schema(dict(settings), self.settings_schema, "settings")

    def normalize_settings(self, settings: Mapping[str, Any]) -> Dict[str, Any]:
        normalized = dict(settings)
        for old_name, new_name in self.settings_aliases.items():
            if new_name not in normalized and old_name in normalized:
                normalized[new_name] = normalized[old_name]
            normalized.pop(old_name, None)
        if self.discard_unknown_settings:
            properties = self.settings_schema.get("properties", {})
            normalized = {
                key: value
                for key, value in normalized.items()
                if key in properties
            }
        return normalized


def _required_text(data: Mapping[str, Any], key: str, source: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PluginManifestError("{}: {} 必须是非空字符串".format(source, key))
    return value.strip()


def _object_schema(value: Any, source: Path, field_name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise PluginManifestError("{}: {} 必须是 JSON 对象".format(source, field_name))
    return dict(value)


def load_manifest(path: Path, source: str) -> PluginManifest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PluginManifestError("{}: 无法读取插件清单：{}".format(path, exc)) from exc
    if not isinstance(data, dict):
        raise PluginManifestError("{}: 顶层必须是 JSON 对象".format(path))
    allowed = {
        "schema_version", "id", "name", "version", "description", "entrypoint",
        "core_api", "icon", "color", "tools", "settings_schema", "instructions",
        "dependencies", "services", "background_jobs",
        "settings_aliases", "discard_unknown_settings", "prepare", "env_allowlist",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise PluginManifestError(
            "{}: 包含未知字段：{}".format(path, "、".join(unknown))
        )
    schema_version = data.get("schema_version")
    if schema_version != 1:
        raise PluginManifestError("{}: 仅支持 schema_version=1".format(path))
    plugin_id = _required_text(data, "id", path)
    if not PLUGIN_ID_PATTERN.fullmatch(plugin_id):
        raise PluginManifestError("{}: id 格式无效".format(path))
    entrypoint = _required_text(data, "entrypoint", path)
    _validate_module_reference(entrypoint, path, "entrypoint", source)
    prepare = str(data.get("prepare") or "").strip()
    if prepare:
        _validate_module_reference(prepare, path, "prepare", source)

    raw_tools = _object_schema(data.get("tools", {}), path, "tools")
    tools: Dict[str, PluginToolDefinition] = {}
    for tool_name, raw in raw_tools.items():
        if not isinstance(tool_name, str) or not TOOL_NAME_PATTERN.fullmatch(tool_name):
            raise PluginManifestError("{}: 工具名称格式无效：{}".format(path, tool_name))
        raw = _object_schema(raw, path, "tools.{}".format(tool_name))
        tool_unknown = sorted(
            set(raw) - {"description", "parameters", "approval", "direct_response"}
        )
        if tool_unknown:
            raise PluginManifestError(
                "{}: tools.{} 包含未知字段：{}".format(
                    path, tool_name, "、".join(tool_unknown)
                )
            )
        approval = raw.get("approval", "none")
        if approval not in {"none", "optional", "required"}:
            raise PluginManifestError(
                "{}: tools.{}.approval 无效".format(path, tool_name)
            )
        tools[tool_name] = PluginToolDefinition(
            description=_required_text(raw, "description", path),
            parameters=_object_schema(
                raw.get(
                    "parameters",
                    {"type": "object", "properties": {}, "additionalProperties": False},
                ),
                path,
                "tools.{}.parameters".format(tool_name),
            ),
            requires_approval=approval == "required",
            direct_response=bool(raw.get("direct_response", False)),
            approval_policy=approval,
        )
        if tools[tool_name].parameters.get("type") != "object":
            raise PluginManifestError(
                "{}: tools.{}.parameters 必须是对象 Schema".format(path, tool_name)
            )

    raw_dependencies = data.get("dependencies", [])
    if not isinstance(raw_dependencies, list):
        raise PluginManifestError("{}: dependencies 必须是数组".format(path))
    dependencies: List[PluginDependency] = []
    for index, raw in enumerate(raw_dependencies):
        raw = _object_schema(raw, path, "dependencies[{}]".format(index))
        unknown_dependency = sorted(
            set(raw) - {"distribution", "import", "version"}
        )
        if unknown_dependency:
            raise PluginManifestError(
                "{}: dependencies[{}] 包含未知字段：{}".format(
                    path, index, "、".join(unknown_dependency)
                )
            )
        dependencies.append(
            PluginDependency(
                distribution=_required_text(raw, "distribution", path),
                import_name=_required_text(raw, "import", path),
                version=str(raw.get("version") or ""),
            )
        )
        if dependencies[-1].version:
            try:
                SpecifierSet(dependencies[-1].version)
            except InvalidSpecifier as exc:
                raise PluginManifestError(
                    "{}: dependencies[{}].version 格式无效".format(path, index)
                ) from exc

    services = data.get("services", [])
    if not isinstance(services, list) or any(not isinstance(item, str) for item in services):
        raise PluginManifestError("{}: services 必须是字符串数组".format(path))
    unsupported_services = sorted(set(services) - SUPPORTED_SERVICES)
    if unsupported_services:
        raise PluginManifestError(
            "{}: 不支持的平台服务：{}".format(
                path, "、".join(unsupported_services)
            )
        )
    raw_jobs = data.get("background_jobs", [])
    if not isinstance(raw_jobs, list):
        raise PluginManifestError("{}: background_jobs 必须是数组".format(path))
    jobs: List[PluginJobDefinition] = []
    for index, raw in enumerate(raw_jobs):
        raw = _object_schema(raw, path, "background_jobs[{}]".format(index))
        job_id = _required_text(raw, "id", path)
        interval = raw.get("interval_seconds")
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 1:
            raise PluginManifestError(
                "{}: background_jobs[{}].interval_seconds 必须是正整数".format(
                    path, index
                )
            )
        jobs.append(PluginJobDefinition(job_id, interval))
    job_ids = [item.id for item in jobs]
    if len(set(job_ids)) != len(job_ids):
        raise PluginManifestError("{}: 后台任务 ID 不能重复".format(path))

    settings_schema = _object_schema(
        data.get(
            "settings_schema",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        path,
        "settings_schema",
    )
    if settings_schema.get("type") != "object":
        raise PluginManifestError("{}: settings_schema 必须是对象 Schema".format(path))
    settings_aliases = _object_schema(
        data.get("settings_aliases", {}), path, "settings_aliases"
    )
    if any(
        not isinstance(old_name, str)
        or not isinstance(new_name, str)
        or not old_name
        or new_name not in settings_schema.get("properties", {})
        for old_name, new_name in settings_aliases.items()
    ):
        raise PluginManifestError("{}: settings_aliases 格式无效".format(path))
    discard_unknown_settings = data.get("discard_unknown_settings", False)
    if not isinstance(discard_unknown_settings, bool):
        raise PluginManifestError(
            "{}: discard_unknown_settings 必须是布尔值".format(path)
        )
    try:
        env_allowlist = tuple(normalize_allowlist(data.get("env_allowlist", [])))
    except ValueError as exc:
        raise PluginManifestError("{}: env_allowlist 无效：{}".format(path, exc)) from exc
    core_api = _required_text(data, "core_api", path)
    if core_api != "1":
        raise PluginManifestError("{}: 不兼容的核心插件 API：{}".format(path, core_api))
    manifest = PluginManifest(
        schema_version=1,
        id=plugin_id,
        name=_required_text(data, "name", path),
        version=_required_text(data, "version", path),
        description=_required_text(data, "description", path),
        entrypoint=entrypoint,
        core_api=core_api,
        source=source,
        package_root=path.parent.resolve(),
        tools=tools,
        settings_schema=settings_schema,
        instructions=str(data.get("instructions") or "").strip(),
        icon=str(data.get("icon") or plugin_id[:1].upper())[:3],
        color=str(data.get("color") or "#6b7280"),
        dependencies=tuple(dependencies),
        services=tuple(services),
        jobs=tuple(jobs),
        settings_aliases=settings_aliases,
        discard_unknown_settings=discard_unknown_settings,
        prepare=prepare,
        env_allowlist=env_allowlist,
    )
    return manifest


def _validate_module_reference(
    reference: str, path: Path, field_name: str, source: str
) -> None:
    """Validate a module:attribute reference and its file for external packages."""
    if ":" not in reference:
        raise PluginManifestError(
            "{}: {} 必须使用 module:attribute".format(path, field_name)
        )
    module_name, attribute = reference.split(":", 1)
    module_parts = module_name.split(".")
    if (
        not module_name
        or not attribute
        or any(not MODULE_SEGMENT_PATTERN.fullmatch(item) for item in module_parts)
        or not MODULE_SEGMENT_PATTERN.fullmatch(attribute)
    ):
        raise PluginManifestError("{}: {} 格式无效".format(path, field_name))
    if source == "external":
        relative = Path(*module_parts)
        file_target = (path.parent / relative).with_suffix(".py").resolve()
        package_target = (path.parent / relative / "__init__.py").resolve()
        package_root = path.parent.resolve()
        target = package_target if package_target.is_file() else file_target
        if package_root not in target.parents or not target.is_file():
            raise PluginManifestError(
                "{}: {} 文件不存在或越界".format(path, field_name)
            )


def validate_json_schema(value: Any, schema: Mapping[str, Any], field: str) -> None:
    """Validate the JSON-Schema subset used by plugin settings forms."""
    expected = schema.get("type")
    if isinstance(expected, list):
        if value is None and "null" in expected:
            return
        candidates = [item for item in expected if item != "null"]
        if not any(_matches_type(value, item) for item in candidates):
            raise PluginManifestError("{} 类型无效".format(field))
    elif expected and not _matches_type(value, expected):
        raise PluginManifestError("{} 必须是 {}".format(field, expected))
    if "enum" in schema and value not in schema["enum"]:
        raise PluginManifestError("{} 只能是：{}".format(field, "、".join(map(str, schema["enum"]))))
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                raise PluginManifestError("{} 缺少必填字段：{}".format(field, name))
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise PluginManifestError(
                    "{} 包含未知字段：{}".format(field, "、".join(unknown))
                )
        for name, item in value.items():
            child = properties.get(name)
            if child is not None:
                validate_json_schema(item, child, "{}.{}".format(field, name))
        for reference in schema.get("x-references", []):
            if not isinstance(reference, dict):
                raise PluginManifestError("{} 的 x-references 格式无效".format(field))
            source_name = reference.get("field")
            collection_name = reference.get("array")
            key_name = reference.get("key")
            if (
                not isinstance(source_name, str)
                or not source_name
                or not isinstance(collection_name, str)
                or not collection_name
                or not isinstance(key_name, str)
                or not key_name
            ):
                raise PluginManifestError(
                    "{} 的 x-references 格式无效".format(field)
                )
            source_value = value.get(source_name)
            collection = value.get(collection_name, [])
            if source_value is None:
                continue
            if (
                not isinstance(collection, list)
                or not any(
                    isinstance(item, dict)
                    and item.get(key_name) == source_value
                    for item in collection
                )
            ):
                raise PluginManifestError(
                    "{}.{} 必须引用 {} 中存在的 {}".format(
                        field,
                        source_name,
                        collection_name,
                        key_name,
                    )
                )
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise PluginManifestError(
                "{} 至少需要 {} 项".format(field, schema["minItems"])
            )
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise PluginManifestError(
                "{} 最多允许 {} 项".format(field, schema["maxItems"])
            )
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_json_schema(item, item_schema, "{}[{}]".format(field, index))
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise PluginManifestError("{} 不能包含重复值".format(field))
        unique_key = schema.get("x-unique-key")
        if unique_key is not None:
            if not isinstance(unique_key, str) or not unique_key:
                raise PluginManifestError("{} 的 x-unique-key 格式无效".format(field))
            keys = [
                item.get(unique_key)
                for item in value
                if isinstance(item, dict)
            ]
            if len(keys) != len(value) or len(set(keys)) != len(keys):
                raise PluginManifestError(
                    "{} 的 {} 不能重复".format(field, unique_key)
                )
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise PluginManifestError("{} 不能小于 {}".format(field, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            raise PluginManifestError("{} 不能大于 {}".format(field, schema["maximum"]))
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise PluginManifestError(
                "{} 长度不能少于 {}".format(field, schema["minLength"])
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise PluginManifestError(
                "{} 长度不能超过 {}".format(field, schema["maxLength"])
            )
        pattern = schema.get("pattern")
        if pattern and re.fullmatch(str(pattern), value) is None:
            raise PluginManifestError("{} 格式无效".format(field))


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(str(expected), True)
