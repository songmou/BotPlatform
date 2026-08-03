"""Persistent tool audit log backed by SQLite."""

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


class ToolAuditStore:
    """Record and query tool invocation audit entries."""

    def __init__(self, registry: TenantRegistry) -> None:
        self.registry = registry

    def record(
        self,
        tenant_id: Optional[str],
        session_id: str,
        agent_id: str,
        tool_name: str,
        status: str,
        duration_ms: int,
        output_bytes: int,
        args_hash: Optional[str],
        error: Optional[str],
        user_id: Optional[int] = None,
    ) -> None:
        with self.registry.database.transaction() as connection:
            connection.execute(
                "INSERT INTO tool_audit_log"
                " (ts, tenant_id, user_id, session_id, agent_id, tool_name, status,"
                "  duration_ms, output_bytes, args_hash, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _utc_now(),
                    tenant_id,
                    user_id,
                    session_id or None,
                    agent_id or None,
                    tool_name,
                    status,
                    duration_ms,
                    output_bytes,
                    args_hash,
                    error,
                ),
            )

    def list_recent(
        self,
        limit: int = 50,
        tool_name: Optional[str] = None,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conditions = []
        params: List[Any] = []
        if tool_name:
            conditions.append("tool_name LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(tool_name))
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_audit_log"
                + where
                + " ORDER BY ts DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def count(
        self,
        tool_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        conditions = []
        params: List[Any] = []
        if tool_name:
            conditions.append("tool_name LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(tool_name))
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM tool_audit_log" + where,
                params,
            ).fetchone()
        return int(row[0]) if row else 0
