"""Unit tests for the standalone knowledge recall benchmark helpers."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "knowledge_recall_benchmark.py"
SPEC = importlib.util.spec_from_file_location("knowledge_recall_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


class KnowledgeRecallBenchmarkTests(unittest.TestCase):
    def test_stable_order_and_source_mapping(self) -> None:
        first = benchmark.stable_order(["a", "b", "c"], "demo")
        second = benchmark.stable_order(["c", "a", "b"], "demo")
        self.assertEqual(first, second)
        self.assertEqual(
            benchmark.source_doc_id("t2ranking__123.md"), ("t2ranking", "123")
        )
        self.assertIsNone(benchmark.source_doc_id("ordinary.md"))

    def test_query_metrics_with_graded_relevance(self) -> None:
        metrics = benchmark.query_metrics(
            ["noise", "doc-b", "doc-a"], {"doc-a": 2.0, "doc-b": 1.0}
        )
        self.assertEqual(metrics["hit_rate@1"], 0.0)
        self.assertEqual(metrics["hit_rate@3"], 1.0)
        self.assertEqual(metrics["recall@3"], 1.0)
        self.assertEqual(metrics["mrr@3"], 0.5)
        self.assertGreater(metrics["ndcg@3"], 0.0)
        self.assertLess(metrics["ndcg@3"], 1.0)

    def test_aggregate_and_reports(self) -> None:
        rows = []
        for benchmark_name, hit, latency in (
            ("t2ranking", 1.0, 10.0),
            ("scifact", 0.0, 30.0),
        ):
            metrics = {
                "{}@{}".format(name, k): hit
                for k in benchmark.KS
                for name in ("hit_rate", "recall", "precision", "mrr", "ndcg")
            }
            rows.append(
                {
                    "benchmark": benchmark_name,
                    "split": "test",
                    "query_id": "q1",
                    "query": "question",
                    "relevant": {"doc": 1.0},
                    "retrieved": ["doc"] if hit else [],
                    "latency_ms": latency,
                    "metrics": metrics,
                    "diagnostics": {
                        "vector_degraded": not bool(hit),
                        "rerank_degraded": False,
                    },
                }
            )
        summary = benchmark.aggregate(rows)
        self.assertEqual(summary["combined"]["queries"], 2)
        self.assertEqual(summary["combined"]["hit_rate@6"], 0.5)
        self.assertEqual(summary["combined"]["latency_p50_ms"], 20.0)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            payload = {"split": "test", "limit": 6, "summary": summary, "rows": rows}
            benchmark.write_reports(payload, output)
            self.assertTrue(output.with_suffix(".json").exists())
            self.assertTrue(output.with_suffix(".csv").exists())
            markdown = output.with_suffix(".md").read_text(encoding="utf-8")
            self.assertIn("严格验收", markdown)
            self.assertIn("| t2ranking | 1 |", markdown)
            csv_text = output.with_suffix(".csv").read_text(encoding="utf-8")
            self.assertIn("precision@3", csv_text.splitlines()[0])
            self.assertIn("failure_reason", csv_text.splitlines()[0])
            self.assertIn("empty_result", csv_text)
            loaded = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(len(loaded["rows"]), 2)


if __name__ == "__main__":
    unittest.main()
