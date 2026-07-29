"""SSE streaming utilities for chat responses."""

from __future__ import annotations

import json
from typing import Generator

from fastapi.responses import StreamingResponse


def sse_event(data: dict) -> str:
    return "data: {}\n\n".format(json.dumps(data, ensure_ascii=False))


def sse_token(content: str) -> str:
    return sse_event({"type": "token", "content": content})


def sse_done(full_text: str, run_id: str | None = None) -> str:
    payload = {"type": "done", "full_text": full_text}
    if run_id:
        payload["run_id"] = run_id
    return sse_event(payload)


def sse_error(message: str) -> str:
    return sse_event({"type": "error", "message": message})


def sse_plan(plan: list) -> str:
    return sse_event({"type": "plan", "plan": plan})


def sse_agent_start(agent_id: str, agent_name: str, subtask: str) -> str:
    return sse_event(
        {"type": "agent_start", "agent_id": agent_id, "agent_name": agent_name, "subtask": subtask}
    )


def sse_agent_done(agent_id: str, full_text: str, status: str = "ok") -> str:
    return sse_event({"type": "agent_done", "agent_id": agent_id, "full_text": full_text, "status": status})


def sse_thinking(content: str) -> str:
    return sse_event({"type": "thinking", "content": content})


def sse_tool_call(name: str, arguments: dict) -> str:
    return sse_event({"type": "tool_call", "name": name, "arguments": arguments})


def sse_tool_result(name: str, result: dict) -> str:
    return sse_event({"type": "tool_result", "name": name, "result": result})


def sse_sources(sources: list) -> str:
    return sse_event({"type": "sources", "sources": sources})


def sse_summary_start() -> str:
    return sse_event({"type": "summary_start"})


def streaming_response(generator: Generator[str, None, None]) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
