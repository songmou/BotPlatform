"""Chat endpoints with SSE streaming and multi-conversation support."""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Generator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from src.api.deps import (
    get_config,
    get_conversation_store,
    get_model_analytics_store,
    get_organization_store,
    get_principal,
    get_resource_store,
    get_router,
    require_permission,
)
from src.api.schemas import ChatHistoryItem, ChatHistoryResponse, ChatRequest
from src.api.sse import (
    sse_agent_done,
    sse_agent_start,
    sse_done,
    sse_error,
    sse_plan,
    sse_sources,
    sse_summary_start,
    sse_thinking,
    sse_token,
    sse_tool_call,
    sse_tool_progress,
    sse_tool_result,
    streaming_response,
)
from src.core.modeling.contracts import (
    CanonicalMessage,
    GenerationOptions,
    ModelCallContext,
    ModelError,
    ModelRequest,
)
from src.core.config.loader import AgentPreset, Capability
from src.core.services.agent_tools import (
    build_system_prompt,
    is_tool_call_text,
    resolve_tool_names,
    sanitize_tool_call_text,
    strip_tool_call_text,
)
from src.core.services.resources import ResourceError
from src.core.storage.organizations import OrganizationError
from src.core.storage.tenants import TenantContext
from src.core.tooling.models import ToolAuditContext, ToolError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

DEFAULT_TITLE = "新对话"


def _agent_from_payload(resource_id: str, payload: dict) -> AgentPreset:
    capabilities = []
    for item in payload.get("capabilities", []):
        if isinstance(item, dict):
            capabilities.append(
                Capability(
                    name=str(item.get("name") or ""),
                    description=str(item.get("description") or ""),
                )
            )
    return AgentPreset(
        id=str(payload.get("id") or resource_id),
        name=str(payload.get("name") or resource_id),
        role=str(payload.get("role") or "assistant"),
        description=str(payload.get("description") or ""),
        system_prompt=str(payload.get("system_prompt") or "你是一个有帮助的助手。"),
        capabilities=capabilities,
        enabled=bool(payload.get("enabled", True)),
        image_prompt=payload.get("image_prompt"),
        tools=list(payload.get("tools") or []),
        plugin_tools=dict(payload.get("plugin_tools") or {}),
        skills=list(payload.get("skills") or []),
        mcp_servers=list(payload.get("mcp_servers") or []),
        datasources=list(payload.get("datasources") or []),
        model=payload.get("model"),
        greeting=payload.get("greeting"),
        greeting_hints=list(payload.get("greeting_hints") or []),
        temperature=payload.get("temperature"),
        max_tokens=payload.get("max_tokens"),
    )


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


def _effective_agents(request: Request, organization_id: str) -> dict:
    config = get_config(request)
    try:
        resources = get_resource_store(request).list_effective(
            organization_id, "agents"
        )
        agents = {
            item["resource_id"]: _agent_from_payload(
                item["resource_id"], item["payload"]
            )
            for item in resources
            if bool(item["payload"].get("enabled", True))
        }
        return agents or config.agents
    except (ResourceError, TypeError, ValueError):
        logger.warning("读取组织智能体配置失败，回退到平台配置", exc_info=True)
        return config.agents


def _effective_skills(request: Request, organization_id: str) -> list:
    config = get_config(request)
    try:
        resources = get_resource_store(request).list_effective(
            organization_id, "skills"
        )
        return [
            item["payload"]
            for item in resources
            if bool(item["payload"].get("enabled", True))
        ]
    except (ResourceError, TypeError, ValueError):
        logger.warning("读取组织 Skill 配置失败，回退到平台配置", exc_info=True)
        return config.skills


PLANNER_PROMPT = (
    "你是一个任务规划器。请根据用户请求和可用智能体列表，决定由哪些智能体参与处理，"
    "以及每个智能体负责的具体子任务。\n"
    "可用智能体：\n{agents}\n\n"
    "用户请求：{request}\n\n"
    "请只输出一个 JSON 数组（不要输出任何其他文字），格式为："
    '[{{"agent_id":"智能体id","subtask":"该智能体负责的子任务描述"}}]。'
    "如果请求简单、由一个智能体直接完成即可，数组只包含一个元素。"
)

SUMMARY_PROMPT = (
    "用户请求：{request}\n\n"
    "以下是各智能体的处理结果：\n{results}\n\n"
    "请整合以上结果，给出一个结构清晰、完整的最终回答。直接输出整合后的内容。"
)


def _extract_plan_json(text: str):
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _make_plan(
    user_message: str,
    agents: list,
    model_router,
    context: ModelCallContext,
) -> Optional[List[dict]]:
    agents_desc = "\n".join(
        "- id: {}, 名称: {}, 描述: {}".format(a.id, a.name, a.description) for a in agents
    )
    prompt = PLANNER_PROMPT.format(agents=agents_desc, request=user_message)
    client = model_router.clients[model_router.primary_profile_id]
    response = client.complete(
        ModelRequest(
            messages=[CanonicalMessage("user", prompt)],
            context=ModelCallContext(
                **{**context.__dict__, "operation": "planner", "agent_id": None}
            ),
        )
    )
    plan = _extract_plan_json(response.message.content)
    if not isinstance(plan, list) or not plan:
        return None
    agent_ids = {a.id for a in agents}
    valid = []
    for item in plan:
        if isinstance(item, dict) and item.get("agent_id") in agent_ids:
            valid.append(
                {
                    "agent_id": item["agent_id"],
                    "subtask": str(item.get("subtask") or user_message),
                }
            )
    return valid or None


def _run_agent(
    agent,
    subtask: str,
    history: list,
    model_router,
    skills: list,
    context: ModelCallContext,
    tool_runtime=None,
    tenant: Optional[TenantContext] = None,
    knowledge_service=None,
    return_knowledge: bool = False,
):
    messages = [
        CanonicalMessage("system", build_system_prompt(agent, skills, tool_runtime))
    ]
    knowledge_hits = []
    if knowledge_service is not None and tenant is not None:
        try:
            knowledge_hits = knowledge_service.search(
                tenant.tenant_id, subtask, limit=6, agent_id=agent.id
            )
        except Exception:
            knowledge_hits = []
        if knowledge_hits:
            messages.append(
                CanonicalMessage(
                    "system", knowledge_service.context_message(knowledge_hits)
                )
            )

    def result(answer: str):
        if knowledge_service is not None and knowledge_hits:
            answer = knowledge_service.append_citations(answer, knowledge_hits)
        return (answer, knowledge_hits) if return_knowledge else answer

    messages.extend(history)
    messages.append(CanonicalMessage("user", subtask))
    agent_model = getattr(agent, "model", None)
    start_profile = (
        agent_model if agent_model and agent_model in model_router.clients else None
    )
    session = model_router.session("auto", start_profile_id=start_profile)
    generation = GenerationOptions(
        temperature=getattr(agent, "temperature", None),
        max_tokens=getattr(agent, "max_tokens", None),
    )
    call_context = ModelCallContext(
        **{
            **context.__dict__,
            "operation": "agent_subtask",
            "agent_id": agent.id,
        }
    )

    tool_names = resolve_tool_names(agent, tool_runtime) if tool_runtime else []
    tool_schemas = []
    if tool_runtime and tenant and tool_names and session.capabilities.tools:
        try:
            _bind_agent_scope(tool_runtime, tenant, agent)
            tool_schemas = tool_runtime.schemas(tool_names)
        except Exception as exc:
            raise ModelError(
                "多智能体工具初始化失败：{}".format(str(exc)),
                provider=session.identity.provider,
            ) from exc

    if not tool_schemas:
        response = session.complete(
            ModelRequest(
                messages=messages,
                generation=generation,
                context=call_context,
            )
        )
        answer = response.message.content.strip()
        if not answer:
            raise ModelError(
                "智能体 {} 没有返回文字".format(agent.name),
                provider=session.identity.provider,
            )
        return result(answer)

    allowed_tool_names = {
        schema["function"]["name"]
        for schema in tool_schemas
        if isinstance(schema, dict)
        and isinstance(schema.get("function"), dict)
        and isinstance(schema["function"].get("name"), str)
    }
    max_rounds = tool_runtime.config.max_tool_rounds
    max_calls = tool_runtime.config.max_total_tool_calls
    rounds_used = 0
    total_calls = 0

    while rounds_used < max_rounds:
        response = session.complete(
            ModelRequest(
                messages=messages,
                generation=generation,
                tools=tool_schemas,
                context=call_context,
            )
        )
        rounds_used += 1
        model_message = response.message
        raw_calls = model_message.tool_calls
        if not raw_calls:
            answer = model_message.content.strip()
            if not answer:
                raise ModelError(
                    "智能体 {} 结束工具循环时没有返回文字".format(agent.name),
                    provider=session.identity.provider,
                )
            return result(answer)

        if total_calls + len(raw_calls) > max_calls:
            return result(
                "本次子任务需要的工具步骤超过安全上限，请缩小问题范围后重试。"
            )

        # One assistant message may contain multiple parallel tool calls. It
        # must appear exactly once before the corresponding tool results.
        messages.append(model_message)
        total_calls += len(raw_calls)
        for call in raw_calls:
            tool_name = call.name or "unknown"
            tool_args = call.arguments if isinstance(call.arguments, dict) else {}
            if tool_name not in allowed_tool_names:
                result_payload = {
                    "ok": False,
                    "error": "该智能体无权调用工具：{}".format(tool_name),
                }
            else:
                try:
                    if tool_runtime.requires_approval(tool_name, tool_args):
                        result_payload = {
                            "ok": False,
                            "error": (
                                "多智能体模式暂不执行需要确认的工具：{}；"
                                "请改用支持审批的交互渠道完成该操作。"
                            ).format(tool_name),
                        }
                    else:
                        _bind_agent_scope(tool_runtime, tenant, agent)
                        if tool_name == "knowledge_search":
                            tool_result = tool_runtime.execute(
                                tool_name,
                                tool_args,
                                ToolAuditContext(agent_id=agent.id),
                            )
                        else:
                            tool_result = tool_runtime.execute(
                                tool_name, tool_args
                            )
                        result_payload = tool_result.payload()
                except Exception as exc:
                    result_payload = {"ok": False, "error": str(exc)}

            messages.append(
                CanonicalMessage(
                    role="tool",
                    content=json.dumps(result_payload, ensure_ascii=False),
                    tool_call_id=call.call_id,
                )
            )

    return result("本次子任务达到工具调用轮次上限，请缩小问题范围后重试。")


def _orchestrate(
    user_message: str,
    agents: list,
    history: list,
    model_router,
    store,
    tenant_id: str,
    tenant: TenantContext,
    conv_id: str,
    skills: list,
    tool_runtime,
    analytics_store,
    context: ModelCallContext,
    knowledge_service=None,
    session_key: str = "default",
    user_id: Optional[int] = None,
    organization_store=None,
    allow_delegation: bool = False,
) -> Generator[str, None, None]:
    agents_by_id = {a.id: a for a in agents}
    try:
        if len(agents) <= 2:
            plan = [{"agent_id": a.id, "subtask": user_message} for a in agents]
        else:
            plan = _make_plan(user_message, agents, model_router, context)
            if plan is None:
                plan = [{"agent_id": a.id, "subtask": user_message} for a in agents]

        for item in plan:
            item["agent_name"] = agents_by_id[item["agent_id"]].name
        yield sse_plan(plan)

        for item in plan:
            yield sse_agent_start(item["agent_id"], item["agent_name"], item["subtask"])

        results: List[Optional[Dict[str, Any]]] = [None] * len(plan)
        with ThreadPoolExecutor(max_workers=min(len(plan), 4)) as executor:
            future_to_idx = {}
            for idx, item in enumerate(plan):
                agent = agents_by_id[item["agent_id"]]
                future = executor.submit(
                    _run_agent,
                    agent,
                    item["subtask"],
                    history,
                    model_router,
                    skills,
                    context,
                    tool_runtime,
                    tenant,
                    knowledge_service,
                    True,
                )
                future_to_idx[future] = idx

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                item = plan[idx]
                try:
                    output, knowledge_hits = future.result()
                    results[idx] = {
                        "agent_name": item["agent_name"],
                        "subtask": item["subtask"],
                        "output": output,
                        "status": "ok",
                        "knowledge_hits": knowledge_hits,
                    }
                    yield sse_agent_done(item["agent_id"], output, "ok")
                except Exception as exc:
                    error_text = "执行出错：{}".format(str(exc))
                    results[idx] = {
                        "agent_name": item["agent_name"],
                        "subtask": item["subtask"],
                        "output": error_text,
                        "status": "error",
                    }
                    yield sse_agent_done(item["agent_id"], error_text, "error")

        valid_results = [r for r in results if r and r["status"] == "ok"]
        if not valid_results:
            yield sse_error("所有智能体均执行失败")
            return

        yield sse_summary_start()
        merged_hits = []
        seen_sources = set()
        for result in valid_results:
            for hit in result.get("knowledge_hits", []):
                source_id = str(hit.get("source_id") or "")
                if not source_id or source_id in seen_sources:
                    continue
                seen_sources.add(source_id)
                normalized = dict(hit)
                normalized["citation"] = len(seen_sources)
                merged_hits.append(normalized)
        results_text = "\n\n".join(
            "【{}】子任务：{}\n结果：{}".format(
                r["agent_name"], r["subtask"], r["output"]
            )
            for r in valid_results
        )
        summary_prompt = SUMMARY_PROMPT.format(request=user_message, results=results_text)
        summary_messages = []
        if knowledge_service is not None and merged_hits:
            summary_messages.append(
                CanonicalMessage(
                    "system", knowledge_service.context_message(merged_hits)
                )
            )
        summary_messages.append(CanonicalMessage("user", summary_prompt))
        summary_client = model_router.clients[model_router.primary_profile_id]
        full_summary = ""
        if hasattr(summary_client, "complete_stream"):
            for chunk in summary_client.complete_stream(
                ModelRequest(
                    messages=summary_messages,
                    context=ModelCallContext(
                        **{
                            **context.__dict__,
                            "operation": "summary",
                            "agent_id": None,
                        }
                    ),
                )
            ):
                full_summary += chunk
                yield sse_token(chunk)
        else:
            resp = summary_client.complete(
                ModelRequest(
                    messages=summary_messages,
                    context=ModelCallContext(
                        **{
                            **context.__dict__,
                            "operation": "summary",
                            "agent_id": None,
                        }
                    ),
                )
            )
            full_summary = resp.message.content
            yield sse_token(full_summary)
        if knowledge_service is not None and merged_hits:
            cited_summary = knowledge_service.append_citations(
                full_summary, merged_hits
            )
            suffix = cited_summary[len(full_summary) :]
            if suffix:
                full_summary = cited_summary
                yield sse_token(suffix)
            yield sse_sources(
                knowledge_service.citation_sources(merged_hits)
            )
        if analytics_store is not None:
            analytics_store.finish_run(context.run_id or "", "success")
        yield sse_done(full_summary, context.run_id)

        updated = list(history)
        updated.append(CanonicalMessage("user", user_message))
        updated.append(CanonicalMessage("assistant", full_summary))
        store.save_context(
            tenant_id, updated, session_key=session_key
        )
        store.append_transcript(
            tenant_id, "user", user_message, session_key=session_key, user_id=user_id,
            actor_type="member", actor_account=str(user_id or "")
        )
        store.append_transcript(
            tenant_id,
            "assistant",
            full_summary,
            session_key=session_key,
            user_id=user_id,
            actor_type="agent",
            actor_account="multi_agent_summary",
        )
        if organization_store is not None and user_id is not None:
            organization_store.touch_conversation(
                user_id,
                conv_id,
                user_message,
                allow_delegation=allow_delegation,
            )
    except ModelError as exc:
        if analytics_store is not None:
            analytics_store.finish_run(
                context.run_id or "", "failed", error_category="model_error"
            )
        yield sse_error(exc.safe_message)
    except Exception as exc:
        if analytics_store is not None:
            analytics_store.finish_run(
                context.run_id or "", "failed", error_category=exc.__class__.__name__
            )
        yield sse_error("编排执行出错：{}".format(str(exc)))


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
def chat(
    body: ChatRequest,
    request: Request,
    principal=Depends(require_permission("panel.read")),
):
    config = get_config(request)
    model_router = get_router(request)
    store = get_conversation_store(request)

    conv_id = body.conversation_id
    organizations = get_organization_store(request)
    try:
        conversation = organizations.get_conversation(
            principal.user.user_id,
            conv_id or "",
            allow_delegation=principal.allows("admins.manage"),
        )
    except OrganizationError:
        raise HTTPException(status_code=400, detail="缺少有效的会话 ID")
    organization_id = str(conversation["organization_id"])
    if conversation.get("status") == "archived":
        raise HTTPException(status_code=409, detail="会话已归档，请先恢复后再继续")
    tenant = organizations.registry.get(organization_id)
    # Tools that mutate organization state need the acting member, not just
    # the tenant. member_user_id is compare=False, so bind_tenant's equality
    # check stays valid after this replace().
    tenant = replace(tenant, member_user_id=principal.user.user_id)
    tenant_id = tenant.tenant_id
    user_id = principal.user.user_id
    session_key = "organization:{}".format(conv_id)
    agents_by_id = _effective_agents(request, organization_id)
    effective_skills = _effective_skills(request, organization_id)
    analytics_store = get_model_analytics_store(request)
    selected_agent_id = body.agent_id or config.app.default_agent
    run_id = (
        analytics_store.start_run(
            tenant_id=tenant_id,
            user_id=user_id,
            source="web",
            agent_id=selected_agent_id,
            conversation_id=conv_id,
        )
        if analytics_store is not None
        else None
    )
    call_context = ModelCallContext(
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        source="web",
        operation="answer",
        agent_id=selected_agent_id,
        conversation_id=conv_id,
    )

    history = store.load_context(tenant_id, session_key=session_key)

    agent_ids = body.agent_ids or []
    if not body.regenerate and len(agent_ids) > 1:
        agents = []
        for aid in agent_ids:
            agent = agents_by_id.get(aid)
            if agent is not None and agent.enabled:
                agents.append(agent)
        if not agents:
            agents = [
                agents_by_id.get(config.app.default_agent)
                or next(iter(agents_by_id.values()))
            ]
        return streaming_response(
            _orchestrate(
                body.message,
                agents,
                history,
                model_router,
                store,
                tenant_id,
                tenant,
                conv_id,
                effective_skills,
                getattr(request.app.state, "tool_runtime", None),
                analytics_store,
                call_context,
                getattr(request.app.state, "knowledge_service", None),
                session_key,
                user_id,
                organizations,
                principal.allows("admins.manage"),
            )
        )

    agent = agents_by_id.get(body.agent_id or config.app.default_agent)
    if agent is None or not agent.enabled:
        agent = (
            agents_by_id.get(config.app.default_agent)
            or next(iter(agents_by_id.values()))
        )

    tool_runtime = getattr(request.app.state, "tool_runtime", None)
    system_prompt = build_system_prompt(agent, effective_skills, tool_runtime)

    if body.regenerate:
        if history and history[-1].role == "assistant":
            history = history[:-1]
        messages = [CanonicalMessage(role="system", content=system_prompt)]
        messages.extend(history)
    else:
        messages = [CanonicalMessage(role="system", content=system_prompt)]
        messages.extend(history)
        messages.append(CanonicalMessage(role="user", content=body.message))

    knowledge_sources = []
    knowledge_hits = []
    knowledge_service = getattr(request.app.state, "knowledge_service", None)
    if knowledge_service:
        try:
            knowledge_hits = knowledge_service.search(
                tenant_id, body.message, limit=6, agent_id=agent.id
            )
        except Exception:
            knowledge_hits = []
        if knowledge_hits:
            messages.insert(
                1,
                CanonicalMessage(
                    role="system",
                    content=knowledge_service.context_message(knowledge_hits),
                ),
            )
            knowledge_sources = knowledge_service.citation_sources(knowledge_hits)

    generation = GenerationOptions(
        temperature=getattr(agent, "temperature", None),
        max_tokens=getattr(agent, "max_tokens", None),
    )

    agent_tools = resolve_tool_names(agent, tool_runtime)
    tool_schemas = []
    if tool_runtime and agent_tools:
        try:
            _bind_agent_scope(tool_runtime, tenant, agent)
            tool_schemas = tool_runtime.schemas(agent_tools)
        except Exception:
            tool_schemas = []

    def generate() -> Generator[str, None, None]:
        full_text = ""
        try:
            if knowledge_sources:
                yield sse_sources(knowledge_sources)

            agent_model = getattr(agent, "model", None)
            start_profile = (
                agent_model
                if agent_model and agent_model in model_router.clients
                else None
            )
            session = model_router.session("auto", start_profile_id=start_profile)
            client = model_router.clients[session.profile_id]

            current_messages = list(messages)
            max_tool_rounds = 10

            def stream_final(msgs):
                nonlocal full_text
                req = ModelRequest(
                    messages=msgs,
                    generation=generation,
                    context=ModelCallContext(
                        **{**call_context.__dict__, "operation": "answer"}
                    ),
                )
                if hasattr(client, "complete_stream"):
                    for chunk in client.complete_stream(req):
                        full_text += chunk
                        # 硬拦截：模型输出文本格式工具调用（幻觉）时立即停止，
                        # 不把 <tool_calls> 等原文展示给用户。
                        if is_tool_call_text(full_text):
                            break
                        yield sse_token(chunk)
                    if is_tool_call_text(full_text):
                        # 已发出的 token 无法撤回，只发 done 让前端用全文覆盖显示。
                        full_text = sanitize_tool_call_text(full_text)
                        yield sse_done(full_text, run_id)
                        return
                    if knowledge_service is not None and knowledge_hits:
                        cited = knowledge_service.append_citations(
                            full_text, knowledge_hits
                        )
                        suffix = cited[len(full_text) :]
                        if suffix:
                            full_text = cited
                            yield sse_token(suffix)
                    yield sse_done(full_text, run_id)
                else:
                    resp = session.complete(req)
                    full_text = resp.message.content.strip()
                    if is_tool_call_text(full_text):
                        full_text = sanitize_tool_call_text(full_text)
                        yield sse_done(full_text, run_id)
                        return
                    if knowledge_service is not None and knowledge_hits:
                        full_text = knowledge_service.append_citations(
                            full_text, knowledge_hits
                        )
                    yield sse_token(full_text)
                    yield sse_done(full_text, run_id)

            if not tool_schemas:
                yield from stream_final(current_messages)
            else:
                tool_used = False
                loop_started = time.monotonic()
                for _round in range(max_tool_rounds):
                    if time.monotonic() - loop_started > 600:
                        full_text = "工具执行超过 10 分钟，已停止。请重试。"
                        yield sse_token(full_text)
                        yield sse_done(full_text)
                        return
                    current_request = ModelRequest(
                        messages=current_messages,
                        generation=generation,
                        tools=tool_schemas,
                        context=ModelCallContext(
                            **{**call_context.__dict__, "operation": "tool_loop"}
                        ),
                    )
                    response = session.complete(current_request)

                    thinking_content = ""
                    for field in ("thinking", "reasoning_content"):
                        val = response.message.extensions.get(field)
                        if val and str(val).strip():
                            thinking_content = str(val).strip()
                            break
                    if thinking_content:
                        # 思考草稿里常混有文本格式工具调用（幻觉），剥离后
                        # 为空则不再发送，避免把 <tool_calls> 原文展示给用户。
                        if is_tool_call_text(thinking_content):
                            thinking_content = strip_tool_call_text(thinking_content)
                        if thinking_content:
                            yield sse_thinking(thinking_content)

                    if not response.message.tool_calls:
                        # 硬拦截：模型应调用工具却输出文本格式调用（幻觉）时，
                        # 直接给出友好提示，不把 <tool_calls> 原文展示给用户。
                        answer = response.message.content.strip()
                        if is_tool_call_text(answer):
                            answer = sanitize_tool_call_text(answer)
                            yield sse_token(answer)
                            yield sse_done(answer, run_id)
                            return
                        if not tool_used:
                            yield from stream_final(current_messages)
                        else:
                            yield from stream_final(current_messages)
                        break

                    tool_used = True
                    # assistant(tool_calls) 消息每个 round 只追加一次；
                    # 若在循环内追加，多个 tool_calls 时会被重复加入，导致
                    # 后一条 assistant 缺少对应 tool 响应而触发 400。
                    current_messages.append(response.message)
                    for tc in response.message.tool_calls:
                        tool_name = tc.name or "unknown"
                        tool_args = tc.arguments if isinstance(tc.arguments, dict) else {}
                        yield sse_tool_call(tool_name, tool_args)

                        try:
                            # Web 聊天没有审批交互，需要确认的 git 写操作
                            # （commit/push/reset/branch -d 等）一律拒绝执行；
                            # clone/pull/fetch 只写沙箱，不在此列。
                            if tool_name == "git" and tool_runtime.requires_approval(
                                tool_name, tool_args
                            ):
                                raise ToolError(
                                    "该 git 操作需要人工确认，Web 聊天暂不支持审批；"
                                    "请改用支持审批的聊天渠道执行，"
                                    "或改用 clone/pull/fetch 等拉取类操作。"
                                )
                            _bind_agent_scope(tool_runtime, tenant, agent)
                            if tool_name == "knowledge_search":
                                result = tool_runtime.execute(
                                    tool_name,
                                    tool_args,
                                    ToolAuditContext(
                                        session_id=run_id or "",
                                        agent_id=agent.id,
                                    ),
                                )
                            elif tool_name == "git":
                                progress_queue: queue.Queue[Any] = queue.Queue()
                                holder: dict = {}

                                def _progress(percent: int, detail: str) -> None:
                                    try:
                                        progress_queue.put(
                                            {"percent": int(percent), "detail": str(detail)}
                                        )
                                    except Exception:
                                        pass

                                def _run_tool() -> None:
                                    try:
                                        # 租户绑定是线程局部的，新线程必须重新绑定
                                        _bind_agent_scope(tool_runtime, tenant, agent)
                                        holder["result"] = tool_runtime.execute(
                                            tool_name,
                                            tool_args,
                                            progress_callback=_progress,
                                        )
                                    except Exception as exc:
                                        holder["error"] = exc
                                    finally:
                                        progress_queue.put(None)

                                worker = threading.Thread(target=_run_tool, daemon=True)
                                worker.start()
                                while True:
                                    item = progress_queue.get()
                                    if item is None:
                                        break
                                    yield sse_tool_progress(
                                        tool_name, item["detail"], item["percent"]
                                    )
                                worker.join(timeout=2)
                                if "error" in holder:
                                    raise holder["error"]
                                result = holder["result"]
                            else:
                                result = tool_runtime.execute(
                                    tool_name, tool_args
                                )
                            result_payload = result.payload()
                        except Exception as exc:
                            result_payload = {"ok": False, "error": str(exc)}

                        yield sse_tool_result(tool_name, result_payload)

                        current_messages.append(CanonicalMessage(
                            role="tool",
                            content=json.dumps(result_payload, ensure_ascii=False),
                            tool_call_id=tc.call_id,
                        ))
                else:
                    full_text = "工具调用轮次已达上限。"
                    yield sse_token(full_text)
                    yield sse_done(full_text)

            updated = list(history)
            if not body.regenerate:
                updated.append(CanonicalMessage(role="user", content=body.message))
            updated.append(CanonicalMessage(role="assistant", content=full_text))
            store.save_context(
                tenant_id,
                updated,
                session_key=session_key,
            )
            if not body.regenerate:
                store.append_transcript(
                    tenant_id,
                    "user",
                    body.message,
                    session_key=session_key,
                    user_id=user_id,
                    actor_type="member",
                    actor_account=str(user_id),
                )
            store.append_transcript(
                tenant_id,
                "assistant",
                full_text,
                session_key=session_key,
                user_id=user_id,
                actor_type="agent",
                actor_account=agent.id,
            )
            organizations.touch_conversation(
                user_id,
                conv_id,
                body.message if not body.regenerate else None,
                allow_delegation=principal.allows("admins.manage"),
            )
            if analytics_store is not None:
                analytics_store.finish_run(run_id or "", "success")
        except GeneratorExit:
            if analytics_store is not None:
                analytics_store.finish_run(run_id or "", "cancelled")
            raise
        except ModelError as exc:
            if analytics_store is not None:
                analytics_store.finish_run(
                    run_id or "", "failed", error_category="model_error"
                )
            yield sse_error(exc.safe_message)
        except Exception as exc:
            if analytics_store is not None:
                analytics_store.finish_run(
                    run_id or "", "failed", error_category=exc.__class__.__name__
                )
            yield sse_error("对话出错：{}".format(str(exc)))

    return streaming_response(generate())


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
