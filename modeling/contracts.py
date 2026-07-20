"""Pure domain contracts shared by model adapters and orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class ModelIdentity:
    profile_id: str
    provider: str
    configured_model: str


@dataclass(frozen=True)
class ModelCapabilities:
    tools: bool = False
    vision: bool = False
    reasoning: bool = False


@dataclass(frozen=True)
class CanonicalToolCall:
    call_id: str
    name: str
    arguments: Dict[str, Any]


@dataclass(frozen=True)
class CanonicalMessage:
    role: str
    content: str = ""
    tool_calls: List[CanonicalToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    extensions: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GenerationOptions:
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    reasoning: Optional[bool] = None


@dataclass(frozen=True)
class ModelRequest:
    messages: List[CanonicalMessage]
    tools: List[Dict[str, Any]] = field(default_factory=list)
    image: Optional[bytes] = None
    generation: GenerationOptions = field(default_factory=GenerationOptions)


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


@dataclass(frozen=True)
class ModelResponse:
    message: CanonicalMessage
    actual_model: Optional[str] = None
    usage: Optional[ModelUsage] = None
    request_id: Optional[str] = None
    finish_reason: Optional[str] = None


class ModelError(RuntimeError):
    """Sanitized model error safe to surface outside the adapter layer."""

    def __init__(
        self,
        safe_message: str,
        *,
        provider: str = "unknown",
        status_code: Optional[int] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(safe_message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.safe_message = safe_message


class ModelClient(Protocol):
    @property
    def identity(self) -> ModelIdentity: ...

    @property
    def capabilities(self) -> ModelCapabilities: ...

    def ensure_ready(self) -> None: ...

    def complete(self, request: ModelRequest) -> ModelResponse: ...

    def close(self) -> None: ...
