"""Bounded exponential backoff for transient model call failures."""

from __future__ import annotations

import logging
import time
from typing import Callable

from .contracts import ModelError, ModelResponse

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
BASE_DELAY_SECONDS = 0.5
MAX_DELAY_SECONDS = 4.0


def complete_with_retry(
    complete: Callable[[], ModelResponse],
    *,
    profile_id: str,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = BASE_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> ModelResponse:
    """Run one model call, retrying transient failures with backoff.

    Only errors flagged ``retryable`` by the adapters (timeouts, connection
    failures, HTTP 429/5xx) are retried; authentication and validation errors
    surface immediately. The final error is re-raised unchanged so router
    failover and cooldown still apply.
    """
    attempt = 1
    while True:
        try:
            return complete()
        except ModelError as exc:
            if not exc.retryable or attempt >= attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), MAX_DELAY_SECONDS)
            logger.warning(
                "模型档案 %s 调用失败（第 %d/%d 次）：%s，%.1f 秒后重试",
                profile_id,
                attempt,
                attempts,
                exc.safe_message,
                delay,
            )
            sleep(delay)
            attempt += 1
