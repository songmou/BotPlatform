"""Unit tests for embedding/rerank adapters and their factory dispatch."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from src.core.config.loader import ModelProfile
from src.core.modeling import ModelCapabilities
from src.core.modeling.adapters.ollama_embedding import OllamaEmbeddingAdapter
from src.core.modeling.adapters.openai_embedding import OpenAIEmbeddingAdapter
from src.core.modeling.adapters.openai_rerank import OpenAIRerankAdapter
from src.core.modeling.adapters.local_transformers_rerank import (
    LocalTransformersRerankAdapter,
)
from src.core.modeling.contracts import EmbeddingError, ModelError, RerankError
from src.core.modeling.factory import (
    create_embedding_client,
    create_model_client,
    create_rerank_client,
)


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _profile(**overrides) -> ModelProfile:
    base = dict(
        id="vec",
        enabled=True,
        type="ollama",
        provider="ollama",
        base_url="http://127.0.0.1:11434",
        model="bge-m3",
        temperature=0.0,
        max_tokens=1,
        timeout_seconds=30,
        capabilities=ModelCapabilities(False, False, False),
        modality="embedding",
        dimensions=4,
    )
    base.update(overrides)
    return ModelProfile(**base)


class OllamaEmbeddingAdapterTest(unittest.TestCase):
    def test_embed_success_hits_api_embed(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"embeddings": [[1.0, 0.0, 0.0, 0.0]]})

        adapter = OllamaEmbeddingAdapter(
            profile_id="vec",
            base_url="http://127.0.0.1:11434",
            model="bge-m3",
            dimensions=4,
            timeout_seconds=5,
            client=_mock_client(handler),
        )
        self.assertEqual(adapter.model_id, "vec")
        self.assertEqual(adapter.dimensions, 4)
        self.assertEqual(adapter.embed(["水果"]), [[1.0, 0.0, 0.0, 0.0]])
        self.assertTrue(seen["url"].endswith("/api/embed"))

    def test_dimension_mismatch_raises(self):
        adapter = OllamaEmbeddingAdapter(
            profile_id="vec",
            base_url="http://127.0.0.1:11434",
            model="bge-m3",
            dimensions=4,
            timeout_seconds=5,
            client=_mock_client(
                lambda r: httpx.Response(200, json={"embeddings": [[1.0, 2.0]]})
            ),
        )
        with self.assertRaises(EmbeddingError):
            adapter.embed(["水果"])

    def test_count_mismatch_raises(self):
        adapter = OllamaEmbeddingAdapter(
            profile_id="vec",
            base_url="http://127.0.0.1:11434",
            model="bge-m3",
            dimensions=4,
            timeout_seconds=5,
            client=_mock_client(
                lambda r: httpx.Response(200, json={"embeddings": [[1.0, 0, 0, 0]]})
            ),
        )
        with self.assertRaises(EmbeddingError):
            adapter.embed(["a", "b"])

    def test_non_numeric_value_raises(self):
        adapter = OllamaEmbeddingAdapter(
            profile_id="vec",
            base_url="http://127.0.0.1:11434",
            model="bge-m3",
            dimensions=4,
            timeout_seconds=5,
            client=_mock_client(
                lambda r: httpx.Response(
                    200, json={"embeddings": [["x", 0, 0, 0]]}
                )
            ),
        )
        with self.assertRaises(EmbeddingError):
            adapter.embed(["水果"])

    def test_http_error_wrapped(self):
        adapter = OllamaEmbeddingAdapter(
            profile_id="vec",
            base_url="http://127.0.0.1:11434",
            model="bge-m3",
            dimensions=4,
            timeout_seconds=5,
            client=_mock_client(lambda r: httpx.Response(500, text="boom")),
        )
        with self.assertRaises(EmbeddingError):
            adapter.embed(["水果"])

    def test_oversized_batch_rejected_before_request(self):
        adapter = OllamaEmbeddingAdapter(
            profile_id="vec",
            base_url="http://127.0.0.1:11434",
            model="bge-m3",
            dimensions=4,
            timeout_seconds=5,
            client=_mock_client(lambda r: httpx.Response(200, json={})),
        )
        with self.assertRaises(EmbeddingError):
            adapter.embed(["x"] * 65)


class OpenAIEmbeddingAdapterTest(unittest.TestCase):
    def test_embed_sorts_by_index_and_sends_bearer(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            seen["url"] = str(request.url)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0]},
                        {"index": 0, "embedding": [1.0, 0.0]},
                    ]
                },
            )

        adapter = OpenAIEmbeddingAdapter(
            profile_id="vec",
            base_url="https://api.example.com",
            api_key="secret",
            model="text-embedding-3",
            dimensions=2,
            timeout_seconds=5,
            client=_mock_client(handler),
        )
        result = adapter.embed(["a", "b"])
        self.assertEqual(result, [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(seen["auth"], "Bearer secret")
        self.assertTrue(seen["url"].endswith("/v1/embeddings"))

    def test_http_error_wrapped(self):
        adapter = OpenAIEmbeddingAdapter(
            profile_id="vec",
            base_url="https://api.example.com",
            api_key="secret",
            model="text-embedding-3",
            dimensions=2,
            timeout_seconds=5,
            client=_mock_client(lambda r: httpx.Response(401, text="no")),
        )
        with self.assertRaises(EmbeddingError):
            adapter.embed(["a"])


class OpenAIRerankAdapterTest(unittest.TestCase):
    def _adapter(self, handler):
        return OpenAIRerankAdapter(
            profile_id="rr",
            base_url="https://api.example.com",
            api_key="secret",
            model="bge-reranker",
            timeout_seconds=5,
            client=_mock_client(handler),
        )

    def test_rerank_sorts_desc_and_forwards_top_n(self):
        seen = {}

        def handler(request):
            import json

            seen["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 0, "relevance_score": 0.1},
                        {"index": 2, "relevance_score": 0.9},
                        {"index": 1, "relevance_score": 0.5},
                    ]
                },
            )

        adapter = self._adapter(handler)
        ranked = adapter.rerank("q", ["a", "b", "c"], top_n=2)
        self.assertEqual([index for index, _ in ranked], [2, 1, 0])
        self.assertEqual(seen["body"]["top_n"], 2)
        self.assertEqual(seen["body"]["query"], "q")

    def test_index_out_of_range_raises(self):
        adapter = self._adapter(
            lambda r: httpx.Response(
                200, json={"results": [{"index": 9, "relevance_score": 0.5}]}
            )
        )
        with self.assertRaises(RerankError):
            adapter.rerank("q", ["a", "b"])

    def test_duplicate_index_raises(self):
        adapter = self._adapter(
            lambda r: httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 0, "relevance_score": 0.5},
                        {"index": 0, "relevance_score": 0.4},
                    ]
                },
            )
        )
        with self.assertRaises(RerankError):
            adapter.rerank("q", ["a", "b"])

    def test_http_error_wrapped(self):
        adapter = self._adapter(lambda r: httpx.Response(500, text="boom"))
        with self.assertRaises(RerankError):
            adapter.rerank("q", ["a", "b"])


class FactoryDispatchTest(unittest.TestCase):
    def test_embedding_dispatch_ollama(self):
        client = create_embedding_client(_profile())
        self.assertIsInstance(client, OllamaEmbeddingAdapter)
        self.assertEqual(client.model_id, "vec")
        client.close()

    def test_embedding_dispatch_openai_requires_key(self):
        profile = _profile(
            type="openai_compatible",
            provider="siliconflow",
            base_url="https://api.example.com",
            api_key_env="EMB_KEY",
        )
        with patch.dict("os.environ", {"EMB_KEY": "secret"}, clear=False):
            client = create_embedding_client(profile)
        self.assertIsInstance(client, OpenAIEmbeddingAdapter)
        client.close()

    def test_embedding_openai_missing_key_raises(self):
        profile = _profile(
            type="openai_compatible",
            base_url="https://api.example.com",
            api_key_env="MISSING_KEY",
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ModelError):
                create_embedding_client(profile)

    def test_embedding_rejects_non_embedding_modality(self):
        with self.assertRaises(ModelError):
            create_embedding_client(_profile(modality="chat"))

    def test_embedding_requires_dimensions(self):
        with self.assertRaises(ModelError):
            create_embedding_client(_profile(dimensions=None))

    def test_rerank_dispatch_openai(self):
        profile = _profile(
            modality="rerank",
            type="openai_compatible",
            base_url="https://api.example.com",
            api_key_env="RR_KEY",
            dimensions=None,
        )
        with patch.dict("os.environ", {"RR_KEY": "secret"}, clear=False):
            client = create_rerank_client(profile)
        self.assertIsInstance(client, OpenAIRerankAdapter)
        client.close()

    def test_rerank_rejects_ollama_type(self):
        profile = _profile(modality="rerank", type="ollama", dimensions=None)
        with self.assertRaises(ModelError):
            create_rerank_client(profile)

    def test_rerank_dispatch_local_transformers(self):
        profile = _profile(
            modality="rerank",
            type="local_transformers",
            provider="local",
            base_url="local://transformers",
            model="BAAI/bge-reranker-v2-m3",
            dimensions=None,
        )
        client = create_rerank_client(profile)
        self.assertIsInstance(client, LocalTransformersRerankAdapter)
        client.close()

    def test_chat_factory_rejects_embedding_modality(self):
        with self.assertRaises(ModelError):
            create_model_client(_profile(modality="embedding"))


if __name__ == "__main__":
    unittest.main()
