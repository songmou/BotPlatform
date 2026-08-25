"""Provider-neutral model contracts and client construction."""

from .contracts import (
    CanonicalMessage,
    CanonicalToolCall,
    EmbeddingClient,
    EmbeddingError,
    GenerationOptions,
    ModelCallContext,
    ModelCapabilities,
    ModelClient,
    ModelError,
    ModelIdentity,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
    RerankClient,
    RerankError,
)
from .router import ModelRouter, ModelSession

__all__ = [
    "CanonicalMessage",
    "CanonicalToolCall",
    "EmbeddingClient",
    "EmbeddingError",
    "GenerationOptions",
    "ModelCallContext",
    "ModelCapabilities",
    "ModelClient",
    "ModelError",
    "ModelIdentity",
    "ModelRequest",
    "ModelResponse",
    "ModelStreamEvent",
    "ModelUsage",
    "RerankClient",
    "RerankError",
    "ModelRouter",
    "ModelSession",
]
