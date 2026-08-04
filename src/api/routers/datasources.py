"""Datasource management endpoints."""

from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from src.api.deps import get_config, require_permission
from src.api.schemas import (
    DatasourceCreate,
    DatasourceOut,
    DatasourceStatusUpdate,
    DatasourceUpdate,
    DatasourceTestRequest,
    DatasourceTestResponse,
    DatasourceQueryRequest,
)
from src.core.config.datasource_secrets import (
    DATASOURCE_SECRETS_FILE,
    delete_password,
    merge_passwords,
    save_password,
    strip_passwords,
)
from src.core.config.loader import ConfigError
from src.core.datasource.drivers import driver_availability
from src.core.paths import CONFIG_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/datasources", tags=["datasources"])

DATASOURCES_FILE = CONFIG_DIR / "datasources.json"
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _load() -> list:
    if DATASOURCES_FILE.exists():
        entries = json.loads(DATASOURCES_FILE.read_text(encoding="utf-8")).get(
            "datasources", []
        )
        return merge_passwords(entries)
    return []


def _save(entries: list) -> None:
    stripped = strip_passwords(entries)
    DATASOURCES_FILE.write_text(
        json.dumps({"datasources": stripped}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sync(request: Request, entries: list) -> None:
    """Validate, persist, and apply the new datasource list to the live config."""
    try:
        get_config(request).update_datasources(entries)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _save(entries)
    ds_service = getattr(request.app.state, "datasource_service", None)
    if ds_service is not None:
        ds_service.reload(merge_passwords(entries))


def _to_out(entry: dict) -> DatasourceOut:
    available, hint = driver_availability(entry.get("engine", ""))
    return DatasourceOut(
        id=entry["id"],
        name=entry["name"],
        engine=entry.get("engine", ""),
        host=entry.get("host", ""),
        port=entry.get("port", 0),
        database=entry.get("database", ""),
        username=entry.get("username", ""),
        password="",
        password_set=bool(entry.get("password", "")),
        options=entry.get("options"),
        enabled=entry.get("enabled", True),
        read_only=entry.get("read_only", True),
        connect_timeout_seconds=entry.get("connect_timeout_seconds", 5),
        statement_timeout_seconds=entry.get("statement_timeout_seconds", 15),
        pool_size=entry.get("pool_size", 3),
        max_rows=entry.get("max_rows", 200),
        max_result_bytes=entry.get("max_result_bytes", 262144),
        tables=entry.get("tables"),
        prompt_injection=entry.get("prompt_injection"),
        driver_ready=available,
        driver_hint=hint if not available else "",
    )


# ------------------------------------------------------------------ CRUD


@router.get("", response_model=list[DatasourceOut])
def list_datasources(
    response: Response,
    _principal=Depends(require_permission("panel.read")),
):
    response.headers["Cache-Control"] = "no-store"
    return [_to_out(s) for s in _load()]


@router.post("", response_model=DatasourceOut, status_code=201)
def create_datasource(
    body: DatasourceCreate,
    request: Request,
    _principal=Depends(require_permission("panel.write")),
):
    if not _ID_PATTERN.match(body.id):
        raise HTTPException(
            status_code=400, detail="ID 只能包含小写字母、数字和下划线，且以字母开头"
        )
    entries = _load()
    if any(e["id"] == body.id for e in entries):
        raise HTTPException(status_code=409, detail="数据源 ID 已存在")
    item = {
        "id": body.id,
        "name": body.name,
        "engine": body.engine,
        "host": body.host,
        "port": body.port,
        "database": body.database,
        "username": body.username,
        "password": body.password,
        "options": body.options or {},
        "enabled": body.enabled,
        "read_only": body.read_only,
        "connect_timeout_seconds": body.connect_timeout_seconds,
        "statement_timeout_seconds": body.statement_timeout_seconds,
        "pool_size": body.pool_size,
        "max_rows": body.max_rows,
        "max_result_bytes": body.max_result_bytes,
        "tables": body.tables or [],
        "prompt_injection": body.prompt_injection or {},
    }
    entries.append(item)
    save_password(body.id, body.password)
    _sync(request, entries)
    return _to_out(item)


@router.put("/{datasource_id}", response_model=DatasourceOut)
def update_datasource(
    datasource_id: str,
    body: DatasourceUpdate,
    request: Request,
    _principal=Depends(require_permission("panel.write")),
):
    entries = _load()
    for e in entries:
        if e["id"] == datasource_id:
            if body.name is not None:
                e["name"] = body.name
            if body.engine is not None:
                e["engine"] = body.engine
            if body.host is not None:
                e["host"] = body.host
            if body.port is not None:
                e["port"] = body.port
            if body.database is not None:
                e["database"] = body.database
            if body.username is not None:
                e["username"] = body.username
            if body.password is not None:
                e["password"] = body.password
                save_password(datasource_id, body.password)
            if body.options is not None:
                e["options"] = body.options
            if body.enabled is not None:
                e["enabled"] = body.enabled
            if body.read_only is not None:
                e["read_only"] = body.read_only
            if body.connect_timeout_seconds is not None:
                e["connect_timeout_seconds"] = body.connect_timeout_seconds
            if body.statement_timeout_seconds is not None:
                e["statement_timeout_seconds"] = body.statement_timeout_seconds
            if body.pool_size is not None:
                e["pool_size"] = body.pool_size
            if body.max_rows is not None:
                e["max_rows"] = body.max_rows
            if body.max_result_bytes is not None:
                e["max_result_bytes"] = body.max_result_bytes
            if body.tables is not None:
                e["tables"] = body.tables
            if body.prompt_injection is not None:
                e["prompt_injection"] = body.prompt_injection
            _sync(request, entries)
            return _to_out(e)
    raise HTTPException(status_code=404, detail="数据源不存在")


@router.delete("/{datasource_id}")
def delete_datasource(
    datasource_id: str,
    request: Request,
    _principal=Depends(require_permission("panel.write")),
):
    entries = _load()
    filtered = [e for e in entries if e["id"] != datasource_id]
    if len(filtered) == len(entries):
        raise HTTPException(status_code=404, detail="数据源不存在")
    delete_password(datasource_id)
    _sync(request, filtered)
    return {"status": "ok"}


@router.patch("/{datasource_id}/status", response_model=DatasourceOut)
def toggle_datasource_status(
    datasource_id: str,
    body: DatasourceStatusUpdate,
    request: Request,
    _principal=Depends(require_permission("panel.write")),
):
    """Enable or disable a datasource without touching other fields."""
    entries = _load()
    for e in entries:
        if e["id"] == datasource_id:
            e["enabled"] = body.enabled
            _sync(request, entries)
            return _to_out(e)
    raise HTTPException(status_code=404, detail="数据源不存在")


@router.post("/install-drivers")
def install_drivers(
    _principal=Depends(require_permission("panel.write")),
):
    """Install database drivers from requirements-db.txt."""
    import subprocess
    import sys

    from src.core.paths import PROJECT_ROOT

    req_file = PROJECT_ROOT / "requirements-db.txt"
    if not req_file.exists():
        raise HTTPException(
            status_code=404, detail="requirements-db.txt 文件不存在"
        )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="安装超时（180秒），请手动执行 pip install -r requirements-db.txt",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="pip 执行异常：{}".format(exc)
        )
    return {
        "ok": result.returncode == 0,
        "stdout": (result.stdout or "")[-3000:],
        "stderr": (result.stderr or "")[-3000:],
    }


# ------------------------------------------------------------------ Test & schema


def _find_datasource(datasource_id: str) -> dict:
    for e in _load():
        if e["id"] == datasource_id:
            return e
    raise HTTPException(status_code=404, detail="数据源不存在")


@router.post("/test", response_model=DatasourceTestResponse)
def test_connection(
    body: DatasourceTestRequest,
    _principal=Depends(require_permission("panel.write")),
):
    """Test a datasource connection from draft parameters (no save)."""
    from src.core.datasource import DataSourceService

    svc = DataSourceService()
    entry = {
        "engine": body.engine,
        "host": body.host,
        "port": body.port,
        "database": body.database,
        "username": body.username,
        "password": body.password,
        "options": body.options or {},
        "connect_timeout_seconds": body.connect_timeout_seconds,
        "statement_timeout_seconds": body.statement_timeout_seconds,
    }
    result = svc.test_connection(entry)
    return DatasourceTestResponse(
        ok=result["ok"],
        latency_ms=result["latency_ms"],
        version=result["version"],
        error=result["error"],
    )


@router.post("/{datasource_id}/test", response_model=DatasourceTestResponse)
def test_saved_datasource(
    datasource_id: str,
    _principal=Depends(require_permission("panel.write")),
):
    """Test the connection of a saved datasource."""
    entry = _find_datasource(datasource_id)
    from src.core.datasource import DataSourceService

    svc = DataSourceService()
    result = svc.test_connection(entry)
    return DatasourceTestResponse(
        ok=result["ok"],
        latency_ms=result["latency_ms"],
        version=result["version"],
        error=result["error"],
    )


@router.get("/{datasource_id}/tables")
def list_remote_tables(
    datasource_id: str,
    request: Request,
    refresh: bool = False,
    _principal=Depends(require_permission("panel.read")),
):
    """Fetch all tables from the remote database."""
    ds_service = getattr(request.app.state, "datasource_service", None)
    if ds_service is None:
        raise HTTPException(status_code=400, detail="数据源服务未配置")
    try:
        tables = ds_service.remote_tables(datasource_id, refresh=refresh)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"tables": tables}


@router.get("/{datasource_id}/schema")
def get_datasource_schema(
    datasource_id: str,
    request: Request,
    _principal=Depends(require_permission("panel.read")),
):
    """Return the authorised table schema snapshot."""
    ds_service = getattr(request.app.state, "datasource_service", None)
    if ds_service is None:
        raise HTTPException(status_code=400, detail="数据源服务未配置")
    try:
        tables = ds_service.schema_snapshot(datasource_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"tables": tables}


@router.post("/{datasource_id}/query")
def admin_query_datasource(
    datasource_id: str,
    body: DatasourceQueryRequest,
    request: Request,
    _principal=Depends(require_permission("panel.write")),
):
    """Admin-side trial query."""
    ds_service = getattr(request.app.state, "datasource_service", None)
    if ds_service is None:
        raise HTTPException(status_code=400, detail="数据源服务未配置")
    try:
        result = ds_service.query(datasource_id, body.sql, limit=body.limit)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@router.get("/{datasource_id}/audit")
def get_datasource_audit(
    datasource_id: str,
    request: Request,
    limit: int = 50,
    offset: int = 0,
    _principal=Depends(require_permission("panel.read")),
):
    """Paginated audit log for a datasource."""
    from src.core.storage.datasource_audit import DatasourceQueryAuditStore

    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return {"rows": [], "total": 0}
    try:
        from src.core.storage.database import Database

        db = Database()
        store = DatasourceQueryAuditStore()
        with db.read() as conn:
            rows = store.list_paginated(
                conn,
                datasource_id=datasource_id,
                limit=limit,
                offset=offset,
            )
    except Exception as exc:
        logger.warning("读取数据源审计日志失败：%s", exc)
        return {"rows": [], "total": 0}
    return {"rows": rows, "total": len(rows)}
