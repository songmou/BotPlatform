"""MCP server management endpoints."""

from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Body, HTTPException, Request

from src.api.deps import get_config, get_tool_runtime
from src.api.schemas import McpServerCreate, McpServerOut, McpServerUpdate
from src.core.config.loader import ConfigError
from src.core.paths import CONFIG_DIR
from src.core.tooling.mcp_client import namespaced_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

MCP_FILE = CONFIG_DIR / "mcp_servers.json"
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TRANSPORTS = {"stdio", "sse", "streamablehttp"}


def _load() -> list:
    if MCP_FILE.exists():
        return json.loads(MCP_FILE.read_text(encoding="utf-8")).get("servers", [])
    return []


def _save(servers: list) -> None:
    MCP_FILE.write_text(
        json.dumps({"servers": servers}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _ensure_manager(request: Request):
    tool_runtime = get_tool_runtime(request)
    if tool_runtime is None:
        return None
    manager = getattr(tool_runtime, "mcp_manager", None)
    if manager is None:
        from src.core.tooling.mcp_client import McpClientManager

        manager = McpClientManager()
        manager.start()
        tool_runtime.mcp_manager = manager
    return manager


def _sync(request: Request, servers: list) -> None:
    """Validate, persist, and apply the new server list to the live config."""
    try:
        get_config(request).update_mcp_servers(servers)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _save(servers)
    manager = _ensure_manager(request)
    if manager is not None:
        try:
            manager.reload(servers)
        except Exception:  # noqa: BLE001 - config is saved; reload takes effect on restart
            logger.warning("重载 MCP 服务器连接失败，重启后生效", exc_info=True)


def _to_out(item: dict) -> McpServerOut:
    return McpServerOut(
        id=item["id"],
        name=item["name"],
        transport=item.get("transport", "stdio"),
        command=item.get("command"),
        args=item.get("args", []),
        env=item.get("env", {}),
        url=item.get("url"),
        headers=item.get("headers", {}),
        enabled=item.get("enabled", True),
    )


@router.get("", response_model=list[McpServerOut])
def list_servers():
    return [_to_out(s) for s in _load()]


@router.post("", response_model=McpServerOut, status_code=201)
def create_server(body: McpServerCreate, request: Request):
    if not _ID_PATTERN.match(body.id):
        raise HTTPException(status_code=400, detail="ID 只能包含小写字母、数字和下划线，且以字母开头")
    if body.transport not in _TRANSPORTS:
        raise HTTPException(status_code=400, detail="transport 必须是 stdio、sse 或 streamablehttp")
    if body.transport == "stdio" and not body.command:
        raise HTTPException(status_code=400, detail="stdio 模式需要 command 字段")
    if body.transport in ("sse", "streamablehttp") and not body.url:
        raise HTTPException(status_code=400, detail="{} 模式需要 url 字段".format(body.transport))
    servers = _load()
    if any(s["id"] == body.id for s in servers):
        raise HTTPException(status_code=409, detail="MCP 服务 ID 已存在")
    item = {
        "id": body.id,
        "name": body.name,
        "transport": body.transport,
        "command": body.command,
        "args": body.args,
        "env": body.env,
        "url": body.url,
        "headers": body.headers,
        "enabled": body.enabled,
    }
    servers.append(item)
    _sync(request, servers)
    return _to_out(item)


@router.put("/{server_id}", response_model=McpServerOut)
def update_server(server_id: str, body: McpServerUpdate, request: Request):
    servers = _load()
    for s in servers:
        if s["id"] == server_id:
            if body.name is not None:
                s["name"] = body.name
            if body.transport is not None:
                if body.transport not in _TRANSPORTS:
                    raise HTTPException(status_code=400, detail="transport 必须是 stdio、sse 或 streamablehttp")
                s["transport"] = body.transport
            if body.command is not None:
                s["command"] = body.command
            if body.args is not None:
                s["args"] = body.args
            if body.env is not None:
                s["env"] = body.env
            if body.url is not None:
                s["url"] = body.url
            if body.headers is not None:
                s["headers"] = body.headers
            if body.enabled is not None:
                s["enabled"] = body.enabled
            _sync(request, servers)
            return _to_out(s)
    raise HTTPException(status_code=404, detail="MCP 服务不存在")


@router.delete("/{server_id}")
def delete_server(server_id: str, request: Request):
    servers = _load()
    filtered = [s for s in servers if s["id"] != server_id]
    if len(filtered) == len(servers):
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    _sync(request, filtered)
    return {"status": "ok"}


def _find_server(server_id: str) -> dict:
    for s in _load():
        if s["id"] == server_id:
            return s
    raise HTTPException(status_code=404, detail="MCP 服务不存在")


@router.get("/{server_id}/tools")
def list_server_tools(server_id: str, request: Request):
    cfg = _find_server(server_id)
    manager = _ensure_manager(request)
    if manager is None:
        return {"connected": False, "tools": [], "error": "工具运行时未启用"}
    error = None
    if server_id not in manager.server_ids():
        try:
            manager.connect_server(cfg)
        except Exception as exc:  # noqa: BLE001 - surface to the UI
            error = str(exc)
    tools = manager.server_tools(server_id)
    return {"connected": server_id in manager.server_ids(), "tools": tools, "error": error}


@router.post("/{server_id}/tools/{tool_name}/invoke")
def invoke_server_tool(
    server_id: str,
    tool_name: str,
    request: Request,
    body: dict = Body(default={}),
):
    cfg = _find_server(server_id)
    manager = _ensure_manager(request)
    if manager is None:
        return {"ok": False, "error": "工具运行时未启用"}
    if server_id not in manager.server_ids():
        try:
            manager.connect_server(cfg)
        except Exception as exc:  # noqa: BLE001 - surface to the UI
            return {"ok": False, "error": str(exc)}
    arguments = body.get("arguments") if isinstance(body, dict) else None
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        result = manager.call_tool(namespaced_name(server_id, tool_name), arguments)
    except Exception as exc:  # noqa: BLE001 - surface to the UI
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "result": result}
