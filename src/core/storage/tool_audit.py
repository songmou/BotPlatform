"""Persistent tool audit log backed by SQLite."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.core.storage.tenants import TenantRegistry


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    ) -> None:
        with self.registry.database.transaction() as connection:
            connection.execute(
                "INSERT INTO tool_audit_log"
                " (ts, tenant_id, session_id, agent_id, tool_name, status,"
                "  duration_ms, output_bytes, args_hash, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _utc_now(),
                    tenant_id,
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
    ) -> List[Dict[str, Any]]:
        with self.registry.database.read() as connection:
            if tool_name:
                rows = connection.execute(
                    "SELECT * FROM tool_audit_log"
                    " WHERE tool_name = ?"
                    " ORDER BY ts DESC LIMIT ? OFFSET ?",
                    (tool_name, limit, offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM tool_audit_log"
                    " ORDER BY ts DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [dict(row) for row in rows]

    def count(self, tool_name: Optional[str] = None) -> int:
        with self.registry.database.read() as connection:
            if tool_name:
                row = connection.execute(
                    "SELECT COUNT(*) FROM tool_audit_log WHERE tool_name = ?",
                    (tool_name,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM tool_audit_log"
                ).fetchone()
        return int(row[0]) if row else 0
