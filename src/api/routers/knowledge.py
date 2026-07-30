"""Scoped knowledge library management endpoints."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

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

from src.api.deps import get_registry, require_permission
from src.api.schemas import (
    KnowledgeCategoryCreateIn,
    KnowledgeCategoryUpdateIn,
    KnowledgeDriveImportIn,
    KnowledgeEmbeddingConfigIn,
    KnowledgeMoveIn,
    KnowledgeRefreshIn,
    KnowledgeReindexIn,
    KnowledgeTextIn,
)
from src.core.config.loader import ConfigError, validate_embedding_profile
from src.core.paths import CONFIG_DIR
from src.core.services.knowledge import SUPPORTED_SUFFIXES
from src.core.storage.tenants import TenantStoreError

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
UPLOAD_SUBDIR = "knowledge_uploads"
_UNSAFE_FILENAME = re.compile(r"[\\/\x00-\x1f]")
EMBEDDINGS_FILE = CONFIG_DIR / "embeddings.json"


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


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


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


@router.get("/categories")
def list_categories(
    request: Request,
    scope: str = Query(default=""),
    tenant_id: str = Query(default=""),
    principal=Depends(require_permission("knowledge.read")),
):
    service = _knowledge_service(request)
    try:
        return {
            # Lets the page explain why sources stay in pending_embedding.
            "embedding_enabled": service.embedding is not None,
            "categories": service.list_categories(
                scope or None, tenant_id or None
            ),
        }
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/categories", status_code=201)
def create_category(
    body: KnowledgeCategoryCreateIn,
    request: Request,
    principal=Depends(require_permission("knowledge.manage")),
):
    try:
        return _knowledge_service(request).create_category(
            body.scope, body.name, body.description, body.tenant_id
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.put("/categories/{category_id}")
def update_category(
    category_id: str,
    body: KnowledgeCategoryUpdateIn,
    request: Request,
    principal=Depends(require_permission("knowledge.manage")),
):
    try:
        return _knowledge_service(request).update_category(
            category_id, body.name, body.description
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.delete("/categories/{category_id}")
def delete_category(
    category_id: str,
    request: Request,
    principal=Depends(require_permission("knowledge.manage")),
):
    try:
        _knowledge_service(request).delete_category(category_id)
    except ValueError as exc:
        status = 409 if "仍包含" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return {"deleted": True}


@router.get("")
def list_knowledge(
    request: Request,
    tenant_id: str = Query(default=""),
    category_id: str = Query(default=""),
    principal=Depends(require_permission("knowledge.read")),
):
    service = _knowledge_service(request)
    if category_id and not tenant_id:
        return {"sources": service.list_category(category_id)}
    if not tenant_id:
        raise HTTPException(status_code=400, detail="必须提供租户编号或知识库编号")
    tenant = _tenant(request, tenant_id)
    return {
        "sources": service.list(tenant.tenant_id, category_id or None)
    }


@router.get("/search")
def search_knowledge(
    request: Request,
    tenant_id: str = Query(min_length=1),
    q: str = Query(min_length=1),
    limit: int = Query(default=6, ge=1, le=20),
    agent_id: str = Query(default=""),
    category_ids: Optional[List[str]] = Query(default=None),
    principal=Depends(require_permission("knowledge.read")),
):
    service = _knowledge_service(request)
    tenant = _tenant(request, tenant_id)
    return {
        "results": service.search(
            tenant.tenant_id,
            q,
            limit,
            agent_id=agent_id or None,
            category_ids=category_ids,
        )
    }


@router.post("/text")
def add_knowledge_text(
    body: KnowledgeTextIn,
    request: Request,
    principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge_service(request)
    try:
        if body.category_id:
            category = service.get_category(body.category_id)
            if category["scope"] == "tenant":
                _tenant(request, str(category["tenant_id"]))
                if body.tenant_id and body.tenant_id != category["tenant_id"]:
                    raise ValueError("知识库不属于指定租户")
            return service.add_text_to_category(
                body.category_id, body.name, body.content
            )
        if not body.tenant_id:
            raise ValueError("未指定知识库时必须提供租户编号")
        tenant = _tenant(request, body.tenant_id)
        return service.add_text(
            tenant.tenant_id, body.name, body.content
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/upload")
def upload_knowledge_file(
    request: Request,
    tenant_id: str = Form(default=""),
    category_id: str = Form(default=""),
    file: UploadFile = File(...),
    principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge_service(request)
    target = None
    existed = False
    filename = _UNSAFE_FILENAME.sub("_", (file.filename or "").strip()).lstrip(".")
    if not filename:
        raise HTTPException(status_code=400, detail="文件名无效")
    suffix = Path(filename).suffix.lower()
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

    try:
        if category_id:
            category = service.get_category(category_id)
        else:
            if not tenant_id:
                raise ValueError("未指定知识库时必须提供租户编号")
            _tenant(request, tenant_id)
            category_id = service.ensure_default_category(tenant_id)
            category = service.get_category(category_id)
        if category["scope"] == "public":
            if tenant_id:
                raise ValueError("公共知识库上传不能指定租户")
            root = service.public_root
            scope = "public"
            owner_tenant = None
            drive_path = "{}/{}".format(UPLOAD_SUBDIR, filename)
        else:
            owner_tenant = str(category["tenant_id"])
            if tenant_id and tenant_id != owner_tenant:
                raise ValueError("知识库不属于指定租户")
            _tenant(request, owner_tenant)
            root = get_registry(request).tenant_root(owner_tenant)
            scope = "tenant"
            drive_path = "workspace/{}/{}".format(UPLOAD_SUBDIR, filename)
        target = root / drive_path
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_bytes(payload)
        if existed:
            service.mark_drive_changed(scope, owner_tenant, drive_path)
        return service.index_drive_file(
            category_id, scope, owner_tenant, drive_path
        )
    except ValueError as exc:
        if target is not None and not existed:
            try:
                target.unlink()
            except OSError:
                pass
        raise _bad_request(exc) from exc


@router.post("/from-drive")
def import_from_drive(
    body: KnowledgeDriveImportIn,
    request: Request,
    principal=Depends(require_permission("knowledge.manage")),
):
    if not body.paths or len(body.paths) > 100:
        raise HTTPException(status_code=400, detail="每次请选择 1 到 100 个文件")
    service = _knowledge_service(request)
    results = []
    for path in dict.fromkeys(body.paths):
        try:
            indexed = service.index_drive_file(
                body.category_id, body.scope, body.tenant_id, path
            )
            results.append({"path": path, "ok": True, **indexed})
        except (OSError, ValueError) as exc:
            results.append({"path": path, "ok": False, "error": str(exc)})
    return {"items": results}


@router.post("/refresh")
def refresh_sources(
    body: KnowledgeRefreshIn,
    request: Request,
    principal=Depends(require_permission("knowledge.manage")),
):
    if not body.source_ids or len(body.source_ids) > 100:
        raise HTTPException(status_code=400, detail="每次请选择 1 到 100 个知识来源")
    return {"items": _knowledge_service(request).refresh(body.source_ids)}


@router.patch("/sources/move")
def move_sources(
    body: KnowledgeMoveIn,
    request: Request,
    principal=Depends(require_permission("knowledge.manage")),
):
    try:
        moved = _knowledge_service(request).move_sources(
            body.source_ids, body.target_category_id
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return {"moved": moved}


@router.get("/drive-links")
def drive_links(
    request: Request,
    scope: str = Query(min_length=1),
    tenant_id: str = Query(default=""),
    path: str = Query(default=""),
    principal=Depends(require_permission("knowledge.read")),
):
    try:
        return {
            "links": _knowledge_service(request).drive_links(
                scope, tenant_id or None, path
            )
        }
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/source-preview/{source_id}")
def preview_source(
    source_id: str,
    request: Request,
    principal=Depends(require_permission("knowledge.read")),
):
    try:
        return _knowledge_service(request).preview_source(source_id)
    except ValueError as exc:
        status = 404 if "未找到" in str(exc) or "已删除" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/embedding-config")
def get_embedding_config(
    request: Request,
    principal=Depends(require_permission("knowledge.read")),
):
    try:
        data = json.loads(EMBEDDINGS_FILE.read_text(encoding="utf-8"))
        profile = validate_embedding_profile(data, EMBEDDINGS_FILE)
    except (OSError, json.JSONDecodeError, ConfigError) as exc:
        raise HTTPException(status_code=500, detail="读取向量配置失败：{}".format(exc)) from exc
    return {
        "id": profile.id,
        "enabled": profile.enabled,
        "base_url": profile.base_url,
        "model": profile.model,
        "dimensions": profile.dimensions,
        "timeout_seconds": profile.timeout_seconds,
        "runtime_enabled": _knowledge_service(request).embedding is not None,
    }


@router.put("/embedding-config")
def update_embedding_config(
    body: KnowledgeEmbeddingConfigIn,
    request: Request,
    principal=Depends(require_permission("knowledge.manage")),
):
    data = {
        "id": body.id,
        "enabled": body.enabled,
        "base_url": body.base_url,
        "model": body.model,
        "dimensions": body.dimensions,
        "timeout_seconds": body.timeout_seconds,
    }
    try:
        profile = validate_embedding_profile(data, EMBEDDINGS_FILE)
        normalized = {
            "id": profile.id,
            "enabled": profile.enabled,
            "base_url": profile.base_url,
            "model": profile.model,
            "dimensions": profile.dimensions,
            "timeout_seconds": profile.timeout_seconds,
        }
        EMBEDDINGS_FILE.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="保存向量配置失败") from exc
    return {**normalized, "restart_required": True}


@router.post("/reindex")
def reindex_knowledge(
    body: KnowledgeReindexIn,
    request: Request,
    principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge_service(request)
    tenant = _tenant(request, body.tenant_id)
    try:
        return service.reindex(tenant.tenant_id, body.category_ids)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.delete("/{source_id}")
def delete_knowledge(
    source_id: str,
    request: Request,
    tenant_id: str = Query(default=""),
    principal=Depends(require_permission("knowledge.manage")),
):
    service = _knowledge_service(request)
    deleted = (
        service.delete(_tenant(request, tenant_id).tenant_id, source_id)
        if tenant_id
        else service.delete_source(source_id)
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="未找到知识来源")
    return {"deleted": True}
