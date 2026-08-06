"""Pure domain contracts shared by model adapters and orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple


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
class ModelCallContext:
    run_id: Optional[str] = None
    tenant_id: Optional[str] = None
    user_id: Optional[int] = None
    source: str = "internal"
    operation: str = "answer"
    agent_id: Optional[str] = None
    conversation_id: Optional[str] = None


@dataclass(frozen=True)
class ModelRequest:
    messages: List[CanonicalMessage]
    tools: List[Dict[str, Any]] = field(default_factory=list)
    image: Optional[bytes] = None
    generation: GenerationOptions = field(default_factory=GenerationOptions)
    context: ModelCallContext = field(default_factory=ModelCallContext)


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    uncached_input_tokens: Optional[int] = None
    reasoning_output_tokens: Optional[int] = None


@dataclass(frozen=True)
class ModelResponse:
    message: CanonicalMessage
    actual_model: Optional[str] = None
    usage: Optional[ModelUsage] = None
    request_id: Optional[str] = None
    finish_reason: Optional[str] = None


@dataclass(frozen=True)
class ModelStreamEvent:
    text: str = ""
    response: Optional[ModelResponse] = None


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


class EmbeddingError(RuntimeError):
    """Sanitized embedding error safe to surface outside the adapter layer."""


class RerankError(RuntimeError):
    """Sanitized rerank error safe to surface outside the adapter layer."""


class EmbeddingClient(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def fingerprint(self) -> str:
        """Stable identity of the embedding space (profile@model@dimensions)."""

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: List[str]) -> List[List[float]]: ...

    def close(self) -> None: ...


class RerankClient(Protocol):
    @property
    def model_id(self) -> str: ...

    def rerank(
        self, query: str, documents: List[str], top_n: Optional[int] = None
    ) -> List[Tuple[int, float]]:
        """Return (original_index, score) pairs sorted by descending score."""
        ...

    def close(self) -> None: ...
