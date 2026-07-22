"""Local Ollama embedding client with strict response validation."""

from __future__ import annotations

from typing import List, Optional

import httpx

from src.core.config.loader import EmbeddingProfile


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(
        self, profile: EmbeddingProfile, client: Optional[httpx.Client] = None
    ) -> None:
        self.profile = profile
        self.client = client or httpx.Client(trust_env=False)
        self._owns_client = client is None

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        if len(texts) > 64 or any(not isinstance(text, str) or not text for text in texts):
            raise EmbeddingError("embedding 批次必须包含 1 到 64 条非空文本")
        try:
            response = self.client.post(
                self.profile.base_url.rstrip("/") + "/api/embed",
                json={"model": self.profile.model, "input": texts},
                timeout=self.profile.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingError("本地 embedding 模型暂不可用") from exc
        vectors = payload.get("embeddings") if isinstance(payload, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbeddingError("embedding 返回数量无效")
        normalized: List[List[float]] = []
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != self.profile.dimensions:
                raise EmbeddingError("embedding 向量维度无效")
            if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in vector):
                raise EmbeddingError("embedding 向量包含无效数值")
            normalized.append([float(value) for value in vector])
        return normalized

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
