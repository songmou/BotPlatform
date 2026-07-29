"""Tenant knowledge base management endpoints for the web panel."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile

from src.api.deps import get_registry, require_permission
from src.api.schemas import KnowledgeReindexIn, KnowledgeTextIn
from src.core.storage.tenants import TenantStoreError

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".pdf", ".docx", ".xlsx", ".pptx"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
UPLOAD_SUBDIR = "knowledge_uploads"

_UNSAFE_FILENAME = re.compile(r"[\\/\x00-\x1f]")


def _knowledge_service(request: Request):
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="知识库服务不可用")
    return service


def _tenant(request: Request, tenant_id: str):
    try:
        return get_registry(request).get(tenant_id)
    except TenantStoreError as exc:
        raise HTTPException(status_code=404, detail="租户不存在") from exc


@router.get("/tenants")
def list_knowledge_tenants(
    request: Request, principal=Depends(require_permission("knowledge.read"))
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


@router.get("")
def list_knowledge(
    request: Request,
    tenant_id: str = Query(min_length=1),
    principal=Depends(require_permission("knowledge.read")),
):
    service = _knowledge_service(request)
    tenant = _tenant(request, tenant_id)
    return {"sources": service.list(tenant.tenant_id)}


@router.get("/search")
def search_knowledge(
    request: Request,
    tenant_id: str = Query(min_length=1),
    q: str = Query(min_length=1),
    limit: int = Query(default=6, ge=1, le=20),
    principal=Depends(require_permission("knowledge.read")),
):
    service = _knowledge_service(request)
    tenant = _tenant(request, tenant_id)
    return {"results": service.search(tenant.tenant_id, q, limit)}


@router.post("/text")
def add_knowledge_text(
    body: KnowledgeTextIn,
    request: Request,
    principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge_service(request)
    tenant = _tenant(request, body.tenant_id)
    try:
        return service.add_text(tenant.tenant_id, body.name, body.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/upload")
def upload_knowledge_file(
    request: Request,
    tenant_id: str = Form(min_length=1),
    file: UploadFile = File(...),
    principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge_service(request)
    registry = get_registry(request)
    tenant = _tenant(request, tenant_id)

    filename = _UNSAFE_FILENAME.sub("_", (file.filename or "").strip()).lstrip(".")
    if not filename:
        raise HTTPException(status_code=400, detail="文件名无效")
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail="仅支持上传 TXT、Markdown、PDF、Word(docx)、Excel(xlsx) 或 PPT(pptx) 文件",
        )
    payload = file.file.read(MAX_UPLOAD_BYTES + 1)
    if not payload:
        raise HTTPException(status_code=400, detail="上传的文件内容为空")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="上传的文件不能超过 20 MiB")

    upload_dir = registry.tenant_root(tenant.tenant_id) / "workspace" / UPLOAD_SUBDIR
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / filename
    target.write_bytes(payload)
    try:
        return service.index_file(tenant, target)
    except ValueError as exc:
        # Remove the saved copy so a broken document does not linger.
        try:
            target.unlink()
        except OSError:
            pass
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reindex")
def reindex_knowledge(
    body: KnowledgeReindexIn,
    request: Request,
    principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge_service(request)
    tenant = _tenant(request, body.tenant_id)
    try:
        return service.reindex(tenant.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{source_id}")
def delete_knowledge(
    source_id: str,
    request: Request,
    tenant_id: str = Query(min_length=1),
    principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge_service(request)
    tenant = _tenant(request, tenant_id)
    if not service.delete(tenant.tenant_id, source_id):
        raise HTTPException(status_code=404, detail="未找到知识来源")
    return {"deleted": True}
