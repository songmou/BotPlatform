"""Login, logout, and current-identity endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.api.auth import SESSION_COOKIE, SESSION_MAX_AGE
from src.api.deps import get_admin_auth, get_admin_role_store, get_principal
from src.api.schemas import AdminRoleOut, AdminUserOut, LoginRequest, MeOut
from src.core.services.auth import AuthError

router = APIRouter(tags=["auth"])


def _user_out(user, role) -> AdminUserOut:
    return AdminUserOut(
        user_id=user.user_id,
        username=user.username,
        role=AdminRoleOut(
            role_id=role.role_id,
            code=role.code,
            name=role.name,
            permissions=role.permissions,
            builtin=role.builtin,
        ),
        disabled=user.disabled,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(name="login.html", request=request, context={})


@router.post("/api/auth/login", response_model=MeOut)
def login(body: LoginRequest, request: Request):
    auth = get_admin_auth(request)
    try:
        token, principal = auth.login(
            body.username,
            body.password,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    payload = MeOut(
        user=_user_out(principal.user, principal.role),
        permissions=principal.permissions,
    )
    response = JSONResponse(payload.model_dump())
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE,
        path="/",
    )
    return response


@router.post("/api/auth/logout")
def logout(request: Request):
    auth = get_admin_auth(request)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth.logout(token)
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/api/auth/me", response_model=MeOut)
def me(request: Request):
    principal = get_principal(request)
    return MeOut(
        user=_user_out(principal.user, principal.role),
        permissions=principal.permissions,
    )
