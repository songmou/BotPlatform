"""Concrete model protocol adapters."""

from .ollama import OllamaAdapter
from .ollama_embedding import OllamaEmbeddingAdapter
from .openai_compatible import OpenAICompatibleAdapter
from .openai_embedding import OpenAIEmbeddingAdapter
from .openai_rerank import OpenAIRerankAdapter

__all__ = [
    "OllamaAdapter",
    "OllamaEmbeddingAdapter",
    "OpenAICompatibleAdapter",
    "OpenAIEmbeddingAdapter",
    "OpenAIRerankAdapter",
]
