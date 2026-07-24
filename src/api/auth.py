"""Token-based authentication for the web management panel."""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.core.paths import SYSTEM_DATA_DIR

TOKEN_FILE = SYSTEM_DATA_DIR / "web_token"
COOKIE_NAME = "web_token"
TOKEN_LENGTH = 32


def load_or_create_token() -> str:
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_hex(TOKEN_LENGTH)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


def verify_token(request: Request, token: str | None) -> bool:
    if not token:
        return False
    expected = getattr(request.app.state, "web_token", None)
    if not expected:
        return False
    return secrets.compare_digest(token, expected)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    OPEN_PATHS = {"/api/health", "/static"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path == p or path.startswith(p + "/") for p in self.OPEN_PATHS):
            return await call_next(request)

        token = (
            request.query_params.get("token")
            or request.cookies.get(COOKIE_NAME)
            or _bearer_token(request)
        )
        if not verify_token(request, token):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "未授权"}, status_code=401)
            return Response(status_code=401, content="Unauthorized")

        response = await call_next(request)
        if request.query_params.get("token") and verify_token(request, token):
            response.set_cookie(
                COOKIE_NAME, token, httponly=True, samesite="lax", max_age=86400 * 30
            )
        return response


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None
