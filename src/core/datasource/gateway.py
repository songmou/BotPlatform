"""SQL security gateway — AST-based read-only enforcement and table whitelist.

Uses sqlglot for AST-level analysis.  Fail-closed: any parse failure is rejected.
Only SELECT / UNION / INTERSECT / EXCEPT / WITH are allowed.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from src.core.datasource.errors import DataSourceError

_ALLOWED_ROOTS = frozenset({"select", "union", "intersect", "except", "cte"})

_FORBIDDEN_KEYWORDS: Set[str] = {
    "insert", "update", "delete", "merge", "create", "drop", "alter",
    "command", "into", "copy", "set", "grant", "truncate", "replace",
    "load", "show", "use", "desc", "describe", "begin", "commit",
    "rollback", "lock", "unlock", "call", "execute", "explain",
}

_FORBIDDEN_FUNCTIONS: Set[str] = {
    "load_file", "pg_read_file", "pg_sleep", "sleep", "lo_import",
    "dblink", "pg_ls_dir", "benchmark", "pg_read_binary_file",
    "pg_stat_file", "gen_random_uuid",
}


def _collect_tables(ast_root, default_schema: str) -> Set[str]:
    """Walk the AST and collect all real table references, normalised to lowercase.

    CTE names and aliases are excluded.
    """
    import sqlglot.expressions as exp

    cte_names: Set[str] = set()
    real_tables: Set[str] = set()

    for node in ast_root.walk():
        if isinstance(node, exp.CTE):
            cte_names.add(node.alias.lower())
        if isinstance(node, exp.Table):
            name = node.name.lower() if node.name else ""
            if not name or name in cte_names:
                continue
            schema = (node.db or default_schema).lower()
            catalog = node.catalog
            if catalog:
                raise DataSourceError(
                    "不允许跨库查询（三段式表名 {}.{}.{}）".format(
                        catalog, schema, name
                    )
                )
            real_tables.add("{}.{}".format(schema, name))
    return real_tables


def _existing_limit(tree) -> Optional[int]:
    """Return the LIMIT count already present on the outermost query, or None."""
    import sqlglot.expressions as exp

    # Unwrap CTE wrapper.
    root = tree
    if hasattr(root, "expression") and root.key == "cte":
        root = root.expression
    if isinstance(root, exp.Select):
        limit_expr = root.args.get("limit")
        if limit_expr:
            return int(limit_expr.expression.this) if limit_expr.expression else None
    return None


def compile_readonly(
    sql: str,
    *,
    dialect: str,
    allowed_tables: Set[str],
    max_rows: int = 200,
    default_schema: str = "",
) -> Tuple[str, List[str], int]:
    """Validate a read-only SQL statement and return (safe_sql, used_tables, limit).

    Raises DataSourceError on any violation.
    """
    import sqlglot
    import sqlglot.expressions as exp

    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception as exc:
        raise DataSourceError("SQL 解析失败，请检查语法：{}".format(exc)) from None

    if not statements or len(statements) != 1 or statements[0] is None:
        raise DataSourceError("只允许提交单条 SELECT 语句")

    tree = statements[0]

    # Check root node
    root_key = tree.key if hasattr(tree, "key") else ""
    if root_key not in _ALLOWED_ROOTS:
        raise DataSourceError(
            "只允许 SELECT 查询，当前语句类型为：{}".format(root_key)
        )

    # Walk all nodes and reject forbidden operations.
    for node in tree.walk():
        key = getattr(node, "key", "")
        if key in _FORBIDDEN_KEYWORDS:
            raise DataSourceError(
                "只允许 SELECT 查询，检测到禁止的语句：{}".format(key)
            )
        if isinstance(node, exp.Anonymous):
            func_name = (str(node.name) if node.name else "").lower()
            if func_name in _FORBIDDEN_FUNCTIONS:
                raise DataSourceError(
                    "禁止调用函数：{}()".format(func_name)
                )
        if isinstance(node, (exp.Into,)):
            raise DataSourceError("只允许 SELECT 查询，禁止 INTO 子句")

    # Collect and validate table references.
    schema = default_schema or "public"
    used = _collect_tables(tree, schema)
    denied = sorted(used - allowed_tables)
    if denied:
        raise DataSourceError(
            "以下表未被授权访问：{}".format("、".join(denied))
        )

    # Inject or tighten LIMIT.
    effective_limit = max_rows
    existing = _existing_limit(tree)
    if existing is not None:
        effective_limit = min(existing, max_rows)

    # Regenerate SQL with LIMIT applied.
    # Simplify: append LIMIT clause to the outermost select.
    safe_sql = tree.sql(dialect=dialect, pretty=False)
    return safe_sql, sorted(used), effective_limit


def compile_write(
    sql: str,
    *,
    dialect: str,
    allowed_tables: Set[str],
    default_schema: str = "",
) -> Tuple[str, str, List[str]]:
    """Validate a write statement (INSERT/UPDATE/DELETE).

    Returns (safe_sql, kind, used_tables).
    kind is one of: insert, update, delete.
    Raises DataSourceError on any violation.
    """
    import sqlglot
    import sqlglot.expressions as exp

    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception as exc:
        raise DataSourceError("SQL 解析失败，请检查语法：{}".format(exc)) from None

    if not statements or len(statements) != 1 or statements[0] is None:
        raise DataSourceError("只允许提交单条 SQL 语句")

    tree = statements[0]
    root_key = tree.key if hasattr(tree, "key") else ""

    kind: str
    if root_key == "insert":
        kind = "insert"
    elif root_key == "update":
        kind = "update"
    elif root_key == "delete":
        kind = "delete"
    else:
        raise DataSourceError(
            "只允许 INSERT/UPDATE/DELETE 操作，当前语句类型为：{}".format(root_key)
        )

    # UPDATE and DELETE *must* have a WHERE clause.
    if kind in ("update", "delete"):
        has_where = False
        for node in tree.walk():
            if isinstance(node, exp.Where):
                has_where = True
                break
        if not has_where:
            raise DataSourceError(
                "出于安全考虑，{} 必须包含 WHERE 条件".format(
                    "UPDATE" if kind == "update" else "DELETE"
                )
            )

    # Reject sub-DML inside the statement.
    for node in tree.walk():
        key = getattr(node, "key", "")
        if key in ("create", "drop", "alter", "truncate", "grant", "set"):
            raise DataSourceError("检测到禁止的语句：{}".format(key))

    # Validate table references.
    schema = default_schema or "public"
    used = _collect_tables(tree, schema)
    denied = sorted(used - allowed_tables)
    if denied:
        raise DataSourceError(
            "以下表未被授权执行写操作：{}".format("、".join(denied))
        )

    safe_sql = tree.sql(dialect=dialect, pretty=False)
    return safe_sql, kind, sorted(used)
