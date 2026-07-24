"""Bot adapter management endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request

from src.core.paths import CREDENTIALS_PATH

bots_router = APIRouter(prefix="/api/bots", tags=["bots"])


@bots_router.get("")
def list_bots(request: Request):
    """List configured bot adapters and their connection status."""
    result = []

    if CREDENTIALS_PATH.exists():
        try:
            data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
            result.append({
                "id": "ilink",
                "channel": "ilink",
                "bot_id": data.get("bot_id", ""),
                "user_id": data.get("user_id", ""),
                "connected": True,
            })
        except (OSError, ValueError, json.JSONDecodeError):
            result.append({
                "id": "ilink",
                "channel": "ilink",
                "bot_id": "",
                "user_id": "",
                "connected": False,
            })
    else:
        result.append({
            "id": "ilink",
            "channel": "ilink",
            "bot_id": "",
            "user_id": "",
            "connected": False,
        })

    return result
