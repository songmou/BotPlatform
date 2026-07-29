"""Pending tool-approval bookkeeping and user-facing approval prompts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

from src.core.tooling.models import ApprovalRequired, PendingApproval, ToolError


APPROVAL_SUMMARY_MAX_BYTES = 16_384


class ApprovalStore:
    """Track at most one pending approval per subject with TTL semantics.

    Callers are expected to hold the per-subject conversation lock; the
    store itself performs no locking.
    """

    def __init__(self) -> None:
        self._items: Dict[str, PendingApproval] = {}

    @property
    def items(self) -> Dict[str, PendingApproval]:
        """Expose the backing map for tests and diagnostics."""
        return self._items

    def put(self, key: str, pending: PendingApproval) -> None:
        self._items[key] = pending

    def peek(self, key: str) -> Optional[PendingApproval]:
        """Return the stored approval without checking its deadline."""
        return self._items.get(key)

    def active(self, key: str) -> Optional[PendingApproval]:
        """Return the unexpired approval for ``key``, dropping stale ones."""
        pending = self._items.get(key)
        if pending and datetime.now(timezone.utc) >= pending.expires_at:
            self._items.pop(key, None)
            return None
        return pending

    def cancel(self, key: str) -> bool:
        """Discard any stored approval and report whether one existed."""
        return self._items.pop(key, None) is not None

    def expire(
        self, key: str, approval_id: str, now: Optional[datetime] = None
    ) -> bool:
        """Atomically discard a matching approval once its deadline passed."""
        pending = self._items.get(key)
        if not pending or pending.approval_id != approval_id:
            return False
        current_time = now or datetime.now(timezone.utc)
        if current_time < pending.expires_at:
            return False
        self._items.pop(key, None)
        return True

    def take(self, key: str, approval_id: str) -> PendingApproval:
        """Remove and return the approval matching ``approval_id``.

        Raises ``ToolError`` when nothing is pending, the request expired,
        or the identifier does not match.
        """
        pending = self._items.get(key)
        if not pending:
            raise ToolError("没有待确认的操作，或请求已经失效")
        if datetime.now(timezone.utc) >= pending.expires_at:
            self._items.pop(key, None)
            raise ToolError("确认请求已经过期，请重新发起操作")
        if approval_id != pending.approval_id:
            raise ToolError("确认编号不匹配")
        self._items.pop(key, None)
        return pending


def build_approval_request(
    pending: PendingApproval, ttl_seconds: int
) -> ApprovalRequired:
    """Render the user-facing confirmation prompt for a pending approval."""
    risky = [call for call in pending.calls if call.requires_approval]
    instructions = [
        "回复“同意”或“确认”：执行以上操作",
        "回复“不同意”“拒绝”或“取消”：不执行以上操作",
        "若在 {} 秒内未回复，将默认按“不同意”处理。".format(ttl_seconds),
    ]
    lines = ["需要确认以下 {} 项本机操作：".format(len(risky))]
    for index, call in enumerate(risky, 1):
        lines.extend(["", "{}. {}".format(index, call.name), call.preview])
    lines.extend(["", *instructions])
    summary = "\n".join(lines)
    encoded = summary.encode("utf-8")
    if len(encoded) > APPROVAL_SUMMARY_MAX_BYTES:
        suffix = "\n……操作预览已截断\n\n{}".format("\n".join(instructions))
        preview_bytes = APPROVAL_SUMMARY_MAX_BYTES - len(suffix.encode("utf-8"))
        summary = (
            encoded[:preview_bytes].decode("utf-8", errors="ignore")
            + suffix
        )
    return ApprovalRequired(pending.approval_id, summary, pending.expires_at)
