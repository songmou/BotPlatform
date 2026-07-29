"""Admin account and role management endpoints."""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request

from src.api.deps import (
    get_admin_role_store,
    get_admin_user_store,
    require_permission,
)
from src.api.schemas import (
    AdminRoleOut,
    AdminRoleUpdate,
    AdminUserCreate,
    AdminUserOut,
    AdminUserUpdate,
    PasswordResetOut,
)
from src.core.storage.admin_users import AdminStoreError

router = APIRouter(prefix="/api/admins", tags=["admins"])

_USERNAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{2,31}$")
DEFAULT_INITIAL_PASSWORD = "12345"

_KNOWN_PERMISSIONS = {
    "tenants.read",
    "tenants.delete",
    "panel.read",
    "panel.write",
    "admins.manage",
    "scripts.read",
    "scripts.execute",
    "scripts.manage",
    "schedules.manage",
    "model_analytics.read",
    "model_analytics.manage",
    "knowledge.read",
    "knowledge.manage",
}


def _role_out(role) -> AdminRoleOut:
    return AdminRoleOut(
        role_id=role.role_id,
        code=role.code,
        name=role.name,
        permissions=role.permissions,
        builtin=role.builtin,
    )


def _user_out(user, role) -> AdminUserOut:
    return AdminUserOut(
        user_id=user.user_id,
        username=user.username,
        role=_role_out(role),
        disabled=user.disabled,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def _admin_count(users, roles) -> int:
    admin_role = roles.get_by_code("admin")
    return sum(
        1
        for user in users.list_users()
        if user.role_id == admin_role.role_id and not user.disabled
    )


@router.get("", response_model=list[AdminUserOut])
def list_admins(
    request: Request, principal=Depends(require_permission("admins.manage"))
):
    users = get_admin_user_store(request)
    roles = get_admin_role_store(request)
    role_map = {role.role_id: role for role in roles.list_roles()}
    return [_user_out(user, role_map[user.role_id]) for user in users.list_users()]


@router.post("", response_model=AdminUserOut, status_code=201)
def create_admin(
    body: AdminUserCreate,
    request: Request,
    principal=Depends(require_permission("admins.manage")),
):
    users = get_admin_user_store(request)
    roles = get_admin_role_store(request)
    username = body.username.strip()
    if not _USERNAME_PATTERN.match(username):
        raise HTTPException(
            status_code=400,
            detail="用户名需以字母开头，3-32 位，可含数字、下划线、点、连字符",
        )
    try:
        role = roles.get(body.role_id)
        user = users.create(username, DEFAULT_INITIAL_PASSWORD, role.role_id)
    except AdminStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _user_out(user, role)


@router.put("/{user_id}", response_model=AdminUserOut)
def update_admin(
    user_id: int,
    body: AdminUserUpdate,
    request: Request,
    principal=Depends(require_permission("admins.manage")),
):
    users = get_admin_user_store(request)
    roles = get_admin_role_store(request)
    try:
        user = users.get_by_id(user_id)
    except AdminStoreError:
        raise HTTPException(status_code=404, detail="账号不存在")

    admin_role = roles.get_by_code("admin")
    is_last_admin = (
        user.role_id == admin_role.role_id
        and not user.disabled
        and _admin_count(users, roles) <= 1
    )

    if body.role_id is not None and body.role_id != user.role_id:
        try:
            roles.get(body.role_id)
        except AdminStoreError:
            raise HTTPException(status_code=400, detail="角色不存在")
        if is_last_admin:
            raise HTTPException(status_code=400, detail="不能移除最后一个管理员账号的 admin 角色")
        users.update_role(user_id, body.role_id)

    if body.disabled is not None and body.disabled != user.disabled:
        if user.user_id == principal.user.user_id:
            raise HTTPException(status_code=400, detail="不能禁用自己的账号")
        if body.disabled and is_last_admin:
            raise HTTPException(status_code=400, detail="不能禁用最后一个管理员账号")
        users.set_disabled(user_id, body.disabled)

    user = users.get_by_id(user_id)
    return _user_out(user, roles.get(user.role_id))


@router.post("/{user_id}/password", response_model=PasswordResetOut)
def reset_password(
    user_id: int,
    request: Request,
    principal=Depends(require_permission("admins.manage")),
):
    users = get_admin_user_store(request)
    try:
        users.get_by_id(user_id)
    except AdminStoreError:
        raise HTTPException(status_code=404, detail="账号不存在")
    new_password = secrets.token_urlsafe(12)
    users.set_password(user_id, new_password)
    return PasswordResetOut(user_id=user_id, new_password=new_password)


@router.delete("/{user_id}")
def delete_admin(
    user_id: int,
    request: Request,
    principal=Depends(require_permission("admins.manage")),
):
    users = get_admin_user_store(request)
    roles = get_admin_role_store(request)
    try:
        user = users.get_by_id(user_id)
    except AdminStoreError:
        raise HTTPException(status_code=404, detail="账号不存在")
    if user.user_id == principal.user.user_id:
        raise HTTPException(status_code=400, detail="不能删除自己的账号")
    admin_role = roles.get_by_code("admin")
    if (
        user.role_id == admin_role.role_id
        and not user.disabled
        and _admin_count(users, roles) <= 1
    ):
        raise HTTPException(status_code=400, detail="不能删除最后一个管理员账号")
    users.delete(user_id)
    return {"status": "ok"}


@router.get("/roles", response_model=list[AdminRoleOut])
def list_roles(
    request: Request, principal=Depends(require_permission("admins.manage"))
):
    roles = get_admin_role_store(request)
    return [_role_out(role) for role in roles.list_roles()]


@router.put("/roles/{role_id}", response_model=AdminRoleOut)
def update_role(
    role_id: int,
    body: AdminRoleUpdate,
    request: Request,
    principal=Depends(require_permission("admins.manage")),
):
    roles = get_admin_role_store(request)
    invalid = [p for p in body.permissions if p not in _KNOWN_PERMISSIONS]
    if invalid:
        raise HTTPException(
            status_code=400, detail="未知权限：{}".format("、".join(invalid))
        )
    try:
        role = roles.update_permissions(role_id, body.permissions)
    except AdminStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _role_out(role)
