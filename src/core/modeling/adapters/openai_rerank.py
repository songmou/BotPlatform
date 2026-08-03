"""OpenAI-compatible rerank adapter (POST /v1/rerank)."""

from __future__ import annotations

from typing import List, Optional, Tuple

import httpx

from src.core.modeling.contracts import RerankError


class OpenAIRerankAdapter:
    """Call an OpenAI-compatible ``/v1/rerank`` endpoint (Jina/Cohere/SiliconFlow)."""

    def __init__(
        self,
        *,
        profile_id: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._model_id = profile_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.client = client or httpx.Client(trust_env=False)
        self._owns_client = client is None

    @property
    def model_id(self) -> str:
        return self._model_id

    def rerank(
        self, query: str, documents: List[str], top_n: Optional[int] = None
    ) -> List[Tuple[int, float]]:
        if not query or not isinstance(query, str):
            raise RerankError("重排查询必须是非空文本")
        if not documents:
            return []
        payload = {
            "model": self.model,
            "query": query,
            "documents": list(documents),
        }
        if top_n is not None:
            payload["top_n"] = top_n
        headers = {
            "Authorization": "Bearer {}".format(self.api_key),
            "Content-Type": "application/json",
        }
        try:
            response = self.client.post(
                self.base_url + "/v1/rerank",
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RerankError("重排模型暂不可用") from exc
        return _parse_results(data, len(documents))

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


def _parse_results(data: object, document_count: int) -> List[Tuple[int, float]]:
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise RerankError("重排返回结果无效")
    ranked: List[Tuple[int, float]] = []
    seen = set()
    for item in results:
        if not isinstance(item, dict):
            raise RerankError("重排返回结果无效")
        index = item.get("index")
        score = item.get("relevance_score")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= document_count
        ):
            raise RerankError("重排返回的文档序号越界")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise RerankError("重排返回的分数无效")
        if index in seen:
            raise RerankError("重排返回结果包含重复文档")
        seen.add(index)
        ranked.append((index, float(score)))
    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return ranked
