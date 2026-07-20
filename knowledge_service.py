"""Tenant-scoped text knowledge ingestion and hybrid retrieval."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from embedding_service import EmbeddingClient, EmbeddingError
from tenant_store import TenantContext, TenantRegistry


MAX_TEXT_CHARACTERS = 20_000
MAX_FILE_BYTES = 5 * 1024 * 1024
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KnowledgeHit:
    chunk_id: str
    source_id: str
    source_name: str
    heading: str
    content: str
    locator: str
    score: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "heading": self.heading,
            "content": self.content,
            "locator": self.locator,
            "score": round(self.score, 6),
        }


class KnowledgeService:
    def __init__(
        self, registry: TenantRegistry, embedding: Optional[EmbeddingClient] = None
    ) -> None:
        self.registry = registry
        self.embedding = embedding

    @staticmethod
    def _chunks(text: str) -> List[Tuple[str, str, str]]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise ValueError("知识内容不能为空")
        sections: List[Tuple[str, str]] = []
        heading = ""
        buffer: List[str] = []
        for line in normalized.splitlines():
            if re.match(r"^#{1,6}\s+\S", line):
                if buffer:
                    sections.append((heading, "\n".join(buffer).strip()))
                    buffer = []
                heading = re.sub(r"^#{1,6}\s+", "", line).strip()
            else:
                buffer.append(line)
        if buffer or not sections:
            sections.append((heading, "\n".join(buffer).strip()))

        result: List[Tuple[str, str, str]] = []
        position = 0
        for section_heading, body in sections:
            paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
            current = ""
            for paragraph in paragraphs or [body]:
                if current and len(current) + 2 + len(paragraph) > CHUNK_SIZE:
                    result.append((section_heading, current, "chunk:{}".format(position + 1)))
                    position += 1
                    current = current[-CHUNK_OVERLAP:] + "\n\n" + paragraph
                else:
                    current = (current + "\n\n" + paragraph).strip()
                while len(current) > CHUNK_SIZE:
                    piece = current[:CHUNK_SIZE]
                    result.append((section_heading, piece, "chunk:{}".format(position + 1)))
                    position += 1
                    current = current[CHUNK_SIZE - CHUNK_OVERLAP:]
            if current:
                result.append((section_heading, current, "chunk:{}".format(position + 1)))
                position += 1
        return result

    def add_text(self, tenant_id: str, name: str, content: str) -> Dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("知识名称不能为空")
        if len(content) > MAX_TEXT_CHARACTERS:
            raise ValueError("单次手工知识不能超过 {} 字符".format(MAX_TEXT_CHARACTERS))
        return self._index(tenant_id, "text", name.strip(), None, content)

    def index_file(self, tenant: TenantContext, path: Path) -> Dict[str, Any]:
        workspace = (self.registry.tenant_root(tenant.tenant_id) / "workspace").resolve()
        candidate = path.expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        candidate = candidate.resolve(strict=True)
        if workspace not in candidate.parents or not candidate.is_file():
            raise ValueError("知识文件必须位于当前租户 workspace 内")
        if candidate.suffix.lower() not in {".txt", ".md", ".markdown"}:
            raise ValueError("知识文件仅支持 TXT 或 Markdown")
        if candidate.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("知识文件不能超过 5 MiB")
        try:
            content = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("知识文件必须是 UTF-8 文本") from exc
        relative = candidate.relative_to(workspace).as_posix()
        return self._index(tenant.tenant_id, "file", relative, relative, content)

    def _embed_chunks(self, chunks: List[Tuple[str, str, str]]) -> Optional[List[List[float]]]:
        if self.embedding is None:
            return None
        vectors: List[List[float]] = []
        try:
            for offset in range(0, len(chunks), 32):
                texts = [item[1] for item in chunks[offset:offset + 32]]
                vectors.extend(self.embedding.embed(texts))
        except EmbeddingError:
            return None
        return vectors

    def _index(
        self,
        tenant_id: str,
        source_type: str,
        name: str,
        relative_path: Optional[str],
        content: str,
    ) -> Dict[str, Any]:
        chunks = self._chunks(content)
        digest = _hash(content)
        vectors = self._embed_chunks(chunks)
        status = "ready" if vectors is not None else "pending_embedding"
        with self.registry.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT source_id, content_hash, status FROM knowledge_sources "
                "WHERE tenant_id=? AND source_type=? AND name=?",
                (tenant_id, source_type, name),
            ).fetchone()
            if existing and existing["content_hash"] == digest:
                count = connection.execute(
                    "SELECT COUNT(*) FROM knowledge_chunks WHERE source_id=?",
                    (existing["source_id"],),
                ).fetchone()[0]
                return {
                    "source_id": str(existing["source_id"]), "name": name,
                    "status": str(existing["status"]), "chunks": int(count), "unchanged": True,
                }
            source_id = str(existing["source_id"]) if existing else str(uuid.uuid4())
            if existing:
                old_chunks = connection.execute(
                    "SELECT chunk_id FROM knowledge_chunks WHERE source_id=?", (source_id,)
                ).fetchall()
                connection.executemany(
                    "DELETE FROM knowledge_fts WHERE chunk_id=?",
                    [(row["chunk_id"],) for row in old_chunks],
                )
                connection.execute("DELETE FROM knowledge_chunks WHERE source_id=?", (source_id,))
                connection.execute(
                    "UPDATE knowledge_sources SET relative_path=?, content_hash=?, status=?, "
                    "updated_at=? WHERE source_id=?",
                    (relative_path, digest, status, _now(), source_id),
                )
            else:
                created = _now()
                connection.execute(
                    "INSERT INTO knowledge_sources(source_id, tenant_id, source_type, name, "
                    "relative_path, content_hash, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (source_id, tenant_id, source_type, name, relative_path, digest, status, created, created),
                )
            for position, (heading, body, locator) in enumerate(chunks):
                chunk_id = str(uuid.uuid4())
                body_hash = _hash(body)
                connection.execute(
                    "INSERT INTO knowledge_chunks(chunk_id, source_id, tenant_id, position, heading, "
                    "content, content_hash, locator) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (chunk_id, source_id, tenant_id, position, heading, body, body_hash, locator),
                )
                connection.execute(
                    "INSERT INTO knowledge_fts(chunk_id, tenant_id, heading, content) VALUES (?, ?, ?, ?)",
                    (chunk_id, tenant_id, heading, body),
                )
                if vectors is not None:
                    vector = array("f", vectors[position]).tobytes()
                    connection.execute(
                        "INSERT INTO knowledge_embeddings(chunk_id, model_id, dimensions, vector, "
                        "content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (chunk_id, self.embedding.profile.id, len(vectors[position]), vector, body_hash, _now()),
                    )
        return {"source_id": source_id, "name": name, "status": status, "chunks": len(chunks), "unchanged": False}

    def list(self, tenant_id: str) -> List[Dict[str, Any]]:
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT s.source_id, s.source_type, s.name, s.relative_path, s.status, s.updated_at, "
                "COUNT(c.chunk_id) AS chunks FROM knowledge_sources s LEFT JOIN knowledge_chunks c "
                "ON c.source_id=s.source_id WHERE s.tenant_id=? GROUP BY s.source_id "
                "ORDER BY s.updated_at DESC",
                (tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete(self, tenant_id: str, source_id: str) -> bool:
        with self.registry.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT chunk_id FROM knowledge_chunks WHERE source_id=? AND tenant_id=?",
                (source_id, tenant_id),
            ).fetchall()
            if not rows:
                exists = connection.execute(
                    "SELECT 1 FROM knowledge_sources WHERE source_id=? AND tenant_id=?",
                    (source_id, tenant_id),
                ).fetchone()
                if not exists:
                    return False
            connection.executemany(
                "DELETE FROM knowledge_fts WHERE chunk_id=?", [(row["chunk_id"],) for row in rows]
            )
            connection.execute(
                "DELETE FROM knowledge_sources WHERE source_id=? AND tenant_id=?", (source_id, tenant_id)
            )
            return True

    @staticmethod
    def _cosine(left: List[float], right: List[float]) -> float:
        if len(left) != len(right):
            return -1.0
        dot = sum(a * b for a, b in zip(left, right))
        lnorm = math.sqrt(sum(value * value for value in left))
        rnorm = math.sqrt(sum(value * value for value in right))
        return dot / (lnorm * rnorm) if lnorm and rnorm else -1.0

    def search(self, tenant_id: str, query: str, limit: int = 6) -> List[Dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        limit = max(1, min(limit, 20))
        lexical: List[str] = []
        with self.registry.database.read() as connection:
            if len(query) >= 3:
                expression = '"{}"'.format(query.replace('"', '""'))
                try:
                    rows = connection.execute(
                        "SELECT chunk_id FROM knowledge_fts WHERE knowledge_fts MATCH ? "
                        "AND tenant_id=? ORDER BY bm25(knowledge_fts) LIMIT 20",
                        (expression, tenant_id),
                    ).fetchall()
                except Exception:
                    rows = []
                lexical = [str(row["chunk_id"]) for row in rows]
            if not lexical:
                rows = connection.execute(
                    "SELECT chunk_id FROM knowledge_chunks WHERE tenant_id=? AND "
                    "(content LIKE ? OR heading LIKE ?) LIMIT 20",
                    (tenant_id, "%" + query + "%", "%" + query + "%"),
                ).fetchall()
                lexical = [str(row["chunk_id"]) for row in rows]

            vector_rank: List[str] = []
            query_vector: Optional[List[float]] = None
            if self.embedding is not None:
                try:
                    query_vector = self.embedding.embed([query])[0]
                except EmbeddingError:
                    query_vector = None
            if query_vector is not None:
                rows = connection.execute(
                    "SELECT e.chunk_id, e.vector FROM knowledge_embeddings e "
                    "JOIN knowledge_chunks c ON c.chunk_id=e.chunk_id "
                    "WHERE c.tenant_id=? AND e.model_id=?",
                    (tenant_id, self.embedding.profile.id),
                ).fetchall()
                scored = []
                for row in rows:
                    vector = list(array("f", bytes(row["vector"])))
                    scored.append((self._cosine(query_vector, vector), str(row["chunk_id"])))
                scored.sort(reverse=True)
                vector_rank = [chunk_id for _, chunk_id in scored[:20]]

            scores: Dict[str, float] = {}
            for ranked in (lexical, vector_rank):
                for rank, chunk_id in enumerate(ranked, 1):
                    scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank)
            selected = sorted(scores, key=scores.get, reverse=True)[:limit]
            if not selected:
                return []
            placeholders = ",".join("?" for _ in selected)
            detail_rows = connection.execute(
                "SELECT c.chunk_id, c.source_id, s.name AS source_name, c.heading, c.content, c.locator "
                "FROM knowledge_chunks c JOIN knowledge_sources s ON s.source_id=c.source_id "
                "WHERE c.chunk_id IN ({})".format(placeholders),
                selected,
            ).fetchall()
        details = {str(row["chunk_id"]): row for row in detail_rows}
        return [KnowledgeHit(
            chunk_id=chunk_id,
            source_id=str(details[chunk_id]["source_id"]),
            source_name=str(details[chunk_id]["source_name"]),
            heading=str(details[chunk_id]["heading"] or ""),
            content=str(details[chunk_id]["content"]),
            locator=str(details[chunk_id]["locator"] or ""),
            score=scores[chunk_id],
        ).to_dict() for chunk_id in selected if chunk_id in details]

    def reindex(self, tenant_id: str) -> Dict[str, int]:
        if self.embedding is None:
            raise ValueError("embedding 服务未配置")
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT c.chunk_id, c.content, c.content_hash FROM knowledge_chunks c "
                "LEFT JOIN knowledge_embeddings e ON e.chunk_id=c.chunk_id AND e.model_id=? "
                "WHERE c.tenant_id=? AND e.chunk_id IS NULL ORDER BY c.source_id, c.position",
                (self.embedding.profile.id, tenant_id),
            ).fetchall()
        completed = 0
        for offset in range(0, len(rows), 32):
            batch = rows[offset:offset + 32]
            try:
                vectors = self.embedding.embed([str(row["content"]) for row in batch])
            except EmbeddingError:
                break
            with self.registry.database.transaction(immediate=True) as connection:
                for row, vector in zip(batch, vectors):
                    connection.execute(
                        "INSERT OR REPLACE INTO knowledge_embeddings(chunk_id, model_id, dimensions, "
                        "vector, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (row["chunk_id"], self.embedding.profile.id, len(vector), array("f", vector).tobytes(), row["content_hash"], _now()),
                    )
                    completed += 1
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE knowledge_sources SET status='ready', updated_at=? WHERE tenant_id=? "
                "AND NOT EXISTS (SELECT 1 FROM knowledge_chunks c LEFT JOIN knowledge_embeddings e "
                "ON e.chunk_id=c.chunk_id AND e.model_id=? WHERE c.source_id=knowledge_sources.source_id "
                "AND e.chunk_id IS NULL)",
                (_now(), tenant_id, self.embedding.profile.id),
            )
        return {"completed": completed, "remaining": len(rows) - completed}
