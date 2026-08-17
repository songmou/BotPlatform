"""Model usage, cost, quality, feedback, and budget management endpoints."""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from src.api.deps import get_model_analytics_store, require_permission
from src.api.schemas import ModelBudgetIn, ModelFeedbackIn
from src.core.storage.model_analytics import MODEL_RUN_SOURCES


router = APIRouter(tags=["model-analytics"])


def _store(request: Request):
    store = get_model_analytics_store(request)
    if store is None:
        raise HTTPException(status_code=503, detail="模型分析服务未启用")
    return store


def _range(
    date_from: Optional[datetime],
    date_to: Optional[datetime],
) -> tuple[str, str]:
    end = date_to or datetime.now(timezone.utc)
    start = date_from or (end - timedelta(days=7))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if end <= start:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")
    if end - start > timedelta(days=366):
        raise HTTPException(status_code=400, detail="单次查询时间范围不能超过 366 天")
    return start.isoformat(), end.isoformat()


def _filters(
    *,
    date_from: Optional[datetime],
    date_to: Optional[datetime],
    tenant_id: Optional[str],
    profile_id: Optional[str],
    agent_id: Optional[str],
    source: Optional[str],
    status: Optional[str],
) -> Dict[str, Any]:
    start, end = _range(date_from, date_to)
    if source and source not in MODEL_RUN_SOURCES:
        raise HTTPException(status_code=400, detail="调用来源无效")
    if status and status not in {
        "running",
        "success",
        "partial",
        "failed",
        "cancelled",
    }:
        raise HTTPException(status_code=400, detail="运行状态无效")
    return {
        "date_from": start,
        "date_to": end,
        "tenant_id": tenant_id,
        "profile_id": profile_id,
        "agent_id": agent_id,
        "source": source,
        "status": status,
    }


def _common_filters(
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    tenant_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    return _filters(
        date_from=date_from,
        date_to=date_to,
        tenant_id=tenant_id,
        profile_id=profile_id,
        agent_id=agent_id,
        source=source,
        status=status,
    )


@router.get("/api/model-analytics/overview")
def overview(
    request: Request,
    filters: Dict[str, Any] = Depends(_common_filters),
    principal=Depends(require_permission("model_analytics.read")),
):
    return _store(request).overview(**filters)


@router.get("/api/model-analytics/timeseries")
def timeseries(
    request: Request,
    bucket: str = Query(default="day", pattern="^(hour|day)$"),
    filters: Dict[str, Any] = Depends(_common_filters),
    principal=Depends(require_permission("model_analytics.read")),
):
    return {
        "currency": _store(request).currency,
        "bucket": bucket,
        "items": _store(request).timeseries(bucket=bucket, **filters),
    }


@router.get("/api/model-analytics/breakdown")
def breakdown(
    request: Request,
    dimension: str = Query(pattern="^(profile|tenant|agent|source)$"),
    filters: Dict[str, Any] = Depends(_common_filters),
    principal=Depends(require_permission("model_analytics.read")),
):
    return {
        "currency": _store(request).currency,
        "dimension": dimension,
        "items": _store(request).breakdown(dimension, **filters),
    }


@router.get("/api/model-analytics/runs")
def runs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    filters: Dict[str, Any] = Depends(_common_filters),
    principal=Depends(require_permission("model_analytics.read")),
):
    return {
        "currency": _store(request).currency,
        "items": _store(request).list_runs(limit=limit, offset=offset, **filters),
        "limit": limit,
        "offset": offset,
    }


@router.get("/api/model-analytics/runs/{run_id}")
def run_detail(
    run_id: str,
    request: Request,
    principal=Depends(require_permission("model_analytics.read")),
):
    result = _store(request).run_detail(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="模型运行记录不存在")
    return result


@router.get("/api/model-analytics/export.csv")
def export_csv(
    request: Request,
    filters: Dict[str, Any] = Depends(_common_filters),
    principal=Depends(require_permission("model_analytics.read")),
):
    items = _store(request).list_runs(limit=10_000, offset=0, **filters)
    output = io.StringIO()
    fieldnames = [
        "run_id",
        "tenant_id",
        "source",
        "agent_id",
        "status",
        "started_at",
        "finished_at",
        "call_count",
        "input_tokens",
        "output_tokens",
        "cost_micros",
        "unpriced_calls",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(items)
    payload = "\ufeff" + output.getvalue()
    return Response(
        payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="model-analytics.csv"'},
    )


@router.put("/api/model-runs/{run_id}/feedback")
def put_feedback(
    run_id: str,
    body: ModelFeedbackIn,
    request: Request,
    principal=Depends(require_permission("model_analytics.read")),
):
    try:
        return _store(request).put_feedback(
            run_id,
            actor_type="admin",
            actor_ref=str(principal.user.user_id),
            rating=body.rating,
            reasons=body.reasons,
            comment=body.comment,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/model-budgets")
def list_budgets(
    request: Request,
    period: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    principal=Depends(require_permission("model_analytics.read")),
):
    store = _store(request)
    store.refresh_budget_alerts()
    return {
        "currency": store.currency,
        "items": store.list_budgets(period),
        "alerts": store.list_alerts(),
    }


@router.post("/api/model-budgets", status_code=201)
def create_budget(
    body: ModelBudgetIn,
    request: Request,
    principal=Depends(require_permission("model_analytics.manage")),
):
    try:
        return _store(request).save_budget(budget_id=None, **body.model_dump())
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="该范围已经配置预算")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/api/model-budgets/{budget_id}")
def update_budget(
    budget_id: int,
    body: ModelBudgetIn,
    request: Request,
    principal=Depends(require_permission("model_analytics.manage")),
):
    try:
        return _store(request).save_budget(
            budget_id=budget_id, **body.model_dump()
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="该范围已经配置预算")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/api/model-budgets/{budget_id}")
def delete_budget(
    budget_id: int,
    request: Request,
    principal=Depends(require_permission("model_analytics.manage")),
):
    try:
        _store(request).delete_budget(budget_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "ok"}
