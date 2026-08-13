#!/usr/bin/env python3
"""Prepare and evaluate reproducible bilingual knowledge-retrieval benchmarks."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


SEED = 20260810
QUERY_COUNT = 60
TUNING_COUNT = 30
DOCUMENT_CAP = 800
RESULT_LIMIT = 6
KS = (1, 3, 6)
T2_CORPUS_REPO = "C-MTEB/T2Retrieval"
T2_QRELS_REPO = "C-MTEB/T2Retrieval-qrels"
# The corpus revision is the published C-MTEB conversion visible in the
# repository history.  The qrels revision is resolved and recorded in the
# manifest when its repository does not expose the same commit.
T2_CORPUS_REVISION = "8731a845f1bf500a4f111cf1070785c793d10e64"
T2_QRELS_REVISION = "1c83b8d1544e529875e3f6930f3a1fcf749a8e97"
SCIFACT_URL = (
    "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/"
    "datasets/scifact.zip"
)
SCIFACT_MD5 = "5f7d1de60b170fc8027bb7898e2efca1"
CATEGORY_NAMES = {
    "t2ranking": "召回评测-T2Ranking",
    "scifact": "召回评测-SciFact",
}


@dataclass(frozen=True)
class QueryItem:
    benchmark: str
    query_id: str
    query: str
    split: str
    relevant: Dict[str, float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "benchmark": self.benchmark,
            "query_id": self.query_id,
            "query": self.query,
            "split": self.split,
            "relevant": self.relevant,
        }


def stable_order(values: Iterable[str], namespace: str, seed: int = SEED) -> List[str]:
    return sorted(
        values,
        key=lambda value: hashlib.sha256(
            "{}:{}:{}".format(seed, namespace, value).encode("utf-8")
        ).hexdigest(),
    )


def safe_doc_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")
    if cleaned:
        return cleaned[:160]
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]


def source_doc_id(source_name: str) -> Optional[Tuple[str, str]]:
    stem = Path(source_name).stem
    match = re.fullmatch(r"(t2ranking|scifact)__(.+)", stem)
    if not match:
        return None
    return match.group(1), match.group(2)


def _hash_file(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, target: Path) -> Dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        request = urllib.request.Request(url, headers={"User-Agent": "BotPlatform-benchmark/1"})
        with urllib.request.urlopen(request, timeout=180) as response, target.open("wb") as output:
            shutil.copyfileobj(response, output)
    return {
        "url": url,
        "path": target.name,
        "bytes": target.stat().st_size,
        "sha256": _hash_file(target),
    }


def _hf_tree(repo: str, revision: str) -> Tuple[str, List[Dict[str, Any]]]:
    meta_url = "https://huggingface.co/api/datasets/{}".format(repo)
    request = urllib.request.Request(meta_url, headers={"User-Agent": "BotPlatform-benchmark/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        metadata = json.load(response)
    resolved = str(metadata.get("sha") or revision)
    tree_url = "https://huggingface.co/api/datasets/{}/tree/{}?recursive=true&expand=false".format(
        repo, revision
    )
    request = urllib.request.Request(tree_url, headers={"User-Agent": "BotPlatform-benchmark/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        entries = json.load(response)
    return resolved, [item for item in entries if item.get("type") == "file"]


def _hf_parquets(
    repo: str, revision: str, cache_dir: Path
) -> Tuple[str, List[Path], List[Dict[str, Any]]]:
    resolved, entries = _hf_tree(repo, revision)
    parquet_entries = [
        item for item in entries if str(item.get("path") or "").endswith(".parquet")
    ]
    if not parquet_entries:
        raise RuntimeError("数据集 {} 没有可下载的 parquet 文件".format(repo))
    paths: List[Path] = []
    downloads: List[Dict[str, Any]] = []
    for item in parquet_entries:
        relative = str(item["path"])
        target = cache_dir / repo.replace("/", "__") / relative
        url = "https://huggingface.co/datasets/{}/resolve/{}/{}?download=true".format(
            repo, revision, relative
        )
        downloads.append(_download(url, target))
        paths.append(target)
    return resolved, paths, downloads


def _parquet_rows(paths: Sequence[Path]) -> Iterator[Dict[str, Any]]:
    try:
        import pyarrow.parquet as parquet  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "准备 T²Ranking 需要 pyarrow，请先安装 requirements-benchmark.txt"
        ) from exc
    for path in paths:
        table = parquet.read_table(path)
        for batch in table.to_batches(max_chunksize=4096):
            columns = batch.to_pydict()
            for index in range(batch.num_rows):
                yield {name: values[index] for name, values in columns.items()}


def _first(row: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _load_t2(cache_dir: Path) -> Tuple[Dict[str, Dict[str, str]], Dict[str, str], Dict[str, Dict[str, float]], Dict[str, Any]]:
    corpus_revision, corpus_paths, corpus_downloads = _hf_parquets(
        T2_CORPUS_REPO, T2_CORPUS_REVISION, cache_dir
    )
    qrels_revision, qrels_paths, qrels_downloads = _hf_parquets(
        T2_QRELS_REPO, T2_QRELS_REVISION, cache_dir
    )
    corpus: Dict[str, Dict[str, str]] = {}
    queries: Dict[str, str] = {}
    for row in _parquet_rows(corpus_paths):
        row_id = _first(row, ("_id", "id", "corpus-id", "corpus_id", "query-id", "query_id"))
        text = _first(row, ("text", "query", "sentence", "passage"))
        title = _first(row, ("title",)) or ""
        split = str(_first(row, ("split", "type", "subset")) or "").lower()
        if row_id is None or text is None:
            continue
        identifier = str(row_id)
        path_hint = " ".join(path.name.lower() for path in corpus_paths)
        if split == "queries" or ("queries" in path_hint and "corpus" not in path_hint):
            queries[identifier] = str(text)
        else:
            corpus[identifier] = {"title": str(title), "text": str(text)}
    # C-MTEB keeps corpus and queries in separate parquet files.  If file-level
    # hints were insufficient, classify rows by their schema/path names.
    if not queries:
        corpus = {}
        for path in corpus_paths:
            target = queries if "quer" in path.name.lower() else corpus
            for row in _parquet_rows([path]):
                row_id = _first(row, ("_id", "id", "query-id", "query_id", "corpus-id", "corpus_id"))
                text = _first(row, ("text", "query", "sentence", "passage"))
                if row_id is None or text is None:
                    continue
                if target is queries:
                    queries[str(row_id)] = str(text)
                else:
                    corpus[str(row_id)] = {
                        "title": str(_first(row, ("title",)) or ""),
                        "text": str(text),
                    }
    qrels: Dict[str, Dict[str, float]] = {}
    for row in _parquet_rows(qrels_paths):
        query_id = _first(row, ("query-id", "query_id", "qid", "_id"))
        corpus_id = _first(row, ("corpus-id", "corpus_id", "docid", "pid"))
        score = _first(row, ("score", "relevance", "label"))
        if query_id is None or corpus_id is None:
            continue
        qrels.setdefault(str(query_id), {})[str(corpus_id)] = float(score or 1)
    if not corpus or not queries or not qrels:
        raise RuntimeError(
            "T²Ranking 数据格式无法识别：corpus={} queries={} qrels={}".format(
                len(corpus), len(queries), len(qrels)
            )
        )
    return corpus, queries, qrels, {
        "corpus_repo": T2_CORPUS_REPO,
        "corpus_revision": corpus_revision,
        "qrels_repo": T2_QRELS_REPO,
        "qrels_revision": qrels_revision,
        "downloads": corpus_downloads + qrels_downloads,
        "license": "Apache-2.0",
    }


def _jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _load_scifact(cache_dir: Path) -> Tuple[Dict[str, Dict[str, str]], Dict[str, str], Dict[str, Dict[str, float]], Dict[str, Any]]:
    archive = cache_dir / "scifact.zip"
    download = _download(SCIFACT_URL, archive)
    if hashlib.md5(archive.read_bytes()).hexdigest() != SCIFACT_MD5:  # nosec B324 - published integrity value
        raise RuntimeError("SciFact 下载文件 MD5 不匹配")
    root = cache_dir / "scifact"
    if not root.exists():
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(cache_dir)
    corpus_path = next(root.rglob("corpus.jsonl"))
    queries_path = next(root.rglob("queries.jsonl"))
    qrels_path = next(root.rglob("qrels/test.tsv"))
    corpus = {
        str(row.get("_id")): {
            "title": str(row.get("title") or ""),
            "text": str(row.get("text") or ""),
        }
        for row in _jsonl(corpus_path)
    }
    queries = {
        str(row.get("_id")): str(row.get("text") or "")
        for row in _jsonl(queries_path)
    }
    qrels: Dict[str, Dict[str, float]] = {}
    with qrels_path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            query_id = row.get("query-id") or row.get("query_id")
            corpus_id = row.get("corpus-id") or row.get("corpus_id")
            if query_id and corpus_id:
                qrels.setdefault(str(query_id), {})[str(corpus_id)] = float(
                    row.get("score") or 1
                )
    return corpus, queries, qrels, {
        "url": SCIFACT_URL,
        "md5": SCIFACT_MD5,
        "download": download,
        "license": "CC-BY-SA-4.0",
    }


def _trigrams(text: str) -> List[str]:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    seen: Dict[str, None] = {}
    for index in range(max(0, len(normalized) - 2)):
        value = normalized[index : index + 3]
        if value.strip():
            seen[value] = None
    return list(seen)[:48]


def _hard_negative_candidates(
    corpus: Mapping[str, Mapping[str, str]], selected_queries: Sequence[Tuple[str, str]],
    qrels: Mapping[str, Mapping[str, float]], work_dir: Path,
) -> Dict[str, List[str]]:
    database_path = work_dir / "hard-negatives.sqlite3"
    if database_path.exists():
        database_path.unlink()
    connection = sqlite3.connect(str(database_path))
    try:
        connection.execute("CREATE VIRTUAL TABLE docs USING fts5(doc_id UNINDEXED, text, tokenize='trigram')")
        connection.executemany(
            "INSERT INTO docs(doc_id, text) VALUES (?, ?)",
            (
                (doc_id, "{}\n{}".format(item.get("title", ""), item.get("text", "")))
                for doc_id, item in corpus.items()
            ),
        )
        result: Dict[str, List[str]] = {}
        for query_id, query in selected_queries:
            terms = _trigrams(query)
            if not terms:
                result[query_id] = []
                continue
            expression = " OR ".join(
                '"{}"'.format(term.replace('"', '""')) for term in terms
            )
            try:
                rows = connection.execute(
                    "SELECT doc_id FROM docs WHERE docs MATCH ? ORDER BY bm25(docs) LIMIT 80",
                    (expression,),
                ).fetchall()
            except sqlite3.Error:
                rows = []
            relevant = set(qrels.get(query_id, {}))
            result[query_id] = [str(row[0]) for row in rows if str(row[0]) not in relevant]
        return result
    finally:
        connection.close()


def _build_pack(
    benchmark: str,
    corpus: Mapping[str, Mapping[str, str]],
    queries: Mapping[str, str],
    qrels: Mapping[str, Mapping[str, float]],
    output_root: Path,
    work_dir: Path,
    query_count: int,
    tuning_count: int,
    document_cap: int,
) -> Tuple[List[QueryItem], Dict[str, Any]]:
    eligible = [query_id for query_id in qrels if query_id in queries and qrels[query_id]]
    selected_ids = stable_order(eligible, benchmark)[:query_count]
    if len(selected_ids) != query_count:
        raise RuntimeError("{} 只有 {} 个有效问题".format(benchmark, len(selected_ids)))
    items = [
        QueryItem(
            benchmark=benchmark,
            query_id=query_id,
            query=queries[query_id],
            split="tuning" if index < tuning_count else "test",
            relevant={key: float(value) for key, value in qrels[query_id].items()},
        )
        for index, query_id in enumerate(selected_ids)
    ]
    positives = {
        doc_id for item in items for doc_id in item.relevant if doc_id in corpus
    }
    missing = {
        doc_id for item in items for doc_id in item.relevant if doc_id not in corpus
    }
    if missing:
        raise RuntimeError("{} 有 {} 个 qrels 文档不在 corpus".format(benchmark, len(missing)))
    if len(positives) > document_cap:
        raise RuntimeError(
            "{} 的相关文档 {} 超过上限 {}，拒绝静默截断".format(
                benchmark, len(positives), document_cap
            )
        )
    selected_docs = set(positives)
    hard = _hard_negative_candidates(
        corpus, [(item.query_id, item.query) for item in items], qrels, work_dir
    )
    # Round-robin hard negatives keep every query represented.
    for rank in range(80):
        for item in items:
            candidates = hard.get(item.query_id, [])
            if rank < len(candidates):
                selected_docs.add(candidates[rank])
                if len(selected_docs) >= document_cap:
                    break
        if len(selected_docs) >= document_cap:
            break
    if len(selected_docs) < document_cap:
        for doc_id in stable_order(corpus, benchmark + ":random"):
            selected_docs.add(doc_id)
            if len(selected_docs) >= document_cap:
                break
    docs_dir = output_root / benchmark / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for doc_id in stable_order(selected_docs, benchmark + ":write"):
        record = corpus[doc_id]
        title = str(record.get("title") or "").strip() or "文档 {}".format(doc_id)
        body = str(record.get("text") or "").strip()
        path = docs_dir / "{}__{}.md".format(benchmark, safe_doc_id(doc_id))
        path.write_text("# {}\n\n{}\n".format(title, body), encoding="utf-8")
    return items, {
        "queries": len(items),
        "tuning_queries": sum(item.split == "tuning" for item in items),
        "test_queries": sum(item.split == "test" for item in items),
        "documents": len(selected_docs),
        "positive_documents": len(positives),
        "hard_negative_documents": len(selected_docs - positives),
    }


def prepare(args: argparse.Namespace) -> int:
    output = args.output.resolve()
    cache = args.cache.resolve()
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    all_questions: List[QueryItem] = []
    sources: Dict[str, Any] = {}
    stats: Dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="botplatform-kb-benchmark-") as temp:
        work = Path(temp)
        loaders = {"t2ranking": _load_t2, "scifact": _load_scifact}
        for benchmark in ("t2ranking", "scifact"):
            corpus, queries, qrels, source = loaders[benchmark](cache)
            items, item_stats = _build_pack(
                benchmark,
                corpus,
                queries,
                qrels,
                output,
                work,
                args.query_count,
                args.tuning_count,
                args.document_cap,
            )
            all_questions.extend(items)
            sources[benchmark] = source
            stats[benchmark] = item_stats
    questions_path = output / "questions.jsonl"
    with questions_path.open("w", encoding="utf-8") as handle:
        for item in all_questions:
            handle.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
    licenses = output / "LICENSES.md"
    licenses.write_text(
        "# 评测数据许可与引用\n\n"
        "- T²Ranking：Apache-2.0，https://github.com/THUIR/T2Ranking\n"
        "- SciFact / BEIR 转换：CC-BY-SA-4.0，"
        "https://huggingface.co/datasets/BeIR/scifact\n\n"
        "本目录是固定抽样的评测派生物，仅用于知识检索测试。\n",
        encoding="utf-8",
    )
    manifest = {
        "format_version": 1,
        "seed": SEED,
        "query_count": args.query_count,
        "tuning_count": args.tuning_count,
        "test_count": args.query_count - args.tuning_count,
        "document_cap": args.document_cap,
        "sources": sources,
        "stats": stats,
        "questions_sha256": _hash_file(questions_path),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def _load_questions(path: Path, split: str) -> List[QueryItem]:
    items = []
    for row in _jsonl(path):
        item = QueryItem(
            benchmark=str(row["benchmark"]),
            query_id=str(row["query_id"]),
            query=str(row["query"]),
            split=str(row["split"]),
            relevant={str(key): float(value) for key, value in row["relevant"].items()},
        )
        if split == "all" or item.split == split:
            items.append(item)
    return items


def _dcg(relevances: Sequence[float]) -> float:
    return sum(
        (2.0 ** value - 1.0) / math.log2(index + 2.0)
        for index, value in enumerate(relevances)
    )


def query_metrics(retrieved: Sequence[str], relevant: Mapping[str, float]) -> Dict[str, float]:
    deduplicated = list(dict.fromkeys(retrieved))
    metrics: Dict[str, float] = {}
    relevant_ids = set(relevant)
    first_rank = next(
        (index for index, doc_id in enumerate(deduplicated, 1) if doc_id in relevant_ids),
        None,
    )
    for k in KS:
        top = deduplicated[:k]
        found = relevant_ids.intersection(top)
        metrics["hit_rate@{}".format(k)] = 1.0 if found else 0.0
        metrics["recall@{}".format(k)] = len(found) / len(relevant_ids) if relevant_ids else 0.0
        metrics["precision@{}".format(k)] = len(found) / float(k)
        metrics["mrr@{}".format(k)] = (
            1.0 / first_rank if first_rank is not None and first_rank <= k else 0.0
        )
        actual = [float(relevant.get(doc_id, 0.0)) for doc_id in top]
        ideal = sorted((float(value) for value in relevant.values()), reverse=True)[:k]
        denominator = _dcg(ideal)
        metrics["ndcg@{}".format(k)] = _dcg(actual) / denominator if denominator else 0.0
    return metrics


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def aggregate(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    groups: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row["benchmark"]), []).append(row)
    groups["combined"] = list(rows)
    result: Dict[str, Any] = {}
    metric_names = [
        "{}@{}".format(name, k)
        for k in KS
        for name in ("hit_rate", "recall", "precision", "mrr", "ndcg")
    ]
    for name, items in groups.items():
        latencies = [float(item["latency_ms"]) for item in items]
        summary = {
            "queries": len(items),
            "empty_rate": sum(not item["retrieved"] for item in items) / len(items) if items else 0.0,
            "latency_p50_ms": _percentile(latencies, 0.50),
            "latency_p95_ms": _percentile(latencies, 0.95),
            "vector_degraded": sum(
                bool(item.get("diagnostics", {}).get("vector_degraded")) for item in items
            ),
            "rerank_degraded": sum(
                bool(item.get("diagnostics", {}).get("rerank_degraded")) for item in items
            ),
        }
        for metric in metric_names:
            summary[metric] = statistics.fmean(
                float(item["metrics"][metric]) for item in items
            ) if items else 0.0
        result[name] = summary
    return result


def _project_imports() -> Tuple[Any, Any, Any, Any, Any, Any]:
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from src.core.config.loader import load_project_config
    from src.core.modeling.factory import create_embedding_client, create_rerank_client
    from src.core.paths import CONFIG_DIR, DATA_DIR
    from src.core.services.knowledge import KnowledgeService
    from src.core.storage.tenants import TenantRegistry

    return (
        load_project_config,
        create_embedding_client,
        create_rerank_client,
        CONFIG_DIR,
        DATA_DIR,
        KnowledgeService,
        TenantRegistry,
    )


def _category_ids(registry: Any, names: Sequence[str]) -> List[str]:
    placeholders = ",".join("?" for _ in names)
    with registry.database.read() as connection:
        rows = connection.execute(
            "SELECT category_id, name FROM knowledge_categories WHERE name IN ({})".format(
                placeholders
            ),
            list(names),
        ).fetchall()
    found = {str(row["name"]): str(row["category_id"]) for row in rows}
    missing = [name for name in names if name not in found]
    if missing:
        raise RuntimeError("知识库不存在：{}".format("、".join(missing)))
    return [found[name] for name in names]


def evaluate(args: argparse.Namespace) -> int:
    (
        load_project_config,
        create_embedding_client,
        create_rerank_client,
        config_dir,
        default_data_dir,
        KnowledgeService,
        TenantRegistry,
    ) = _project_imports()
    config = load_project_config(args.config_dir or config_dir)
    data_dir = (args.data_dir or default_data_dir).resolve()
    registry = TenantRegistry(data_dir)
    embedding = None
    rerank = None
    embedding_model_id = args.embedding_model or config.app.embedding_model
    rerank_model_id = args.rerank_model or config.app.rerank_model
    if args.configured_models:
        profile = config.models.get(embedding_model_id)
        if profile is not None and profile.enabled:
            embedding = create_embedding_client(profile)
        rerank_profile = config.models.get(rerank_model_id)
        if rerank_profile is not None and rerank_profile.enabled:
            rerank = create_rerank_client(rerank_profile)
    service = KnowledgeService(
        registry,
        embedding,
        rerank,
        lexical_weight=args.lexical_weight,
        vector_weight=args.vector_weight,
        rrf_k=args.rrf_k,
        candidate_pool=args.candidate_pool,
    )
    categories = _category_ids(registry, list(CATEGORY_NAMES.values()))
    questions = _load_questions(args.questions.resolve(), args.split)
    rows: List[Dict[str, Any]] = []
    try:
        for index, item in enumerate(questions, 1):
            started = time.perf_counter()
            hits = service.search(
                None,
                item.query,
                limit=RESULT_LIMIT,
                category_ids=categories,
            )
            latency_ms = (time.perf_counter() - started) * 1000.0
            retrieved: List[str] = []
            hit_details = []
            for hit in hits:
                parsed = source_doc_id(str(hit.get("source_name") or ""))
                if parsed is None:
                    continue
                benchmark, doc_id = parsed
                retrieved.append(doc_id)
                hit_details.append(
                    {
                        "benchmark": benchmark,
                        "doc_id": doc_id,
                        "source_name": hit.get("source_name"),
                        "chunk_id": hit.get("chunk_id"),
                        "heading": hit.get("heading"),
                        "score": hit.get("score"),
                        "retrieval_sources": hit.get("retrieval_sources", []),
                    }
                )
            row = {
                **item.to_dict(),
                "retrieved": list(dict.fromkeys(retrieved)),
                "hits": hit_details,
                "latency_ms": round(latency_ms, 3),
                "diagnostics": service.last_search_diagnostics(),
            }
            row["metrics"] = query_metrics(row["retrieved"], item.relevant)
            rows.append(row)
            if args.progress:
                print("[{}/{}] {} {}".format(index, len(questions), item.benchmark, item.query_id))
    finally:
        if embedding is not None:
            embedding.close()
        if rerank is not None:
            rerank.close()
    payload = {
        "format_version": 1,
        "split": args.split,
        "limit": RESULT_LIMIT,
        "parameters": {
            "configured_models": args.configured_models,
            "embedding_model": embedding_model_id if args.configured_models else "",
            "rerank_model": rerank_model_id if args.configured_models else "",
            "lexical_weight": args.lexical_weight,
            "vector_weight": args.vector_weight,
            "rrf_k": args.rrf_k,
            "candidate_pool": args.candidate_pool,
        },
        "summary": aggregate(rows),
        "rows": rows,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_reports(payload, args.output.resolve())
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 0


def _markdown_report(payload: Mapping[str, Any]) -> str:
    lines = [
        "# 知识库召回评测报告",
        "",
        "- 数据划分：`{}`".format(payload.get("split", "unknown")),
        "- Top-K：`{}`".format(payload.get("limit", RESULT_LIMIT)),
        "",
        "| 基准 | K | HitRate | Recall | Precision | MRR | nDCG |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, summary in payload.get("summary", {}).items():
        for cutoff in KS:
            lines.append(
                "| {} | {} | {:.2%} | {:.2%} | {:.2%} | {:.3f} | {:.3f} |".format(
                    name,
                    cutoff,
                    summary["hit_rate@{}".format(cutoff)],
                    summary["recall@{}".format(cutoff)],
                    summary["precision@{}".format(cutoff)],
                    summary["mrr@{}".format(cutoff)],
                    summary["ndcg@{}".format(cutoff)],
                )
            )
    lines.extend(
        [
            "",
            "| 基准 | 问题数 | 空结果率 | P50 ms | P95 ms | 向量降级 | 重排降级 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name, summary in payload.get("summary", {}).items():
        lines.append(
            "| {} | {} | {:.2%} | {:.1f} | {:.1f} | {} | {} |".format(
                name,
                summary["queries"],
                summary["empty_rate"],
                summary["latency_p50_ms"],
                summary["latency_p95_ms"],
                summary["vector_degraded"],
                summary["rerank_degraded"],
            )
        )
    failures = [
        row for row in payload.get("rows", []) if not row["metrics"]["hit_rate@6"]
    ]
    lines.extend(["", "## Top-6 未命中", ""])
    if not failures:
        lines.append("无。")
    else:
        for row in failures:
            lines.append(
                "- `{}/{}`：{}；相关={}；召回={}".format(
                    row["benchmark"],
                    row["query_id"],
                    row["query"],
                    "、".join(row["relevant"]),
                    "、".join(row["retrieved"]) or "空",
                )
            )
    lines.extend(
        [
            "",
            "## 严格验收",
            "",
            "每种语言需同时满足 `HitRate@6 ≥ 95%` 和 `MRR@6 ≥ 0.75`。",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(payload: Mapping[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    json_path = output.with_suffix(".json")
    csv_path = output.with_suffix(".csv")
    md_path = output.with_suffix(".md")
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        metric_fields = [
            "{}@{}".format(metric, cutoff)
            for cutoff in KS
            for metric in ("hit_rate", "recall", "precision", "mrr", "ndcg")
        ]
        fieldnames = [
            "benchmark", "split", "query_id", "query", "relevant", "retrieved",
            "retrieval_sources", "latency_ms", *metric_fields,
            "vector_degraded", "rerank_degraded", "failure_reason",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in payload.get("rows", []):
            metrics = row["metrics"]
            retrieval_sources = sorted(
                {
                    source
                    for hit in row.get("hits", [])
                    for source in hit.get("retrieval_sources", [])
                }
            )
            if not row.get("retrieved"):
                failure_reason = "empty_result"
            elif not metrics["hit_rate@6"]:
                failure_reason = "qrels_not_in_top6"
            else:
                failure_reason = ""
            record = {
                    "benchmark": row["benchmark"],
                    "split": row["split"],
                    "query_id": row["query_id"],
                    "query": row["query"],
                    "relevant": "|".join(row["relevant"]),
                    "retrieved": "|".join(row["retrieved"]),
                    "retrieval_sources": "|".join(retrieval_sources),
                    "latency_ms": row["latency_ms"],
                    "vector_degraded": row.get("diagnostics", {}).get("vector_degraded", False),
                    "rerank_degraded": row.get("diagnostics", {}).get("rerank_degraded", False),
                    "failure_reason": failure_reason,
                }
            record.update({field: metrics[field] for field in metric_fields})
            writer.writerow(record)
    md_path.write_text(_markdown_report(payload), encoding="utf-8")


def report(args: argparse.Namespace) -> int:
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    payload["summary"] = aggregate(payload.get("rows", []))
    write_reports(payload, args.output.resolve())
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare", help="下载并生成固定双语评测包")
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.add_argument(
        "--cache", type=Path, default=Path("data/system/benchmark-cache")
    )
    prepare_parser.add_argument("--query-count", type=int, default=QUERY_COUNT)
    prepare_parser.add_argument("--tuning-count", type=int, default=TUNING_COUNT)
    prepare_parser.add_argument("--document-cap", type=int, default=DOCUMENT_CAP)
    prepare_parser.set_defaults(func=prepare)

    evaluate_parser = subparsers.add_parser("evaluate", help="评测当前知识库召回")
    evaluate_parser.add_argument("--questions", type=Path, required=True)
    evaluate_parser.add_argument("--output", type=Path, required=True)
    evaluate_parser.add_argument("--split", choices=("tuning", "test", "all"), default="tuning")
    evaluate_parser.add_argument("--data-dir", type=Path)
    evaluate_parser.add_argument("--config-dir", type=Path)
    evaluate_parser.add_argument("--configured-models", action="store_true")
    evaluate_parser.add_argument("--embedding-model", default="")
    evaluate_parser.add_argument("--rerank-model", default="")
    evaluate_parser.add_argument("--lexical-weight", type=float, default=1.0)
    evaluate_parser.add_argument("--vector-weight", type=float, default=1.0)
    evaluate_parser.add_argument("--rrf-k", type=int, default=20)
    evaluate_parser.add_argument("--candidate-pool", type=int, default=100)
    evaluate_parser.add_argument("--progress", action="store_true")
    evaluate_parser.set_defaults(func=evaluate)

    report_parser = subparsers.add_parser("report", help="从 JSON 重新生成 CSV/Markdown")
    report_parser.add_argument("--input", type=Path, required=True)
    report_parser.add_argument("--output", type=Path, required=True)
    report_parser.set_defaults(func=report)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "tuning_count", 0) >= getattr(args, "query_count", 1):
        raise SystemExit("tuning-count 必须小于 query-count")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
