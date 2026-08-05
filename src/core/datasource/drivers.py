"""Database driver abstraction with lazy imports for MySQL and PostgreSQL.

Supports: MySQL (PyMySQL) and PostgreSQL (psycopg).
Drivers are not imported at module load — only when connect() is called.
"""

from __future__ import annotations

import importlib.util
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


# (driver_module, user_hint_message)
_DRIVER_HINTS: Dict[str, Tuple[str, str]] = {
    "mysql": (
        "pymysql",
        "未安装 MySQL 驱动，请执行 pip install -r requirements-db.txt 后重启服务",
    ),
    "postgresql": (
        "psycopg",
        "未安装 PostgreSQL 驱动，请执行 pip install -r requirements-db.txt 后重启服务",
    ),
}

# Dialect name used by sqlglot.
_DIALECT_MAP: Dict[str, str] = {
    "mysql": "mysql",
    "postgresql": "postgres",
}

_DEFAULT_PORTS: Dict[str, int] = {
    "mysql": 3306,
    "postgresql": 5432,
}


def driver_availability(engine: str) -> Tuple[bool, str]:
    """Return (available, hint) for the given engine."""
    entry = _DRIVER_HINTS.get(engine)
    if entry is None:
        return False, "不支持的数据库类型：{}".format(engine)
    if importlib.util.find_spec(entry[0]) is None:
        return False, entry[1]
    return True, ""


def dialect_for(engine: str) -> str:
    """Map engine name to sqlglot dialect name."""
    return _DIALECT_MAP.get(engine, engine)


def default_port(engine: str) -> int:
    """Return the default port for a given engine."""
    return _DEFAULT_PORTS.get(engine, 0)


class DatabaseDriver(ABC):
    """Abstract driver for a database engine."""

    engine: str = ""

    @abstractmethod
    def connect(self, cfg: Dict[str, Any], password: str) -> Any:
        """Create a new connection. Returns a driver-native connection object."""
        ...

    @abstractmethod
    def ping(self, conn: Any) -> bool:
        """Check if the connection is still alive."""
        ...

    @abstractmethod
    def close(self, conn: Any) -> None:
        """Close a connection."""
        ...

    def begin_readonly(self, conn: Any) -> None:
        """Set the connection to read-only mode (e.g. START TRANSACTION READ ONLY)."""
        pass

    def set_statement_timeout(self, conn: Any, timeout_seconds: int) -> None:
        """Set a per-statement timeout in seconds, if supported by the driver/engine."""
        pass

    @abstractmethod
    def quote_identifier(self, name: str) -> str:
        """Quote a table/column identifier for the engine."""
        ...

    @abstractmethod
    def fetch_tables(self, conn: Any, database: str) -> List[Dict[str, Any]]:
        """Return [{schema, name, comment, estimated_rows}] for all tables in the database."""
        ...

    @abstractmethod
    def fetch_columns(
        self, conn: Any, database: str, tables: List[Tuple[str, str]]
    ) -> List[Dict[str, Any]]:
        """Return column metadata for the given (schema, name) tables.

        Each dict: {schema, table, name, type, nullable, is_pk, comment, default}
        """
        ...


class MySQLDriver(DatabaseDriver):
    engine = "mysql"

    def connect(self, cfg: Dict[str, Any], password: str) -> Any:
        import pymysql  # type: ignore[import-untyped]

        options = dict(cfg.get("options") or {})
        charset = options.pop("charset", "utf8mb4")
        ssl_mode = options.pop("ssl_mode", "preferred")
        ssl_args: Dict[str, Any] = {}
        if ssl_mode == "required":
            ssl_args["ssl"] = {"ssl": True}
        elif ssl_mode == "disabled":
            ssl_args["ssl"] = None
        connect_timeout = int(cfg.get("connect_timeout_seconds", 5))
        return pymysql.connect(
            host=cfg["host"],
            port=int(cfg.get("port", 3306)),
            user=cfg.get("username", ""),
            password=password,
            database=cfg.get("database", ""),
            charset=charset,
            connect_timeout=connect_timeout,
            read_timeout=int(cfg.get("statement_timeout_seconds", 15)),
            autocommit=True,
            **ssl_args,
            **options,
        )

    def ping(self, conn: Any) -> bool:
        try:
            conn.ping(reconnect=False)
            return True
        except Exception:
            return False

    def close(self, conn: Any) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def begin_readonly(self, conn: Any) -> None:
        with conn.cursor() as cur:
            cur.execute("START TRANSACTION READ ONLY")

    def set_statement_timeout(self, conn: Any, timeout_seconds: int) -> None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SET SESSION MAX_EXECUTION_TIME = %s", (timeout_seconds * 1000,)
                )
        except Exception:
            pass  # user may not have SUPER/privilege

    def quote_identifier(self, name: str) -> str:
        return "`{}`".format(name.replace("`", "``"))

    def fetch_tables(self, conn: Any, database: str) -> List[Dict[str, Any]]:
        tables: List[Dict[str, Any]] = []
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_COMMENT, TABLE_ROWS "
                "FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
                "ORDER BY TABLE_NAME",
                (database,),
            )
            for row in cur.fetchall():
                tables.append(
                    {
                        "schema": row[0],
                        "name": row[1],
                        "comment": row[2] or "",
                        "estimated_rows": row[3] or 0,
                    }
                )
        return tables

    def fetch_columns(
        self, conn: Any, database: str, tables: List[Tuple[str, str]]
    ) -> List[Dict[str, Any]]:
        if not tables:
            return []
        columns: List[Dict[str, Any]] = []
        placeholders = ",".join(["(%s,%s)"] * len(tables))
        params: List[Any] = []
        for schema, name in tables:
            params.extend([schema or database, name])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, "
                "IS_NULLABLE, COLUMN_KEY, COLUMN_COMMENT, COLUMN_DEFAULT, ORDINAL_POSITION "
                "FROM information_schema.COLUMNS "
                "WHERE (TABLE_SCHEMA, TABLE_NAME) IN ({}) "
                "ORDER BY ORDINAL_POSITION".format(placeholders),
                params,
            )
            for row in cur.fetchall():
                columns.append(
                    {
                        "schema": row[0],
                        "table": row[1],
                        "name": row[2],
                        "type": row[3],
                        "nullable": row[4] == "YES",
                        "is_pk": row[5] == "PRI",
                        "comment": row[6] or "",
                        "default": str(row[7]) if row[7] is not None else None,
                    }
                )
        return columns


class PostgresDriver(DatabaseDriver):
    engine = "postgresql"

    def connect(self, cfg: Dict[str, Any], password: str) -> Any:
        import psycopg  # type: ignore[import-untyped]

        options = dict(cfg.get("options") or {})
        ssl_mode = options.pop("ssl_mode", "prefer")
        connect_timeout = int(cfg.get("connect_timeout_seconds", 5))
        dsn_params: Dict[str, Any] = {
            "host": cfg["host"],
            "port": int(cfg.get("port", 5432)),
            "user": cfg.get("username", ""),
            "password": password,
            "dbname": cfg.get("database", ""),
            "connect_timeout": connect_timeout,
        }
        if ssl_mode == "required":
            dsn_params["sslmode"] = "require"
        elif ssl_mode == "disabled":
            dsn_params["sslmode"] = "disable"
        dsn_params.update(options)
        return psycopg.connect(**dsn_params)

    def ping(self, conn: Any) -> bool:
        import psycopg

        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except psycopg.OperationalError:
            return False
        except Exception:
            return False

    def close(self, conn: Any) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def begin_readonly(self, conn: Any) -> None:
        conn.execute("BEGIN READ ONLY")

    def set_statement_timeout(self, conn: Any, timeout_seconds: int) -> None:
        try:
            conn.execute(
                "SET LOCAL statement_timeout = '{}s'".format(timeout_seconds)
            )
        except Exception:
            pass

    def quote_identifier(self, name: str) -> str:
        return '"{}"'.format(name.replace('"', '""'))

    def fetch_tables(self, conn: Any, database: str) -> List[Dict[str, Any]]:
        tables: List[Dict[str, Any]] = []
        with conn.cursor() as cur:
            cur.execute(
                "SELECT n.nspname, c.relname, "
                "COALESCE(obj_description(c.oid, 'pg_class'), '') AS comment, "
                "COALESCE("
                "  (SELECT s.n_live_tup FROM pg_stat_user_tables s "
                "   WHERE s.schemaname = n.nspname AND s.relname = c.relname), 0"
                ") AS estimated_rows "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND c.relkind IN ('r', 'p') "
                "ORDER BY c.relname"
            )
            for row in cur.fetchall():
                tables.append(
                    {
                        "schema": row[0],
                        "name": row[1],
                        "comment": row[2] or "",
                        "estimated_rows": row[3] or 0,
                    }
                )
        return tables

    def fetch_columns(
        self, conn: Any, database: str, tables: List[Tuple[str, str]]
    ) -> List[Dict[str, Any]]:
        if not tables:
            return []
        columns: List[Dict[str, Any]] = []
        placeholders = ",".join(["(%s,%s)"] * len(tables))
        params: List[Any] = []
        for schema, name in tables:
            params.extend([schema or "public", name])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.table_schema, c.table_name, c.column_name, c.data_type, "
                "c.is_nullable, "
                "CASE WHEN pk.column_name IS NOT NULL THEN 'YES' ELSE 'NO' END AS is_pk, "
                "COALESCE(pg_catalog.col_description("
                "  to_regclass(c.table_schema||'.'||c.table_name), c.ordinal_position"
                "), '') AS comment, "
                "CASE WHEN c.column_default IS NOT NULL "
                "  THEN pg_get_expr(pg_node_tree(c.column_default), 0) ELSE NULL END AS default_value, "
                "c.ordinal_position "
                "FROM information_schema.columns c "
                "LEFT JOIN ("
                "  SELECT ku.table_schema, ku.table_name, ku.column_name "
                "  FROM information_schema.table_constraints tc "
                "  JOIN information_schema.key_column_usage ku "
                "    ON tc.constraint_name = ku.constraint_name "
                "    AND tc.table_schema = ku.table_schema "
                "  WHERE tc.constraint_type = 'PRIMARY KEY'"
                ") pk ON c.table_schema = pk.table_schema "
                "  AND c.table_name = pk.table_name AND c.column_name = pk.column_name "
                "WHERE (c.table_schema, c.table_name) IN ({}) "
                "ORDER BY c.ordinal_position".format(placeholders),
                params,
            )
            for row in cur.fetchall():
                columns.append(
                    {
                        "schema": row[0],
                        "table": row[1],
                        "name": row[2],
                        "type": row[3],
                        "nullable": row[4] == "YES",
                        "is_pk": row[5] == "YES",
                        "comment": row[6] or "",
                        "default": row[7] if row[7] is not None else None,
                    }
                )
        return columns


def get_driver(engine: str) -> DatabaseDriver:
    """Return the driver instance for the given engine.

    Raises DataSourceError if the engine is not supported.
    """
    from src.core.datasource.errors import DataSourceError

    if engine == "mysql":
        return MySQLDriver()
    elif engine == "postgresql":
        return PostgresDriver()
    else:
        raise DataSourceError(
            "不支持的数据库类型：{}，仅支持 mysql、postgresql".format(engine)
        )
