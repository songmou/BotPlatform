"""Helpers to expand an agent's configured tools and build its system prompt."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

from src.core.tooling.definitions import DATASOURCE_READONLY_TOOLS

if TYPE_CHECKING:
    from src.core.config.loader import AgentPreset
    from src.core.tooling.runtime import ToolRuntime


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


def build_system_prompt(
    agent: "AgentPreset",
    skills: List[Dict[str, Any]],
    tool_runtime: "ToolRuntime" = None,
) -> str:
    """Return the agent system prompt with selected skill instructions appended."""
    prompt = agent.system_prompt
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
