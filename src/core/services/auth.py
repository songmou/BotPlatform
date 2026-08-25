"""Admin authentication and permission service for the web panel."""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from src.core.storage.admin_users import (
    AdminRole,
    AdminRoleStore,
    AdminSessionStore,
    AdminStoreError,
    AdminUser,
    AdminUserStore,
    verify_password,
)

MAX_LOGIN_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 300
_FAILURE_MAP_PURGE_THRESHOLD = 4096


class AuthError(RuntimeError):
    """Raised when login fails."""


class LoginThrottled(AuthError):
    """Raised when a username/IP pair exceeded the login failure budget."""


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
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.users = user_store
        self.roles = role_store
        self.sessions = session_store
        self.system_root = system_root
        self._monotonic = monotonic
        # username|ip -> (consecutive failures, locked-until monotonic time)
        self._login_failures: Dict[str, Tuple[int, float]] = {}
        self._login_failures_lock = threading.Lock()

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

    def _throttle_key(self, username: str, ip: Optional[str]) -> str:
        return "{}|{}".format(username.strip().lower(), ip or "-")

    def _ensure_not_throttled(self, key: str) -> None:
        with self._login_failures_lock:
            entry = self._login_failures.get(key)
            if entry is None:
                return
            _, locked_until = entry
            if locked_until and self._monotonic() < locked_until:
                raise LoginThrottled("登录失败次数过多，请稍后再试")
            if locked_until and self._monotonic() >= locked_until:
                self._login_failures.pop(key, None)

    def _record_login_failure(self, key: str) -> None:
        with self._login_failures_lock:
            if len(self._login_failures) > _FAILURE_MAP_PURGE_THRESHOLD:
                now = self._monotonic()
                self._login_failures = {
                    k: v
                    for k, v in self._login_failures.items()
                    if v[1] and v[1] > now
                }
            count, locked_until = self._login_failures.get(key, (0, 0.0))
            count += 1
            if count >= MAX_LOGIN_FAILURES:
                locked_until = self._monotonic() + LOGIN_LOCKOUT_SECONDS
            self._login_failures[key] = (count, locked_until)

    def _clear_login_failures(self, key: str) -> None:
        with self._login_failures_lock:
            self._login_failures.pop(key, None)

    def login(
        self,
        username: str,
        password: str,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Tuple[str, AuthPrincipal]:
        key = self._throttle_key(username, ip)
        self._ensure_not_throttled(key)
        try:
            user = self.users.get_by_username(username.strip())
            if user is None:
                raise AuthError("用户名或密码错误")
            if user.disabled:
                raise AuthError("账号已被禁用")
            if not verify_password(password, self.users.password_hash(user.user_id)):
                raise AuthError("用户名或密码错误")
        except AuthError:
            self._record_login_failure(key)
            raise
        self._clear_login_failures(key)
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
