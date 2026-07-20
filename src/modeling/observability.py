"""Provider-independent model call instrumentation."""

from __future__ import annotations

import time
from typing import Callable, Optional

from .contracts import (
    ModelCapabilities,
    ModelClient,
    ModelIdentity,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)


ModelCallLogger = Callable[
    [ModelIdentity, str, str, float, Optional[ModelUsage], int, Optional[str]], None
]


class ObservedModelClient:
    """Decorate any model client without exposing request content to logging."""

    def __init__(self, client: ModelClient, logger: ModelCallLogger) -> None:
        self._client = client
        self._logger = logger

    @property
    def identity(self) -> ModelIdentity:
        return self._client.identity

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._client.capabilities

    def ensure_ready(self) -> None:
        self._client.ensure_ready()

    def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.monotonic()
        try:
            response = self._client.complete(request)
        except Exception:
            self._logger(
                self.identity,
                self.identity.configured_model,
                "失败",
                time.monotonic() - started,
                None,
                0,
                None,
            )
            raise
        self._logger(
            self.identity,
            response.actual_model or self.identity.configured_model,
            "成功",
            time.monotonic() - started,
            response.usage,
            len(response.message.tool_calls),
            response.request_id,
        )
        return response

    def close(self) -> None:
        self._client.close()
