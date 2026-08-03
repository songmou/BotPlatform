"""Session-based authentication middleware for the web management panel."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

SESSION_COOKIE = "admin_session"
SESSION_MAX_AGE = 86400 * 7

OPEN_PATHS = {
    "/api/health",
    "/static",
    "/login",
    "/api/auth/login",
    "/api/v2/invitations/accept",
}

# Panel templates rely on inline scripts/styles and only load local assets,
# so the policy allows 'unsafe-inline' but pins every source to self.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def _is_open(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in OPEN_PATHS)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach conservative security headers to every panel response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        if (
            request.url.path.startswith("/api/")
            and not request.url.path.startswith("/api/v2/")
            and not request.url.path.startswith("/api/auth/")
            and request.url.path != "/api/health"
        ):
            response.headers.setdefault("Deprecation", "true")
            response.headers.setdefault(
                "Link",
                '</docs/organization-multitenancy>; rel="deprecation"',
            )
        return response


class OrganizationAuditMiddleware(BaseHTTPMiddleware):
    """Record metadata-only audit rows for every mutating V2 request."""

    _ORGANIZATION_PATH = re.compile(r"^/api/v2/orgs/([^/]+)")

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", request_id)
        if (
            request.method not in {"POST", "PUT", "PATCH", "DELETE"}
            or not request.url.path.startswith("/api/v2/")
        ):
            return response
        organization_store = getattr(
            request.app.state, "organization_store", None
        )
        if organization_store is None:
            return response
        principal = getattr(request.state, "principal", None)
        if principal is None:
            auth_service = getattr(request.app.state, "admin_auth", None)
            token = request.cookies.get(SESSION_COOKIE)
            principal = auth_service.identify(token) if auth_service and token else None
        match = self._ORGANIZATION_PATH.match(request.url.path)
        organization_id = match.group(1) if match else None
        resource = request.url.path
        audit_source = "web"
        if (
            organization_id
            and principal is not None
            and principal.allows("admins.manage")
        ):
            audit_source = "platform_delegation"
        try:
            with organization_store.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO security_audit_log("
                    "occurred_at, request_id, source, actor_user_id, "
                    "organization_id, action, resource, status_code, detail"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        request_id,
                        audit_source,
                        (
                            principal.user.user_id
                            if principal is not None
                            else None
                        ),
                        organization_id,
                        request.method,
                        str(resource),
                        int(response.status_code),
                        "success"
                        if response.status_code < 400
                        else "rejected",
                    ),
                )
        except Exception:
            # Audit failures must not disclose request data or break the action.
            pass
        return response


class SessionAuthMiddleware(BaseHTTPMiddleware):
    _RETIRED_CONFIG_PREFIXES = (
        "/api/models",
        "/api/agents",
        "/api/skills",
        "/api/mcp",
        "/api/channels",
        "/api/schedules",
    )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_open(path):
            return await call_next(request)

        auth_service = getattr(request.app.state, "admin_auth", None)
        token = request.cookies.get(SESSION_COOKIE)
        principal = auth_service.identify(token) if auth_service else None
        if principal is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "未登录"}, status_code=401)
            login_url = "/login"
            if path and path != "/":
                login_url = "/login?next={}".format(path)
            return RedirectResponse(login_url, status_code=302)

        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and path.startswith("/api/")
            and not path.startswith("/api/v2/")
            and path != "/api/auth/logout"
            and not principal.allows("admins.manage")
        ):
            return JSONResponse(
                {"detail": "旧版管理接口仅允许平台管理员调用"},
                status_code=403,
            )

        request.state.principal = principal
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not bool(
                getattr(request.app.state, "allow_legacy_config_writes", False)
            )
            and any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in self._RETIRED_CONFIG_PREFIXES
            )
        ):
            return JSONResponse(
                {
                    "detail": (
                        "旧配置写入接口已停用，请使用 "
                        "/api/v2/platform/catalog 的草稿与发布接口"
                    )
                },
                status_code=410,
            )
        return await call_next(request)
