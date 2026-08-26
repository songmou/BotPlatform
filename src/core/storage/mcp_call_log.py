"""Persistent MCP tool call log backed by SQLite.

Records each invocation of an MCP tool (both manual panel calls and agent
runtime calls) with full input/output payloads, truncated when oversized.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.core.storage.tenants import TenantRegistry


# Single payload (input or output) is capped at this many UTF-8 bytes before
# being truncated with a marker. Keeps very large tool outputs (e.g. base64
# images) from bloating the database.
_MAX_PAYLOAD_BYTES = 65536
_TRUNCATION_MARKER = "\n…[已截断]"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_truncated(obj: Any) -> Tuple[Optional[str], int]:
    """Serialize ``obj`` to a JSON text, truncating above the byte cap.

    Returns ``(text, truncated_flag)`` where ``truncated_flag`` is 1 when the
    serialized form exceeded the cap and was cut, 0 otherwise. ``obj`` of
    ``None`` yields ``(None, 0)``.
    """
    if obj is None:
        return None, 0
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        # Fall back to repr for non-JSON-serializable results.
        text = json.dumps(repr(obj), ensure_ascii=False)
    encoded = text.encode("utf-8")
    if len(encoded) <= _MAX_PAYLOAD_BYTES:
        return text, 0
    # Truncate on a UTF-8 boundary to avoid mojibake.
    truncated_bytes = encoded[:_MAX_PAYLOAD_BYTES]
    truncated_text = truncated_bytes.decode("utf-8", errors="ignore")
    return truncated_text + _TRUNCATION_MARKER, 1


class McpCallLogStore:
    """Record and query MCP tool invocation logs."""

    def __init__(self, registry: TenantRegistry) -> None:
        self.registry = registry

    def record(
        self,
        *,
        server_id: str,
        tool_name: str,
        source: str,
        status: str,
        duration_ms: int,
        arguments: Any = None,
        result: Any = None,
        error: Optional[str] = None,
        tenant_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> None:
        input_json, input_truncated = _serialize_truncated(arguments)
        output_json, output_truncated = _serialize_truncated(result)
        with self.registry.database.transaction() as connection:
            connection.execute(
                "INSERT INTO mcp_call_log"
                " (ts, server_id, tool_name, source, status, duration_ms,"
                "  input_json, output_json, input_truncated, output_truncated,"
                "  error, tenant_id, agent_id, session_id, user_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _utc_now(),
                    server_id,
                    tool_name,
                    source,
                    status,
                    duration_ms,
                    input_json,
                    output_json,
                    input_truncated,
                    output_truncated,
                    error,
                    tenant_id,
                    agent_id,
                    session_id,
                    user_id,
                ),
            )

    def list_by_server(
        self,
        server_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        tool: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conditions = ["server_id = ?"]
        params: List[Any] = [server_id]
        if tool:
            conditions.append("tool_name = ?")
            params.append(tool)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(conditions)
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM mcp_call_log"
                + where
                + " ORDER BY ts DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_by_server(
        self,
        server_id: str,
        *,
        tool: Optional[str] = None,
        source: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        conditions = ["server_id = ?"]
        params: List[Any] = [server_id]
        if tool:
            conditions.append("tool_name = ?")
            params.append(tool)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(conditions)
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM mcp_call_log" + where,
                params,
            ).fetchone()
        return int(row[0]) if row else 0
