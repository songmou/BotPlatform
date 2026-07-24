from __future__ import annotations

import unittest

from src.core.services.agent_tools import build_system_prompt, resolve_tool_names


class _Agent:
    def __init__(self, system_prompt="基础提示", tools=None, skills=None, mcp_servers=None):
        self.system_prompt = system_prompt
        self.tools = tools or []
        self.skills = skills or []
        self.mcp_servers = mcp_servers or []


class _Manager:
    def __init__(self, by_server):
        self._by_server = by_server

    def tool_names(self, server_id=None):
        if server_id is None:
            names = []
            for vals in self._by_server.values():
                names.extend(vals)
            return names
        return list(self._by_server.get(server_id, []))


class _Runtime:
    def __init__(self, manager=None):
        self.mcp_manager = manager


class BuildSystemPromptTests(unittest.TestCase):
    def test_no_skills_returns_base_prompt(self):
        agent = _Agent(system_prompt="你是助手")
        self.assertEqual(build_system_prompt(agent, []), "你是助手")

    def test_selected_enabled_skill_is_appended(self):
        agent = _Agent(system_prompt="你是助手", skills=["greet"])
        skills = [{"id": "greet", "name": "问候", "prompt": "保持礼貌。", "enabled": True}]
        result = build_system_prompt(agent, skills)
        self.assertIn("你是助手", result)
        self.assertIn("# Skill: 问候", result)
        self.assertIn("保持礼貌。", result)

    def test_disabled_skill_is_skipped(self):
        agent = _Agent(skills=["greet"])
        skills = [{"id": "greet", "name": "问候", "prompt": "保持礼貌。", "enabled": False}]
        self.assertEqual(build_system_prompt(agent, skills), "基础提示")

    def test_unselected_skill_is_skipped(self):
        agent = _Agent(skills=["other"])
        skills = [{"id": "greet", "name": "问候", "prompt": "保持礼貌。", "enabled": True}]
        self.assertEqual(build_system_prompt(agent, skills), "基础提示")

    def test_skill_without_prompt_is_skipped(self):
        agent = _Agent(skills=["greet"])
        skills = [{"id": "greet", "name": "问候", "prompt": "", "enabled": True}]
        self.assertEqual(build_system_prompt(agent, skills), "基础提示")


class ResolveToolNamesTests(unittest.TestCase):
    def test_returns_builtin_and_plugin_tools(self):
        agent = _Agent(tools=["read_text_file", "knowledge_search"])
        self.assertEqual(resolve_tool_names(agent, _Runtime()), ["read_text_file", "knowledge_search"])

    def test_expands_selected_mcp_servers(self):
        manager = _Manager({"echo": ["echo__echo", "echo__reverse"], "other": ["other__x"]})
        agent = _Agent(tools=["read_text_file"], mcp_servers=["echo"])
        names = resolve_tool_names(agent, _Runtime(manager))
        self.assertEqual(names, ["read_text_file", "echo__echo", "echo__reverse"])

    def test_no_runtime_returns_only_tools(self):
        agent = _Agent(tools=["a"], mcp_servers=["echo"])
        self.assertEqual(resolve_tool_names(agent, None), ["a"])


class McpNamespacingTests(unittest.TestCase):
    def test_namespaced_name(self):
        from src.core.tooling.mcp_client import namespaced_name

        self.assertEqual(namespaced_name("echo", "run"), "echo__run")

    def test_empty_manager_has_no_tools(self):
        from src.core.tooling.mcp_client import McpClientManager

        manager = McpClientManager()
        self.assertEqual(manager.tool_names(), [])
        self.assertFalse(manager.has_tool("echo__run"))
        self.assertIsNone(manager.tool_schema("echo__run"))


if __name__ == "__main__":
    unittest.main()
