"""Full platform-public and organization-scoped knowledge/drive APIs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from src.api.deps import (
    get_config,
    get_drive_audit_store,
    get_drive_service,
    get_organization_store,
    get_principal,
    require_permission,
)
from src.api.routers.v2 import _record_content_owner, _require_content_manager
from src.core.services.authorization import AuthorizationError, AuthorizationService
from src.core.services.drive import MAX_PREVIEW_BYTES, MAX_UPLOAD_BYTES
from src.core.services.knowledge import SUPPORTED_SUFFIXES


router = APIRouter(prefix="/api/v2", tags=["content-v2"])
KNOWLEDGE_DRIVE_IMPORT_MAX_FILES = 1000

_KNOWLEDGE_UPLOAD_BYTES = 20 * 1024 * 1024
_KNOWLEDGE_UPLOAD_DIR = "knowledge_uploads"
_UNSAFE_FILENAME = re.compile(r"[\\/\x00-\x1f]")


def _knowledge(request: Request):
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="知识库服务不可用")
    return service


def _drive(request: Request):
    service = get_drive_service(request)
    if service is None:
        raise HTTPException(status_code=503, detail="文件服务不可用")
    return service


def _organization(request: Request, principal, organization_id: str):
    try:
        return AuthorizationService(get_organization_store(request)).organization_context(
            principal,
            organization_id,
            request_id=request.headers.get("x-request-id", ""),
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _category(service, category_id: str, scope: str, tenant_id: Optional[str]):
    try:
        category = service.get_category(category_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="知识库不存在") from exc
    if category["scope"] != scope or (
        scope == "tenant" and str(category.get("tenant_id") or "") != tenant_id
    ):
        raise HTTPException(status_code=404, detail="知识库不存在")
    return category


def _organization_writable_category(service, category_id: str, organization_id: str):
    try:
        category = service.get_category(category_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="知识库不存在") from exc
    if category["scope"] == "public":
        raise HTTPException(status_code=403, detail="公共知识内容只读")
    if str(category.get("tenant_id") or "") != organization_id:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return category


def _source(service, source_id: str, scope: str, tenant_id: Optional[str]):
    categories = service.list_categories(scope=scope)
    if scope == "tenant":
        categories = [
            item for item in categories
            if str(item.get("tenant_id") or "") == str(tenant_id or "")
        ]
    category_ids = {str(item["category_id"]) for item in categories}
    for category_id in category_ids:
        for item in service.list_category(category_id):
            if str(item.get("source_id")) == source_id:
                return item
    raise HTTPException(status_code=404, detail="知识来源不存在")


def _embedding_status(request: Request) -> Dict[str, Any]:
    config = get_config(request)
    service = _knowledge(request)
    binding = config.app.embedding_model
    profile = config.models.get(binding) if binding else None
    if profile is None:
        return {"bound": False, "runtime_enabled": service.embedding is not None}
    return {
        "bound": True,
        "profile_id": profile.id,
        "model": profile.model,
        "dimensions": profile.dimensions,
        "enabled": profile.enabled,
        "runtime_enabled": service.embedding is not None,
    }


def _clean_upload(file: UploadFile) -> tuple[str, bytes]:
    filename = _UNSAFE_FILENAME.sub("_", (file.filename or "").strip()).lstrip(".")
    if not filename:
        raise HTTPException(status_code=400, detail="文件名无效")
    if Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail="仅支持上传 TXT、Markdown、PDF、Word(docx)、Excel(xlsx) 或 PPT(pptx) 文件",
        )
    payload = file.file.read(_KNOWLEDGE_UPLOAD_BYTES + 1)
    if not payload:
        raise HTTPException(status_code=400, detail="上传的文件内容为空")
    if len(payload) > _KNOWLEDGE_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="上传的文件不能超过 20 MiB")
    return filename, payload


def _save_and_index(
    service,
    category_id: str,
    scope: str,
    tenant_id: Optional[str],
    filename: str,
    payload: bytes,
):
    if scope == "public":
        root = service.public_root
        drive_path = f"{_KNOWLEDGE_UPLOAD_DIR}/{filename}"
    else:
        root = service.registry.tenant_root(str(tenant_id))
        drive_path = f"workspace/{_KNOWLEDGE_UPLOAD_DIR}/{filename}"
    target = root / drive_path
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    target.write_bytes(payload)
    if existed:
        service.mark_drive_changed(scope, tenant_id, drive_path)
    try:
        return service.index_drive_file(category_id, scope, tenant_id, drive_path)
    except Exception:
        if not existed:
            try:
                target.unlink()
            except OSError:
                pass
        raise


def _validate_sources(
    service, source_ids: Iterable[str], scope: str, tenant_id: Optional[str]
) -> list[str]:
    normalized = list(dict.fromkeys(str(value) for value in source_ids if str(value)))
    if not normalized or len(normalized) > 100:
        raise HTTPException(status_code=400, detail="每次请选择 1 到 100 个知识来源")
    for source_id in normalized:
        try:
            _source(service, source_id, scope, tenant_id)
        except HTTPException as exc:
            if scope != "tenant" or exc.status_code != 404:
                raise
            try:
                _source(service, source_id, "public", None)
            except HTTPException:
                raise exc
            raise HTTPException(status_code=403, detail="公共知识内容只读") from exc
    return normalized


def _record_drive(
    request: Request,
    principal,
    scope: str,
    tenant_id: Optional[str],
    action: str,
    path: str,
    *,
    target_path: Optional[str] = None,
    size_bytes: int = 0,
    status: str = "成功",
    error: Optional[str] = None,
) -> None:
    store = get_drive_audit_store(request)
    if store is None:
        return
    store.record(
        operator="web:{}".format(principal.user.username),
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


# ---- platform public knowledge ----


@router.get("/platform/knowledge/categories")
def platform_knowledge_categories(
    request: Request, _principal=Depends(require_permission("knowledge.read"))
):
    service = _knowledge(request)
    return {
        "embedding_enabled": service.embedding is not None,
        "categories": service.list_categories(scope="public"),
    }


@router.post("/platform/knowledge/categories", status_code=201)
def create_platform_knowledge_category(
    request: Request,
    body: Dict[str, Any] = Body(...),
    _principal=Depends(require_permission("knowledge.manage")),
):
    try:
        return _knowledge(request).create_category(
            "public", str(body.get("name") or ""), str(body.get("description") or "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/platform/knowledge/categories/{category_id}")
def update_platform_knowledge_category(
    category_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    _principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge(request)
    _category(service, category_id, "public", None)
    try:
        return service.update_category(
            category_id, str(body.get("name") or ""), str(body.get("description") or "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/platform/knowledge/categories/{category_id}")
def delete_platform_knowledge_category(
    category_id: str,
    request: Request,
    _principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge(request)
    _category(service, category_id, "public", None)
    try:
        service.delete_category(category_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=409 if "仍包含" in str(exc) else 400, detail=str(exc)
        ) from exc
    return {"deleted": True}


@router.get("/platform/knowledge/sources")
def list_platform_knowledge_sources(
    request: Request,
    category_id: str = Query(min_length=1),
    _principal=Depends(require_permission("knowledge.read")),
):
    service = _knowledge(request)
    _category(service, category_id, "public", None)
    return {"sources": service.list_category(category_id)}


@router.get("/platform/knowledge/sources/{source_id}")
def preview_platform_knowledge_source(
    source_id: str,
    request: Request,
    _principal=Depends(require_permission("knowledge.read")),
):
    service = _knowledge(request)
    _source(service, source_id, "public", None)
    try:
        return service.preview_source(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/platform/knowledge/sources/{source_id}")
def delete_platform_knowledge_source(
    source_id: str,
    request: Request,
    _principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge(request)
    _source(service, source_id, "public", None)
    return {"deleted": service.delete_source(source_id)}


@router.get("/platform/knowledge/search")
def search_platform_knowledge(
    request: Request,
    q: str = Query(min_length=1),
    category_ids: Optional[list[str]] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=20),
    _principal=Depends(require_permission("knowledge.read")),
):
    service = _knowledge(request)
    for category_id in category_ids or []:
        _category(service, category_id, "public", None)
    return {"results": service.search(None, q, limit, category_ids=category_ids)}


@router.post("/platform/knowledge/text")
def add_platform_knowledge_text(
    request: Request,
    body: Dict[str, Any] = Body(...),
    _principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge(request)
    category_id = str(body.get("category_id") or "")
    _category(service, category_id, "public", None)
    try:
        return service.add_text_to_category(
            category_id, str(body.get("name") or ""), str(body.get("content") or "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/platform/knowledge/upload")
def upload_platform_knowledge(
    request: Request,
    category_id: str = Form(min_length=1),
    file: UploadFile = File(...),
    _principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge(request)
    _category(service, category_id, "public", None)
    filename, payload = _clean_upload(file)
    try:
        return _save_and_index(service, category_id, "public", None, filename, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/platform/knowledge/from-drive")
def import_platform_drive_knowledge(
    request: Request,
    body: Dict[str, Any] = Body(...),
    _principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge(request)
    category_id = str(body.get("category_id") or "")
    _category(service, category_id, "public", None)
    paths = list(dict.fromkeys(body.get("paths") or []))
    if not paths or len(paths) > KNOWLEDGE_DRIVE_IMPORT_MAX_FILES:
        raise HTTPException(status_code=400, detail="每次请选择 1 到 1000 个文件")
    items = []
    for path in paths:
        try:
            result = service.index_drive_file(category_id, "public", None, str(path))
            items.append({"path": path, "ok": True, **result})
        except (OSError, ValueError) as exc:
            items.append({"path": path, "ok": False, "error": str(exc)})
    return {"items": items}


@router.post("/platform/knowledge/refresh")
def refresh_platform_knowledge(
    request: Request,
    body: Dict[str, Any] = Body(...),
    _principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge(request)
    source_ids = _validate_sources(service, body.get("source_ids") or [], "public", None)
    return {"items": service.refresh(source_ids)}


@router.patch("/platform/knowledge/sources/move")
def move_platform_knowledge(
    request: Request,
    body: Dict[str, Any] = Body(...),
    _principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge(request)
    source_ids = _validate_sources(service, body.get("source_ids") or [], "public", None)
    target = str(body.get("target_category_id") or "")
    _category(service, target, "public", None)
    try:
        return {"moved": service.move_sources(source_ids, target)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/platform/knowledge/drive-links")
def platform_knowledge_drive_links(
    request: Request,
    path: str = Query(default=""),
    _principal=Depends(require_permission("knowledge.read")),
):
    return {"links": _knowledge(request).drive_links("public", None, path)}


@router.get("/platform/knowledge/embedding-config")
def platform_embedding_status(
    request: Request, _principal=Depends(require_permission("knowledge.read"))
):
    return _embedding_status(request)


@router.post("/platform/knowledge/reembed")
def reembed_platform_knowledge(
    request: Request,
    body: Dict[str, Any] = Body(...),
    _principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge(request)
    source_ids = _validate_sources(service, body.get("source_ids") or [], "public", None)
    try:
        return service.reembed_sources(None, source_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/platform/knowledge/embedding-health")
def platform_embedding_health(
    request: Request,
    category_id: Optional[str] = Query(default=None),
    _principal=Depends(require_permission("knowledge.read")),
):
    return _knowledge(request).embedding_health(
        None, [category_id] if category_id else None
    )


@router.post("/platform/knowledge/reindex")
def reindex_platform_knowledge(
    request: Request,
    body: Dict[str, Any] = Body(...),
    _principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge(request)
    category_ids = body.get("category_ids")
    force = bool(body.get("force", False))
    for category_id in category_ids or []:
        _category(service, str(category_id), "public", None)
    try:
        return service.reindex(None, category_ids, force=force)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---- organization knowledge additions ----


@router.post("/orgs/{organization_id}/knowledge/categories", status_code=201)
def create_organization_knowledge_category(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    _organization(request, principal, organization_id)
    try:
        return _knowledge(request).create_category(
            "tenant",
            str(body.get("name") or ""),
            str(body.get("description") or ""),
            organization_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/orgs/{organization_id}/knowledge/categories/{category_id}")
def update_organization_knowledge_category(
    organization_id: str,
    category_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    _organization(request, principal, organization_id)
    service = _knowledge(request)
    _organization_writable_category(service, category_id, organization_id)
    try:
        return service.update_category(
            category_id, str(body.get("name") or ""), str(body.get("description") or "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/orgs/{organization_id}/knowledge/categories/{category_id}")
def delete_organization_knowledge_category(
    organization_id: str,
    category_id: str,
    request: Request,
    principal=Depends(get_principal),
):
    _organization(request, principal, organization_id)
    service = _knowledge(request)
    _organization_writable_category(service, category_id, organization_id)
    try:
        service.delete_category(category_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=409 if "仍包含" in str(exc) else 400, detail=str(exc)
        ) from exc
    return {"deleted": True}


@router.post("/orgs/{organization_id}/knowledge/upload")
def upload_organization_knowledge(
    organization_id: str,
    request: Request,
    category_id: str = Form(min_length=1),
    file: UploadFile = File(...),
    principal=Depends(get_principal),
):
    context = _organization(request, principal, organization_id)
    service = _knowledge(request)
    _organization_writable_category(service, category_id, organization_id)
    filename, payload = _clean_upload(file)
    drive_path = f"workspace/{_KNOWLEDGE_UPLOAD_DIR}/{filename}"
    if (service.registry.tenant_root(organization_id) / drive_path).exists():
        _require_content_manager(request, context, "drive_entry", drive_path)
    try:
        result = _save_and_index(
            service, category_id, "tenant", organization_id, filename, payload
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    source_id = str(result.get("source_id") or "")
    if source_id:
        _record_content_owner(
            request, organization_id, "knowledge_source", source_id, context.user_id
        )
    _record_content_owner(
        request, organization_id, "drive_entry", drive_path, context.user_id
    )
    return result


@router.post("/orgs/{organization_id}/knowledge/from-drive")
def import_organization_drive_knowledge(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization(request, principal, organization_id)
    service = _knowledge(request)
    category_id = str(body.get("category_id") or "")
    _organization_writable_category(service, category_id, organization_id)
    paths = list(dict.fromkeys(body.get("paths") or []))
    if not paths or len(paths) > KNOWLEDGE_DRIVE_IMPORT_MAX_FILES:
        raise HTTPException(status_code=400, detail="每次请选择 1 到 1000 个文件")
    items = []
    for path in paths:
        try:
            result = service.index_drive_file(
                category_id, "tenant", organization_id, str(path)
            )
            source_id = str(result.get("source_id") or "")
            if source_id:
                _record_content_owner(
                    request,
                    organization_id,
                    "knowledge_source",
                    source_id,
                    context.user_id,
                )
            items.append({"path": path, "ok": True, **result})
        except (OSError, ValueError) as exc:
            items.append({"path": path, "ok": False, "error": str(exc)})
    return {"items": items}


@router.post("/orgs/{organization_id}/knowledge/refresh")
def refresh_organization_knowledge(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization(request, principal, organization_id)
    service = _knowledge(request)
    source_ids = _validate_sources(
        service, body.get("source_ids") or [], "tenant", organization_id
    )
    for source_id in source_ids:
        _require_content_manager(request, context, "knowledge_source", source_id)
    return {"items": service.refresh(source_ids)}


@router.patch("/orgs/{organization_id}/knowledge/sources/move")
def move_organization_knowledge(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization(request, principal, organization_id)
    service = _knowledge(request)
    source_ids = _validate_sources(
        service, body.get("source_ids") or [], "tenant", organization_id
    )
    for source_id in source_ids:
        _require_content_manager(request, context, "knowledge_source", source_id)
    target = str(body.get("target_category_id") or "")
    _organization_writable_category(service, target, organization_id)
    try:
        return {"moved": service.move_sources(source_ids, target)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/orgs/{organization_id}/knowledge/drive-links")
def organization_knowledge_drive_links(
    organization_id: str,
    request: Request,
    path: str = Query(default=""),
    principal=Depends(get_principal),
):
    _organization(request, principal, organization_id)
    return {
        "links": _knowledge(request).drive_links("tenant", organization_id, path)
    }


@router.get("/orgs/{organization_id}/knowledge/embedding-config")
def organization_embedding_status(
    organization_id: str, request: Request, principal=Depends(get_principal)
):
    _organization(request, principal, organization_id)
    return _embedding_status(request)


@router.post("/orgs/{organization_id}/knowledge/reembed")
def reembed_organization_knowledge(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    context = _organization(request, principal, organization_id)
    service = _knowledge(request)
    source_ids = _validate_sources(
        service, body.get("source_ids") or [], "tenant", organization_id
    )
    for source_id in source_ids:
        _require_content_manager(request, context, "knowledge_source", source_id)
    try:
        return service.reembed_sources(organization_id, source_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/orgs/{organization_id}/knowledge/embedding-health")
def organization_embedding_health(
    organization_id: str,
    request: Request,
    category_id: Optional[str] = Query(default=None),
    principal=Depends(get_principal),
):
    _organization(request, principal, organization_id)
    return _knowledge(request).embedding_health(
        organization_id, [category_id] if category_id else None
    )


@router.post("/orgs/{organization_id}/knowledge/reindex")
def reindex_organization_knowledge(
    organization_id: str,
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(get_principal),
):
    _organization(request, principal, organization_id)
    service = _knowledge(request)
    category_ids = body.get("category_ids")
    force = bool(body.get("force", False))
    for category_id in category_ids or []:
        _organization_writable_category(service, str(category_id), organization_id)
    try:
        return service.reindex(organization_id, category_ids, force=force)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---- platform public drive ----


@router.get("/platform/drive/entries")
def list_platform_drive(
    request: Request,
    path: str = Query(default=""),
    _principal=Depends(require_permission("drive.read")),
):
    try:
        return _drive(request).list_entries("public", None, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/platform/drive/usage")
def platform_drive_usage(
    request: Request, _principal=Depends(require_permission("drive.read"))
):
    return _drive(request).usage("public", None)


@router.post("/platform/drive/folders")
def create_platform_drive_folder(
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(require_permission("drive.manage")),
):
    try:
        result = _drive(request).create_folder(
            "public",
            None,
            str(body.get("path") or ""),
            str(body.get("name") or ""),
            exist_ok=bool(body.get("exist_ok")),
        )
        if result.get("created"):
            _record_drive(request, principal, "public", None, "mkdir", result["path"])
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _drive_upload(
    request: Request,
    principal,
    scope: str,
    tenant_id: Optional[str],
    path: str,
    overwrite: bool,
    file: UploadFile,
):
    payload = file.file.read(MAX_UPLOAD_BYTES + 1)
    if not payload:
        raise HTTPException(status_code=400, detail="上传的文件内容为空")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="上传文件超过大小限制")
    try:
        result = _drive(request).save_file(
            scope, tenant_id, path, file.filename or "upload.bin", payload, overwrite
        )
    except ValueError as exc:
        _record_drive(
            request, principal, scope, tenant_id, "upload", path,
            size_bytes=len(payload), status="失败", error=str(exc)
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record_drive(
        request, principal, scope, tenant_id, "upload", result["path"],
        size_bytes=result["size"]
    )
    return result


@router.post("/platform/drive/upload")
def upload_platform_drive(
    request: Request,
    path: str = Form(default=""),
    overwrite: bool = Form(default=False),
    file: UploadFile = File(...),
    principal=Depends(require_permission("drive.manage")),
):
    return _drive_upload(request, principal, "public", None, path, overwrite, file)


@router.get("/platform/drive/download")
def download_platform_drive(
    request: Request,
    path: str = Query(min_length=1),
    principal=Depends(require_permission("drive.read")),
):
    try:
        real_path = _drive(request).read_file("public", None, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record_drive(
        request, principal, "public", None, "download", path,
        size_bytes=real_path.stat().st_size
    )
    return FileResponse(real_path, filename=real_path.name, media_type="application/octet-stream")


@router.get("/platform/drive/preview")
def preview_platform_drive(
    request: Request,
    path: str = Query(min_length=1),
    max_bytes: int = Query(default=MAX_PREVIEW_BYTES, ge=1),
    _principal=Depends(require_permission("drive.read")),
):
    try:
        return _drive(request).read_text("public", None, path, max_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _drive_update(
    request: Request,
    principal,
    scope: str,
    tenant_id: Optional[str],
    body: Dict[str, Any],
):
    action = str(body.get("action") or "")
    path = str(body.get("path") or "")
    target = str(body.get("target") or "")
    try:
        if action == "rename":
            result = _drive(request).rename(scope, tenant_id, path, target)
        elif action == "move":
            result = _drive(request).move(scope, tenant_id, path, target)
        else:
            raise ValueError("action 仅支持 rename 或 move")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record_drive(
        request, principal, scope, tenant_id, action, path, target_path=result["path"]
    )
    return result


@router.put("/platform/drive/entries")
def update_platform_drive(
    request: Request,
    body: Dict[str, Any] = Body(...),
    principal=Depends(require_permission("drive.manage")),
):
    return _drive_update(request, principal, "public", None, body)


@router.delete("/platform/drive/entries")
def delete_platform_drive(
    request: Request,
    path: str = Query(min_length=1),
    recursive: bool = Query(default=False),
    principal=Depends(require_permission("drive.manage")),
):
    try:
        result = _drive(request).delete("public", None, path, recursive=recursive)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record_drive(request, principal, "public", None, "delete", path)
    return result


@router.get("/platform/drive/audit")
def platform_drive_audit(
    request: Request,
    action: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _principal=Depends(require_permission("drive.read")),
):
    store = get_drive_audit_store(request)
    if store is None:
        raise HTTPException(status_code=503, detail="文件审计服务不可用")
    filters = {"scope": "public", "tenant_id": None, "action": action or None}
    return {
        "items": store.list_recent(limit=limit, offset=offset, **filters),
        "total": store.count(**filters),
    }


# ---- organization drive additions ----


@router.get("/orgs/{organization_id}/drive/usage")
def organization_drive_usage(
    organization_id: str,
    request: Request,
    scope: str = Query(default="organization"),
    principal=Depends(get_principal),
):
    _organization(request, principal, organization_id)
    storage_scope, tenant_id = (
        ("public", None) if scope == "public" else ("tenant", organization_id)
    )
    try:
        return _drive(request).usage(storage_scope, tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/orgs/{organization_id}/drive/audit")
def organization_drive_audit(
    organization_id: str,
    request: Request,
    action: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal=Depends(get_principal),
):
    _organization(request, principal, organization_id)
    store = get_drive_audit_store(request)
    if store is None:
        raise HTTPException(status_code=503, detail="文件审计服务不可用")
    filters = {"scope": "tenant", "tenant_id": organization_id, "action": action or None}
    return {
        "items": store.list_recent(limit=limit, offset=offset, **filters),
        "total": store.count(**filters),
    }
