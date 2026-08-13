"""真实 Postgres 供给 helper（Docker 或本机已有 PG）。

提供 ``require_postgres()``：
- 若设置了环境变量 ``BOTPLATFORM_TEST_PG_DSN``（形如
  ``postgresql://user:pass@host:port/db``），直接复用本机已有 PG；
- 否则检测 Docker 守护进程，拉起一个临时 ``postgres:16-alpine`` 容器，
  建表并 seed；
- 两者都不可用时 ``raise unittest.SkipTest``，附清晰提示。

返回的 ``PostgresFixture`` 包含连接参数与两个数据源配置
（``ds_ro`` 只读 / ``ds_rw`` 可写），以及 ``stop()`` 清理方法。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import unittest
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import psycopg

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 15432
DEFAULT_USER = "testuser"
DEFAULT_PASSWORD = "testpass"
DEFAULT_DATABASE = "botplatform_test"


@dataclass
class PostgresFixture:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    user: str = DEFAULT_USER
    password: str = DEFAULT_PASSWORD
    database: str = DEFAULT_DATABASE
    container_id: Optional[str] = None

    @property
    def dsn(self) -> str:
        return "postgresql://{}:{}@{}:{}/{}".format(
            self.user, self.password, self.host, self.port, self.database
        )

    def datasource_config(self, ds_id: str, read_only: bool) -> Dict[str, Any]:
        return {
            "id": ds_id,
            "name": ds_id,
            "engine": "postgresql",
            "host": self.host,
            "port": self.port,
            "username": self.user,
            "database": self.database,
            "password": self.password,
            "read_only": read_only,
            "enabled": True,
            "options": {"ssl_mode": "disabled"},
            "max_rows": 200,
            "tables": [
                {
                    "schema": "public",
                    "name": "customers",
                    "description": "客户表",
                    "columns": ["id", "name", "city"],
                }
            ],
        }

    def stop(self) -> None:
        if self.container_id:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", self.container_id],
                    capture_output=True,
                    timeout=30,
                )
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
            self.container_id = None


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=10).check_returncode()
    except Exception:  # noqa: BLE001
        return False
    return True


def _connect(fix: PostgresFixture, timeout: float = 60.0) -> "psycopg.Connection":
    deadline = time.monotonic() + timeout
    last: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            conn = psycopg.connect(fix.dsn, connect_timeout=5)
            conn.execute("SELECT 1")
            return conn
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.0)
    raise RuntimeError("无法连接到临时 Postgres：{}".format(last))


def _seed(fix: PostgresFixture) -> None:
    conn = _connect(fix)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS customers ("
                "id SERIAL PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL)"
            )
            cur.execute("TRUNCATE customers RESTART IDENTITY")
            cur.executemany(
                "INSERT INTO customers (name, city) VALUES (%s, %s)",
                [
                    ("Alice", "Shanghai"),
                    ("Bob", "Beijing"),
                    ("Carol", "Shanghai"),
                    ("Dave", "Guangzhou"),
                ],
            )
        conn.commit()
    finally:
        conn.close()


def _fixture_from_dsn(dsn: str) -> PostgresFixture:
    parsed = urllib.parse.urlsplit(dsn)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise ValueError("BOTPLATFORM_TEST_PG_DSN 必须是 postgresql:// 形式：{}".format(dsn))
    user = parsed.username or DEFAULT_USER
    password = parsed.password or DEFAULT_PASSWORD
    host = parsed.hostname or DEFAULT_HOST
    port = parsed.port or 5432
    database = parsed.path.lstrip("/") or DEFAULT_DATABASE
    return PostgresFixture(
        host=host, port=port, user=user, password=password, database=database
    )


def _start_docker_postgres() -> PostgresFixture:
    fix = PostgresFixture()
    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-p",
            "{}:5432".format(fix.port),
            "-e",
            "POSTGRES_USER={}".format(fix.user),
            "-e",
            "POSTGRES_PASSWORD={}".format(fix.password),
            "-e",
            "POSTGRES_DB={}".format(fix.database),
            "postgres:16-alpine",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError("启动 Postgres 容器失败：{}".format(result.stderr.strip()))
    fix.container_id = result.stdout.strip()
    return fix


def require_postgres() -> PostgresFixture:
    """返回可用的 PG fixture；不可用时 raise SkipTest。"""
    env_dsn = os.environ.get("BOTPLATFORM_TEST_PG_DSN")
    if env_dsn:
        fix = _fixture_from_dsn(env_dsn)
        _seed(fix)
        return fix
    if not _docker_available():
        raise unittest.SkipTest(
            "Docker 守护进程不可用，跳过真实 PG 集成测试："
            "请启动 Docker Desktop，或设置 BOTPLATFORM_TEST_PG_DSN 指向本机 PG"
        )
    fix = _start_docker_postgres()
    _seed(fix)
    return fix
