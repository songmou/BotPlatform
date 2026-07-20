"""Provider-neutral model contracts and client construction."""

from .contracts import (
    CanonicalMessage,
    CanonicalToolCall,
    GenerationOptions,
    ModelCapabilities,
    ModelClient,
    ModelError,
    ModelIdentity,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from .router import ModelRouter, ModelSession

__all__ = [
    "CanonicalMessage",
    "CanonicalToolCall",
    "GenerationOptions",
    "ModelCapabilities",
    "ModelClient",
    "ModelError",
    "ModelIdentity",
    "ModelRequest",
    "ModelResponse",
    "ModelUsage",
    "ModelRouter",
    "ModelSession",
]
