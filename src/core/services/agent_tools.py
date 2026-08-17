"""Helpers to expand an agent's configured tools and build its system prompt."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List

from src.core.tooling.definitions import DATASOURCE_READONLY_TOOLS

if TYPE_CHECKING:
    from src.core.config.loader import AgentPreset
    from src.core.tooling.runtime import ToolRuntime

#: Text-format tool call patterns some models emit instead of using the
#: structured function-calling channel (e.g. Claude-style <tool_calls> XML).
#: Only strong signals are matched: a bare <parameter> or <result> also appears
#: in legitimate answers about XML/HTML and must not be treated as a tool call.
_TAG_NAMES = r"tool_calls?|function_calls?|use_mcp_tool|use_mcp_server"

_TOOL_CALL_TEXT_RE = re.compile(
    r"<\s*/?\s*(?:antml:)?(?:{})\b[^>]*>"
    r"|<\s*/?\s*(?:antml:)?invoke\b[^>]*>"
    r"|<\s*(?:antml:)?parameter\s+name\s*=".format(_TAG_NAMES),
    re.IGNORECASE,
)

#: 优先整块删除成对开闭标签及其内容，再清理残余的孤立标签。
_TOOL_CALL_BLOCK_RE = re.compile(
    r"<\s*(?:antml:)?(?:{tags}|invoke)\b[^>]*>.*?"
    r"<\s*/\s*(?:antml:)?(?:{tags}|invoke)\s*>"
    r"|<\s*(?:antml:)?parameter\s+name\s*=[^>]*>.*?<\s*/\s*(?:antml:)?parameter\s*>"
    r"|{single}".format(tags=_TAG_NAMES, single=_TOOL_CALL_TEXT_RE.pattern),
    re.IGNORECASE | re.DOTALL,
)

#: Below this length the leftover text is treated as tag debris, not an answer.
_MIN_MEANINGFUL_LENGTH = 8

HALLUCINATION_HINT = (
    "抱歉，当前助手没有可用工具来完成该操作。"
    "如需使用 git 等工具，请在平台 Agent 配置中为当前助手授权相应工具后再试。"
)


def is_tool_call_text(text: str) -> bool:
    """Return True when ``text`` contains a text-format tool call block."""
    if not text:
        return False
    return _TOOL_CALL_TEXT_RE.search(text) is not None


def sanitize_tool_call_text(text: str) -> str:
    """Strip hallucinated tool call markup, keeping any real answer around it."""
    if not is_tool_call_text(text):
        return text
    stripped = strip_tool_call_text(text)
    if len(stripped) >= _MIN_MEANINGFUL_LENGTH:
        return stripped
    return HALLUCINATION_HINT


def strip_tool_call_text(text: str) -> str:
    """Remove text-format tool call blocks (e.g. from thinking drafts)."""
    if not text:
        return text
    return _TOOL_CALL_BLOCK_RE.sub("", text).strip()


def resolve_tool_names(agent: "AgentPreset", tool_runtime: "ToolRuntime") -> List[str]:
    """Return the concrete tool names an agent may use.

    Combines the agent's built-in/plugin tool names with the namespaced tools
    exposed by each selected MCP server that is currently connected, plus the
    read-only datasource tools whenever the agent has any datasource bound.
    """
    names: List[str] = list(agent.tools)
    for plugin_names in getattr(agent, "plugin_tools", {}).values():
        names.extend(plugin_names)
    manager = getattr(tool_runtime, "mcp_manager", None) if tool_runtime else None
    if manager is not None:
        for server_id in getattr(agent, "mcp_servers", []):
            names.extend(manager.tool_names(server_id))
    if getattr(agent, "datasources", None):
        existing = set(names)
        for tool_name in DATASOURCE_READONLY_TOOLS:
            if tool_name not in existing:
                names.append(tool_name)
    return names


_TOOL_USAGE_RULES = (
    "# 工具使用规范\n"
    "1. 只能调用系统为你提供的工具（通过 function calling 接口），"
    "严禁在回复文本中输出任何工具调用格式（如 <tool_calls>、<invoke>、XML 标签或伪代码）。\n"
    "2. 如果用户请求的操作没有对应工具可用（例如需要 git 操作但当前助手未授权 git 工具），"
    "不要假装执行、不要编造执行过程或结果，直接如实告知用户："
    "「当前助手没有可用工具来完成该操作，请在平台 Agent 配置中为当前助手授权相应工具后再试」。\n"
    "3. 工具的执行结果以系统返回为准，不得虚构工具输出内容。\n"
)


def build_system_prompt(
    agent: "AgentPreset",
    skills: List[Dict[str, Any]],
    tool_runtime: "ToolRuntime" = None,
) -> str:
    """Return the agent system prompt with selected skill instructions appended."""
    prompt = _TOOL_USAGE_RULES + "\n\n" + agent.system_prompt
    manager = getattr(tool_runtime, "plugin_manager", None) if tool_runtime else None
    if manager is not None:
        for plugin_id, tool_names in getattr(agent, "plugin_tools", {}).items():
            if not tool_names:
                continue
            if manager.get(plugin_id) is None:
                continue
            manifest = manager.catalog.get(plugin_id)
            if manifest is not None and manifest.instructions:
                prompt += "\n\n# 插件：{}\n{}".format(
                    manifest.name, manifest.instructions
                )
    selected = set(agent.skills)
    # -- Inject datasource schema block before the skills section --
    ds_service = getattr(tool_runtime, "datasource_service", None) if tool_runtime else None
    bound = list(getattr(agent, "datasources", []) or [])
    if ds_service is not None and bound:
        block = ds_service.prompt_block(
            bound, allow_write="db_execute" in set(agent.tools)
        )
        if block:
            prompt += "\n\n" + block
    # -- End datasource injection --
    if not selected:
        return prompt
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        if skill.get("id") not in selected:
            continue
        if not skill.get("enabled", True):
            continue
        text = skill.get("prompt")
        if not text:
            continue
        prompt += "\n\n# Skill: {}\n{}".format(skill.get("name") or skill.get("id"), text)
    return prompt
