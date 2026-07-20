"""Automatic, reviewable long-term memory backed by SQLite."""

from __future__ import annotations

import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from config_loader import ModelProfile
from tenant_store import TenantRegistry


MEMORY_KINDS = {"preference", "identity", "goal", "constraint"}
SECRET_PATTERN = re.compile(
    r"(?i)(password|passwd|密码|口令|token|secret|api[_-]?key|access[_-]?key)\s*[:=：]\s*\S+"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OllamaMemoryExtractor:
    """Use only a local Ollama chat model to identify durable memories."""

    def __init__(self, profile: ModelProfile, client: Optional[httpx.Client] = None) -> None:
        self.profile = profile
        self.client = client or httpx.Client(trust_env=False)
        self._owns_client = client is None

    def extract(self, question: str, answer: str) -> List[Dict[str, Any]]:
        prompt = (
            "从下面一次对话中提取值得长期记住的用户信息。只允许类型 preference、identity、"
            "goal、constraint。忽略密码、令牌、临时请求、一次性状态、助手推测和知识性问答。"
            "仅输出 JSON 数组；每项包含 kind、key、content、confidence，confidence 为 0 到 1。"
            "没有合适内容时输出 []。\n\n用户：{}\n\n助手：{}"
        ).format(question[:6000], answer[:6000])
        try:
            response = self.client.post(
                self.profile.base_url.rstrip("/") + "/api/chat",
                json={
                    "model": self.profile.model,
                    "stream": False,
                    "format": "json",
                    "messages": [{"role": "user", "content": prompt}],
                    "options": {"temperature": 0},
                },
                timeout=min(self.profile.timeout_seconds, 60),
            )
            response.raise_for_status()
            payload = response.json()
            content = payload.get("message", {}).get("content", "")
            parsed = json.loads(content)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return []
        if isinstance(parsed, dict):
            parsed = parsed.get("memories", [])
        return parsed if isinstance(parsed, list) else []

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class MemoryService:
    def __init__(
        self,
        registry: TenantRegistry,
        extractor: Optional[OllamaMemoryExtractor] = None,
    ) -> None:
        self.registry = registry
        self.extractor = extractor
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-extract")
        self._closed = False
        self._lock = threading.Lock()

    @staticmethod
    def _normalize_key(kind: str, raw: str, content: str) -> str:
        value = re.sub(r"\s+", " ", str(raw or content)).strip().lower()
        return "{}:{}".format(kind, value[:160])

    @staticmethod
    def _safe_candidate(raw: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        kind = raw.get("kind")
        content = raw.get("content")
        confidence = raw.get("confidence")
        if kind not in MEMORY_KINDS or not isinstance(content, str):
            return None
        content = re.sub(r"\s+", " ", content).strip()
        if not content or len(content) > 500 or SECRET_PATTERN.search(content):
            return None
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            return None
        return {
            "kind": kind,
            "content": content,
            "confidence": max(0.0, min(1.0, float(confidence))),
            "key": str(raw.get("key", "")),
        }

    def extract(
        self,
        tenant_id: str,
        question: str,
        answer: str,
        source_event_ids: Optional[List[int]] = None,
    ) -> List[str]:
        if self.extractor is None or SECRET_PATTERN.search(question):
            return []
        if source_event_ids is None:
            with self.registry.database.read() as connection:
                row = connection.execute(
                    "SELECT event_id FROM conversation_events WHERE tenant_id=? "
                    "AND role='user' AND content=? ORDER BY event_id DESC LIMIT 1",
                    (tenant_id, question),
                ).fetchone()
            source_event_ids = [int(row["event_id"])] if row else []
        candidates = [self._safe_candidate(item) for item in self.extractor.extract(question, answer)]
        candidates = [item for item in candidates if item is not None]
        created: List[str] = []
        now = _now()
        with self.registry.database.transaction(immediate=True) as connection:
            for candidate in candidates:
                assert candidate is not None
                key = self._normalize_key(candidate["kind"], candidate["key"], candidate["content"])
                status = "active" if candidate["confidence"] >= 0.8 else "pending"
                current = connection.execute(
                    "SELECT memory_id, content, status FROM memory_items WHERE tenant_id=? "
                    "AND normalized_key=? AND status IN ('active', 'pending') ORDER BY updated_at DESC LIMIT 1",
                    (tenant_id, key),
                ).fetchone()
                if current and current["content"] == candidate["content"]:
                    connection.execute(
                        "UPDATE memory_items SET confidence=?, updated_at=? WHERE memory_id=?",
                        (candidate["confidence"], now, current["memory_id"]),
                    )
                    continue
                memory_id = str(uuid.uuid4())
                if current:
                    connection.execute(
                        "UPDATE memory_items SET status='superseded', superseded_by=?, updated_at=? "
                        "WHERE memory_id=?",
                        (memory_id, now, current["memory_id"]),
                    )
                connection.execute(
                    "INSERT INTO memory_items(memory_id, tenant_id, kind, content, normalized_key, "
                    "confidence, status, source_event_ids, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        memory_id, tenant_id, candidate["kind"], candidate["content"], key,
                        candidate["confidence"], status,
                        json.dumps(source_event_ids or [], separators=(",", ":")), now, now,
                    ),
                )
                created.append(memory_id)
        return created

    def extract_async(self, tenant_id: str, question: str, answer: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._executor.submit(self.extract, tenant_id, question, answer)

    def search(self, tenant_id: str, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT memory_id, kind, content, confidence, updated_at FROM memory_items "
                "WHERE tenant_id=? AND status='active' ORDER BY updated_at DESC LIMIT 100",
                (tenant_id,),
            ).fetchall()
        terms = {item.lower() for item in re.findall(r"[\w\u4e00-\u9fff]+", query) if item}
        scored = []
        for position, row in enumerate(rows):
            content = str(row["content"]).lower()
            overlap = sum(1 for term in terms if term in content)
            score = overlap * 10 + float(row["confidence"]) + 1.0 / (position + 1)
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        selected = [row for _, row in scored[:max(1, min(limit, 20))]]
        if selected:
            with self.registry.database.transaction() as connection:
                connection.executemany(
                    "UPDATE memory_items SET last_used_at=? WHERE memory_id=?",
                    [(_now(), row["memory_id"]) for row in selected],
                )
        return [dict(row) for row in selected]

    def list(self, tenant_id: str, include_pending: bool = True) -> List[Dict[str, Any]]:
        statuses = ("active", "pending") if include_pending else ("active",)
        placeholders = ",".join("?" for _ in statuses)
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT memory_id, kind, content, confidence, status, created_at, updated_at "
                "FROM memory_items WHERE tenant_id=? AND status IN ({}) ORDER BY updated_at DESC".format(placeholders),
                (tenant_id, *statuses),
            ).fetchall()
        return [dict(row) for row in rows]

    def confirm(self, tenant_id: str, memory_id: str) -> bool:
        with self.registry.database.transaction(immediate=True) as connection:
            resolved = self._resolve_id(connection, tenant_id, memory_id, "pending")
            if resolved is None:
                return False
            cursor = connection.execute(
                "UPDATE memory_items SET status='active', updated_at=? WHERE tenant_id=? "
                "AND memory_id=? AND status='pending'",
                (_now(), tenant_id, resolved),
            )
            return cursor.rowcount == 1

    def forget(self, tenant_id: str, memory_id: str) -> bool:
        with self.registry.database.transaction(immediate=True) as connection:
            resolved = self._resolve_id(connection, tenant_id, memory_id, "active", "pending")
            if resolved is None:
                return False
            cursor = connection.execute(
                "UPDATE memory_items SET status='deleted', updated_at=? WHERE tenant_id=? "
                "AND memory_id=? AND status IN ('active', 'pending')",
                (_now(), tenant_id, resolved),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _resolve_id(connection: Any, tenant_id: str, prefix: str, *statuses: str) -> Optional[str]:
        if not prefix or len(prefix) < 6:
            return None
        placeholders = ",".join("?" for _ in statuses)
        rows = connection.execute(
            "SELECT memory_id FROM memory_items WHERE tenant_id=? AND memory_id LIKE ? "
            "AND status IN ({}) LIMIT 2".format(placeholders),
            (tenant_id, prefix + "%", *statuses),
        ).fetchall()
        return str(rows[0]["memory_id"]) if len(rows) == 1 else None

    def clear(self, tenant_id: str) -> int:
        with self.registry.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE memory_items SET status='deleted', updated_at=? WHERE tenant_id=? "
                "AND status IN ('active', 'pending')",
                (_now(), tenant_id),
            )
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True)
        if self.extractor is not None:
            self.extractor.close()
