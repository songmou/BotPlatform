"""Authenticated run submission, resumable event streams and task controls."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.api.deps import get_organization_store, require_permission
from src.api.schemas import ChatRequest
from src.api.sse import streaming_response
from src.core.storage.agent_runs import AgentRunError, TERMINAL, WAITING
from src.core.storage.organizations import OrganizationError

router = APIRouter(prefix="/api/agent-runs", tags=["agent-runs"])


class ResumeIn(BaseModel):
    user_input: str = Field(default="", max_length=100000)


class ApprovalIn(BaseModel):
    approval_id: str
    approved: bool


class ReconcileIn(BaseModel):
    step_key: str
    ok: bool
    data: Optional[Any] = None
    error: Optional[str] = None


def get_service(request: Request):
    service = request.app.state.agent_service
    # Tests and the resource activation callback may replace these bindings.
    service.tool_runtime = request.app.state.tool_runtime
    if service.runs is None:
        raise HTTPException(status_code=503, detail="智能体任务存储服务不可用")
    return service.runs


def public_run(run: dict) -> dict:
    state = run["state"]
    return {
        "run_id": run["run_id"],
        "organization_id": run["tenant_id"],
        "conversation_id": run["conversation_id"],
        "parent_run_id": run["parent_run_id"],
        "agent_id": run["agent_id"],
        "source": run["source"],
        "status": "stopping" if run["cancel_requested"] and run["status"] not in TERMINAL else run["status"],
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "answer": state.get("answer", ""),
        "reason": state.get("reason", ""),
        "reason_code": state.get("reason_code", ""),
        "plan": state.get("plan", []),
        "completion_criteria": state.get("completion_criteria", []),
        "rounds": state.get("rounds", 0),
        "tool_calls": state.get("total_calls", 0),
        "approval": state.get("approval"),
        "input_question": state.get("input_question") if run["status"] == "waiting_input" else None,
        "waiting_child_id": state.get("waiting_child_id"),
        "analytics_run_id": state.get("analytics_run_id"),
        "uncertain_step": (
            {
                "step_key": "{}:{}".format(state["rounds"], state["queue_index"]),
                "call": state["queue"][state["queue_index"]],
            }
            if state.get("reason_code") == "execution_unknown"
            and state.get("queue_index", 0) < len(state.get("queue", []))
            else None
        ),
    }


def scoped_run(request: Request, principal, run_id: str) -> dict:
    service = get_service(request)
    try:
        run = service.store.get(run_id)
        organizations = get_organization_store(request)
        session = run["conversation_id"]
        if session.startswith("organization:"):
            conv = organizations.get_conversation(
                principal.user.user_id,
                session[len("organization:") :],
                allow_delegation=principal.allows("admins.manage"),
            )
            if str(conv["organization_id"]) != run["tenant_id"]:
                raise AgentRunError("任务不存在或无权访问")
        elif principal.allows("admins.manage"):
            organizations.get(run["tenant_id"])
        else:
            organizations.membership(principal.user.user_id, run["tenant_id"])
        return run
    except (AgentRunError, OrganizationError):
        raise HTTPException(status_code=404, detail="任务不存在或无权访问")


def controlled_run(request: Request, principal, run_id: str) -> dict:
    run = scoped_run(request, principal, run_id)
    actor = run["state"]["tenant"].get("member_user_id")
    if actor == principal.user.user_id or principal.allows("admins.manage"):
        return run
    try:
        member = get_organization_store(request).membership(principal.user.user_id, run["tenant_id"])
        if member["role"] in {"owner", "admin"}:
            return run
    except OrganizationError:
        pass
    raise HTTPException(status_code=403, detail="只有任务发起人或组织管理员可以处理该任务")


def submit_web_run(body: ChatRequest, request: Request, principal) -> dict:
    organizations = get_organization_store(request)
    try:
        conversation = organizations.get_conversation(
            principal.user.user_id, body.conversation_id or "", allow_delegation=principal.allows("admins.manage")
        )
    except OrganizationError:
        raise HTTPException(status_code=400, detail="缺少有效的会话 ID")
    if conversation.get("status") == "archived":
        raise HTTPException(status_code=409, detail="会话已归档，请先恢复后再继续")
    tenant = replace(
        organizations.registry.get(str(conversation["organization_id"])), member_user_id=principal.user.user_id
    )
    service = get_service(request)
    try:
        run = service.submit(
            tenant,
            body.message,
            body.agent_id,
            "organization:" + conversation["id"],
            "web",
            agent_ids=body.agent_ids if not body.regenerate else None,
            allow_private_context=False,
            regenerate=body.regenerate,
        )
        organizations.touch_conversation(
            principal.user.user_id, conversation["id"], body.message, allow_delegation=principal.allows("admins.manage")
        )
        return run
    except AgentRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def stream_run(request: Request, run: dict, after: int = 0):
    service = get_service(request)
    run_id = run["run_id"]

    def generate():
        cursor = after
        last_ping = time.monotonic()
        # The worker, rather than this generator, owns task execution. Closing
        # a tab closes only this subscription.
        while True:
            events = service.store.events(run_id, cursor)
            for event in events:
                cursor = event["event_id"]
                yield "id: {}\ndata: {}\n\n".format(cursor, json.dumps(event, ensure_ascii=False))
            current = service.store.get(run_id, run["tenant_id"])
            if current["status"] in TERMINAL and not service.store.published(run_id):
                service._publish(current, current["status"])
            if current["status"] in TERMINAL or current["status"] in WAITING:
                # Drain all batches before closing, including a concurrently
                # checkpointed final status or approval event.
                if service.store.events(run_id, cursor):
                    continue
                if current["status"] in WAITING:
                    yield "data: {}\n\n".format(
                        json.dumps({"type": "run_state", **public_run(current)}, ensure_ascii=False)
                    )
                return
            if time.monotonic() - last_ping > 10:
                yield ": keep-alive\n\n"
                last_ping = time.monotonic()
            time.sleep(0.05)

    return streaming_response(generate())


@router.post("", status_code=202)
def create_run(body: ChatRequest, request: Request, principal=Depends(require_permission("panel.read"))):
    return public_run(submit_web_run(body, request, principal))


@router.get("")
def list_runs(
    request: Request,
    conversation_id: Optional[str] = Query(None),
    organization_id: Optional[str] = Query(None),
    principal=Depends(require_permission("panel.read")),
):
    organizations = get_organization_store(request)
    try:
        if conversation_id:
            conv = organizations.get_conversation(
                principal.user.user_id, conversation_id, allow_delegation=principal.allows("admins.manage")
            )
            organization_id = str(conv["organization_id"])
            session = "organization:" + conversation_id
        elif organization_id:
            if principal.allows("admins.manage"):
                organizations.get(organization_id)
            else:
                organizations.membership(principal.user.user_id, organization_id)
            session = None
        else:
            raise HTTPException(status_code=400, detail="请选择组织或对话")
    except OrganizationError:
        raise HTTPException(status_code=404, detail="组织或对话不存在或无权访问")
    return {"items": [public_run(run) for run in get_service(request).store.list(organization_id, session)]}


@router.get("/{run_id}")
def get_run(run_id: str, request: Request, principal=Depends(require_permission("panel.read"))):
    return public_run(scoped_run(request, principal, run_id))


@router.get("/{run_id}/events")
def events(
    run_id: str, request: Request, after: int = Query(0, ge=0), principal=Depends(require_permission("panel.read"))
):
    value = request.headers.get("last-event-id")
    if value is not None:
        try:
            after = max(after, int(value))
        except ValueError:
            raise HTTPException(status_code=400, detail="事件序号必须是非负整数")
    return stream_run(request, scoped_run(request, principal, run_id), after)


@router.post("/{run_id}/cancel")
def cancel(run_id: str, request: Request, principal=Depends(require_permission("panel.read"))):
    run = controlled_run(request, principal, run_id)
    return public_run(get_service(request).cancel(run_id, run["tenant_id"], principal.user.user_id))


@router.post("/{run_id}/resume")
def resume(run_id: str, body: ResumeIn, request: Request, principal=Depends(require_permission("panel.read"))):
    run = controlled_run(request, principal, run_id)
    try:
        return public_run(
            get_service(request).resume(run_id, run["tenant_id"], body.user_input, principal.user.user_id)
        )
    except AgentRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{run_id}/approvals")
def approve(run_id: str, body: ApprovalIn, request: Request, principal=Depends(require_permission("panel.read"))):
    run = controlled_run(request, principal, run_id)
    try:
        get_service(request).resolve_approval(
            run_id, run["tenant_id"], body.approval_id, body.approved, principal.user.user_id
        )
        return public_run(get_service(request).store.get(run_id, run["tenant_id"]))
    except AgentRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{run_id}/reconcile")
def reconcile(run_id: str, body: ReconcileIn, request: Request, principal=Depends(require_permission("panel.read"))):
    run = controlled_run(request, principal, run_id)
    if not principal.allows("admins.manage"):
        member = get_organization_store(request).membership(principal.user.user_id, run["tenant_id"])
        if member["role"] not in {"owner", "admin"}:
            raise HTTPException(status_code=403, detail="只有管理员可以核对执行结果")
    result = (
        {"ok": body.ok, "data": body.data} if body.ok else {"ok": False, "error": body.error or "管理员确认操作未完成"}
    )
    try:
        get_service(request).store.reconcile(run_id, run["tenant_id"], body.step_key, result)
        get_service(request)._release_workflow_wait(run_id, principal.user.user_id)
        return public_run(get_service(request).store.get(run_id, run["tenant_id"]))
    except AgentRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
