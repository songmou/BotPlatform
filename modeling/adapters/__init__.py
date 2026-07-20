"""Concrete model protocol adapters."""

from .ollama import OllamaAdapter
from .openai_compatible import OpenAICompatibleAdapter

__all__ = ["OllamaAdapter", "OpenAICompatibleAdapter"]
