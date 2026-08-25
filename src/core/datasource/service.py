"""DataSourceService — facade for datasource management and query execution.

Wires together drivers, connection pools, SQL gateway, introspection, and
prompt injection.  The service is instantiated at app startup and stored on
app.state.datasource_service.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from src.core.datasource.drivers import (
    DatabaseDriver,
    get_driver,
    driver_availability,
    dialect_for,
)
from src.core.datasource.errors import DataSourceError
from src.core.datasource.gateway import compile_readonly, compile_write
from src.core.datasource.introspect import SchemaCache
from src.core.datasource.pool import ConnectionPool

logger = logging.getLogger(__name__)


class DataSourceService:
    """Manages datasource lifecycle: config → pools → query.

    Thread-safe.  Designed to be created once and held on app.state.
    """

    #: Cooldown (seconds) before retrying schema introspection for a
    #: datasource that just failed.  Prevents every chat turn from paying the
    #: full TCP/auth timeout when a database is down.
    SCHEMA_FAILURE_COOLDOWN = 60.0

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._configs: Dict[str, Dict[str, Any]] = {}
        self._drivers: Dict[str, DatabaseDriver] = {}
        self._pools: Dict[str, ConnectionPool] = {}
        self._schema_cache = SchemaCache(ttl_seconds=900)
        # datasource_id -> monotonic deadline until which prompt injection
        # should skip this datasource without attempting a connection.
        self._schema_failures: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Config management
    # ------------------------------------------------------------------

    def reload(self, entries: List[Dict[str, Any]]) -> None:
        """Replace the entire datasource configuration atomically.

        Old connection pools are drained so file descriptors are released.
        """
        new_configs: Dict[str, Dict[str, Any]] = {}
        new_drivers: Dict[str, DatabaseDriver] = {}
        for entry in entries:
            ds_id = entry.get("id")
            if not ds_id:
                continue
            new_configs[ds_id] = dict(entry)
            new_drivers[ds_id] = get_driver(entry.get("engine", ""))
        with self._lock:
            old_pools = self._pools
            self._configs = new_configs
            self._drivers = new_drivers
            self._pools = {}
            self._schema_cache.clear()
            self._schema_failures.clear()
        for pool in old_pools.values():
            try:
                pool.drain()
            except Exception:
                pass

    def get_config(self, datasource_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._configs.get(datasource_id) or {})

    def list_configs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {**cfg, "driver_ready": driver_availability(cfg.get("engine", ""))[0]}
                for cfg in self._configs.values()
            ]

    def _get_driver_and_cfg(
        self, datasource_id: str
    ) -> Tuple[DatabaseDriver, Dict[str, Any]]:
        with self._lock:
            cfg = self._configs.get(datasource_id)
            if not cfg:
                raise DataSourceError("数据源不存在：{}".format(datasource_id))
            if not cfg.get("enabled", True):
                raise DataSourceError("数据源已停用：{}".format(datasource_id))
            driver = self._drivers.get(datasource_id)
            if driver is None:
                raise DataSourceError("数据源驱动不可用：{}".format(datasource_id))
            return driver, dict(cfg)

    def _get_pool(self, datasource_id: str) -> ConnectionPool:
        with self._lock:
            pool = self._pools.get(datasource_id)
            if pool is not None:
                return pool
            driver, cfg = self._get_driver_and_cfg(datasource_id)
            password = cfg.get("password", "")
            pool_size = int(cfg.get("pool_size", 3))

            def factory():
                return driver.connect(cfg, password)

            pool = ConnectionPool(
                max_size=pool_size, factory=factory, ping=driver.ping
            )
            self._pools[datasource_id] = pool
            return pool

    # ------------------------------------------------------------------
    # Test connection
    # ------------------------------------------------------------------

    def test_connection(
        self, entry: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Test a datasource configuration (saved or draft).

        Returns {ok, latency_ms, version, error}.
        Does NOT require sqlglot.
        """
        engine = entry.get("engine", "")
        available, hint = driver_availability(engine)
        if not available:
            return {"ok": False, "latency_ms": 0, "version": "", "error": hint}

        try:
            driver = get_driver(engine)
            t0 = time.time()
            conn = driver.connect(entry, entry.get("password", ""))
            try:
                latency = int((time.time() - t0) * 1000)
                version = ""
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT VERSION()")
                        version = str(cur.fetchone()[0])
                except Exception:
                    pass
                return {"ok": True, "latency_ms": latency, "version": version, "error": ""}
            finally:
                driver.close(conn)
        except Exception as exc:
            return {"ok": False, "latency_ms": 0, "version": "", "error": str(exc)}

    # ------------------------------------------------------------------
    # Table listing
    # ------------------------------------------------------------------

    def remote_tables(
        self, datasource_id: str, refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """Fetch all tables from the remote database."""
        if not refresh:
            cached = self._schema_cache.get(datasource_id)
            if cached is not None:
                return cached
        driver, cfg = self._get_driver_and_cfg(datasource_id)
        pool = self._get_pool(datasource_id)
        item = pool.get()
        try:
            driver.begin_readonly(item.conn)
            tables = driver.fetch_tables(item.conn, cfg.get("database", ""))
            self._schema_cache.set(datasource_id, tables)
            return tables
        finally:
            pool.put(item)

    # ------------------------------------------------------------------
    # Schema snapshot (authorised tables only)
    # ------------------------------------------------------------------

    @staticmethod
    def _columns_cache_key(datasource_id: str) -> str:
        return "cols:{}".format(datasource_id)

    def schema_snapshot(
        self, datasource_id: str, refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """Return column metadata for the authorised tables of a datasource.

        Results are TTL-cached (see ``SchemaCache``) so prompt injection and
        ``db_describe_table`` do not open a database connection on every turn.
        Cache is cleared wholesale by :meth:`reload`, which runs whenever the
        datasource configuration changes.
        """
        cache_key = self._columns_cache_key(datasource_id)
        if not refresh:
            cached = self._schema_cache.get(cache_key)
            if cached is not None:
                return cached

        driver, cfg = self._get_driver_and_cfg(datasource_id)
        authorised = cfg.get("tables") or []

        if not authorised:
            self._schema_cache.set(cache_key, [])
            return []

        # Build (schema, name) tuples from config.
        target_tables: List[Tuple[str, str]] = []
        for tbl in authorised:
            if not isinstance(tbl, dict):
                continue
            schema = tbl.get("schema") or cfg.get("database", "")
            name = tbl.get("name", "")
            if name:
                target_tables.append((schema, name))

        if not target_tables:
            self._schema_cache.set(cache_key, [])
            return []

        pool = self._get_pool(datasource_id)
        item = pool.get()
        try:
            driver.begin_readonly(item.conn)
            columns = driver.fetch_columns(
                item.conn, cfg.get("database", ""), target_tables
            )
        finally:
            pool.put(item)

        # Group by table.
        result: List[Dict[str, Any]] = []
        for schema, name in target_tables:
            tbl_cols = [
                {
                    "name": c["name"],
                    "type": c["type"],
                    "nullable": c["nullable"],
                    "is_pk": c["is_pk"],
                    "comment": c.get("comment", ""),
                    "default": c.get("default"),
                }
                for c in columns
                if c["schema"] == schema and c["table"] == name
            ]
            description = ""
            if authorised:
                for tbl_entry in authorised:
                    if (
                        isinstance(tbl_entry, dict)
                        and tbl_entry.get("schema", "") == schema
                        and tbl_entry.get("name") == name
                    ):
                        description = tbl_entry.get("description", "")
                        break
            allowed_columns = None
            if authorised:
                for tbl_entry in authorised:
                    if (
                        isinstance(tbl_entry, dict)
                        and tbl_entry.get("schema", "") == schema
                        and tbl_entry.get("name") == name
                    ):
                        allowed = tbl_entry.get("columns")
                        if isinstance(allowed, list) and len(allowed) > 0:
                            allowed_columns = set(allowed)
                        break
            if allowed_columns is not None:
                tbl_cols = [c for c in tbl_cols if c["name"] in allowed_columns]
            result.append(
                {
                    "schema": schema,
                    "name": name,
                    "description": description,
                    "columns": tbl_cols,
                }
            )
        self._schema_cache.set(cache_key, result)
        return result

    # ------------------------------------------------------------------
    # Prompt injection block
    # ------------------------------------------------------------------

    def _prompt_cooldown_active(self, datasource_id: str) -> bool:
        with self._lock:
            deadline = self._schema_failures.get(datasource_id)
            if deadline is None:
                return False
            if time.monotonic() >= deadline:
                self._schema_failures.pop(datasource_id, None)
                return False
            return True

    def _mark_prompt_failure(self, datasource_id: str) -> None:
        with self._lock:
            self._schema_failures[datasource_id] = (
                time.monotonic() + self.SCHEMA_FAILURE_COOLDOWN
            )

    def _clear_prompt_failure(self, datasource_id: str) -> None:
        with self._lock:
            self._schema_failures.pop(datasource_id, None)

    def prompt_block(
        self, datasource_ids: List[str], *, allow_write: bool = False
    ) -> str:
        """Build a compact system-prompt block summarising authorised tables.

        Respects prompt_injection limits from each datasource config.

        This runs on the hot path of every chat turn, so it is defensive by
        design: any failure (driver missing, connection refused, auth error,
        timeout) degrades to skipping that datasource rather than propagating
        and breaking the whole request.  A short cooldown avoids paying the
        connect timeout again on the very next turn.
        """
        if not datasource_ids:
            return ""
        parts: List[str] = []
        for ds_id in datasource_ids:
            try:
                _, cfg = self._get_driver_and_cfg(ds_id)
            except DataSourceError as exc:
                logger.warning("跳过数据源提示词注入 %s：%s", ds_id, exc)
                continue
            except Exception:
                logger.warning(
                    "读取数据源配置失败，跳过提示词注入 %s", ds_id, exc_info=True
                )
                continue

            if self._prompt_cooldown_active(ds_id):
                logger.debug("数据源 %s 处于失败冷却期，跳过提示词注入", ds_id)
                continue

            pi = cfg.get("prompt_injection") or {}
            max_tables = int(pi.get("max_tables", 20))
            max_columns = int(pi.get("max_columns_per_table", 40))
            max_chars = int(pi.get("max_chars", 4000))
            include_comments = bool(pi.get("include_comments", True))

            try:
                tables = self.schema_snapshot(ds_id)
            except Exception:
                self._mark_prompt_failure(ds_id)
                logger.warning(
                    "数据源 %s 表结构读取失败，本轮跳过提示词注入（%.0f 秒内不再重试）",
                    ds_id,
                    self.SCHEMA_FAILURE_COOLDOWN,
                    exc_info=True,
                )
                continue
            self._clear_prompt_failure(ds_id)

            engine = cfg.get("engine", "")
            database = cfg.get("database", "")
            read_only = cfg.get("read_only", True)
            label = "{}{}".format(
                engine.upper(),
                "（只读）" if read_only else "",
            )
            block = "数据源 {}（{} / 库 {}，{}）：\n".format(
                cfg.get("name", ds_id), engine.upper(), database, label
            )
            shown = 0
            truncated = False
            for tbl in tables[:max_tables]:
                if len(block) >= max_chars:
                    truncated = True
                    break
                cols = tbl.get("columns", [])[:max_columns]
                col_strs: List[str] = []
                for c in cols:
                    parts_list: List[str] = [c["name"], c["type"]]
                    if c.get("is_pk"):
                        parts_list.append("PK")
                    if include_comments and c.get("comment"):
                        parts_list.append(c["comment"])
                    col_strs.append(" ".join(parts_list))
                line = "- {}: {}".format(tbl["name"], ", ".join(col_strs))
                if tbl.get("description") and include_comments:
                    line = "- {} {}: {}".format(tbl["name"], tbl["description"], ", ".join(col_strs))
                block += line + "\n"
                shown += 1
            if truncated or shown < len(tables):
                block += "（其余表请用 db_list_tables / db_describe_table 按需查询）\n"
            parts.append(block.strip())
        if not parts:
            return ""
        prompt = "\n\n".join(parts)
        if prompt:
            rule = (
                "\n\n规则：只能用 db_query 执行单条 SELECT；表名仅限上表；"
                "需要更多表结构时用 db_list_tables / db_describe_table。"
            )
            if allow_write:
                rule += "写操作必须用 db_execute，需用户确认。"
            else:
                rule += "本智能体仅有只读权限，不能执行任何写操作。"
            prompt += rule
        return prompt

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def validate_readonly_query(
        self,
        datasource_id: str,
        sql: str,
        *,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Validate SQL and table access without opening a database connection."""
        _driver, cfg = self._get_driver_and_cfg(datasource_id)
        dialect = dialect_for(cfg.get("engine", ""))
        default_schema = cfg.get("database", "")
        allowed_tables: Set[str] = set()
        for table in cfg.get("tables") or []:
            if not isinstance(table, dict):
                continue
            schema = table.get("schema") or default_schema
            name = table.get("name", "")
            if name:
                allowed_tables.add("{}.{}".format(schema.lower(), name.lower()))
        max_rows = int(cfg.get("max_rows", 200))
        if limit is not None and limit > 0:
            max_rows = min(limit, max_rows)
        safe_sql, used_tables, effective_limit = compile_readonly(
            sql,
            dialect=dialect,
            allowed_tables=allowed_tables,
            max_rows=max_rows,
            default_schema=default_schema,
        )
        return {
            "sql": safe_sql,
            "tables": used_tables,
            "limit": effective_limit,
        }

    def query(
        self,
        datasource_id: str,
        sql: str,
        *,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute a read-only SQL query against the datasource."""
        driver, _cfg = self._get_driver_and_cfg(datasource_id)
        validated = self.validate_readonly_query(datasource_id, sql, limit=limit)
        safe_sql = str(validated["sql"])
        used_tables = list(validated["tables"])
        effective_limit = int(validated["limit"])
        cfg = self.get_config(datasource_id) or {}

        pool = self._get_pool(datasource_id)
        item = pool.get()
        t0 = time.time()
        try:
            driver.begin_readonly(item.conn)
            timeout_s = int(cfg.get("statement_timeout_seconds", 15))
            driver.set_statement_timeout(item.conn, timeout_s)
            with item.conn.cursor() as cur:
                cur.execute(safe_sql)
                # Apply effective limit at application level for safety.
                rows = cur.fetchmany(effective_limit)
                columns = (
                    [desc[0] for desc in cur.description] if cur.description else []
                )
                truncated = len(rows) >= effective_limit

            duration_ms = int((time.time() - t0) * 1000)
            max_bytes = int(cfg.get("max_result_bytes", 262144))
            packed, byte_size, byte_truncated = self._pack_results(
                rows, max_bytes
            )

            return {
                "columns": columns,
                "rows": packed,
                "row_count": len(rows),
                "truncated": truncated or byte_truncated,
                "duration_ms": duration_ms,
                "tables": used_tables,
            }
        finally:
            pool.put(item)

    def plan_write(
        self, datasource_id: str, sql: str, *, allowed: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Plan a write operation — validate SQL, estimate affected rows."""
        driver, cfg = self._get_driver_and_cfg(datasource_id)
        if cfg.get("read_only", True):
            raise DataSourceError("数据源 {} 未开启写权限".format(cfg.get("name", datasource_id)))

        engine = cfg.get("engine", "")
        dialect = dialect_for(engine)
        default_schema = cfg.get("database", "")

        authorised = cfg.get("tables") or []
        allowed_tables: Set[str] = set()
        for tbl in authorised:
            if not isinstance(tbl, dict):
                continue
            schema = tbl.get("schema") or default_schema
            name = tbl.get("name", "")
            if name:
                allowed_tables.add("{}.{}".format(schema.lower(), name.lower()))

        safe_sql, kind, used_tables = compile_write(
            sql,
            dialect=dialect,
            allowed_tables=allowed_tables,
            default_schema=default_schema,
        )

        kind_label = {"insert": "插入", "update": "更新", "delete": "删除"}.get(kind, kind)

        pool = self._get_pool(datasource_id)
        item = pool.get()
        try:
            driver.begin_readonly(item.conn)
            estimated = 0
            # For UPDATE/DELETE, try to estimate affected rows using COUNT.
            if kind in ("update", "delete"):
                # Extract WHERE clause from original SQL and build SELECT COUNT(*).
                import sqlglot.expressions as exp
                import sqlglot
                parsed = sqlglot.parse(sql, read=dialect)[0]
                where_clause = None
                for node in parsed.walk():
                    if isinstance(node, exp.Where):
                        where_clause = node
                        break
                if where_clause and used_tables:
                    target_table = used_tables[0].split(".", 1)
                    schema_part = target_table[0] if len(target_table) > 1 else ""
                    name_part = target_table[-1]
                    count_sql = "SELECT COUNT(*) FROM {}.{} WHERE {}".format(
                        driver.quote_identifier(schema_part),
                        driver.quote_identifier(name_part),
                        where_clause.sql(dialect=dialect, pretty=False),
                    )
                    try:
                        timeout_s = int(cfg.get("statement_timeout_seconds", 15))
                        driver.set_statement_timeout(item.conn, timeout_s)
                        with item.conn.cursor() as cur:
                            cur.execute(count_sql)
                            estimated = int(cur.fetchone()[0])
                    except Exception:
                        pass
        finally:
            pool.put(item)

        return {
            "datasource_id": datasource_id,
            "name": cfg.get("name", datasource_id),
            "engine_label": engine.upper(),
            "kind": kind,
            "kind_label": kind_label,
            "sql": safe_sql,
            "tables": used_tables,
            "estimated_rows": estimated,
        }

    def execute_write(
        self, datasource_id: str, sql: str
    ) -> Dict[str, Any]:
        """Execute an approved write operation."""
        driver, cfg = self._get_driver_and_cfg(datasource_id)
        if cfg.get("read_only", True):
            raise DataSourceError("数据源 {} 未开启写权限".format(cfg.get("name", datasource_id)))

        engine = cfg.get("engine", "")
        dialect = dialect_for(engine)
        default_schema = cfg.get("database", "")

        authorised = cfg.get("tables") or []
        allowed_tables: Set[str] = set()
        for tbl in authorised:
            if not isinstance(tbl, dict):
                continue
            schema = tbl.get("schema") or default_schema
            name = tbl.get("name", "")
            if name:
                allowed_tables.add("{}.{}".format(schema.lower(), name.lower()))

        safe_sql, kind, used_tables = compile_write(
            sql,
            dialect=dialect,
            allowed_tables=allowed_tables,
            default_schema=default_schema,
        )

        pool = self._get_pool(datasource_id)
        item = pool.get()
        t0 = time.time()
        try:
            timeout_s = int(cfg.get("statement_timeout_seconds", 15))
            driver.set_statement_timeout(item.conn, timeout_s)
            with item.conn.cursor() as cur:
                cur.execute(safe_sql)
                rowcount = cur.rowcount
            item.conn.commit()
            duration_ms = int((time.time() - t0) * 1000)
            return {
                "kind": kind,
                "row_count": rowcount,
                "duration_ms": duration_ms,
                "tables": used_tables,
            }
        except Exception as exc:
            try:
                item.conn.rollback()
            except Exception:
                pass
            raise DataSourceError("写操作执行失败：{}".format(exc)) from exc
        finally:
            pool.put(item)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pack_results(
        rows: List[Any], max_bytes: int
    ) -> Tuple[List[List[Any]], int, bool]:
        """Pack rows into a JSON-safe form, respecting byte limit.

        Returns (rows, total_bytes, truncated).
        """
        import json
        from decimal import Decimal
        from datetime import date, datetime

        packed: List[List[Any]] = []
        total = 0
        truncated = False
        for row in rows:
            out_row: List[Any] = []
            for val in row:
                if val is None:
                    out_row.append(None)
                elif isinstance(val, bytes):
                    out_row.append("<binary {} 字节>".format(len(val)))
                elif isinstance(val, (Decimal,)):
                    out_row.append(str(val))
                elif isinstance(val, (datetime, date)):
                    out_row.append(val.isoformat())
                elif isinstance(val, (int, float, str, bool)):
                    out_row.append(val)
                else:
                    out_row.append(str(val))
            packed.append(out_row)
            try:
                total += len(json.dumps(out_row, ensure_ascii=False))
            except Exception:
                total += 1000
            if total > max_bytes:
                truncated = True
                break
        return packed, total, truncated
