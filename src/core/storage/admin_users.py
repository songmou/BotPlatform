"""Admin account, role, and session repositories for the web panel."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from src.core.storage.database import Database

PBKDF2_ITERATIONS = 200_000
SESSION_TTL_SECONDS = 86400 * 7


class AdminStoreError(RuntimeError):
    """Raised when admin account data is missing or invalid."""


@dataclass(frozen=True)
class AdminRole:
    role_id: int
    code: str
    name: str
    permissions: List[str]
    builtin: bool

    def allows(self, permission: str) -> bool:
        return "*" in self.permissions or permission in self.permissions


@dataclass(frozen=True)
class AdminUser:
    user_id: int
    username: str
    role_id: int
    disabled: bool
    created_at: str
    last_login_at: Optional[str]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return "pbkdf2${}${}${}".format(PBKDF2_ITERATIONS, salt.hex(), digest.hex())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return secrets.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def load_or_create_session_secret(system_root: Path) -> bytes:
    secret_path = system_root / "session_secret"
    if secret_path.exists():
        data = secret_path.read_bytes().strip()
        if data:
            return data
    secret = secrets.token_hex(32).encode("ascii")
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_bytes(secret)
    if os.name != "nt":
        os.chmod(str(secret_path), 0o600)
    return secret


class AdminRoleStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_row(row: Any) -> AdminRole:
        try:
            permissions = json.loads(row["permissions"])
        except (ValueError, TypeError):
            permissions = []
        return AdminRole(
            role_id=int(row["role_id"]),
            code=str(row["code"]),
            name=str(row["name"]),
            permissions=[str(p) for p in permissions],
            builtin=bool(row["builtin"]),
        )

    def list_roles(self) -> List[AdminRole]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT role_id, code, name, permissions, builtin "
                "FROM admin_roles ORDER BY role_id"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, role_id: int) -> AdminRole:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT role_id, code, name, permissions, builtin "
                "FROM admin_roles WHERE role_id=?",
                (role_id,),
            ).fetchone()
        if row is None:
            raise AdminStoreError("角色不存在")
        return self._from_row(row)

    def get_by_code(self, code: str) -> AdminRole:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT role_id, code, name, permissions, builtin "
                "FROM admin_roles WHERE code=?",
                (code,),
            ).fetchone()
        if row is None:
            raise AdminStoreError("角色不存在")
        return self._from_row(row)

    def update_permissions(self, role_id: int, permissions: List[str]) -> AdminRole:
        role = self.get(role_id)
        if role.builtin and role.code == "admin":
            raise AdminStoreError("内置 admin 角色的权限不可修改")
        payload = json.dumps(sorted({str(p) for p in permissions}), ensure_ascii=False)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE admin_roles SET permissions=? WHERE role_id=?",
                (payload, role_id),
            )
        return self.get(role_id)


class AdminUserStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_row(row: Any) -> AdminUser:
        return AdminUser(
            user_id=int(row["user_id"]),
            username=str(row["username"]),
            role_id=int(row["role_id"]),
            disabled=bool(row["disabled"]),
            created_at=str(row["created_at"]),
            last_login_at=(
                str(row["last_login_at"]) if row["last_login_at"] is not None else None
            ),
        )

    _COLUMNS = "user_id, username, role_id, disabled, created_at, last_login_at"

    def count(self) -> int:
        with self.database.read() as connection:
            row = connection.execute("SELECT COUNT(*) FROM admin_users").fetchone()
        return int(row[0])

    def list_users(self) -> List[AdminUser]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT {} FROM admin_users ORDER BY user_id".format(self._COLUMNS)
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get_by_id(self, user_id: int) -> AdminUser:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT {} FROM admin_users WHERE user_id=?".format(self._COLUMNS),
                (user_id,),
            ).fetchone()
        if row is None:
            raise AdminStoreError("账号不存在")
        return self._from_row(row)

    def get_by_username(self, username: str) -> Optional[AdminUser]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT {} FROM admin_users WHERE username=?".format(self._COLUMNS),
                (username,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def password_hash(self, user_id: int) -> str:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT password_hash FROM admin_users WHERE user_id=?", (user_id,)
            ).fetchone()
        if row is None:
            raise AdminStoreError("账号不存在")
        return str(row["password_hash"])

    def create(self, username: str, password: str, role_id: int) -> AdminUser:
        if self.get_by_username(username) is not None:
            raise AdminStoreError("用户名已存在")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO admin_users(username, password_hash, role_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (username, hash_password(password), role_id, _utc_now()),
            )
        user = self.get_by_username(username)
        assert user is not None
        return user

    def update_role(self, user_id: int, role_id: int) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE admin_users SET role_id=? WHERE user_id=?", (role_id, user_id)
            )

    def set_password(self, user_id: int, new_password: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE admin_users SET password_hash=? WHERE user_id=?",
                (hash_password(new_password), user_id),
            )
            connection.execute(
                "DELETE FROM admin_sessions WHERE user_id=?", (user_id,)
            )

    def set_disabled(self, user_id: int, disabled: bool) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE admin_users SET disabled=? WHERE user_id=?",
                (1 if disabled else 0, user_id),
            )
            if disabled:
                connection.execute(
                    "DELETE FROM admin_sessions WHERE user_id=?", (user_id,)
                )

    def delete(self, user_id: int) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM admin_users WHERE user_id=?", (user_id,))

    def touch_login(self, user_id: int) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE admin_users SET last_login_at=? WHERE user_id=?",
                (_utc_now(), user_id),
            )


class AdminSessionStore:
    def __init__(self, database: Database, secret: bytes) -> None:
        self.database = database
        self._secret = secret

    def _hash(self, token: str) -> str:
        return hmac.new(self._secret, token.encode("ascii"), hashlib.sha256).hexdigest()

    def create(
        self,
        user_id: int,
        ttl_seconds: int = SESSION_TTL_SECONDS,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Tuple[str, str]:
        token = secrets.token_urlsafe(32)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ).isoformat()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO admin_sessions"
                "(session_hash, user_id, created_at, expires_at, ip, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self._hash(token), user_id, _utc_now(), expires_at, ip, user_agent),
            )
        return token, expires_at

    def resolve(self, token: str) -> Optional[int]:
        """Return the user_id for a valid, unexpired session token."""
        if not token:
            return None
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT user_id, expires_at FROM admin_sessions WHERE session_hash=?",
                (self._hash(token),),
            ).fetchone()
        if row is None:
            return None
        try:
            expires = datetime.fromisoformat(str(row["expires_at"]))
        except ValueError:
            return None
        if expires <= datetime.now(timezone.utc):
            self.delete(token)
            return None
        return int(row["user_id"])

    def refresh(self, token: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ).isoformat()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE admin_sessions SET expires_at=? WHERE session_hash=?",
                (expires_at, self._hash(token)),
            )

    def delete(self, token: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM admin_sessions WHERE session_hash=?", (self._hash(token),)
            )

    def purge_expired(self) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ?",
                (datetime.now(timezone.utc).isoformat(),),
            )
