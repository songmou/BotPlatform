"""Session-based authentication middleware for the web management panel."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

SESSION_COOKIE = "admin_session"
SESSION_MAX_AGE = 86400 * 7

OPEN_PATHS = {"/api/health", "/static", "/login", "/api/auth/login"}

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
        return response


class SessionAuthMiddleware(BaseHTTPMiddleware):
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

        request.state.principal = principal
        return await call_next(request)
