"""Plugin management endpoints."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from src.api.deps import get_config, get_plugin_context, get_tool_audit_store, get_tool_runtime
from src.api.schemas import PluginOut, PluginToolOut, PluginUpdate, ToolStateUpdate
from src.core.paths import CONFIG_DIR, DATA_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

tools_router = APIRouter(prefix="/api/tools", tags=["tools"])

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
    ("脚本", ["list_scripts", "run_script", "get_script_run"]),
]

PLUGINS_FILE = CONFIG_DIR / "plugins.json"


def _build_plugin_out(plugin_id: str, config) -> PluginOut:
    from src.core.plugins.registry import PLUGIN_TYPES

    plugin_type = PLUGIN_TYPES.get(plugin_id)
    if plugin_type is None:
        raise HTTPException(status_code=404, detail="插件不存在")

    plugin_config = config.plugins.get(plugin_id)
    enabled = plugin_config.enabled if plugin_config else False
    settings = dict(plugin_config.settings) if plugin_config else {}

    tool_defs = getattr(plugin_type, "TOOL_DEFINITIONS", {})
    tools = [
        PluginToolOut(
            name=name,
            description=td.description,
            requires_approval=td.requires_approval,
            parameters=dict(td.parameters) if td.parameters else {},
        )
        for name, td in tool_defs.items()
    ]

    return PluginOut(
        id=plugin_id,
        enabled=enabled,
        tool_count=len(tools),
        tools=tools,
        settings=settings,
    )


def _load_plugins_json() -> dict:
    if PLUGINS_FILE.exists():
        return json.loads(PLUGINS_FILE.read_text(encoding="utf-8"))
    return {"plugins": []}


def _save_plugins_json(data: dict) -> None:
    PLUGINS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


@router.get("", response_model=list[PluginOut])
def list_plugins(request: Request):
    config = get_config(request)
    from src.core.plugins.registry import PLUGIN_TYPES

    return [_build_plugin_out(pid, config) for pid in PLUGIN_TYPES]


@router.get("/{plugin_id}", response_model=PluginOut)
def get_plugin(plugin_id: str, request: Request):
    config = get_config(request)
    return _build_plugin_out(plugin_id, config)


@router.put("/{plugin_id}", response_model=PluginOut)
def update_plugin(plugin_id: str, body: PluginUpdate, request: Request):
    config = get_config(request)
    from src.core.plugins.registry import PLUGIN_TYPES

    if plugin_id not in PLUGIN_TYPES:
        raise HTTPException(status_code=404, detail="插件不存在")

    plugin_config = config.plugins.get(plugin_id)
    if plugin_config is None:
        raise HTTPException(status_code=404, detail="插件配置不存在")

    new_enabled = body.enabled if body.enabled is not None else plugin_config.enabled
    new_settings = body.settings if body.settings is not None else dict(plugin_config.settings)

    from src.core.config.loader import PluginConfig

    config.plugins[plugin_id] = PluginConfig(
        id=plugin_id,
        enabled=new_enabled,
        settings=new_settings,
    )

    data = _load_plugins_json()
    for entry in data.get("plugins", []):
        if entry.get("id") == plugin_id:
            entry["enabled"] = new_enabled
            entry["settings"] = new_settings
            break
    else:
        data.setdefault("plugins", []).append({
            "id": plugin_id,
            "enabled": new_enabled,
            "settings": new_settings,
        })
    _save_plugins_json(data)

    tool_runtime = get_tool_runtime(request)
    plugin_context = get_plugin_context(request)
    if tool_runtime is not None and plugin_context is not None:
        from src.core.plugins.registry import build_plugins
        try:
            new_plugins = build_plugins(config.plugins, context=plugin_context)
            tool_runtime.reload_plugins(new_plugins)
        except Exception:  # noqa: BLE001 - config is saved; reload takes effect on restart
            logger.warning("热重载插件失败，重启后生效", exc_info=True)

    return _build_plugin_out(plugin_id, config)


@tools_router.get("")
def list_tools(request: Request):
    from src.core.tooling.runtime import APPROVAL_TOOLS, TOOL_DEFINITIONS

    tool_runtime = get_tool_runtime(request)
    result = []
    for category, tool_names in TOOL_CATEGORIES:
        for name in tool_names:
            definition = TOOL_DEFINITIONS.get(name, {})
            available = tool_runtime.is_available(name) if tool_runtime else False
            if tool_runtime:
                state = tool_runtime.get_tool_state(name)
                enabled = state["enabled"]
                requires_approval = state["require_approval"]
            else:
                enabled = True
                requires_approval = name in APPROVAL_TOOLS
            result.append({
                "name": name,
                "description": definition.get("description", ""),
                "category": category,
                "available": available,
                "enabled": enabled,
                "requires_approval": requires_approval,
            })
    return result


TOOL_STATE_FILE = DATA_DIR / "tool_state.json"


def _load_tool_states() -> dict:
    if TOOL_STATE_FILE.exists():
        return json.loads(TOOL_STATE_FILE.read_text(encoding="utf-8"))
    return {"tools": {}}


def _save_tool_states(data: dict) -> None:
    TOOL_STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


@tools_router.patch("/{name}")
def update_tool_state(name: str, body: ToolStateUpdate, request: Request):
    from src.core.tooling.runtime import TOOL_DEFINITIONS

    tool_runtime = get_tool_runtime(request)
    valid_names = set(TOOL_DEFINITIONS.keys())
    if tool_runtime:
        valid_names |= set(tool_runtime._plugin_tools.keys())
    if name not in valid_names:
        raise HTTPException(status_code=404, detail="工具不存在")

    data = _load_tool_states()
    tools = data.setdefault("tools", {})
    current = tools.get(name, {})

    if body.enabled is not None:
        current["enabled"] = body.enabled
    if body.require_approval is not None:
        current["require_approval"] = body.require_approval
    tools[name] = current
    _save_tool_states(data)

    if tool_runtime is not None:
        tool_runtime.reload_tool_states(tools)

    if tool_runtime:
        state = tool_runtime.get_tool_state(name)
    else:
        state = {"enabled": current.get("enabled", True),
                 "require_approval": current.get("require_approval", name in TOOL_DEFINITIONS)}
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
    # Clamp pagination inputs to sane bounds instead of failing the request.
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
