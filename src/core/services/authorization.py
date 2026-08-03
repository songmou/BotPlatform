"""Central platform and organization authorization primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.core.storage.organizations import OrganizationError, OrganizationStore


class AuthorizationError(PermissionError):
    """Raised when a principal cannot access a platform or organization action."""


@dataclass(frozen=True)
class ExecutionContext:
    """Immutable actor and organization context passed through runtime operations."""

    user_id: int
    organization_id: str
    role: str
    channel_identity_id: str = ""
    conversation_id: str = ""
    source: str = "web"
    request_id: str = ""
    platform_delegation: bool = False


class AuthorizationService:
    def __init__(self, organizations: OrganizationStore) -> None:
        self.organizations = organizations

    @staticmethod
    def require_platform(principal, permission: str) -> None:
        if principal is None:
            raise AuthorizationError("未登录")
        if not principal.allows(permission):
            raise AuthorizationError("没有权限执行该平台操作")

    def organization_context(
        self,
        principal,
        organization_id: str,
        *,
        minimum_role: Optional[str] = None,
        conversation_id: str = "",
        source: str = "web",
        request_id: str = "",
    ) -> ExecutionContext:
        if principal is None:
            raise AuthorizationError("未登录")
        if principal.allows("admins.manage"):
            try:
                self.organizations.get(organization_id)
            except OrganizationError as exc:
                raise AuthorizationError(str(exc)) from exc
            return ExecutionContext(
                user_id=principal.user.user_id,
                organization_id=organization_id,
                role="owner",
                conversation_id=conversation_id,
                source="platform_delegation",
                request_id=request_id,
                platform_delegation=True,
            )
        try:
            membership = self.organizations.membership(
                principal.user.user_id, organization_id
            )
        except OrganizationError as exc:
            raise AuthorizationError(str(exc)) from exc
        role = str(membership["role"])
        if minimum_role:
            rank = {"member": 1, "admin": 2, "owner": 3}
            if rank.get(role, 0) < rank.get(minimum_role, 99):
                raise AuthorizationError("当前组织角色没有权限执行该操作")
        return ExecutionContext(
            user_id=principal.user.user_id,
            organization_id=organization_id,
            role=role,
            conversation_id=conversation_id,
            source=source,
            request_id=request_id,
        )
