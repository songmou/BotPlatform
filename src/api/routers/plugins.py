"""Plugin catalog, package management, tool state, and audit endpoints."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from src.api.deps import (
    get_config,
    get_plugin_manager,
    get_registry,
    get_tool_audit_store,
    get_tool_runtime,
    require_permission,
)
from src.api.schemas import (
    PluginDataDeleteIn,
    PluginOut,
    PluginPackageIn,
    PluginToolOut,
    PluginUpdate,
    ToolStateUpdate,
)
from src.core.paths import CONFIG_DIR, DATA_DIR, PROJECT_ROOT, SYSTEM_DATA_DIR
from src.core.plugins.base import PluginError
from src.core.plugins.manifest import (
    PLUGIN_ID_PATTERN,
    PluginManifestError,
    load_manifest,
)
from src.core.plugins.registry import (
    default_catalog,
    normalize_plugin_settings,
    refresh_catalog,
)
from src.core.plugins.setup import (
    STATUS_IDLE,
    PluginSetupBusyError,
    default_setup_service,
)


router = APIRouter(prefix="/api/plugins", tags=["plugins"])
tools_router = APIRouter(prefix="/api/tools", tags=["tools"])

PLUGINS_FILE = CONFIG_DIR / "plugins.json"
PLUGIN_PACKAGES_DIR = SYSTEM_DATA_DIR / "plugins"
PLUGIN_TRASH_DIR = SYSTEM_DATA_DIR / "plugin_trash"
TOOL_STATE_FILE = DATA_DIR / "tool_state.json"

TOOL_CATEGORIES = [
    ("知识库", [
        "knowledge_add_text", "knowledge_index_file", "knowledge_search",
        "knowledge_list", "knowledge_delete",
    ]),
    ("文件系统", [
        "list_allowed_roots", "list_directory", "find_files", "search_text",
        "read_text_file", "get_path_info", "create_directory", "write_text_file",
        "replace_text", "copy_path", "move_path", "move_to_trash",
    ]),
    ("系统信息", ["get_current_time", "get_system_info", "get_disk_usage", "list_processes"]),
    ("命令执行", ["run_command"]),
    ("脚本", [
        "list_scripts", "run_script", "get_script_run", "cancel_script_run",
        "list_script_schedules", "manage_script_schedule",
    ]),
]


def _load_plugins_json() -> dict:
    if not PLUGINS_FILE.exists():
        return {"plugins": []}
    try:
        data = json.loads(PLUGINS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="插件配置无法读取") from exc
    if not isinstance(data, dict):
        return {"plugins": []}
    for entry in data.get("plugins", []):
        if not isinstance(entry, dict):
            continue
        raw = entry.get("settings")
        if not isinstance(raw, dict):
            continue
        try:
            entry["settings"] = normalize_plugin_settings(
                str(entry.get("id") or ""), raw
            )
        except ValueError:
            continue
    return data


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, raw_temp = tempfile.mkstemp(
        prefix=".".join((path.name, "tmp", "")),
        dir=str(path.parent),
    )
    temp = Path(raw_temp)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            temp.unlink()


def _configured_entry(plugin_id: str) -> Optional[dict]:
    for entry in _load_plugins_json().get("plugins", []):
        if isinstance(entry, dict) and entry.get("id") == plugin_id:
            return entry
    return None


def _build_plugin_out(
    plugin_id: str,
    request: Request,
    configured: Optional[dict] = None,
    restart_required: bool = False,
) -> PluginOut:
    manifest = default_catalog().get(plugin_id)
    if configured is None:
        configured = _configured_entry(plugin_id)
    if manifest is None:
        if configured is None:
            raise HTTPException(status_code=404, detail="插件不存在")
        return PluginOut(
            id=plugin_id,
            name=plugin_id,
            description="配置引用的插件包当前未安装",
            installed=False,
            enabled=bool(configured.get("enabled", False)),
            configuration_status="unresolved",
            runtime_status="missing",
            restart_required=restart_required,
            load_error="插件包未安装",
            tool_count=0,
            settings=dict(configured.get("settings", {}) or {}),
        )
    enabled = bool(configured and configured.get("enabled", False))
    settings = dict(configured.get("settings", {})) if configured else {}
    configuration_status = "valid"
    try:
        manifest.validate_settings(settings)
    except PluginManifestError:
        configuration_status = "invalid"
    manager = get_plugin_manager(request)
    runtime_plugin = manager.get(plugin_id) if manager is not None else None
    error = manager.errors.get(plugin_id) if manager is not None else None
    runtime_enabled = runtime_plugin is not None
    pending_restart = runtime_enabled != enabled
    if runtime_plugin is not None:
        runtime_status = "running"
    elif error:
        runtime_status = "error"
    elif enabled:
        runtime_status = "pending_restart"
    else:
        runtime_status = "disabled"
    missing = manifest.missing_dependencies
    if missing:
        runtime_status = "dependency_missing"
    setup_snapshot = default_setup_service().status(plugin_id)
    setup_status = (
        setup_snapshot if setup_snapshot.get("status") != STATUS_IDLE else None
    )
    tools = [
        PluginToolOut(
            name=name,
            description=definition.description,
            requires_approval=definition.approval_policy == "required",
            approval_policy=definition.approval_policy,
            parameters=dict(definition.parameters),
        )
        for name, definition in manifest.tools.items()
    ]
    return PluginOut(
        id=manifest.id,
        name=manifest.name,
        version=manifest.version,
        description=manifest.description,
        source=manifest.source,
        icon=manifest.icon,
        color=manifest.color,
        enabled=enabled,
        configuration_status=configuration_status,
        runtime_status=runtime_status,
        restart_required=restart_required or pending_restart,
        missing_dependencies=missing,
        load_error=error,
        setup_status=setup_status,
        tool_count=len(tools),
        tools=tools,
        settings=settings,
        settings_schema=dict(manifest.settings_schema),
    )


def _assert_safe_package(source: Path) -> None:
    if not source.is_dir():
        raise HTTPException(status_code=400, detail="插件来源必须是本地目录")
    for item in source.rglob("*"):
        if item.is_symlink():
            raise HTTPException(status_code=400, detail="插件包不能包含符号链接")


def _stage_package(source: Path):
    PLUGIN_PACKAGES_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage_root = Path(
        tempfile.mkdtemp(prefix=".plugin-stage-", dir=str(PLUGIN_PACKAGES_DIR))
    )
    package = stage_root / "package"
    shutil.copytree(str(source), str(package))
    manifest = load_manifest(package / "plugin.json", "external")
    return stage_root, package, manifest


def _assert_tool_names_available(manifest, *, replacing_id: str = "") -> None:
    from src.core.tooling.definitions import TOOL_DEFINITIONS

    conflicts = sorted(
        set(manifest.tools) & set(TOOL_DEFINITIONS)
        | {
            tool_name
            for plugin_id, installed in default_catalog().manifests.items()
            if plugin_id != replacing_id
            for tool_name in set(manifest.tools) & set(installed.tools)
        }
    )
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail="插件工具名称与现有插件冲突：{}".format("、".join(conflicts)),
        )


@router.get("", response_model=list[PluginOut])
def list_plugins(
    request: Request,
    _principal=Depends(require_permission("plugins.read")),
):
    configured_ids = {
        str(entry.get("id"))
        for entry in _load_plugins_json().get("plugins", [])
        if isinstance(entry, dict) and entry.get("id")
    }
    plugin_ids = set(default_catalog().manifests) | configured_ids
    return [
        _build_plugin_out(plugin_id, request)
        for plugin_id in sorted(plugin_ids)
    ]


@router.post("/install", response_model=PluginOut, status_code=201)
def install_plugin(
    body: PluginPackageIn,
    request: Request,
    _principal=Depends(require_permission("plugins.manage")),
):
    source = Path(body.source_path).expanduser().resolve()
    _assert_safe_package(source)
    stage_root: Optional[Path] = None
    try:
        stage_root, package, manifest = _stage_package(source)
        if default_catalog().get(manifest.id) is not None:
            raise HTTPException(status_code=409, detail="插件 ID 已安装或与内置插件冲突")
        _assert_tool_names_available(manifest)
        destination = PLUGIN_PACKAGES_DIR / manifest.id
        if destination.exists():
            raise HTTPException(status_code=409, detail="插件目录已存在")
        os.replace(str(package), str(destination))
    except PluginManifestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if stage_root is not None and stage_root.exists():
            shutil.rmtree(str(stage_root))
    refresh_catalog()
    return _build_plugin_out(manifest.id, request, restart_required=True)


@router.put("/{plugin_id}/package", response_model=PluginOut)
def update_plugin_package(
    plugin_id: str,
    body: PluginPackageIn,
    request: Request,
    _principal=Depends(require_permission("plugins.manage")),
):
    current = default_catalog().get(plugin_id)
    if current is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    if current.source != "external":
        raise HTTPException(status_code=400, detail="内置插件不能通过本接口更新")
    source = Path(body.source_path).expanduser().resolve()
    _assert_safe_package(source)
    stage_root: Optional[Path] = None
    backup: Optional[Path] = None
    destination = PLUGIN_PACKAGES_DIR / plugin_id
    try:
        stage_root, package, manifest = _stage_package(source)
        if manifest.id != plugin_id:
            raise HTTPException(status_code=400, detail="更新包的插件 ID 不一致")
        _assert_tool_names_available(manifest, replacing_id=plugin_id)
        PLUGIN_TRASH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = PLUGIN_TRASH_DIR / "{}-update-{}".format(
            plugin_id, datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        )
        os.replace(str(destination), str(backup))
        try:
            os.replace(str(package), str(destination))
        except Exception:
            os.replace(str(backup), str(destination))
            backup = None
            raise
    except PluginManifestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if stage_root is not None and stage_root.exists():
            shutil.rmtree(str(stage_root))
    refresh_catalog()
    return _build_plugin_out(plugin_id, request, restart_required=True)


@router.get("/{plugin_id}", response_model=PluginOut)
def get_plugin(
    plugin_id: str,
    request: Request,
    _principal=Depends(require_permission("plugins.read")),
):
    return _build_plugin_out(plugin_id, request)


@router.put("/{plugin_id}", response_model=PluginOut)
def update_plugin(
    plugin_id: str,
    body: PluginUpdate,
    request: Request,
    _principal=Depends(require_permission("plugins.manage")),
):
    manifest = default_catalog().get(plugin_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    current = _configured_entry(plugin_id) or {
        "id": plugin_id,
        "enabled": False,
        "settings": {},
    }
    was_enabled = bool(current["enabled"])
    enabled = body.enabled if body.enabled is not None else was_enabled
    settings = body.settings if body.settings is not None else dict(current["settings"])
    try:
        settings = manifest.normalize_settings(settings)
        manifest.validate_settings(settings)
    except (ValueError, PluginManifestError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    data = _load_plugins_json()
    entries = data.setdefault("plugins", [])
    replacement = {
        "id": plugin_id,
        "enabled": enabled,
        "settings": settings,
    }
    for index, entry in enumerate(entries):
        if entry.get("id") == plugin_id:
            entries[index] = replacement
            break
    else:
        entries.append(replacement)
    _atomic_json(PLUGINS_FILE, data)
    if enabled and not was_enabled and manifest.missing_dependencies:
        # Kick off dependency installation when the plugin gets enabled.
        try:
            default_setup_service().start(manifest, settings)
        except PluginError:
            pass
    return _build_plugin_out(
        plugin_id,
        request,
        configured=replacement,
        restart_required=True,
    )


@router.post("/{plugin_id}/setup")
def start_plugin_setup(
    plugin_id: str,
    _principal=Depends(require_permission("plugins.manage")),
):
    manifest = default_catalog().get(plugin_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    configured = _configured_entry(plugin_id) or {"settings": {}}
    settings = manifest.normalize_settings(
        dict(configured.get("settings", {}) or {})
    )
    try:
        return default_setup_service().start(manifest, settings)
    except PluginSetupBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PluginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{plugin_id}/setup")
def get_plugin_setup(
    plugin_id: str,
    _principal=Depends(require_permission("plugins.manage")),
):
    if default_catalog().get(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    return default_setup_service().status(plugin_id)


@router.delete("/{plugin_id}")
def remove_plugin(
    plugin_id: str,
    request: Request,
    _principal=Depends(require_permission("plugins.manage")),
):
    manifest = default_catalog().get(plugin_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="插件不存在")
    if manifest.source != "external":
        raise HTTPException(status_code=400, detail="内置插件不能移除，只能禁用")
    configured = _configured_entry(plugin_id)
    if configured and configured.get("enabled"):
        raise HTTPException(status_code=409, detail="请先禁用插件并重启")
    manager = get_plugin_manager(request)
    if manager is not None and manager.get(plugin_id) is not None:
        raise HTTPException(status_code=409, detail="插件仍在运行，请先完整重启")
    config = get_config(request)
    referenced_agents = [
        item.id for item in config.agents.values() if item.plugin_tools.get(plugin_id)
    ]
    referenced_tasks = [
        item.id
        for item in config.schedules
        if item.action.type == "plugin" and item.action.plugin_id == plugin_id
    ]
    if referenced_agents or referenced_tasks:
        raise HTTPException(
            status_code=409,
            detail="插件仍被智能体或定时任务引用，请先解除绑定",
        )
    source = PLUGIN_PACKAGES_DIR / plugin_id
    PLUGIN_TRASH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = PLUGIN_TRASH_DIR / "{}-removed-{}".format(
        plugin_id, datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    )
    os.replace(str(source), str(target))
    data = _load_plugins_json()
    data["plugins"] = [
        entry for entry in data.get("plugins", []) if entry.get("id") != plugin_id
    ]
    _atomic_json(PLUGINS_FILE, data)
    refresh_catalog()
    return {
        "id": plugin_id,
        "removed": True,
        "data_preserved": True,
        "restart_required": True,
    }


@router.delete("/{plugin_id}/data")
def delete_plugin_data(
    plugin_id: str,
    body: PluginDataDeleteIn,
    request: Request,
    _principal=Depends(require_permission("plugins.manage")),
):
    if not PLUGIN_ID_PATTERN.fullmatch(plugin_id):
        raise HTTPException(status_code=404, detail="插件不存在")
    configured = _configured_entry(plugin_id)
    if configured and configured.get("enabled"):
        raise HTTPException(status_code=409, detail="清除数据前必须先禁用插件并重启")
    manager = get_plugin_manager(request)
    if manager is not None and manager.get(plugin_id) is not None:
        raise HTTPException(status_code=409, detail="插件仍在运行，请先完整重启")
    if body.confirmation != plugin_id:
        raise HTTPException(status_code=400, detail="确认文本必须与插件 ID 完全一致")
    registry = get_registry(request)
    deleted = 0
    for tenant_root in registry.users_root.iterdir() if registry.users_root.exists() else []:
        target = tenant_root / "plugins" / plugin_id
        if target.is_dir():
            shutil.rmtree(str(target))
            deleted += 1
    global_target = DATA_DIR / "plugins" / plugin_id
    if global_target.is_dir():
        shutil.rmtree(str(global_target))
        deleted += 1
    if deleted == 0 and default_catalog().get(plugin_id) is None:
        raise HTTPException(status_code=404, detail="插件或插件数据不存在")
    return {"id": plugin_id, "deleted_directories": deleted}


@tools_router.get("")
def list_tools(request: Request):
    from src.core.tooling.runtime import APPROVAL_TOOLS, TOOL_DEFINITIONS

    runtime = get_tool_runtime(request)
    result = []
    for category, tool_names in TOOL_CATEGORIES:
        for name in tool_names:
            definition = TOOL_DEFINITIONS.get(name, {})
            state = runtime.get_tool_state(name) if runtime else {
                "enabled": True,
                "require_approval": name in APPROVAL_TOOLS,
            }
            result.append({
                "name": name,
                "description": definition.get("description", ""),
                "category": category,
                "source_type": "builtin",
                "source_id": None,
                "available": runtime.is_available(name) if runtime else False,
                **state,
                "requires_approval": state["require_approval"],
            })
    for manifest in default_catalog().manifests.values():
        configured = _configured_entry(manifest.id)
        if not configured:
            continue
        for name, definition in manifest.tools.items():
            state = runtime.get_tool_state(name) if runtime else {
                "enabled": True,
                "require_approval": definition.approval_policy == "required",
            }
            required = (
                True
                if definition.approval_policy == "required"
                else state["require_approval"]
            )
            result.append({
                "name": name,
                "description": definition.description,
                "category": "插件工具",
                "source_type": "plugin",
                "source_id": manifest.id,
                "available": runtime.is_available(name) if runtime else False,
                "enabled": state["enabled"],
                "require_approval": required,
                "requires_approval": required,
                "approval_policy": definition.approval_policy,
            })
    return result


def _load_tool_states() -> dict:
    if TOOL_STATE_FILE.exists():
        return json.loads(TOOL_STATE_FILE.read_text(encoding="utf-8"))
    return {"tools": {}}


@tools_router.patch("/{name}")
def update_tool_state(name: str, body: ToolStateUpdate, request: Request):
    from src.core.tooling.runtime import TOOL_DEFINITIONS

    runtime = get_tool_runtime(request)
    plugin_definition = next(
        (
            manifest.tools[name]
            for manifest in default_catalog().manifests.values()
            if name in manifest.tools
        ),
        None,
    )
    if name not in TOOL_DEFINITIONS and plugin_definition is None:
        raise HTTPException(status_code=404, detail="工具不存在")
    if (
        plugin_definition is not None
        and plugin_definition.approval_policy == "required"
        and body.require_approval is False
    ):
        raise HTTPException(status_code=400, detail="该插件工具强制要求审批，不能降低")
    data = _load_tool_states()
    current = data.setdefault("tools", {}).get(name, {})
    if body.enabled is not None:
        current["enabled"] = body.enabled
    if body.require_approval is not None:
        current["require_approval"] = body.require_approval
    data["tools"][name] = current
    _atomic_json(TOOL_STATE_FILE, data)
    if runtime is not None:
        runtime.reload_tool_states(data["tools"])
        state = runtime.get_tool_state(name)
        if plugin_definition is not None and plugin_definition.approval_policy == "required":
            state["require_approval"] = True
    else:
        state = {
            "enabled": current.get("enabled", True),
            "require_approval": current.get(
                "require_approval",
                bool(
                    plugin_definition
                    and plugin_definition.approval_policy == "required"
                ),
            ),
        }
    return {"name": name, **state}


@tools_router.get("/audit")
def list_tool_audit(
    request: Request,
    limit: int = 50,
    tool: str = None,
    status: str = None,
    offset: int = 0,
):
    if status is not None and status not in {"成功", "失败"}:
        raise HTTPException(status_code=422, detail="状态仅支持“成功”或“失败”")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    store = get_tool_audit_store(request)
    if store is None:
        return {"items": [], "total": 0}
    items = store.list_recent(
        limit=limit,
        tool_name=tool,
        offset=offset,
        status=status,
    )
    total = store.count(tool_name=tool, status=status)
    return {"items": items, "total": total}
