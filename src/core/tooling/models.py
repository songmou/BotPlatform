"""Shared types for local tool execution and user approval."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.core.modeling import CanonicalMessage


class ToolError(RuntimeError):
    """Raised when a tool request is invalid or cannot be executed safely."""


@dataclass(frozen=True)
class FinalAnswer:
    text: str
    thinking: str = ""


@dataclass(frozen=True)
class ApprovalRequired:
    approval_id: str
    summary: str
    expires_at: datetime

    @property
    def text(self) -> str:
        return self.summary


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Optional[Any] = None
    error: Optional[str] = None

    def payload(self) -> Dict[str, Any]:
        if self.ok:
            return {"ok": True, "data": self.data}
        return {"ok": False, "error": self.error or "工具执行失败"}


@dataclass(frozen=True)
class ToolAuditContext:
    user_id: str = ""
    member_user_id: Optional[int] = None
    organization_id: str = ""
    provider: str = "-"
    profile_id: str = "-"
    model: str = "-"
    session_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True)
class PreparedToolCall:
    index: int
    call_id: str
    name: str
    arguments: Dict[str, Any]
    requires_approval: bool
    preview: str
    result: Optional[ToolResult] = None
    audit_context: ToolAuditContext = ToolAuditContext()


@dataclass
class PendingApproval:
    approval_id: str
    user_id: str
    expires_at: datetime
    question: str
    history: List[CanonicalMessage]
    messages: List[CanonicalMessage]
    calls: List[PreparedToolCall]
    image_bytes: Optional[bytes]
    rounds_used: int
    total_calls: int
    tool_names: List[str]
    thinking_parts: List[str] = field(default_factory=list)
    model_mode: str = "auto"
    model_profile_id: Optional[str] = None
    session_key: str = "direct"
    #: Datasource ids the paused agent was allowed to touch.  Needed so the
    #: db_* grant can be restored when the approval is resolved on another
    #: request (and possibly another thread).
    datasource_ids: List[str] = field(default_factory=list)
