"""MCP server management endpoints."""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from src.api.deps import get_config, get_tool_runtime, require_permission
from src.api.schemas import (
    McpServerCreate,
    McpServerOut,
    McpServerUpdate,
    McpTemplateAuth,
    McpTemplateOut,
)
from src.core.config.loader import ConfigError
from src.core.config.mcp_headers import (
    delete_headers,
    merge_headers,
    save_headers,
)
from src.core.paths import CONFIG_DIR
from src.core.tooling.mcp_client import namespaced_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

MCP_FILE = CONFIG_DIR / "mcp_servers.json"
MCP_TEMPLATES_FILE = CONFIG_DIR / "mcp_templates.json"
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TRANSPORTS = {"stdio", "sse", "streamablehttp"}


def _flatten_exception(exc: BaseException) -> list[BaseException]:
    """Expand ExceptionGroup trees into their leaf exceptions."""
    group = getattr(exc, "exceptions", None)
    if not group:
        return [exc]
    leaves: list[BaseException] = []
    for sub in group:
        leaves.extend(_flatten_exception(sub))
    return leaves or [exc]


def _describe_error(exc: Exception, context: str) -> str:
    """Return a non-empty, human-readable error and log the full traceback."""
    print(
        "MCP {} 失败：{}".format(context, repr(exc)),
        file=sys.stderr,
    )
    traceback.print_exc()
    parts = []
    for leaf in _flatten_exception(exc):
        text = str(leaf).strip() or repr(leaf).strip()
        label = "{}: {}".format(type(leaf).__name__, text) if text else type(leaf).__name__
        if label not in parts:
            parts.append(label)
    message = "；".join(parts).strip()
    if not message:
        message = "{}（{} 未返回错误详情，请查看服务端日志）".format(
            type(exc).__name__, context
        )
    return message


def _load() -> list:
    if MCP_FILE.exists():
        servers = json.loads(MCP_FILE.read_text(encoding="utf-8")).get("servers", [])
        return merge_headers(servers)
    return []


def _save(servers: list) -> None:
    stripped = [
        {**s, "headers": {}} if isinstance(s, dict) else s for s in servers
    ]
    MCP_FILE.write_text(
        json.dumps({"servers": stripped}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_templates() -> list:
    """Load the curated MCP template catalog (a list of blueprint dicts)."""
    if not MCP_TEMPLATES_FILE.exists():
        return []
    try:
        data = json.loads(MCP_TEMPLATES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("templates", [])
    else:
        return []
    return [t for t in items if isinstance(t, dict) and t.get("key") and t.get("name")]


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


def _template_out(t: dict) -> McpTemplateOut:
    auth = t.get("auth")
    return McpTemplateOut(
        key=t["key"],
        name=t["name"],
        description=t.get("description", ""),
        category=t.get("category", ""),
        transport=t.get("transport", "stdio"),
        command=t.get("command"),
        args=t.get("args", []),
        env=t.get("env", {}),
        url=t.get("url"),
        icon=t.get("icon", ""),
        auth=McpTemplateAuth(**auth) if isinstance(auth, dict) else None,
        help_url=t.get("help_url"),
    )


@router.get("", response_model=list[McpServerOut])
def list_servers(
    response: Response,
    _principal=Depends(require_permission("panel.read")),
):
    response.headers["Cache-Control"] = "no-store"
    return [_to_out(s) for s in _load()]


@router.post("", response_model=McpServerOut, status_code=201)
def create_server(
    body: McpServerCreate,
    request: Request,
    _principal=Depends(require_permission("panel.write")),
):
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
    save_headers(body.id, body.headers)
    _sync(request, servers)
    return _to_out(item)


@router.get("/templates", response_model=list[McpTemplateOut])
def list_templates(
    response: Response,
    _principal=Depends(require_permission("panel.read")),
):
    """返回内置的 MCP 服务模板目录（蓝图，不含密钥）。"""
    response.headers["Cache-Control"] = "no-store"
    return [_template_out(t) for t in _load_templates()]


@router.get("/templates/{key}", response_model=McpTemplateOut)
def get_template(
    key: str,
    _principal=Depends(require_permission("panel.read")),
):
    """返回单个 MCP 服务模板；创建实例时可据此预填固定配置。"""
    for t in _load_templates():
        if t["key"] == key:
            return _template_out(t)
    raise HTTPException(status_code=404, detail="模板不存在")


@router.put("/{server_id}", response_model=McpServerOut)
def update_server(
    server_id: str,
    body: McpServerUpdate,
    request: Request,
    _principal=Depends(require_permission("panel.write")),
):
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
                save_headers(server_id, body.headers)
            if body.enabled is not None:
                s["enabled"] = body.enabled
            _sync(request, servers)
            return _to_out(s)
    raise HTTPException(status_code=404, detail="MCP 服务不存在")


@router.delete("/{server_id}")
def delete_server(
    server_id: str,
    request: Request,
    _principal=Depends(require_permission("panel.write")),
):
    servers = _load()
    filtered = [s for s in servers if s["id"] != server_id]
    if len(filtered) == len(servers):
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    delete_headers(server_id)
    _sync(request, filtered)
    return {"status": "ok"}


def _find_server(server_id: str) -> dict:
    for s in _load():
        if s["id"] == server_id:
            return s
    raise HTTPException(status_code=404, detail="MCP 服务不存在")


@router.get("/{server_id}/tools")
def list_server_tools(
    server_id: str,
    request: Request,
    response: Response,
    _principal=Depends(require_permission("panel.read")),
):
    response.headers["Cache-Control"] = "no-store"
    cfg = _find_server(server_id)
    manager = _ensure_manager(request)
    if manager is None:
        return {"connected": False, "tools": [], "error": "工具运行时未启用"}
    error = None
    if server_id not in manager.server_ids():
        try:
            manager.connect_server(cfg)
        except Exception as exc:  # noqa: BLE001 - surface to the UI
            error = _describe_error(exc, "连接服务")
    tools = manager.server_tools(server_id)
    return {"connected": server_id in manager.server_ids(), "tools": tools, "error": error}


@router.post("/{server_id}/tools/{tool_name}/invoke")
def invoke_server_tool(
    server_id: str,
    tool_name: str,
    request: Request,
    body: dict = Body(default={}),
    _principal=Depends(require_permission("panel.write")),
):
    cfg = _find_server(server_id)
    manager = _ensure_manager(request)
    if manager is None:
        return {"ok": False, "error": "工具运行时未启用"}
    if server_id not in manager.server_ids():
        try:
            manager.connect_server(cfg)
        except Exception as exc:  # noqa: BLE001 - surface to the UI
            return {"ok": False, "error": _describe_error(exc, "连接服务")}
    arguments = body.get("arguments") if isinstance(body, dict) else None
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        result = manager.call_tool(namespaced_name(server_id, tool_name), arguments)
    except Exception as exc:  # noqa: BLE001 - surface to the UI
        return {"ok": False, "error": _describe_error(exc, "调用工具")}
    return {"ok": True, "result": result}
