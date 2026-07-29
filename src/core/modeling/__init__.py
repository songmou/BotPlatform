"""Provider-neutral model contracts and client construction."""

from .contracts import (
    CanonicalMessage,
    CanonicalToolCall,
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
)
from .router import ModelRouter, ModelSession

__all__ = [
    "CanonicalMessage",
    "CanonicalToolCall",
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
    "ModelRouter",
    "ModelSession",
]
