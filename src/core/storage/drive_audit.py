"""Persistent audit log for network drive file operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.core.storage.tenants import TenantRegistry


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _like_pattern(fragment: str) -> str:
    """Build a substring LIKE pattern with %/_/\\ escaped literally."""
    escaped = (
        fragment.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return "%" + escaped + "%"


class DriveAuditStore:
    """Record and query drive operation audit entries."""

    def __init__(self, registry: TenantRegistry) -> None:
        self.registry = registry

    def record(
        self,
        operator: str,
        source: str,
        scope: str,
        tenant_id: Optional[str],
        action: str,
        path: str,
        target_path: Optional[str] = None,
        size_bytes: int = 0,
        status: str = "成功",
        error: Optional[str] = None,
    ) -> None:
        with self.registry.database.transaction() as connection:
            connection.execute(
                "INSERT INTO drive_audit_log"
                " (ts, operator, source, scope, tenant_id, action, path,"
                "  target_path, size_bytes, status, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _utc_now(),
                    operator,
                    source,
                    scope,
                    tenant_id,
                    action,
                    path,
                    target_path,
                    size_bytes,
                    status,
                    error,
                ),
            )

    @staticmethod
    def _filters(
        scope: Optional[str],
        tenant_id: Optional[str],
        action: Optional[str],
        operator: Optional[str],
    ):
        conditions: List[str] = []
        params: List[Any] = []
        if scope:
            conditions.append("scope = ?")
            params.append(scope)
        if tenant_id:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if operator:
            conditions.append("operator LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(operator))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return where, params

    def list_recent(
        self,
        limit: int = 50,
        offset: int = 0,
        scope: Optional[str] = None,
        tenant_id: Optional[str] = None,
        action: Optional[str] = None,
        operator: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        where, params = self._filters(scope, tenant_id, action, operator)
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM drive_audit_log"
                + where
                + " ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def count(
        self,
        scope: Optional[str] = None,
        tenant_id: Optional[str] = None,
        action: Optional[str] = None,
        operator: Optional[str] = None,
    ) -> int:
        where, params = self._filters(scope, tenant_id, action, operator)
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM drive_audit_log" + where,
                params,
            ).fetchone()
        return int(row[0]) if row else 0
