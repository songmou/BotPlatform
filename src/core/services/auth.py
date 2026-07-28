"""Admin authentication and permission service for the web panel."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from src.core.storage.admin_users import (
    AdminRole,
    AdminRoleStore,
    AdminSessionStore,
    AdminStoreError,
    AdminUser,
    AdminUserStore,
    verify_password,
)


class AuthError(RuntimeError):
    """Raised when login fails."""


class PermissionDenied(RuntimeError):
    """Raised when the current principal lacks a required permission."""


@dataclass(frozen=True)
class AuthPrincipal:
    user: AdminUser
    role: AdminRole

    @property
    def permissions(self) -> List[str]:
        return list(self.role.permissions)

    def allows(self, permission: str) -> bool:
        return self.role.allows(permission)


class AdminAuthService:
    def __init__(
        self,
        user_store: AdminUserStore,
        role_store: AdminRoleStore,
        session_store: AdminSessionStore,
        system_root: Path,
    ) -> None:
        self.users = user_store
        self.roles = role_store
        self.sessions = session_store
        self.system_root = system_root

    def bootstrap_default_admin(self) -> Optional[str]:
        """Create the initial admin account; return its plaintext password once."""
        if self.users.count() > 0:
            return None
        role = self.roles.get_by_code("admin")
        password = secrets.token_urlsafe(18)
        self.users.create("admin", password, role.role_id)
        password_file = self.system_root / "admin_initial_password"
        password_file.parent.mkdir(parents=True, exist_ok=True)
        password_file.write_text(password + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(str(password_file), 0o600)
        return password

    def login(
        self,
        username: str,
        password: str,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Tuple[str, AuthPrincipal]:
        user = self.users.get_by_username(username.strip())
        if user is None:
            raise AuthError("用户名或密码错误")
        if user.disabled:
            raise AuthError("账号已被禁用")
        if not verify_password(password, self.users.password_hash(user.user_id)):
            raise AuthError("用户名或密码错误")
        role = self.roles.get(user.role_id)
        token, _ = self.sessions.create(user.user_id, ip=ip, user_agent=user_agent)
        self.users.touch_login(user.user_id)
        self.sessions.purge_expired()
        return token, AuthPrincipal(user=user, role=role)

    def logout(self, token: str) -> None:
        self.sessions.delete(token)

    def identify(self, token: Optional[str]) -> Optional[AuthPrincipal]:
        if not token:
            return None
        user_id = self.sessions.resolve(token)
        if user_id is None:
            return None
        try:
            user = self.users.get_by_id(user_id)
            role = self.roles.get(user.role_id)
        except AdminStoreError:
            return None
        if user.disabled:
            return None
        return AuthPrincipal(user=user, role=role)

    @staticmethod
    def require(principal: Optional[AuthPrincipal], permission: str) -> None:
        if principal is None:
            raise PermissionDenied("未登录")
        if not principal.allows(permission):
            raise PermissionDenied("没有权限执行该操作")
