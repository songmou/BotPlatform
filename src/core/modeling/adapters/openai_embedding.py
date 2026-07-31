"""OpenAI-compatible embedding adapter (POST /v1/embeddings)."""

from __future__ import annotations

from typing import List, Optional

import httpx

from src.core.modeling.adapters.ollama_embedding import _normalize_vectors
from src.core.modeling.contracts import EmbeddingError

_MAX_BATCH = 64


class OpenAIEmbeddingAdapter:
    """Use the portable subset of OpenAI's embeddings protocol."""

    def __init__(
        self,
        *,
        profile_id: str,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        timeout_seconds: float,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._model_id = profile_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._dimensions = dimensions
        self.timeout_seconds = timeout_seconds
        self.client = client or httpx.Client(trust_env=False)
        self._owns_client = client is None

    @property
    def model_id(self) -> str:
        return self._model_id

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
        headers = {
            "Authorization": "Bearer {}".format(self.api_key),
            "Content-Type": "application/json",
        }
        try:
            response = self.client.post(
                self.base_url + "/v1/embeddings",
                headers=headers,
                json={"model": self.model, "input": texts},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingError("向量模型暂不可用") from exc
        vectors = _extract_vectors(payload)
        return _normalize_vectors(vectors, len(texts), self._dimensions)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


def _extract_vectors(payload: object) -> List[object]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise EmbeddingError("embedding 返回数量无效")
    ordered = sorted(
        data,
        key=lambda item: (
            item.get("index", 0) if isinstance(item, dict) else 0
        ),
    )
    return [
        item.get("embedding") if isinstance(item, dict) else None
        for item in ordered
    ]
