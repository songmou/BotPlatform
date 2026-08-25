"""Local Ollama embedding adapter with strict response validation."""

from __future__ import annotations

from typing import List, Optional

import httpx

from src.core.modeling.contracts import EmbeddingError

_MAX_BATCH = 64


class OllamaEmbeddingAdapter:
    """Call the Ollama ``/api/embed`` endpoint behind the embedding contract."""

    def __init__(
        self,
        *,
        profile_id: str,
        base_url: str,
        model: str,
        dimensions: int,
        timeout_seconds: float,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._model_id = profile_id
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._dimensions = dimensions
        self.timeout_seconds = timeout_seconds
        self.client = client or httpx.Client(trust_env=False)
        self._owns_client = client is None

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def fingerprint(self) -> str:
        return "{}@{}@{}".format(self._model_id, self.model, self._dimensions)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        if len(texts) > _MAX_BATCH or any(
            not isinstance(text, str) or not text for text in texts
        ):
            raise EmbeddingError(
                "embedding 批次必须包含 1 到 {} 条非空文本".format(_MAX_BATCH)
            )
        try:
            response = self.client.post(
                self.base_url + "/api/embed",
                json={"model": self.model, "input": texts},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingError("向量模型暂不可用") from exc
        vectors = payload.get("embeddings") if isinstance(payload, dict) else None
        return _normalize_vectors(vectors, len(texts), self._dimensions)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


def _normalize_vectors(
    vectors: object, expected_count: int, dimensions: int
) -> List[List[float]]:
    if not isinstance(vectors, list) or len(vectors) != expected_count:
        raise EmbeddingError("embedding 返回数量无效")
    normalized: List[List[float]] = []
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimensions:
            raise EmbeddingError("embedding 向量维度无效")
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in vector
        ):
            raise EmbeddingError("embedding 向量包含无效数值")
        normalized.append([float(value) for value in vector])
    return normalized
