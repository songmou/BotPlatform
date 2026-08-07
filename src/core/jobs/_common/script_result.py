"""Shared helper for built-in scripts to report results to the platform.

Built-in scripts run as isolated subprocesses. They communicate their outcome
to the platform by writing a JSON document to the path in the
``ILINKBOT_SCRIPT_RESULT_FILE`` environment variable. Keeping that contract in
one place means the platform and every script can never drift apart.

When a script needs to pause and wait for the user (for example to type a
CAPTCHA), it passes ``await_input`` to :func:`write_script_result`. The platform
reads that field and registers a pending input state so the user's next reply
is routed back to the script instead of being handed to the model.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

SUCCESS = "success"
FAILED = "failed"
SKIPPED = "skipped"
AWAITING_INPUT = "awaiting_input"


def write_script_result(
    status: str,
    summary: str,
    artifacts: Optional[List[str]] = None,
    error: str = "",
    await_input: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist a script result for the platform to pick up.

    ``await_input`` declares that the script is pausing to wait for user input.
    It must be a dict with at least ``param`` (the script argument the reply
    maps to) and ``ttl_seconds`` (how long the platform should wait). Optional
    keys: ``prompt`` (human-facing text) and ``hint`` (input format hint).
    """
    result_file = os.getenv("ILINKBOT_SCRIPT_RESULT_FILE")
    if not result_file:
        return
    payload: Dict[str, Any] = {
        "status": status,
        "summary": summary,
        "artifacts": artifacts or [],
        "error": error,
    }
    if await_input:
        payload["await_input"] = await_input
    path = Path(result_file)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)
