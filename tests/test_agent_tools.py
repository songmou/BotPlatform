from __future__ import annotations

import unittest

from src.core.services.agent_tools import (
    build_system_prompt,
    is_tool_call_text,
    resolve_tool_names,
    sanitize_tool_call_text,
    strip_tool_call_text,
)


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
    RULES_HEADER = "# 工具使用规范"

    def test_no_skills_returns_base_prompt(self):
        agent = _Agent(system_prompt="你是助手")
        result = build_system_prompt(agent, [])
        self.assertIn(self.RULES_HEADER, result)
        self.assertIn("你是助手", result)

    def test_rules_forbid_text_format_tool_calls(self):
        agent = _Agent(system_prompt="你是助手")
        result = build_system_prompt(agent, [])
        self.assertIn("严禁在回复文本中输出任何工具调用格式", result)
        self.assertIn("<tool_calls>", result)

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
        result = build_system_prompt(agent, skills)
        self.assertIn(self.RULES_HEADER, result)
        self.assertIn("基础提示", result)

    def test_unselected_skill_is_skipped(self):
        agent = _Agent(skills=["other"])
        skills = [{"id": "greet", "name": "问候", "prompt": "保持礼貌。", "enabled": True}]
        result = build_system_prompt(agent, skills)
        self.assertIn(self.RULES_HEADER, result)
        self.assertIn("基础提示", result)

    def test_skill_without_prompt_is_skipped(self):
        agent = _Agent(skills=["greet"])
        skills = [{"id": "greet", "name": "问候", "prompt": "", "enabled": True}]
        result = build_system_prompt(agent, skills)
        self.assertIn(self.RULES_HEADER, result)
        self.assertIn("基础提示", result)


class ToolCallTextSanitizerTests(unittest.TestCase):
    """硬拦截：文本格式工具调用必须被检测并替换，不展示给用户。"""

    def test_detects_claude_tool_calls_block(self):
        text = '让我先看看。<tool_calls> <invoke name="list_directory"> </invoke> </tool_calls>'
        self.assertTrue(is_tool_call_text(text))

    def test_detects_invoke_with_parameter(self):
        text = '<invoke name="git"><parameter name="command">clone</parameter></invoke>'
        self.assertTrue(is_tool_call_text(text))

    def test_detects_function_calls_and_mcp(self):
        self.assertTrue(is_tool_call_text("<function_calls>"))
        self.assertTrue(is_tool_call_text("<use_mcp_tool>"))
        self.assertTrue(is_tool_call_text("</tool_calls>"))

    def test_normal_text_not_detected(self):
        self.assertFalse(is_tool_call_text("这是普通回复，没有工具调用。"))
        self.assertFalse(is_tool_call_text(""))
        self.assertFalse(is_tool_call_text(None))
        # 代码示例里的尖括号不应误伤
        self.assertFalse(is_tool_call_text("比较 a < b 和 c > d"))

    def test_sanitize_replaces_with_hint(self):
        result = sanitize_tool_call_text(
            '<tool_calls> <invoke name="git"> </invoke> </tool_calls>'
        )
        self.assertIn("没有可用工具", result)
        self.assertNotIn("tool_calls", result)
        self.assertNotIn("invoke", result)

    def test_sanitize_keeps_normal_text(self):
        text = "好的，我已经完成了。"
        self.assertEqual(sanitize_tool_call_text(text), text)

    def test_strip_removes_block_keeps_context(self):
        text = (
            "我先分析一下。<tool_calls> <invoke name=\"git\">"
            " <parameter name=\"command\">clone</parameter> </invoke> </tool_calls>"
            " 然后继续。"
        )
        result = strip_tool_call_text(text)
        self.assertIn("我先分析一下", result)
        self.assertIn("然后继续", result)
        self.assertNotIn("tool_calls", result)
        self.assertNotIn("invoke", result)
        self.assertNotIn("parameter", result)

    def test_strip_removes_nested_block(self):
        text = (
            "<tool_calls><invoke name=\"run_command\">"
            "<parameter name=\"command\">git clone https://example.com/x.git</parameter>"
            "</invoke></tool_calls>"
        )
        self.assertEqual(strip_tool_call_text(text), "")

    def test_strip_keeps_normal_text(self):
        self.assertEqual(strip_tool_call_text("正在检查代码结构"), "正在检查代码结构")
        self.assertEqual(strip_tool_call_text(""), "")
        self.assertIsNone(strip_tool_call_text(None))


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
