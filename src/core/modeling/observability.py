"""Provider-independent model call instrumentation."""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .contracts import (
    ModelCapabilities,
    ModelClient,
    ModelIdentity,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
)

logger = logging.getLogger(__name__)

ModelCallLogger = Callable[
    [
        ModelIdentity,
        str,
        str,
        float,
        Optional[ModelUsage],
        int,
        Optional[str],
        object,
        Optional[str],
        Optional[float],
        Optional[BaseException],
    ],
    None,
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
        except Exception as exc:
            self._safe_log(
                self.identity,
                self.identity.configured_model,
                "失败",
                time.monotonic() - started,
                None,
                0,
                None,
                request.context,
                None,
                None,
                exc,
            )
            raise
        self._safe_log(
            self.identity,
            response.actual_model or self.identity.configured_model,
            "成功",
            time.monotonic() - started,
            response.usage,
            len(response.message.tool_calls),
            response.request_id,
            request.context,
            response.finish_reason,
            None,
            None,
        )
        return response

    def complete_stream(self, request: ModelRequest):
        started = time.monotonic()
        first_token_seconds: Optional[float] = None
        final_response: Optional[ModelResponse] = None
        try:
            stream = self._client.complete_stream(request)  # type: ignore[attr-defined]
            for item in stream:
                if isinstance(item, ModelStreamEvent):
                    if item.text:
                        if first_token_seconds is None:
                            first_token_seconds = time.monotonic() - started
                        yield item.text
                    if item.response is not None:
                        final_response = item.response
                else:
                    text = str(item)
                    if text:
                        if first_token_seconds is None:
                            first_token_seconds = time.monotonic() - started
                        yield text
        except GeneratorExit as exc:
            self._safe_log(
                self.identity,
                self.identity.configured_model,
                "取消",
                time.monotonic() - started,
                None,
                0,
                None,
                request.context,
                None,
                first_token_seconds,
                exc,
            )
            raise
        except Exception as exc:
            self._safe_log(
                self.identity,
                self.identity.configured_model,
                "失败",
                time.monotonic() - started,
                None,
                0,
                None,
                request.context,
                None,
                first_token_seconds,
                exc,
            )
            raise
        response = final_response or ModelResponse(
            message=self._empty_message(),
            actual_model=self.identity.configured_model,
        )
        self._safe_log(
            self.identity,
            response.actual_model or self.identity.configured_model,
            "成功",
            time.monotonic() - started,
            response.usage,
            len(response.message.tool_calls),
            response.request_id,
            request.context,
            response.finish_reason,
            first_token_seconds,
            None,
        )

    @staticmethod
    def _empty_message():
        from .contracts import CanonicalMessage

        return CanonicalMessage("assistant", "")

    def _safe_log(self, *values) -> None:
        try:
            self._logger(*values)
        except Exception:
            logger.exception("模型观测数据记录失败，已忽略")

    def close(self) -> None:
        self._client.close()
