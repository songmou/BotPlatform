"""Audit store for datasource query operations.

Records every query/execute call with full SQL text, affected tables,
row count, duration, and status for later review.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class DatasourceQueryAuditStore:
    """Persists datasource query audit records to the SQLite database.

    The caller is responsible for opening a Database.transaction()
    context manager and passing the connection to the store methods.
    """

    def record(
        self,
        conn: Any,
        *,
        tenant_id: str = "",
        agent_id: str = "",
        session_id: str = "",
        user_id: int = 0,
        datasource_id: str,
        statement_kind: str,
        sql_text: str,
        tables: str = "",
        row_count: int = 0,
        truncated: bool = False,
        duration_ms: int = 0,
        status: str = "ok",
        error: str = "",
        created_at: str = "",
    ) -> None:
        """Insert a single audit record.

        sql_text is sanitised by replacing long string literals (>32 chars)
        with placeholders before storage.
        """
        import re

        # Sanitise long string literals in SQL to avoid storing sensitive data.
        sanitised = re.sub(
            r"'(?:(?!')(?:[^'\\]|\\.))*'",
            lambda m: "'<redacted {} chars>'" if len(m.group()) > 34 else m.group(),
            sql_text,
        )

        conn.execute(
            "INSERT INTO datasource_query_audit "
            "(tenant_id, agent_id, session_id, user_id, datasource_id, "
            "statement_kind, sql_text, tables, row_count, truncated, "
            "duration_ms, status, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tenant_id,
                agent_id,
                session_id,
                user_id,
                datasource_id,
                statement_kind,
                sanitised,
                tables,
                row_count,
                1 if truncated else 0,
                duration_ms,
                status,
                error,
                created_at,
            ),
        )

    def list_paginated(
        self,
        conn: Any,
        *,
        datasource_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        order_desc: bool = True,
    ) -> list[Dict[str, Any]]:
        """Return a page of audit records."""

        where = ""
        params: list = []
        if datasource_id:
            where = " WHERE datasource_id = ?"
            params.append(datasource_id)
        order = "DESC" if order_desc else "ASC"
        params.extend([limit, offset])
        conn.execute(
            "SELECT audit_id, tenant_id, agent_id, session_id, user_id, "
            "datasource_id, statement_kind, sql_text, tables, row_count, "
            "truncated, duration_ms, status, error, created_at "
            "FROM datasource_query_audit{} "
            "ORDER BY created_at {} "
            "LIMIT ? OFFSET ?".format(where, order),
            params,
        )
        rows = conn.fetchall()
        return [
            {
                "audit_id": r[0],
                "tenant_id": r[1],
                "agent_id": r[2],
                "session_id": r[3],
                "user_id": r[4],
                "datasource_id": r[5],
                "statement_kind": r[6],
                "sql_text": r[7],
                "tables": r[8],
                "row_count": r[9],
                "truncated": bool(r[10]),
                "duration_ms": r[11],
                "status": r[12],
                "error": r[13],
                "created_at": r[14],
            }
            for r in rows
        ]
