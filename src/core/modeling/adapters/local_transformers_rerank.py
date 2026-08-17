"""Optional local Transformers cross-encoder rerank adapter."""

from __future__ import annotations

import threading
from typing import Any, List, Optional, Tuple

from src.core.modeling.contracts import RerankError


class LocalTransformersRerankAdapter:
    """Rerank query/document pairs with a local sequence classifier.

    Heavy dependencies are imported and the model is loaded lazily so the
    default installation remains unchanged when this optional profile is off.
    """

    def __init__(
        self,
        *,
        profile_id: str,
        model: str,
        timeout_seconds: float,
        batch_size: int = 8,
        max_length: int = 1024,
        tokenizer: Any = None,
        classifier: Any = None,
        torch_module: Any = None,
    ) -> None:
        self._model_id = profile_id
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.batch_size = max(1, int(batch_size))
        self.max_length = max(128, int(max_length))
        self._tokenizer = tokenizer
        self._classifier = classifier
        self._torch = torch_module
        self._device = "cpu"
        self._load_lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return self._model_id

    def _ensure_loaded(self) -> None:
        if self._tokenizer is not None and self._classifier is not None:
            return
        with self._load_lock:
            if self._tokenizer is not None and self._classifier is not None:
                return
            try:
                import torch  # type: ignore
                from transformers import (  # type: ignore
                    AutoModelForSequenceClassification,
                    AutoTokenizer,
                )

                self._torch = torch
                self._tokenizer = AutoTokenizer.from_pretrained(self.model)
                self._classifier = AutoModelForSequenceClassification.from_pretrained(
                    self.model
                )
                if torch.cuda.is_available():
                    self._device = "cuda"
                elif (
                    hasattr(torch.backends, "mps")
                    and torch.backends.mps.is_available()
                ):
                    self._device = "mps"
                self._classifier.to(self._device)
                self._classifier.eval()
            except Exception as exc:
                raise RerankError(
                    "本地重排模型不可用，请安装 requirements-rerank.txt 并确认模型已下载"
                ) from exc

    def rerank(
        self, query: str, documents: List[str], top_n: Optional[int] = None
    ) -> List[Tuple[int, float]]:
        if not isinstance(query, str) or not query.strip():
            raise RerankError("重排查询必须是非空文本")
        if not documents:
            return []
        self._ensure_loaded()
        scores: List[float] = []
        try:
            for offset in range(0, len(documents), self.batch_size):
                batch = documents[offset : offset + self.batch_size]
                encoded = self._tokenizer(
                    [query] * len(batch),
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=self.max_length,
                    return_tensors="pt",
                )
                if self._device != "cpu":
                    encoded = {
                        key: value.to(self._device) for key, value in encoded.items()
                    }
                with self._torch.no_grad():
                    logits = self._classifier(**encoded).logits
                values = logits.detach().float().cpu().reshape(-1).tolist()
                scores.extend(float(value) for value in values)
        except Exception as exc:
            raise RerankError("本地重排模型推理失败") from exc
        ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
        if top_n is not None:
            ranked = ranked[: max(0, int(top_n))]
        return ranked

    def close(self) -> None:
        self._classifier = None
        self._tokenizer = None
        if self._torch is not None and self._device == "cuda":
            self._torch.cuda.empty_cache()
        elif self._torch is not None and self._device == "mps":
            self._torch.mps.empty_cache()
