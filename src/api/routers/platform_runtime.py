"""Platform runtime views and actions that are not plain catalog CRUD.

Everything here lives under ``/api/v2/platform`` so it shares the modern
authorization surface with the catalog endpoints instead of the retired
``/api/*`` configuration writers.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
import traceback
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.api.deps import (
    get_config,
    get_resource_store,
    get_router,
    get_tool_runtime,
    require_permission,
)
from src.api.schemas import (
    AgentOut,
    KnowledgeAgentBindingsIn,
    McpTemplateAuth,
    McpTemplateOut,
)
from src.core.config.mcp_headers import merge_headers
from src.core.integrations.keychain import KeychainError
from src.core.paths import CONFIG_DIR
from src.core.tooling.mcp_client import namespaced_name
from src.core.services.resources import ResourceError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/platform", tags=["platform-runtime"])

ROLE_FIELDS = (
    "active_model",
    "fallback_model",
    "local_model",
    "flash_model",
    "pro_model",
    "vision_model",
    "embedding_model",
    "rerank_model",
)

ROLE_LABELS = {
    "active_model": "主模型",
    "fallback_model": "备用模型",
    "local_model": "本地模型",
    "flash_model": "快速模型",
    "pro_model": "高阶模型",
    "vision_model": "视觉模型",
    "embedding_model": "向量模型",
    "rerank_model": "重排模型",
}


class ModelRoutingUpdate(BaseModel):
    """Partial update of the runtime model bindings."""

    active_model: Optional[str] = None
    fallback_model: Optional[str] = None
    local_model: Optional[str] = None
    flash_model: Optional[str] = None
    pro_model: Optional[str] = None
    vision_model: Optional[str] = None
    embedding_model: Optional[str] = None
    rerank_model: Optional[str] = None


def _candidates(config, predicate) -> List[Dict[str, Any]]:
    return [
        {"id": profile.id, "model": profile.model, "enabled": profile.enabled}
        for profile in config.models.values()
        if predicate(profile)
    ]


def _published_settings(store) -> Dict[str, Any]:
    """Latest saved settings/runtime payload.

    Uses the *published* revision rather than the active one: embedding and
    rerank changes are persisted immediately but only activated on the next
    restart, and both the roles panel and subsequent partial updates must
    build on what the operator last saved.
    """
    if store is None:
        raise ResourceError("平台资源库不可用")
    return next(
        (
            dict(item["payload"])
            for item in store.list_public("settings", include_unpublished=True)
            if item["resource_id"] == "runtime"
        ),
        None,
    ) or store.get_public("settings", "runtime")["payload"]


def _saved_bindings(config, store) -> Dict[str, str]:
    saved: Dict[str, Any] = {}
    try:
        saved = _published_settings(store)
    except ResourceError:
        saved = {}
    return {
        field: str(saved.get(field, getattr(config.app, field, "")) or "")
        for field in ROLE_FIELDS
    }


def _routing_view(config, model_router, store=None) -> Dict[str, Any]:
    return {
        **_saved_bindings(config, store),
        "primary_profile_id": model_router.primary_profile_id,
        "fallback_profile_id": model_router.fallback_profile_id,
        "local_profile_id": model_router.local_profile_id,
        "flash_profile_id": model_router.flash_profile_id,
        "pro_profile_id": model_router.pro_profile_id,
        "vision_profile_id": model_router.vision_profile_id,
        "cooling_down": model_router.cooling_down,
        "last_primary_error": model_router.last_primary_error,
        "chat_candidates": _candidates(config, lambda p: p.modality == "chat"),
        "vision_candidates": _candidates(
            config, lambda p: p.modality == "chat" and p.capabilities.vision
        ),
        "embedding_candidates": _candidates(
            config, lambda p: p.modality == "embedding"
        ),
        "rerank_candidates": _candidates(config, lambda p: p.modality == "rerank"),
    }


def _resolve_binding(
    config, field: str, value: Any, modality: str, require_vision: bool = False
) -> str:
    binding = str(value or "").strip()
    if not binding:
        return ""
    profile = config.models.get(binding)
    if profile is None:
        raise HTTPException(
            status_code=400, detail="{} 引用的模型不存在".format(ROLE_LABELS[field])
        )
    if profile.modality != modality:
        raise HTTPException(
            status_code=400,
            detail="{} 必须引用 {} 类型的模型".format(ROLE_LABELS[field], modality),
        )
    if require_vision and not profile.capabilities.vision:
        raise HTTPException(status_code=400, detail="视觉模型必须启用 vision 能力")
    if modality == "chat" and not profile.enabled:
        raise HTTPException(
            status_code=400, detail="{} 引用的模型未启用".format(ROLE_LABELS[field])
        )
    return binding


def _pick_alternate_chat(config, exclude: str) -> str:
    for profile in config.models.values():
        if profile.modality == "chat" and profile.enabled and profile.id != exclude:
            return profile.id
    return exclude


@router.get("/model-routing")
def get_model_routing(
    request: Request,
    _principal=Depends(require_permission("panel.read")),
):
    return _routing_view(
        get_config(request), get_router(request), get_resource_store(request)
    )


@router.put("/model-routing")
def update_model_routing(
    request: Request,
    body: ModelRoutingUpdate,
    principal=Depends(require_permission("panel.write")),
):
    config = get_config(request)
    model_router = get_router(request)
    store = get_resource_store(request)
    if store is None:
        raise HTTPException(status_code=503, detail="平台资源库不可用")

    supplied = body.model_fields_set
    if not supplied:
        raise HTTPException(status_code=400, detail="请至少提供一个模型绑定字段")

    try:
        payload = dict(_published_settings(store))
    except ResourceError as exc:
        raise HTTPException(status_code=500, detail="运行时设置尚未初始化") from exc

    rules = {
        "active_model": ("chat", False),
        "fallback_model": ("chat", False),
        "local_model": ("chat", False),
        "flash_model": ("chat", False),
        "pro_model": ("chat", False),
        "vision_model": ("chat", True),
        "embedding_model": ("embedding", False),
        "rerank_model": ("rerank", False),
    }
    for field in ROLE_FIELDS:
        if field not in supplied:
            continue
        modality, require_vision = rules[field]
        payload[field] = _resolve_binding(
            config, field, getattr(body, field), modality, require_vision
        )

    if not payload.get("active_model"):
        raise HTTPException(status_code=400, detail="主模型不能为空")
    if payload.get("fallback_model") == payload.get("active_model"):
        payload["fallback_model"] = _pick_alternate_chat(
            config, payload["active_model"]
        )

    try:
        result = store.upsert_public(
            "settings", "runtime", payload, principal.user.user_id
        )
    except ResourceError as exc:
        # settings/runtime always exists here, so every failure is a bad
        # request (validation) or a failed hot activation - never a 404.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "status": "ok",
        "restart_required": result.get("activation_state") == "restart_required",
        "routing": _routing_view(config, model_router, store),
    }


# --------------------------------------------------------------------------- #
# Agent runtime views
# --------------------------------------------------------------------------- #


def _agent_to_out(agent) -> AgentOut:
    """Serialize an :class:`AgentPreset` to the public ``AgentOut`` schema."""
    return AgentOut(
        id=agent.id,
        name=agent.name,
        role=agent.role,
        description=agent.description,
        system_prompt=agent.system_prompt,
        capabilities=[
            {"name": c.name, "description": c.description} for c in agent.capabilities
        ],
        tools=agent.tools,
        plugin_tools={key: list(value) for key, value in agent.plugin_tools.items()},
        skills=list(agent.skills),
        mcp_servers=list(agent.mcp_servers),
        datasources=list(getattr(agent, "datasources", []) or []),
        model=agent.model,
        greeting=agent.greeting,
        greeting_hints=list(agent.greeting_hints),
        temperature=agent.temperature,
        max_tokens=agent.max_tokens,
        enabled=agent.enabled,
    )


def _knowledge_service(request: Request):
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="知识库服务不可用")
    return service


def _require_platform_agent(request: Request, agent_id: str) -> None:
    """Validate agents against the database-backed platform catalog."""
    try:
        get_resource_store(request).get_public("agents", agent_id)
    except ResourceError as exc:
        raise HTTPException(status_code=404, detail="智能体不存在") from exc


@router.get("/agents/active", response_model=AgentOut)
def get_active_agent(
    request: Request,
    _principal=Depends(require_permission("panel.read")),
):
    """Return the agent referenced by ``config.app.default_agent``."""
    return _agent_to_out(get_config(request).active_agent)


@router.get("/agents/{agent_id}/knowledge-categories")
def get_agent_knowledge_categories(
    agent_id: str,
    request: Request,
    principal=Depends(require_permission("knowledge.read")),
):
    _require_platform_agent(request, agent_id)
    service = _knowledge_service(request)
    return {"category_ids": service.get_agent_bindings(agent_id)}


@router.put("/agents/{agent_id}/knowledge-categories")
def update_agent_knowledge_categories(
    agent_id: str,
    body: KnowledgeAgentBindingsIn,
    request: Request,
    principal=Depends(require_permission("knowledge.manage")),
):
    _require_platform_agent(request, agent_id)
    service = _knowledge_service(request)
    try:
        category_ids = service.set_agent_bindings(agent_id, body.category_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"category_ids": category_ids}


# --------------------------------------------------------------------------- #
# MCP templates + live tool inspection / invocation
# --------------------------------------------------------------------------- #

MCP_TEMPLATES_FILE = CONFIG_DIR / "mcp_templates.json"


def _flatten_exception(exc: BaseException) -> List[BaseException]:
    """Expand ExceptionGroup trees into their leaf exceptions."""
    group = getattr(exc, "exceptions", None)
    if not group:
        return [exc]
    leaves: List[BaseException] = []
    for sub in group:
        leaves.extend(_flatten_exception(sub))
    return leaves or [exc]


def _describe_error(exc: Exception, context: str) -> str:
    """Return a non-empty, human-readable error and log the full traceback."""
    print("MCP {} 失败：{}".format(context, repr(exc)), file=sys.stderr)
    traceback.print_exc()
    parts = []
    for leaf in _flatten_exception(exc):
        text = str(leaf).strip() or repr(leaf).strip()
        label = "{}: {}".format(type(leaf).__name__, text) if text else type(leaf).__name__
        if label not in parts:
            parts.append(label)
    message = "；".join(parts).strip()
    if not message:
        message = "{}（{} 未返回错误详情，请查看服务端日志）".format(type(exc).__name__, context)
    return message


def _load_templates() -> List[Dict[str, Any]]:
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


def _template_out(t: Dict[str, Any]) -> McpTemplateOut:
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


def _ensure_manager(request: Request):
    """Return the shared MCP client manager, starting it on first use."""
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


def _list_mcp_servers(store, include_unpublished: bool = False) -> List[Dict[str, Any]]:
    """All MCP servers from the catalog, with keychain headers merged in."""
    if store is None:
        return []
    items = store.list_public("mcp", include_unpublished=include_unpublished)
    servers: List[Dict[str, Any]] = []
    for item in items:
        payload = dict(item.get("payload") or {})
        payload["id"] = item["resource_id"]
        servers.append(payload)
    try:
        return merge_headers(servers)
    except KeychainError as exc:
        raise HTTPException(
            status_code=500, detail="读取 MCP 请求头失败：{}".format(exc)
        ) from exc


def _find_mcp_server(store, server_id: str) -> Dict[str, Any]:
    for server in _list_mcp_servers(store):
        if server["id"] == server_id:
            return server
    raise HTTPException(status_code=404, detail="MCP 服务不存在")


@router.get("/mcp/templates", response_model=List[McpTemplateOut])
def list_mcp_templates(
    response: Response,
    _principal=Depends(require_permission("panel.read")),
):
    """Return the built-in MCP service template catalog (blueprints, no secrets)."""
    response.headers["Cache-Control"] = "no-store"
    return [_template_out(t) for t in _load_templates()]


@router.get("/mcp/templates/{key}", response_model=McpTemplateOut)
def get_mcp_template(
    key: str,
    _principal=Depends(require_permission("panel.read")),
):
    """Return a single MCP service template by key."""
    for t in _load_templates():
        if t["key"] == key:
            return _template_out(t)
    raise HTTPException(status_code=404, detail="模板不存在")


@router.get("/mcp/{server_id}/tools")
def list_mcp_server_tools(
    server_id: str,
    request: Request,
    response: Response,
    _principal=Depends(require_permission("panel.read")),
):
    response.headers["Cache-Control"] = "no-store"
    cfg = _find_mcp_server(get_resource_store(request), server_id)
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
    return {
        "connected": server_id in manager.server_ids(),
        "tools": tools,
        "error": error,
    }


@router.post("/mcp/{server_id}/tools/{tool_name}/invoke")
def invoke_mcp_server_tool(
    server_id: str,
    tool_name: str,
    request: Request,
    body: dict = Body(default={}),
    _principal=Depends(require_permission("panel.write")),
):
    cfg = _find_mcp_server(get_resource_store(request), server_id)
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


# --------------------------------------------------------------------------- #
# Feishu user-delegated token (UAT) OAuth flow
# --------------------------------------------------------------------------- #
# The platform supports two Feishu auth modes: the app-level TAT (auto-refreshed
# from app_id + app_secret, sufficient for app-context tools) and the
# user-delegated UAT (required by user-context tools such as get-user /
# list-docs).  The UAT is obtained via the operator completing this OAuth flow;
# the resulting token is stored per (server, user) in the keychain.
FEISHU_SERVER_ID = "feishu"


def _feishu_server_cfg(store) -> Dict[str, Any]:
    """Return the Feishu MCP server config from the catalog, or 404."""
    try:
        return _find_mcp_server(store, FEISHU_SERVER_ID)
    except HTTPException:
        raise HTTPException(status_code=404, detail="未找到飞书 MCP 服务，请先在平台创建飞书集成")


def _feishu_app_id(cfg: Dict[str, Any]) -> str:
    app_id = (cfg.get("token_provider") or {}).get("app_id")
    if not app_id:
        raise HTTPException(
            status_code=400,
            detail="飞书集成未配置 app_id（token_provider.app_id），无法发起授权",
        )
    return str(app_id)


@router.get("/feishu-oauth/authorize-url")
def feishu_oauth_authorize_url(
    request: Request,
    redirect_uri: str,
    server_id: str = FEISHU_SERVER_ID,
    _principal=Depends(require_permission("panel.write")),
):
    """Return the Feishu OAuth URL the operator opens to grant a UAT.

    ``redirect_uri`` must equal the redirect URI registered for the Feishu app
    and point back at this endpoint's ``/callback`` path.
    """
    from src.core.tooling.mcp_token_providers import build_feishu_authorize_url

    cfg = _feishu_server_cfg(get_resource_store(request))
    token_provider = cfg.get("token_provider") or {}
    if token_provider.get("kind") not in ("feishu_uat", None):
        raise HTTPException(
            status_code=400,
            detail="当前飞书集成未使用 UAT 模式（token_provider.kind=feishu_uat），"
            "请先在集成配置中切换为 UAT 模式再授权",
        )
    app_id = _feishu_app_id(cfg)
    state = secrets.token_urlsafe(16)
    url = build_feishu_authorize_url(app_id, redirect_uri, state)
    return {"authorize_url": url, "state": state}


@router.get("/feishu-oauth/callback", response_class=HTMLResponse)
def feishu_oauth_callback(
    request: Request,
    code: str,
    state: str = "",
    server_id: str = FEISHU_SERVER_ID,
    principal=Depends(require_permission("panel.write")),
):
    """Handle the Feishu OAuth redirect: exchange the code for a UAT and store it.

    The UAT is bound to the authenticated operator (``principal.user.user_id``).
    After storage the server is connected for that user so its tools are
    discovered and subsequent calls use the UAT.
    """
    from src.core.tooling.mcp_token_providers import exchange_feishu_code

    user_id = principal.user.user_id
    cfg = _feishu_server_cfg(get_resource_store(request))
    app_id = _feishu_app_id(cfg)
    try:
        exchange_feishu_code(server_id, user_id, app_id, code)
    except Exception as exc:  # noqa: BLE001 - show the operator what went wrong
        logger.warning("飞书 OAuth 回调失败（用户 %s）：%s", user_id, exc)
        return HTMLResponse(
            _feishu_oauth_result_page(
                False, "飞书授权失败：{}".format(str(exc).splitlines()[0] if str(exc) else "未知错误")
            )
        )
    manager = _ensure_manager(request)
    if manager is not None:
        try:
            manager.connect_server(cfg, user_id=user_id)
        except Exception as exc:  # noqa: BLE001 - non-fatal; tools appear next call
            logger.warning("飞书 UAT 授权后连接失败（用户 %s）：%s", user_id, exc)
    return HTMLResponse(
        _feishu_oauth_result_page(
            True,
            "飞书授权成功，已以你的身份绑定访问令牌（UAT）。现在可以正常使用飞书用户态工具（如查看文档、查询用户）。",
        )
    )


def _feishu_oauth_result_page(success: bool, message: str) -> str:
    """Minimal HTML page shown to the operator after the OAuth redirect."""
    title = "飞书授权成功" if success else "飞书授权失败"
    color = "#1677ff" if success else "#d4380d"
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>{title}</title></head><body style='font-family:system-ui,"
        "sans-serif;display:flex;align-items:center;justify-content:center;"
        "height:100vh;margin:0;background:#f5f5f5'>"
        "<div style='background:#fff;padding:32px 40px;border-radius:12px;"
        "box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:480px;text-align:center'>"
        "<div style='font-size:40px;margin-bottom:12px'>{emoji}</div>"
        "<h2 style='color:{color};margin:0 0 12px'>{title}</h2>"
        "<p style='color:#333;line-height:1.6;margin:0 0 20px'>{message}</p>"
        "<button onclick='window.close()' style='background:{color};color:#fff;"
        "border:none;padding:10px 24px;border-radius:8px;cursor:pointer;"
        "font-size:14px'>关闭</button>"
        "</div></body></html>"
    ).format(title=title, color=color, message=message, emoji="✅" if success else "⚠️")
