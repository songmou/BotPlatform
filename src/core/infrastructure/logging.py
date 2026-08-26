"""Console log helpers shared by interactive and scheduled messages."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.core.modeling import ModelIdentity, ModelRequest, ModelResponse, ModelUsage
from src.core.tooling.models import ToolAuditContext


def mask_user_id(user_id: str) -> str:
    local, separator, domain = user_id.partition("@")
    if len(local) <= 4:
        masked = local[:1] + "…" + local[-1:]
    elif len(local) <= 10:
        masked = local[:2] + "…" + local[-2:]
    else:
        masked = local[:6] + "…" + local[-4:]
    return masked + (separator + domain if separator else "")


def log_interaction(direction: str, user_id: str, content: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n[{}] {} | 用户={}".format(timestamp, direction, mask_user_id(user_id)))
    print(content)
    print(flush=True)


def log_scheduled_task(
    task_id: str, status: str, detail: str, user_id: Optional[str] = None
) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_part = ""
    if user_id:
        user_part = " | 用户={}".format(mask_user_id(user_id))
    print(
        "[{}] 定时任务 | id={} | 状态={}{} | {}".format(
            timestamp, task_id, status, user_part, detail
        ),
        flush=True,
    )


def log_tool_call(
    context: ToolAuditContext,
    tool_name: str,
    status: str,
    duration_seconds: float,
    output_bytes: int,
) -> None:
    """Log tool metadata without arguments, file contents, or command output."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(
        "[{}] 本机工具 | 用户={} | 提供商={} | 档案={} | 模型={} | 工具={} | 状态={} | 耗时={:.3f}s | 输出={}B".format(
            timestamp,
            mask_user_id(context.user_id) if context.user_id else "系统",
            context.provider,
            context.profile_id,
            context.model,
            tool_name,
            status,
            duration_seconds,
            output_bytes,
        ),
        flush=True,
    )


def log_model_call(
    identity: ModelIdentity,
    actual_model: str,
    status: str,
    duration_seconds: float,
    usage: Optional[ModelUsage],
    tool_call_count: int,
    request_id: Optional[str],
    context=None,
    finish_reason: Optional[str] = None,
    first_token_seconds: Optional[float] = None,
    error: Optional[BaseException] = None,
    request: Optional[ModelRequest] = None,
    response: Optional[ModelResponse] = None,
) -> None:
    """Log model metadata without prompts, images, credentials, or reasoning."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    input_tokens = (
        "-" if not usage or usage.input_tokens is None else str(usage.input_tokens)
    )
    output_tokens = (
        "-" if not usage or usage.output_tokens is None else str(usage.output_tokens)
    )
    print(
        (
            "[{}] 模型调用 | 提供商={} | 档案={} | 模型={} | 状态={} | 耗时={:.3f}s"
            " | 输入={}tok | 输出={}tok | 工具调用={} | 请求={}"
        ).format(
            timestamp,
            identity.provider,
            identity.profile_id,
            actual_model,
            status,
            duration_seconds,
            input_tokens,
            output_tokens,
            tool_call_count,
            request_id or "-",
        ),
        flush=True,
    )


def log_model_fallback(
    source: ModelIdentity, target: ModelIdentity, reason: str
) -> None:
    """Log sanitized routing metadata without request or credential content."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(
        "[{}] 模型切换 | 从={}/{} | 到={}/{} | 原因={}".format(
            timestamp,
            source.provider,
            source.profile_id,
            target.provider,
            target.profile_id,
            reason,
        ),
        flush=True,
    )
