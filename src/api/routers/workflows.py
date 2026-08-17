"""Organization workflow authoring, execution and public trigger APIs."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from src.api.deps import (
    get_organization_store,
    get_principal,
    get_resource_store,
    require_permission,
)
from src.core.services.authorization import AuthorizationError, AuthorizationService
from src.core.services.credentials import CredentialError
from src.core.services.resources import ResourceError
from src.core.tooling.definitions import TOOL_DEFINITIONS
from src.core.workflows import (
    NODE_CATALOG,
    WorkflowError,
    WorkflowValidationError,
    validate_definition,
    validate_field_values,
)
from src.core.workflows.store import redact_workflow_value


router = APIRouter(prefix="/api/v2", tags=["workflows"])
public_router = APIRouter(prefix="/api/workflows/v1", tags=["workflow-runtime"])


def _organization_run_response(run: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(run)
    result["input"] = redact_workflow_value(result.get("input") or {})
    return result


def _public_run_response(run: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "run_id", "workflow_id", "workflow_version", "status", "output", "error",
        "created_at", "started_at", "finished_at",
    )
    return {key: run.get(key) for key in keys}


def _service(request: Request):
    service = getattr(request.app.state, "workflow_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="工作流服务不可用")
    return service


def _context(request: Request, principal: Any, organization_id: str, minimum_role: Optional[str] = None):
    try:
        return AuthorizationService(get_organization_store(request)).organization_context(
            principal,
            organization_id,
            minimum_role=minimum_role,
            request_id=request.headers.get("x-request-id", ""),
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _error(exc: Exception) -> HTTPException:
    text = str(exc)
    if "其他成员更新" in text:
        return HTTPException(status_code=409, detail=text)
    if "不存在" in text:
        return HTTPException(status_code=404, detail=text)
    return HTTPException(status_code=400, detail=text)


def _can_handle_wait(context: Any, wait: Dict[str, Any]) -> bool:
    """Return whether one immutable execution context may see and resolve a wait."""
    if context.platform_delegation:
        return True
    if str(wait.get("wait_type") or "") == "delay":
        return True
    if str(wait.get("wait_type") or "") == "attention":
        return context.role in {"owner", "admin"}
    assignees = wait.get("assignees") or {}
    roles = {str(value) for value in assignees.get("roles") or []}
    users = set()
    for value in assignees.get("user_ids") or []:
        try:
            users.add(int(value))
        except (TypeError, ValueError):
            continue
    if not roles and not users:
        return True
    return context.role in roles or context.user_id in users


@router.get("/workflow-node-catalog")
def node_catalog(request: Request, _principal=Depends(get_principal)):
    return {
        "items": [{"type": key, **value} for key, value in NODE_CATALOG.items()],
        "limits": {"max_steps": 500, "max_for_each_items": 100, "max_subworkflow_depth": 5},
        "timezone": request.app.state.config.app.timezone,
    }


@router.get("/orgs/{organization_id}/workflow-editor-options")
def workflow_editor_options(
    organization_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id)
    service = _service(request)
    resources = get_resource_store(request)
    agents = resources.list_effective(organization_id, "agents")
    workflows = service.store.list_workflows(organization_id)
    scripts = (
        request.app.state.script_service.list_scripts()
        if getattr(request.app.state, "script_service", None) is not None
        else []
    )
    datasources = (
        request.app.state.datasource_service.list_configs()
        if getattr(request.app.state, "datasource_service", None) is not None
        else []
    )
    knowledge = (
        request.app.state.knowledge_service.list_categories(tenant_id=organization_id)
        if getattr(request.app.state, "knowledge_service", None) is not None
        else []
    )
    credentials = request.app.state.credential_service.list_for_user(
        organization_id,
        context.user_id,
        allow_platform_delegation=context.platform_delegation,
    )
    tool_runtime = getattr(request.app.state, "tool_runtime", None)
    model_router = getattr(request.app.state, "model_router", None)
    tool_names = set(TOOL_DEFINITIONS)
    if tool_runtime is not None:
        tool_names.update(getattr(tool_runtime, "_plugin_tools", {}))
        manager = getattr(tool_runtime, "plugin_manager", None)
        tool_names.update(getattr(manager, "tool_names", []) if manager else [])
    return {
        "agents": [
            {"value": item["resource_id"], "label": (item.get("payload") or {}).get("name") or item["resource_id"]}
            for item in agents
        ],
        "models": [
            {"value": profile_id, "label": profile_id}
            for profile_id in sorted(getattr(model_router, "clients", {}))
        ],
        "workflows": [
            {"value": item["workflow_id"], "label": item["name"]}
            for item in workflows if item.get("status") == "published"
        ],
        "scripts": [
            {"value": item["id"], "label": item.get("name") or item["id"]}
            for item in scripts if item.get("enabled", True)
        ],
        "datasources": [
            {"value": item["id"], "label": item.get("name") or item["id"]}
            for item in datasources if item.get("enabled", True)
        ],
        "knowledge": [
            {"value": item["category_id"], "label": item.get("name") or item["category_id"]}
            for item in knowledge
        ],
        "credentials": [
            {"value": item["resource_id"], "label": item.get("label") or item["credential_id"]}
            for item in credentials
            if item.get("resource_type") == "workflow_http" and item.get("configured")
        ],
        "tools": [
            {"value": name, "label": name}
            for name in sorted(tool_names)
            if tool_runtime is None or tool_runtime.is_tool_enabled(name)
        ],
    }


def _platform_template_item(store: Any, row: Any) -> Dict[str, Any]:
    revision = row["draft_revision"] or row["published_revision"]
    if revision is None:
        raise WorkflowError("平台工作流模板没有可用版本")
    with store.database.read() as connection:
        version = connection.execute(
            "SELECT revision, lifecycle, payload_json, created_at, published_at "
            "FROM platform_resource_versions WHERE resource_pk=? AND revision=?",
            (row["resource_pk"], revision),
        ).fetchone()
        versions = connection.execute(
            "SELECT revision, lifecycle, created_at, published_at FROM platform_resource_versions "
            "WHERE resource_pk=? ORDER BY revision DESC",
            (row["resource_pk"],),
        ).fetchall()
    if version is None:
        raise WorkflowError("平台工作流模板版本不存在")
    is_draft = row["draft_revision"] is not None and int(revision) == int(row["draft_revision"])
    payload = validate_definition(
        json.loads(str(version["payload_json"])), allow_incomplete=is_draft
    )
    return {
        "resource_id": str(row["resource_id"]),
        "name": payload["name"],
        "description": payload["description"],
        "status": "draft" if row["draft_revision"] is not None else str(version["lifecycle"]),
        "draft_revision": int(row["draft_revision"]) if row["draft_revision"] is not None else None,
        "published_version": int(row["published_revision"]) if row["published_revision"] is not None else None,
        "payload": payload,
        "versions": [dict(item) for item in versions],
    }


@router.get("/platform/workflow-templates")
def list_platform_workflow_templates(
    request: Request,
    _principal=Depends(require_permission("panel.read")),
):
    store = get_resource_store(request)
    with store.database.read() as connection:
        rows = connection.execute(
            "SELECT * FROM platform_resources WHERE resource_type='workflows' ORDER BY updated_at DESC"
        ).fetchall()
    return {"items": [_platform_template_item(store, row) for row in rows]}


@router.get("/platform/workflow-templates/{template_id}")
def get_platform_workflow_template(
    template_id: str,
    request: Request,
    _principal=Depends(require_permission("panel.read")),
):
    store = get_resource_store(request)
    with store.database.read() as connection:
        row = connection.execute(
            "SELECT * FROM platform_resources WHERE resource_type='workflows' AND resource_id=?",
            (template_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="平台工作流模板不存在")
    return _platform_template_item(store, row)


@router.put("/platform/workflow-templates/{template_id}/draft")
def save_platform_workflow_template(
    template_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(require_permission("panel.write")),
):
    try:
        definition = validate_definition(body.get("definition") or body, allow_incomplete=True)
        get_resource_store(request).save_draft(
            "workflows", template_id, definition, principal.user.user_id
        )
        return get_platform_workflow_template(template_id, request, principal)
    except (ResourceError, WorkflowValidationError, WorkflowError) as exc:
        raise _error(exc) from exc


@router.post("/platform/workflow-templates/{template_id}/publish")
def publish_platform_workflow_template(
    template_id: str,
    request: Request,
    principal=Depends(require_permission("panel.write")),
):
    try:
        current = get_platform_workflow_template(template_id, request, principal)
        validate_definition(current["payload"])
        get_resource_store(request).publish(
            "workflows", template_id, principal.user.user_id
        )
        return get_platform_workflow_template(template_id, request, principal)
    except (ResourceError, WorkflowValidationError, WorkflowError) as exc:
        raise _error(exc) from exc


@router.post("/platform/workflow-templates/{template_id}/validate")
def validate_platform_workflow_template(
    template_id: str,
    request: Request,
    body: Dict[str, Any] = Body(default={}),
    principal=Depends(require_permission("panel.write")),
):
    try:
        definition = body.get("definition")
        if definition is None:
            definition = get_platform_workflow_template(template_id, request, principal)["payload"]
        return {"valid": True, "definition": validate_definition(definition), "warnings": []}
    except (ResourceError, WorkflowValidationError, WorkflowError) as exc:
        raise _error(exc) from exc


@router.post("/platform/workflow-templates/{template_id}/rollback")
def rollback_platform_workflow_template(
    template_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(require_permission("panel.write")),
):
    try:
        get_resource_store(request).rollback(
            "workflows", template_id, int(body.get("version")), principal.user.user_id
        )
        return get_platform_workflow_template(template_id, request, principal)
    except (ResourceError, TypeError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/platform/workflow-templates/{template_id}/design-suggestions")
def platform_design_suggestion(
    template_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(require_permission("panel.write")),
):
    try:
        current = body.get("definition") or get_platform_workflow_template(
            template_id, request, principal
        )["payload"]
        return _service(request).design_suggestion(
            "", str(body.get("instruction") or ""), current, principal.user.user_id
        )
    except (WorkflowError, WorkflowValidationError) as exc:
        raise _error(exc) from exc


@router.get("/orgs/{organization_id}/workflows")
def list_workflows(organization_id: str, request: Request, principal=Depends(get_principal)):
    _context(request, principal, organization_id)
    return {"items": _service(request).store.list_workflows(organization_id)}


@router.post("/orgs/{organization_id}/workflows", status_code=201)
def create_workflow(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id)
    try:
        return _service(request).store.create_workflow(
            organization_id,
            str(body.get("id") or ""),
            str(body.get("name") or "新工作流"),
            context.user_id,
            body.get("definition"),
        )
    except (WorkflowError, WorkflowValidationError) as exc:
        raise _error(exc) from exc


@router.get("/orgs/{organization_id}/workflows/{workflow_id}")
def get_workflow(organization_id: str, workflow_id: str, request: Request, principal=Depends(get_principal)):
    _context(request, principal, organization_id)
    try:
        item = _service(request).store.get_workflow(organization_id, workflow_id)
        item["versions"] = _service(request).store.list_versions(organization_id, workflow_id)
        item["trigger_bindings"] = _service(request).store.list_trigger_bindings(organization_id, workflow_id)
        return item
    except (WorkflowError, WorkflowValidationError, TypeError, ValueError) as exc:
        raise _error(exc) from exc


@router.get("/orgs/{organization_id}/workflows/{workflow_id}/draft")
def get_workflow_draft(organization_id: str, workflow_id: str, request: Request, principal=Depends(get_principal)):
    _context(request, principal, organization_id)
    try:
        return _service(request).store.get_workflow(organization_id, workflow_id)
    except (WorkflowError, WorkflowValidationError, TypeError, ValueError) as exc:
        raise _error(exc) from exc


@router.put("/orgs/{organization_id}/workflows/{workflow_id}/draft")
def save_workflow_draft(
    organization_id: str,
    workflow_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id)
    try:
        return _service(request).store.save_draft(
            organization_id,
            workflow_id,
            body.get("definition") or {},
            int(body.get("base_revision", 0)),
            context.user_id,
        )
    except (WorkflowError, WorkflowValidationError, TypeError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/workflows/{workflow_id}/validate")
def validate_workflow(
    organization_id: str,
    workflow_id: str,
    request: Request,
    body: Dict[str, Any] = Body(default={}),
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    try:
        definition = body.get("definition")
        if definition is None:
            definition = _service(request).store.get_workflow(organization_id, workflow_id)["definition"]
        normalized = _service(request).validate_resources(organization_id, definition)
        return {"valid": True, "definition": normalized, "warnings": []}
    except (WorkflowError, WorkflowValidationError) as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/workflows/{workflow_id}/publish")
def publish_workflow(organization_id: str, workflow_id: str, request: Request, principal=Depends(get_principal)):
    context = _context(request, principal, organization_id)
    try:
        service = _service(request)
        current = service.store.get_workflow(organization_id, workflow_id)
        service.validate_resources(organization_id, current["definition"])
        result = service.store.publish(organization_id, workflow_id, context.user_id)
        _service(request).wake()
        return result
    except (WorkflowError, WorkflowValidationError) as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/workflows/{workflow_id}/unpublish")
def unpublish_workflow(organization_id: str, workflow_id: str, request: Request, principal=Depends(get_principal)):
    context = _context(request, principal, organization_id)
    try:
        return _service(request).store.unpublish(organization_id, workflow_id, context.user_id)
    except WorkflowError as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/workflows/{workflow_id}/rollback")
def rollback_workflow(
    organization_id: str,
    workflow_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id)
    try:
        service = _service(request)
        version = int(body.get("version"))
        snapshot = service.store.get_version(organization_id, workflow_id, version)
        service.validate_resources(organization_id, snapshot["definition"])
        return service.store.rollback(organization_id, workflow_id, version, context.user_id)
    except (WorkflowError, WorkflowValidationError, ValueError, TypeError) as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/workflows/{workflow_id}/archive")
def archive_workflow(organization_id: str, workflow_id: str, request: Request, principal=Depends(get_principal)):
    context = _context(request, principal, organization_id)
    try:
        return _service(request).store.archive(organization_id, workflow_id, context.user_id)
    except WorkflowError as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/workflow-templates/{template_id}/copy", status_code=201)
def copy_template(
    organization_id: str,
    template_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id)
    try:
        return _service(request).store.copy_platform_template(
            organization_id,
            template_id,
            str(body.get("id") or ""),
            context.user_id,
            str(body.get("name") or ""),
        )
    except (WorkflowError, WorkflowValidationError) as exc:
        raise _error(exc) from exc


def _enqueue_run(
    organization_id: str,
    workflow_id: str,
    request: Request,
    body: Dict[str, Any],
    context: Any,
    test_mode: bool,
):
    service = _service(request)
    wait = bool(body.get("wait", False))
    timeout = float(body.get("timeout", 30)) if wait else 30.0
    if test_mode:
        current = service.store.get_workflow(organization_id, workflow_id)
        service.validate_resources(organization_id, current["definition"])
    run = service.enqueue(
        organization_id,
        workflow_id,
        body.get("inputs") or {},
        "test" if test_mode else "manual",
        "web",
        context.user_id,
        test_mode=test_mode,
        allow_side_effects=bool(body.get("allow_side_effects", False)),
    )
    if wait:
        return _organization_run_response(
            service.run_synchronously(organization_id, run["run_id"], timeout)
        )
    return _organization_run_response(run)


@router.post("/orgs/{organization_id}/workflows/{workflow_id}/test", status_code=202)
def test_workflow(
    organization_id: str,
    workflow_id: str,
    request: Request,
    body: Dict[str, Any] = Body(default={}),
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id)
    try:
        return _enqueue_run(organization_id, workflow_id, request, body, context, True)
    except (WorkflowError, WorkflowValidationError, TypeError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/workflows/{workflow_id}/runs", status_code=202)
def run_workflow(
    organization_id: str,
    workflow_id: str,
    request: Request,
    body: Dict[str, Any] = Body(default={}),
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id)
    try:
        return _enqueue_run(organization_id, workflow_id, request, body, context, False)
    except (WorkflowError, WorkflowValidationError, TypeError, ValueError) as exc:
        raise _error(exc) from exc


@router.get("/orgs/{organization_id}/workflow-runs")
def list_workflow_runs(
    organization_id: str,
    request: Request,
    workflow_id: str = "",
    status: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    return {"items": [
        _organization_run_response(item)
        for item in _service(request).store.list_runs(
            organization_id, workflow_id, status, limit, offset
        )
    ]}


@router.get("/orgs/{organization_id}/workflows/{workflow_id}/runs")
def list_workflow_runs_for_workflow(
    organization_id: str,
    workflow_id: str,
    request: Request,
    status: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    return {"items": [
        _organization_run_response(item)
        for item in _service(request).store.list_runs(
            organization_id, workflow_id, status, limit, offset
        )
    ]}


@router.get("/orgs/{organization_id}/workflow-runs/{run_id}")
@router.get("/orgs/{organization_id}/workflows/{workflow_id}/runs/{run_id}")
def get_workflow_run(
    organization_id: str,
    run_id: str,
    request: Request,
    workflow_id: str = "",
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    try:
        run = _service(request).store.get_run(organization_id, run_id)
        if workflow_id and run["workflow_id"] != workflow_id:
            raise WorkflowError("工作流运行记录不存在")
        return _organization_run_response(run)
    except (WorkflowError, WorkflowValidationError, TypeError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/workflow-runs/{run_id}/cancel")
@router.post("/orgs/{organization_id}/workflows/{workflow_id}/runs/{run_id}/cancel")
def cancel_workflow_run(
    organization_id: str,
    run_id: str,
    request: Request,
    workflow_id: str = "",
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    try:
        if (
            workflow_id
            and _service(request).store.get_run(organization_id, run_id)["workflow_id"] != workflow_id
        ):
            raise WorkflowError("工作流运行记录不存在")
        return _organization_run_response(
            _service(request).store.cancel_run(organization_id, run_id)
        )
    except (WorkflowError, WorkflowValidationError, TypeError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/workflow-runs/{run_id}/attention")
def resolve_workflow_attention(
    organization_id: str,
    run_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id, "admin")
    try:
        result = _service(request).store.resolve_attention(
            organization_id,
            run_id,
            str(body.get("action") or ""),
            context.user_id,
            str(body.get("comment") or ""),
        )
        _service(request).wake()
        return _organization_run_response(result)
    except WorkflowError as exc:
        raise _error(exc) from exc


@router.get("/orgs/{organization_id}/workflow-runs/{run_id}/events")
def workflow_run_events(
    organization_id: str,
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id)
    try:
        return {"items": _service(request).store.list_events(organization_id, run_id, after)}
    except WorkflowError as exc:
        raise _error(exc) from exc


@router.get("/orgs/{organization_id}/workflow-runs/{run_id}/stream")
def workflow_run_stream(organization_id: str, run_id: str, request: Request, principal=Depends(get_principal)):
    _context(request, principal, organization_id)
    store = _service(request).store

    async def events():
        cursor = 0
        while True:
            items = store.list_events(organization_id, run_id, cursor)
            for item in items:
                cursor = int(item["event_id"])
                yield "id: {}\nevent: workflow\ndata: {}\n\n".format(cursor, json.dumps(item, ensure_ascii=False))
            run = store.get_run(organization_id, run_id)
            if run["status"] not in {"queued", "running", "waiting"}:
                yield "event: done\ndata: {}\n\n".format(json.dumps({"status": run["status"]}, ensure_ascii=False))
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(events(), media_type="text/event-stream")


@router.get("/orgs/{organization_id}/workflow-waits")
def list_workflow_waits(
    organization_id: str,
    request: Request,
    status: str = "pending",
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id)
    items = _service(request).store.list_waits(organization_id, status)
    return {"items": [item for item in items if _can_handle_wait(context, item)]}


@router.post("/orgs/{organization_id}/workflow-waits/{wait_id}/resolve")
def resolve_workflow_wait(
    organization_id: str,
    wait_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id)
    try:
        wait = _service(request).store.get_wait(organization_id, wait_id)
        wait_type = str(wait.get("wait_type") or "")
        if wait_type == "delay":
            raise HTTPException(status_code=400, detail="延迟等待由系统自动恢复，不能手动处理")
        if wait_type == "attention":
            raise HTTPException(status_code=400, detail="异常运行必须通过运行处置接口处理")
        if not _can_handle_wait(context, wait):
            raise HTTPException(status_code=403, detail="当前成员不在该工作流待办的处理范围内")
        status = str(body.get("status") or "resolved")
        if wait_type == "approval" and status not in {"approved", "rejected"}:
            raise HTTPException(status_code=400, detail="审批待办只能选择通过或拒绝")
        if wait_type == "input" and status != "resolved":
            raise HTTPException(status_code=400, detail="补充输入待办必须提交 resolved 状态")
        if wait_type == "input":
            response = validate_field_values(
                (wait.get("payload") or {}).get("fields") or [],
                body.get("response"),
                subject="补充输入",
            )
            body = {**body, "response": response}
        result = _service(request).store.resolve_wait(organization_id, wait_id, body, context.user_id)
        _service(request).wake()
        return result
    except WorkflowError as exc:
        raise _error(exc) from exc
    except WorkflowValidationError as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/workflows/{workflow_id}/design-suggestions")
def design_suggestion(
    organization_id: str,
    workflow_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id)
    try:
        current = body.get("definition") or _service(request).store.get_workflow(
            organization_id, workflow_id
        )["definition"]
        return _service(request).design_suggestion(
            organization_id, str(body.get("instruction") or ""), current, context.user_id
        )
    except (WorkflowError, WorkflowValidationError) as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/workflows/{workflow_id}/access-tokens", status_code=201)
def create_access_token(
    organization_id: str,
    workflow_id: str,
    request: Request,
    body: Dict[str, Any] = Body(default={}),
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id, "admin")
    try:
        return _service(request).store.issue_access_token(
            organization_id, workflow_id, str(body.get("label") or ""), context.user_id
        )
    except WorkflowError as exc:
        raise _error(exc) from exc


@router.get("/orgs/{organization_id}/workflows/{workflow_id}/access-tokens")
def list_access_tokens(organization_id: str, workflow_id: str, request: Request, principal=Depends(get_principal)):
    _context(request, principal, organization_id, "admin")
    try:
        return {"items": _service(request).store.list_access_tokens(organization_id, workflow_id)}
    except WorkflowError as exc:
        raise _error(exc) from exc


@router.delete("/orgs/{organization_id}/workflows/{workflow_id}/access-tokens/{token_id}")
def delete_access_token(
    organization_id: str,
    workflow_id: str,
    token_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id, "admin")
    try:
        _service(request).store.revoke_access_token(organization_id, workflow_id, token_id)
        return {"revoked": True}
    except WorkflowError as exc:
        raise _error(exc) from exc


@router.post("/orgs/{organization_id}/workflows/{workflow_id}/webhook-triggers/{trigger_id}/secret", status_code=201)
def create_webhook_secret(
    organization_id: str,
    workflow_id: str,
    trigger_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id, "admin")
    try:
        return _service(request).store.issue_webhook_secret(organization_id, workflow_id, trigger_id)
    except WorkflowError as exc:
        raise _error(exc) from exc


@router.delete("/orgs/{organization_id}/workflows/{workflow_id}/webhook-triggers/{trigger_id}/secret")
def delete_webhook_secret(
    organization_id: str,
    workflow_id: str,
    trigger_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _context(request, principal, organization_id, "admin")
    try:
        _service(request).store.revoke_webhook_secret(organization_id, workflow_id, trigger_id)
        return {"revoked": True}
    except WorkflowError as exc:
        raise _error(exc) from exc


@router.put("/orgs/{organization_id}/workflow-http-credentials/{credential_id}")
def put_http_credential(
    organization_id: str,
    credential_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _context(request, principal, organization_id, "admin")
    try:
        return request.app.state.credential_service.put(
            organization_id,
            credential_id,
            actor_user_id=context.user_id,
            scope="organization",
            resource_type="workflow_http",
            resource_id=credential_id,
            label=str(body.get("label") or ""),
            secret=str(body.get("secret") or ""),
            allow_platform_delegation=context.platform_delegation,
        )
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少工作流访问令牌")
    return authorization[7:].strip()


@public_router.post("/{workflow_id}/run", status_code=202)
def public_run(
    workflow_id: str,
    request: Request,
    body: Dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None),
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    service = _service(request)
    auth = service.store.authenticate_token(workflow_id, _bearer(authorization))
    if auth is None:
        raise HTTPException(status_code=401, detail="工作流访问令牌无效或已撤销")
    try:
        wait = bool(body.get("wait", False))
        timeout = float(body.get("timeout", 30)) if wait else 30.0
        inputs = (
            body["inputs"]
            if "inputs" in body
            else {key: value for key, value in body.items() if key not in {"wait", "timeout"}}
        )
        run = service.enqueue(
            auth["organization_id"],
            workflow_id,
            inputs,
            "api",
            auth["trigger_id"],
            None,
            idempotency_key=idempotency_key,
        )
        if wait:
            return _public_run_response(
                service.run_synchronously(auth["organization_id"], run["run_id"], timeout)
            )
        return _public_run_response(run)
    except (WorkflowError, WorkflowValidationError, TypeError, ValueError) as exc:
        raise _error(exc) from exc


@public_router.get("/runs/{run_id}")
def public_run_status(run_id: str, request: Request, authorization: Optional[str] = Header(default=None)):
    service = _service(request)
    auth = service.store.authenticate_run_token(run_id, _bearer(authorization))
    if auth is None:
        raise HTTPException(status_code=401, detail="工作流访问令牌无效或已撤销")
    try:
        run = service.store.get_run(auth["organization_id"], run_id)
        return _public_run_response(run)
    except WorkflowError as exc:
        raise _error(exc) from exc


@public_router.post("/hooks/{trigger_id}", status_code=202)
def public_webhook(
    trigger_id: str,
    request: Request,
    body: Dict[str, Any] = Body(default={}),
    authorization: Optional[str] = Header(default=None),
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
):
    service = _service(request)
    trigger = service.store.authenticate_webhook(trigger_id, _bearer(authorization))
    if trigger is None:
        raise HTTPException(status_code=401, detail="Webhook 令牌无效或触发器未启用")
    try:
        return _public_run_response(service.enqueue(
            trigger["organization_id"],
            trigger["workflow_id"],
            body,
            "webhook",
            trigger_id,
            None,
            idempotency_key=idempotency_key,
            version_override=int(trigger["published_version"]),
        ))
    except (WorkflowError, WorkflowValidationError, TypeError, ValueError) as exc:
        raise _error(exc) from exc
