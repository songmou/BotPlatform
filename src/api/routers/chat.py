"""Chat endpoints with SSE streaming and multi-conversation support."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from src.api.deps import (
    get_conversation_store,
    get_organization_store,
    get_principal,
    require_permission,
)
from src.api.schemas import ChatHistoryItem, ChatHistoryResponse, ChatRequest
from src.core.modeling.contracts import (
    CanonicalMessage,
)
from src.core.config.loader import AgentPreset
from src.core.services.agent_tools import (
    build_system_prompt,
    resolve_tool_names,
)
from src.core.storage.organizations import OrganizationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

DEFAULT_TITLE = "新对话"


def _agent_from_payload(resource_id: str, payload: dict) -> AgentPreset:
    from src.core.services.resources import _agent_from_payload as parse
    return parse(resource_id, payload)


def _bind_agent_scope(tool_runtime, tenant, agent) -> None:
    """Bind tenant workspace and the agent's datasource grant on this thread.

    ``bind_tenant`` deliberately clears any previous datasource grant, so the
    two must always be applied together.  Web requests execute tools on anyio
    worker threads, which is why this is re-applied before every execute().
    """
    if tool_runtime is None:
        return
    if tenant is not None:
        tool_runtime.bind_tenant(tenant)
    binder = getattr(tool_runtime, "bind_agent_datasources", None)
    if binder is not None:
        binder(list(getattr(agent, "datasources", []) or []))


def _run_agent(agent, subtask, history, model_router, skills, context, tool_runtime=None,
               tenant=None, knowledge_service=None, return_knowledge=False):
    """Compatibility adapter for integrations that used the old helper."""
    from types import SimpleNamespace
    from src.core.services.agent import AgentService
    app = SimpleNamespace(default_agent=agent.id, history_rounds=6, timezone="Asia/Shanghai")
    service = AgentService(model_router, app, {agent.id: agent}, tool_runtime=tool_runtime, skills=skills)
    if tenant is not None and tool_runtime is not None:
        _bind_agent_scope(tool_runtime, tenant, agent)
    service._analytics_context.value = context
    model = model_router.session("auto", start_profile_id=agent.model)
    messages = [CanonicalMessage("system", build_system_prompt(agent, skills, tool_runtime)), *history,
                CanonicalMessage("user", subtask)]
    outcome = service._run_tool_loop("compatibility", subtask, [], messages, None,
        resolve_tool_names(agent, tool_runtime) if tool_runtime else [], 0, 0, [], model,
        datasource_ids=agent.datasources, preset=agent)
    return (outcome.text, []) if return_knowledge else outcome.text


@router.get("/conversations")
def list_conversations(
    request: Request,
    principal=Depends(require_permission("panel.read")),
):
    organizations = get_organization_store(request)
    organization_id = organizations.ensure_debug_organization(
        principal.user.user_id, principal.user.username
    )
    return organizations.list_conversations(
        principal.user.user_id, organization_id
    )


@router.post("/conversations", status_code=201)
def create_conversation(
    request: Request,
    principal=Depends(require_permission("panel.read")),
):
    organizations = get_organization_store(request)
    organization_id = organizations.ensure_debug_organization(
        principal.user.user_id, principal.user.username
    )
    return organizations.create_conversation(
        principal.user.user_id, organization_id, DEFAULT_TITLE
    )


@router.get("/conversations/{conv_id}")
def get_conversation_by_id(
    conv_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    organizations = get_organization_store(request)
    try:
        return organizations.get_conversation(
            principal.user.user_id,
            conv_id,
            allow_delegation=principal.allows("admins.manage"),
        )
    except OrganizationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/conversations/{conv_id}")
def delete_conversation(
    conv_id: str,
    request: Request,
    principal=Depends(require_permission("panel.read")),
):
    try:
        get_organization_store(request).delete_conversation(
            principal.user.user_id, conv_id
        )
    except OrganizationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}


@router.post("")
def chat(body: ChatRequest, request: Request, principal=Depends(require_permission("panel.read"))):
    from src.api.routers.agent_runs import stream_run, submit_web_run
    return stream_run(request, submit_web_run(body, request, principal))


@router.get("/history", response_model=ChatHistoryResponse)
def chat_history(
    request: Request,
    conversation_id: Optional[str] = None,
    principal=Depends(require_permission("panel.read")),
):
    organizations = get_organization_store(request)
    try:
        conversation = organizations.get_conversation(
            principal.user.user_id,
            conversation_id or "",
            allow_delegation=principal.allows("admins.manage"),
        )
    except OrganizationError:
        return ChatHistoryResponse(messages=[])
    store = get_conversation_store(request)
    organization_id = str(conversation["organization_id"])
    messages = store.load_transcript(
        organization_id,
        session_key="organization:{}".format(conversation_id),
    )
    return ChatHistoryResponse(
        messages=[ChatHistoryItem(role=m.role, content=m.content) for m in messages]
    )
