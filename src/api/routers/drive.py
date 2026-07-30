"""Network drive endpoints: browse, upload, download and audit."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse

from src.api.deps import (
    get_drive_audit_store,
    get_drive_service,
    get_registry,
    require_permission,
)
from src.api.schemas import DriveEntryActionIn, DriveFolderIn
from src.core.services.drive import MAX_PREVIEW_BYTES, MAX_UPLOAD_BYTES

router = APIRouter(prefix="/api/drive", tags=["drive"])


def _drive_service(request: Request):
    service = get_drive_service(request)
    if service is None:
        raise HTTPException(status_code=503, detail="网盘服务不可用")
    return service


def _operator(principal) -> str:
    return "web:{}".format(principal.user.username)


def _record(
    request: Request,
    principal,
    scope: str,
    tenant_id,
    action: str,
    path: str,
    target_path=None,
    size_bytes: int = 0,
    status: str = "成功",
    error=None,
) -> None:
    store = get_drive_audit_store(request)
    if store is None:
        return
    store.record(
        operator=_operator(principal),
        source="web",
        scope=scope,
        tenant_id=tenant_id,
        action=action,
        path=path,
        target_path=target_path,
        size_bytes=size_bytes,
        status=status,
        error=error,
    )


def _bad_request(exc: ValueError) -> HTTPException:
    message = str(exc)
    # Tenant lookup failures surface as 404 per API convention.
    if "未找到租户" in message or "租户编号格式无效" in message:
        return HTTPException(status_code=404, detail="租户不存在")
    return HTTPException(status_code=400, detail=message)


@router.get("/tenants")
def list_drive_tenants(
    request: Request, principal=Depends(require_permission("drive.read"))
):
    registry = get_registry(request)
    return [
        {
            "tenant_id": item["tenant_id"],
            "bot_id": item["bot_id"],
            "user_id": item["user_id"],
        }
        for item in registry.list_overviews()
    ]


@router.get("/entries")
def list_drive_entries(
    request: Request,
    scope: str = Query(min_length=1),
    tenant_id: str = Query(default=""),
    path: str = Query(default=""),
    principal=Depends(require_permission("drive.read")),
):
    service = _drive_service(request)
    try:
        return service.list_entries(scope, tenant_id or None, path)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/usage")
def drive_usage(
    request: Request,
    scope: str = Query(min_length=1),
    tenant_id: str = Query(default=""),
    principal=Depends(require_permission("drive.read")),
):
    service = _drive_service(request)
    try:
        return service.usage(scope, tenant_id or None)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/folders")
def create_drive_folder(
    body: DriveFolderIn,
    request: Request,
    principal=Depends(require_permission("drive.manage")),
):
    service = _drive_service(request)
    tenant_id = body.tenant_id or None
    try:
        result = service.create_folder(body.scope, tenant_id, body.path, body.name)
    except ValueError as exc:
        _record(
            request,
            principal,
            body.scope,
            tenant_id,
            "mkdir",
            "{}/{}".format(body.path, body.name).strip("/"),
            status="失败",
            error=str(exc),
        )
        raise _bad_request(exc) from exc
    _record(request, principal, body.scope, tenant_id, "mkdir", result["path"])
    return result


@router.post("/upload")
def upload_drive_file(
    request: Request,
    scope: str = Form(min_length=1),
    tenant_id: str = Form(default=""),
    path: str = Form(default=""),
    overwrite: bool = Form(default=False),
    file: UploadFile = File(...),
    principal=Depends(require_permission("drive.manage")),
):
    service = _drive_service(request)
    tenant = tenant_id or None
    filename = (file.filename or "").strip()
    payload = file.file.read(MAX_UPLOAD_BYTES + 1)
    if not payload:
        raise HTTPException(status_code=400, detail="上传的文件内容为空")
    try:
        result = service.save_file(scope, tenant, path, filename, payload, overwrite)
    except ValueError as exc:
        _record(
            request,
            principal,
            scope,
            tenant,
            "upload",
            "{}/{}".format(path, filename).strip("/"),
            size_bytes=len(payload),
            status="失败",
            error=str(exc),
        )
        raise _bad_request(exc) from exc
    _record(
        request,
        principal,
        scope,
        tenant,
        "upload",
        result["path"],
        size_bytes=result["size"],
    )
    return result


@router.get("/download")
def download_drive_file(
    request: Request,
    scope: str = Query(min_length=1),
    tenant_id: str = Query(default=""),
    path: str = Query(min_length=1),
    principal=Depends(require_permission("drive.read")),
):
    service = _drive_service(request)
    tenant = tenant_id or None
    try:
        real_path = service.read_file(scope, tenant, path)
    except ValueError as exc:
        _record(
            request,
            principal,
            scope,
            tenant,
            "download",
            path,
            status="失败",
            error=str(exc),
        )
        raise _bad_request(exc) from exc
    _record(
        request,
        principal,
        scope,
        tenant,
        "download",
        path,
        size_bytes=real_path.stat().st_size,
    )
    return FileResponse(
        real_path,
        filename=real_path.name,
        media_type="application/octet-stream",
    )


@router.get("/preview")
def preview_drive_file(
    request: Request,
    scope: str = Query(min_length=1),
    tenant_id: str = Query(default=""),
    path: str = Query(min_length=1),
    max_bytes: int = Query(default=MAX_PREVIEW_BYTES, ge=1),
    principal=Depends(require_permission("drive.read")),
):
    service = _drive_service(request)
    try:
        return service.read_text(scope, tenant_id or None, path, max_bytes)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.put("/entries")
def update_drive_entry(
    body: DriveEntryActionIn,
    request: Request,
    principal=Depends(require_permission("drive.manage")),
):
    service = _drive_service(request)
    tenant_id = body.tenant_id or None
    if body.action not in ("rename", "move"):
        raise HTTPException(status_code=400, detail="action 仅支持 rename 或 move")
    try:
        if body.action == "rename":
            result = service.rename(body.scope, tenant_id, body.path, body.target)
        else:
            result = service.move(body.scope, tenant_id, body.path, body.target)
    except ValueError as exc:
        _record(
            request,
            principal,
            body.scope,
            tenant_id,
            body.action,
            body.path,
            target_path=body.target,
            status="失败",
            error=str(exc),
        )
        raise _bad_request(exc) from exc
    _record(
        request,
        principal,
        body.scope,
        tenant_id,
        body.action,
        body.path,
        target_path=result["path"],
    )
    return result


@router.delete("/entries")
def delete_drive_entry(
    request: Request,
    scope: str = Query(min_length=1),
    tenant_id: str = Query(default=""),
    path: str = Query(min_length=1),
    recursive: bool = Query(default=False),
    principal=Depends(require_permission("drive.manage")),
):
    service = _drive_service(request)
    tenant = tenant_id or None
    try:
        result = service.delete(scope, tenant, path, recursive=recursive)
    except ValueError as exc:
        _record(
            request,
            principal,
            scope,
            tenant,
            "delete",
            path,
            status="失败",
            error=str(exc),
        )
        raise _bad_request(exc) from exc
    _record(request, principal, scope, tenant, "delete", path)
    return result


@router.get("/audit")
def list_drive_audit(
    request: Request,
    scope: str = Query(default=""),
    tenant_id: str = Query(default=""),
    action: str = Query(default=""),
    operator: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal=Depends(require_permission("drive.read")),
):
    store = get_drive_audit_store(request)
    if store is None:
        raise HTTPException(status_code=503, detail="网盘审计服务不可用")
    filters = {
        "scope": scope or None,
        "tenant_id": tenant_id or None,
        "action": action or None,
        "operator": operator or None,
    }
    return {
        "items": store.list_recent(limit=limit, offset=offset, **filters),
        "total": store.count(**filters),
    }
