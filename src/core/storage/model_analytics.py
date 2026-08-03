"""Persistent model usage, cost, quality, and budget analytics."""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.core.config.loader import ModelProfile, ProjectConfig
from src.core.modeling import (
    ModelCallContext,
    ModelError,
    ModelIdentity,
    ModelUsage,
)
from src.core.storage.tenants import TenantRegistry


FEEDBACK_REASONS = {
    "答非所问",
    "事实错误",
    "格式表达",
    "工具执行失败",
    "响应过慢",
    "其他",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _price_micros(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    return int(
        (Decimal(value) * Decimal(1_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _percentile(values: Iterable[int], percentile: float) -> Optional[int]:
    ordered = sorted(values)
    if not ordered:
        return None
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return int(ordered[index])


class ModelAnalyticsStore:
    """Record metadata-only model telemetry and expose aggregate queries."""

    def __init__(self, registry: TenantRegistry, config: ProjectConfig) -> None:
        self.registry = registry
        self.config = config
        self.timezone = ZoneInfo(config.app.timezone)

    @property
    def currency(self) -> str:
        first = next(iter(self.config.models.values()), None)
        return first.billing_currency if first is not None else "CNY"

    def start_run(
        self,
        *,
        run_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[int] = None,
        source: str = "internal",
        agent_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> str:
        if source not in {"wechat", "web", "schedule", "internal"}:
            source = "internal"
        identifier = run_id or str(uuid.uuid4())
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO model_runs("
                "run_id, tenant_id, user_id, source, agent_id, conversation_id, "
                "status, started_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
                (
                    identifier,
                    tenant_id,
                    user_id,
                    source,
                    agent_id,
                    conversation_id,
                    _utc_now(),
                ),
            )
        return identifier

    def finish_run(
        self,
        run_id: str,
        status: str,
        *,
        response_event_id: Optional[int] = None,
        error_category: Optional[str] = None,
    ) -> None:
        if status not in {"success", "partial", "failed", "cancelled"}:
            status = "failed"
        with self.registry.database.transaction() as connection:
            connection.execute(
                "UPDATE model_runs SET status=?, finished_at=?, "
                "response_event_id=COALESCE(?, response_event_id), error_category=? "
                "WHERE run_id=?",
                (status, _utc_now(), response_event_id, error_category, run_id),
            )

    def record_model_call(
        self,
        identity: ModelIdentity,
        actual_model: str,
        status: str,
        duration_seconds: float,
        usage: Optional[ModelUsage],
        tool_call_count: int,
        request_id: Optional[str],
        context: ModelCallContext,
        finish_reason: Optional[str] = None,
        first_token_seconds: Optional[float] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        run_id = context.run_id or str(uuid.uuid4())
        call_status = {"成功": "success", "失败": "failed", "取消": "cancelled"}.get(
            status, status
        )
        if call_status not in {"success", "failed", "cancelled"}:
            call_status = "failed"
        profile = self.config.models.get(identity.profile_id)
        currency = profile.billing_currency if profile else self.currency
        prices, cost_micros, cost_status = self._cost(profile, usage)
        now = datetime.now(timezone.utc)
        started = now - timedelta(seconds=max(0.0, duration_seconds))
        error_category = self._error_category(error)
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO model_runs("
                "run_id, tenant_id, user_id, source, agent_id, conversation_id, "
                "status, started_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
                (
                    run_id,
                    context.tenant_id,
                    context.user_id,
                    context.source
                    if context.source in {"wechat", "web", "schedule", "internal"}
                    else "internal",
                    context.agent_id,
                    context.conversation_id,
                    started.isoformat(),
                ),
            )
            previous = connection.execute(
                "SELECT profile_id, status, attempt FROM model_calls "
                "WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            sequence_row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM model_calls WHERE run_id=?",
                (run_id,),
            ).fetchone()
            sequence = int(sequence_row[0])
            is_retry = bool(
                previous
                and previous["status"] == "failed"
                and previous["profile_id"] == identity.profile_id
            )
            is_fallback = bool(
                previous
                and previous["status"] == "failed"
                and previous["profile_id"] != identity.profile_id
            )
            attempt = int(previous["attempt"]) + 1 if is_retry else 1
            values = (
                run_id,
                sequence,
                context.operation or "answer",
                identity.profile_id,
                identity.provider,
                identity.configured_model,
                actual_model,
                attempt,
                int(is_retry),
                int(is_fallback),
                call_status,
                started.isoformat(),
                now.isoformat(),
                max(0, int(round(duration_seconds * 1000))),
                (
                    max(0, int(round(first_token_seconds * 1000)))
                    if first_token_seconds is not None
                    else None
                ),
                error.status_code if isinstance(error, ModelError) else None,
                error_category,
                int(bool(isinstance(error, ModelError) and error.retryable)),
                finish_reason,
                max(0, int(tool_call_count)),
                request_id,
                usage.input_tokens if usage else None,
                usage.cached_input_tokens if usage else None,
                usage.uncached_input_tokens if usage else None,
                usage.output_tokens if usage else None,
                usage.reasoning_output_tokens if usage else None,
                usage.total_tokens if usage else None,
                currency,
                *prices,
                cost_micros,
                cost_status,
            )
            connection.execute(
                "INSERT INTO model_calls("
                "run_id, sequence, operation, profile_id, provider, configured_model,"
                " actual_model, attempt, is_retry, is_fallback, status, started_at,"
                " finished_at, duration_ms, first_token_ms, http_status, error_category,"
                " retryable, finish_reason, tool_call_count, provider_request_id,"
                " input_tokens, cached_input_tokens, uncached_input_tokens, output_tokens,"
                " reasoning_output_tokens, total_tokens, currency,"
                " input_price_micros_per_million, cached_input_price_micros_per_million,"
                " output_price_micros_per_million,"
                " reasoning_output_price_micros_per_million, cost_micros, cost_status"
                ") VALUES ({})".format(",".join("?" for _ in values)),
                values,
            )
            run_status = (
                "success"
                if call_status == "success"
                else "cancelled"
                if call_status == "cancelled"
                else "failed"
            )
            connection.execute(
                "UPDATE model_runs SET status=?, finished_at=?, error_category=? "
                "WHERE run_id=?",
                (run_status, now.isoformat(), error_category, run_id),
            )
        if cost_micros is not None and cost_micros >= 0:
            self.refresh_budget_alerts(now)

    @staticmethod
    def _error_category(error: Optional[BaseException]) -> Optional[str]:
        if error is None:
            return None
        if isinstance(error, ModelError):
            if error.status_code:
                return "http_{}".format(error.status_code)
            return "model_retryable" if error.retryable else "model_error"
        if isinstance(error, GeneratorExit):
            return "client_cancelled"
        return error.__class__.__name__[:80]

    def _cost(
        self, profile: Optional[ModelProfile], usage: Optional[ModelUsage]
    ) -> Tuple[Tuple[Optional[int], Optional[int], Optional[int], Optional[int]], Optional[int], str]:
        pricing = profile.pricing if profile else None
        if usage is None or usage.input_tokens is None or usage.output_tokens is None:
            return (None, None, None, None), None, "usage_unknown"
        if pricing is None:
            return (None, None, None, None), None, "unpriced"
        input_price = _price_micros(pricing.input_per_million)
        cached_price = _price_micros(
            pricing.cached_input_per_million or pricing.input_per_million
        )
        output_price = _price_micros(pricing.output_per_million)
        reasoning_price = _price_micros(
            pricing.reasoning_output_per_million or pricing.output_per_million
        )
        assert None not in (input_price, cached_price, output_price, reasoning_price)
        cached_tokens = usage.cached_input_tokens or 0
        uncached_tokens = usage.uncached_input_tokens
        if uncached_tokens is None:
            uncached_tokens = max(0, usage.input_tokens - cached_tokens)
        reasoning_tokens = usage.reasoning_output_tokens or 0
        normal_output_tokens = max(0, usage.output_tokens - reasoning_tokens)
        numerator = (
            Decimal(uncached_tokens) * Decimal(input_price)
            + Decimal(cached_tokens) * Decimal(cached_price)
            + Decimal(normal_output_tokens) * Decimal(output_price)
            + Decimal(reasoning_tokens) * Decimal(reasoning_price)
        )
        cost = int(
            (numerator / Decimal(1_000_000)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        status = "free" if all(
            price == 0
            for price in (input_price, cached_price, output_price, reasoning_price)
        ) else "priced"
        return (
            input_price,
            cached_price,
            output_price,
            reasoning_price,
        ), cost, status

    @staticmethod
    def _filters(
        *,
        date_from: str,
        date_to: str,
        tenant_id: Optional[str] = None,
        profile_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
        call_alias: str = "c",
        run_alias: str = "r",
    ) -> Tuple[str, List[Any]]:
        clauses = [
            "{}.started_at >= ?".format(run_alias),
            "{}.started_at < ?".format(run_alias),
        ]
        params: List[Any] = [date_from, date_to]
        for field, value in (
            ("tenant_id", tenant_id),
            ("agent_id", agent_id),
            ("source", source),
            ("status", status),
        ):
            if value:
                clauses.append("{}.{} = ?".format(run_alias, field))
                params.append(value)
        if profile_id:
            clauses.append("{}.profile_id = ?".format(call_alias))
            params.append(profile_id)
        return " AND ".join(clauses), params

    def overview(self, **filters: Any) -> Dict[str, Any]:
        where, params = self._filters(**filters)
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT c.*, r.status AS run_status FROM model_calls c "
                "JOIN model_runs r ON r.run_id=c.run_id WHERE " + where,
                params,
            ).fetchall()
            run_count = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT r.run_id) FROM model_calls c "
                    "JOIN model_runs r ON r.run_id=c.run_id WHERE " + where,
                    params,
                ).fetchone()[0]
            )
            feedback = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN f.rating='good' THEN 1 ELSE 0 END) AS good "
                "FROM model_feedback f WHERE f.run_id IN ("
                "SELECT DISTINCT r.run_id FROM model_calls c "
                "JOIN model_runs r ON r.run_id=c.run_id WHERE " + where + ")",
                params,
            ).fetchone()
            tool_failures = int(
                connection.execute(
                    "SELECT COUNT(*) FROM tool_audit_log t "
                    "WHERE t.session_id IN (SELECT DISTINCT r.run_id FROM model_calls c "
                    "JOIN model_runs r ON r.run_id=c.run_id WHERE " + where + ") "
                    "AND t.status <> '成功'",
                    params,
                ).fetchone()[0]
            )
        calls = [dict(row) for row in rows]
        call_count = len(calls)
        success_count = sum(row["status"] == "success" for row in calls)
        retry_count = sum(bool(row["is_retry"]) for row in calls)
        fallback_count = sum(bool(row["is_fallback"]) for row in calls)
        truncated_count = sum(row["finish_reason"] == "length" for row in calls)
        total_feedback = int(feedback["total"] or 0)
        good_feedback = int(feedback["good"] or 0)
        return {
            "currency": self.currency,
            "run_count": run_count,
            "call_count": call_count,
            "input_tokens": sum(row["input_tokens"] or 0 for row in calls),
            "cached_input_tokens": sum(
                row["cached_input_tokens"] or 0 for row in calls
            ),
            "output_tokens": sum(row["output_tokens"] or 0 for row in calls),
            "cost_micros": sum(row["cost_micros"] or 0 for row in calls),
            "unpriced_calls": sum(
                row["cost_status"] in {"unpriced", "usage_unknown"} for row in calls
            ),
            "success_rate": success_count / call_count if call_count else None,
            "retry_rate": retry_count / call_count if call_count else None,
            "fallback_rate": fallback_count / call_count if call_count else None,
            "truncation_rate": truncated_count / call_count if call_count else None,
            "duration_p50_ms": _percentile(
                (row["duration_ms"] for row in calls), 0.5
            ),
            "duration_p95_ms": _percentile(
                (row["duration_ms"] for row in calls), 0.95
            ),
            "feedback_count": total_feedback,
            "feedback_coverage": (
                total_feedback / run_count if run_count else None
            ),
            "positive_rate": (
                good_feedback / total_feedback if total_feedback else None
            ),
            "tool_failure_count": tool_failures,
        }

    def timeseries(self, bucket: str = "day", **filters: Any) -> List[Dict[str, Any]]:
        where, params = self._filters(**filters)
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT c.started_at, c.status, c.input_tokens, c.output_tokens,"
                " c.cost_micros FROM model_calls c JOIN model_runs r "
                "ON r.run_id=c.run_id WHERE " + where + " ORDER BY c.started_at",
                params,
            ).fetchall()
        grouped: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            local = datetime.fromisoformat(str(row["started_at"])).astimezone(
                self.timezone
            )
            key = (
                local.strftime("%Y-%m-%dT%H:00:00%z")
                if bucket == "hour"
                else local.strftime("%Y-%m-%d")
            )
            item = grouped.setdefault(
                key,
                {
                    "bucket": key,
                    "call_count": 0,
                    "success_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_micros": 0,
                },
            )
            item["call_count"] += 1
            item["success_count"] += int(row["status"] == "success")
            item["input_tokens"] += row["input_tokens"] or 0
            item["output_tokens"] += row["output_tokens"] or 0
            item["cost_micros"] += row["cost_micros"] or 0
        return list(grouped.values())

    def breakdown(
        self, dimension: str, **filters: Any
    ) -> List[Dict[str, Any]]:
        columns = {
            "profile": "c.profile_id",
            "tenant": "COALESCE(r.tenant_id, '系统')",
            "agent": "COALESCE(r.agent_id, '未标记')",
            "source": "r.source",
        }
        column = columns[dimension]
        where, params = self._filters(**filters)
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT {} AS name, COUNT(*) AS call_count, "
                "SUM(CASE WHEN c.status='success' THEN 1 ELSE 0 END) AS success_count,"
                " SUM(COALESCE(c.input_tokens,0)) AS input_tokens,"
                " SUM(COALESCE(c.output_tokens,0)) AS output_tokens,"
                " SUM(COALESCE(c.cost_micros,0)) AS cost_micros,"
                " SUM(CASE WHEN c.cost_status IN ('unpriced','usage_unknown') "
                "THEN 1 ELSE 0 END) AS unpriced_calls "
                "FROM model_calls c JOIN model_runs r ON r.run_id=c.run_id "
                "WHERE {} GROUP BY {} ORDER BY cost_micros DESC, call_count DESC".format(
                    column, where, column
                ),
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_runs(
        self, limit: int = 50, offset: int = 0, **filters: Any
    ) -> List[Dict[str, Any]]:
        where, params = self._filters(**filters)
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT r.*, COUNT(c.call_id) AS call_count,"
                " SUM(COALESCE(c.input_tokens,0)) AS input_tokens,"
                " SUM(COALESCE(c.output_tokens,0)) AS output_tokens,"
                " SUM(COALESCE(c.cost_micros,0)) AS cost_micros,"
                " SUM(CASE WHEN c.cost_status IN ('unpriced','usage_unknown') "
                "THEN 1 ELSE 0 END) AS unpriced_calls "
                "FROM model_runs r JOIN model_calls c ON c.run_id=r.run_id "
                "WHERE {} GROUP BY r.run_id ORDER BY r.started_at DESC LIMIT ? OFFSET ?".format(
                    where
                ),
                [*params, limit, offset],
            ).fetchall()
        return [dict(row) for row in rows]

    def run_detail(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.registry.database.read() as connection:
            run = connection.execute(
                "SELECT * FROM model_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                return None
            calls = connection.execute(
                "SELECT * FROM model_calls WHERE run_id=? ORDER BY sequence",
                (run_id,),
            ).fetchall()
            feedback = connection.execute(
                "SELECT rating, reasons_json, comment, actor_type, created_at, updated_at "
                "FROM model_feedback WHERE run_id=? ORDER BY updated_at DESC",
                (run_id,),
            ).fetchall()
        result = dict(run)
        result["calls"] = [dict(row) for row in calls]
        result["feedback"] = [
            {
                **dict(row),
                "reasons": json.loads(row["reasons_json"]),
            }
            for row in feedback
        ]
        return result

    def put_feedback(
        self,
        run_id: str,
        *,
        actor_type: str,
        actor_ref: str,
        rating: str,
        reasons: List[str],
        comment: str,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if rating not in {"good", "bad"}:
            raise ValueError("评价只能是好评或差评")
        if actor_type not in {"tenant", "admin"}:
            raise ValueError("评价者类型无效")
        if any(reason not in FEEDBACK_REASONS for reason in reasons):
            raise ValueError("包含未知的差评原因")
        if len(comment) > 500:
            raise ValueError("评价备注不能超过 500 字")
        now = _utc_now()
        with self.registry.database.transaction(immediate=True) as connection:
            run = connection.execute(
                "SELECT tenant_id FROM model_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise LookupError("模型运行记录不存在")
            if tenant_id is not None and run["tenant_id"] != tenant_id:
                raise PermissionError("不能评价其他租户的回答")
            connection.execute(
                "INSERT INTO model_feedback("
                "run_id, actor_type, actor_ref, rating, reasons_json, comment,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, actor_type, actor_ref) DO UPDATE SET "
                "rating=excluded.rating, reasons_json=excluded.reasons_json,"
                "comment=excluded.comment, updated_at=excluded.updated_at",
                (
                    run_id,
                    actor_type,
                    actor_ref,
                    rating,
                    json.dumps(reasons, ensure_ascii=False, separators=(",", ":")),
                    comment.strip(),
                    now,
                    now,
                ),
            )
        return {
            "run_id": run_id,
            "rating": rating,
            "reasons": reasons,
            "comment": comment.strip(),
        }

    def latest_successful_run(self, tenant_id: str) -> Optional[str]:
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT run_id FROM model_runs WHERE tenant_id=? AND status='success' "
                "ORDER BY finished_at DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
        return str(row["run_id"]) if row else None

    def list_budgets(self, period: Optional[str] = None) -> List[Dict[str, Any]]:
        period = period or datetime.now(self.timezone).strftime("%Y-%m")
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM model_budgets ORDER BY scope_type, scope_id"
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            spent = self._budget_spend(item, period)
            item["period"] = period
            item["spent_micros"] = spent
            item["usage_ratio"] = spent / item["monthly_limit_micros"]
            results.append(item)
        return results

    def save_budget(
        self,
        *,
        budget_id: Optional[int],
        scope_type: str,
        scope_id: str,
        monthly_limit_micros: int,
        enabled: bool,
    ) -> Dict[str, Any]:
        if scope_type not in {"global", "tenant", "profile", "agent"}:
            raise ValueError("预算范围无效")
        scope_id = scope_id.strip()
        if scope_type == "global":
            scope_id = ""
        elif not scope_id:
            raise ValueError("非全局预算必须指定范围 ID")
        if monthly_limit_micros <= 0:
            raise ValueError("月度预算必须大于 0")
        now = _utc_now()
        with self.registry.database.transaction(immediate=True) as connection:
            if budget_id is None:
                cursor = connection.execute(
                    "INSERT INTO model_budgets("
                    "scope_type, scope_id, monthly_limit_micros, currency, enabled,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        scope_type,
                        scope_id,
                        monthly_limit_micros,
                        self.currency,
                        int(enabled),
                        now,
                        now,
                    ),
                )
                budget_id = int(cursor.lastrowid)
            else:
                cursor = connection.execute(
                    "UPDATE model_budgets SET scope_type=?, scope_id=?,"
                    " monthly_limit_micros=?, currency=?, enabled=?, updated_at=?"
                    " WHERE budget_id=?",
                    (
                        scope_type,
                        scope_id,
                        monthly_limit_micros,
                        self.currency,
                        int(enabled),
                        now,
                        budget_id,
                    ),
                )
                if cursor.rowcount == 0:
                    raise LookupError("预算不存在")
        return next(
            item for item in self.list_budgets() if item["budget_id"] == budget_id
        )

    def delete_budget(self, budget_id: int) -> None:
        with self.registry.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM model_budgets WHERE budget_id=?", (budget_id,)
            )
            if cursor.rowcount == 0:
                raise LookupError("预算不存在")

    def refresh_budget_alerts(self, now: Optional[datetime] = None) -> None:
        local_now = (now or datetime.now(timezone.utc)).astimezone(self.timezone)
        period = local_now.strftime("%Y-%m")
        budgets = self.list_budgets(period)
        with self.registry.database.transaction(immediate=True) as connection:
            for budget in budgets:
                if not budget["enabled"]:
                    continue
                for threshold in (80, 100):
                    if budget["usage_ratio"] * 100 < threshold:
                        continue
                    connection.execute(
                        "INSERT OR IGNORE INTO model_budget_alerts("
                        "budget_id, period, threshold, spent_micros, created_at"
                        ") VALUES (?, ?, ?, ?, ?)",
                        (
                            budget["budget_id"],
                            period,
                            threshold,
                            budget["spent_micros"],
                            _utc_now(),
                        ),
                    )

    def _budget_spend(self, budget: Dict[str, Any], period: str) -> int:
        start_local = datetime.strptime(period + "-01", "%Y-%m-%d").replace(
            tzinfo=self.timezone
        )
        if start_local.month == 12:
            end_local = start_local.replace(
                year=start_local.year + 1, month=1
            )
        else:
            end_local = start_local.replace(month=start_local.month + 1)
        clauses = ["c.started_at>=?", "c.started_at<?"]
        params: List[Any] = [
            start_local.astimezone(timezone.utc).isoformat(),
            end_local.astimezone(timezone.utc).isoformat(),
        ]
        mapping = {
            "tenant": "r.tenant_id",
            "profile": "c.profile_id",
            "agent": "r.agent_id",
        }
        if budget["scope_type"] != "global":
            clauses.append(mapping[budget["scope_type"]] + "=?")
            params.append(budget["scope_id"])
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT SUM(COALESCE(c.cost_micros,0)) FROM model_calls c "
                "JOIN model_runs r ON r.run_id=c.run_id WHERE "
                + " AND ".join(clauses),
                params,
            ).fetchone()
        return int(row[0] or 0)

    def list_alerts(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT a.*, b.scope_type, b.scope_id, b.monthly_limit_micros,"
                " b.currency FROM model_budget_alerts a JOIN model_budgets b "
                "ON b.budget_id=a.budget_id ORDER BY a.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
