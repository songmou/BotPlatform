from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.config.loader import ConfigError, load_project_config


SOURCE_CONFIG = Path(__file__).resolve().parents[1] / "config"
SOURCE_JOBS = Path(__file__).resolve().parents[1] / "src" / "core" / "jobs"


class ConfigLoaderTests(unittest.TestCase):
    def copy_config(self, directory: str) -> Path:
        target = Path(directory) / "config"
        shutil.copytree(SOURCE_CONFIG, target)
        jobs_target = Path(directory) / "src" / "core" / "jobs"
        jobs_target.parent.mkdir(parents=True)
        shutil.copytree(SOURCE_JOBS, jobs_target)
        return target

    @staticmethod
    def load_json(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def save_json(path: Path, data) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def script_manifest(directory: str, folder: str) -> Path:
        return Path(directory) / "src" / "core" / "jobs" / folder / "script.json"

    def test_valid_project_configuration_loads(self) -> None:
        config = load_project_config(SOURCE_CONFIG)
        self.assertEqual(config.app.default_agent, "general")
        self.assertEqual(config.active_agent.name, "通用 AI 助手")
        self.assertIn("translator", config.agents)
        self.assertEqual(len(config.schedules), 7)
        self.assertEqual(
            set(config.scripts), {"autogen_monitor", "ctsehr_check", "ctsoa_check"}
        )
        self.assertTrue(
            all(not script.requires_approval for script in config.scripts.values())
        )
        todo_reminder = next(
            task for task in config.schedules if task.id == "todo_reminder"
        )
        self.assertEqual(todo_reminder.crons, ["0 9 * * *", "0 18 * * *"])
        self.assertEqual(todo_reminder.action.type, "plugin")
        self.assertEqual(todo_reminder.action.plugin_id, "todo")
        self.assertEqual(todo_reminder.action.tool_name, "todo_manage")
        self.assertEqual(todo_reminder.action.parameters, {"action": "remind"})
        todo_archive = next(
            task for task in config.schedules if task.id == "todo_monthly_archive"
        )
        self.assertEqual(todo_archive.cron, "5 9 1 * *")
        self.assertEqual(todo_archive.action.parameters, {"action": "archive"})
        reminder = next(
            task for task in config.schedules if task.id == "inactive_user_reminder"
        )
        self.assertTrue(all(not task.enabled for task in config.schedules))
        self.assertEqual(reminder.condition.type, "inactivity_once")
        self.assertEqual(reminder.condition.after_hours, 20)
        self.assertEqual(reminder.condition.before_hours, 24)
        self.assertTrue(config.tools.enabled)
        self.assertTrue(config.plugins["todo"].enabled)
        self.assertFalse(config.plugins["browser_automation"].enabled)
        self.assertFalse(config.plugins["web_research"].enabled)
        self.assertFalse(config.plugins["codex_tasks"].enabled)
        self.assertFalse(config.plugins["ocr"].enabled)
        self.assertTrue(config.plugins["ocr"].settings["auto_process_chat_images"])
        self.assertEqual(config.plugins["ocr"].settings["model_tier"], "small")
        self.assertEqual(config.plugins["ocr"].settings["max_pdf_pages"], 10)
        self.assertNotIn("ocr_extract_text", config.active_agent.tools)
        self.assertIn(
            "ocr_extract_text", config.active_agent.plugin_tools.get("ocr", [])
        )
        self.assertIn("run_command", config.active_agent.tools)
        self.assertEqual(config.active_agent.mcp_servers, [])
        self.assertEqual(config.mcp_servers, [])
        self.assertEqual(config.datasources, [])
        self.assertEqual(config.app.active_model, "qwen_cloud")
        self.assertEqual(config.active_model.type, "openai_compatible")
        self.assertTrue(config.active_model.enabled)
        self.assertTrue(config.models["ollama_local"].enabled)
        self.assertEqual(config.models["ollama_local"].model, "gemma4:e4b")
        self.assertEqual(config.app.local_model, "ollama_local")
        self.assertEqual(config.app.vision_model, "ollama_local")
        self.assertEqual(config.app.embedding_model, "bge_m3_local")
        self.assertEqual(config.app.rerank_model, "")
        self.assertEqual(config.models["bge_m3_local"].modality, "embedding")
        self.assertEqual(config.models["bge_m3_local"].dimensions, 1024)
        self.assertFalse(config.models["bge_m3_local"].enabled)
        self.assertEqual(config.app.fallback_model, "qwen_cloud")
        self.assertEqual(config.app.fallback_cooldown_seconds, 60)
        self.assertNotIn("deepseek_cloud", config.models)
        self.assertNotIn("deepseek_pro", config.models)
        self.assertIn("qwen_cloud", config.models)
        self.assertTrue(config.models["qwen_cloud"].enabled)
        self.assertEqual(config.models["qwen_cloud"].model, "qwen-flash")
        self.assertEqual(config.models["qwen_cloud"].provider, "dashscope")
        self.assertFalse(config.models["qwen_cloud"].capabilities.vision)

    def test_agent_prompts_define_user_language_and_chinese_fallback(self) -> None:
        config = load_project_config(SOURCE_CONFIG)

        for agent_id in ("general", "image_analyst", "translator"):
            with self.subTest(agent_id=agent_id):
                prompt = config.agents[agent_id].system_prompt
                self.assertIn("以用户当前一轮输入使用的语言作为交互语言", prompt)
                self.assertIn("thinking（思考过程）", prompt)
                self.assertIn("默认使用简体中文", prompt)

        translator_prompt = config.agents["translator"].system_prompt
        self.assertIn("译文正文必须使用用户指定的目标语言", translator_prompt)
        self.assertIn("不得因上述交互语言规则而改变目标语言", translator_prompt)

    def test_script_approval_defaults_to_true_and_must_be_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = self.script_manifest(directory, "ctsehr")
            data = self.load_json(path)
            data.pop("requires_approval")
            self.save_json(path, data)
            config = load_project_config(config_dir)
            self.assertTrue(config.scripts["ctsehr_check"].requires_approval)

        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = self.script_manifest(directory, "ctsehr")
            data = self.load_json(path)
            data["requires_approval"] = "false"
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "requires_approval.*布尔值"):
                load_project_config(config_dir)

    def test_jobs_folder_without_manifest_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            helper_dir = Path(directory) / "src" / "core" / "jobs" / "helpers"
            helper_dir.mkdir()
            (helper_dir / "shared.py").write_text("VALUE = 1\n", encoding="utf-8")
            config = load_project_config(config_dir)
            self.assertEqual(
                set(config.scripts),
                {"autogen_monitor", "ctsehr_check", "ctsoa_check"},
            )

    def test_invalid_tool_root_and_unknown_agent_tool_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "tools.json"
            data = self.load_json(path)
            data["allowed_roots"] = ["relative/path"]
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "allowed_roots.*绝对路径"):
                load_project_config(config_dir)

        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "agents" / "general.json"
            data = self.load_json(path)
            data["tools"].append("unknown_tool")
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "unknown_tool"):
                load_project_config(config_dir)

    def test_invalid_json_reports_file_and_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "app.json"
            path.write_text("{ invalid", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "app.json.*JSON 格式错误"):
                load_project_config(config_dir)

    def test_missing_default_agent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "app.json"
            data = self.load_json(path)
            data["default_agent"] = "missing"
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "default_agent.*missing"):
                load_project_config(config_dir)

    def test_duplicate_agent_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            shutil.copy(
                config_dir / "agents" / "general.json",
                config_dir / "agents" / "duplicate.json",
            )
            with self.assertRaisesRegex(ConfigError, "Agent id 重复.*general"):
                load_project_config(config_dir)

    def test_agent_enabled_defaults_true_and_flag_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "agents" / "translator.json"
            data = self.load_json(path)
            data["enabled"] = False
            self.save_json(path, data)
            config = load_project_config(config_dir)
            # Field missing on disk -> enabled by default.
            self.assertTrue(config.agents["general"].enabled)
            self.assertFalse(config.agents["translator"].enabled)

    def test_agent_enabled_must_be_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "agents" / "general.json"
            data = self.load_json(path)
            data["enabled"] = 1
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "enabled.*布尔值"):
                load_project_config(config_dir)

    def test_disabled_default_agent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "agents" / "general.json"
            data = self.load_json(path)
            data["enabled"] = False
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "默认 Agent general 必须启用"):
                load_project_config(config_dir)

    def test_invalid_timezone_and_cron_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            app_path = config_dir / "app.json"
            app = self.load_json(app_path)
            app["timezone"] = "Invalid/Timezone"
            self.save_json(app_path, app)
            with self.assertRaisesRegex(ConfigError, "timezone.*有效时区"):
                load_project_config(config_dir)

        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            schedule_path = config_dir / "schedules.json"
            schedules = self.load_json(schedule_path)
            schedules["tasks"][0]["cron"] = "not a cron"
            self.save_json(schedule_path, schedules)
            with self.assertRaisesRegex(ConfigError, "cron.*五段 cron"):
                load_project_config(config_dir)

    def test_unknown_action_and_agent_reference_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "schedules.json"
            data = self.load_json(path)
            data["tasks"][0]["action"] = {"type": "unknown"}
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "action.type.*script"):
                load_project_config(config_dir)

        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "schedules.json"
            data = self.load_json(path)
            data["tasks"][1]["action"]["agent_id"] = "missing"
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "agent_id.*missing"):
                load_project_config(config_dir)

    def test_agent_skills_and_mcp_servers_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            # Prepend the fixture skill so existing agent skill references
            # (writer, coder, ...) still resolve against the copied config.
            skills_data = self.load_json(config_dir / "skills.json")
            skills_data["skills"].insert(
                0,
                {"id": "greeting_skill", "name": "问候", "prompt": "保持礼貌。", "enabled": True},
            )
            self.save_json(config_dir / "skills.json", skills_data)
            self.save_json(config_dir / "mcp_servers.json", {
                "servers": [
                    {"id": "echo", "name": "Echo", "transport": "stdio", "command": "echo", "enabled": True}
                ]
            })
            agent_path = config_dir / "agents" / "general.json"
            agent_data = self.load_json(agent_path)
            agent_data["skills"] = ["greeting_skill"]
            agent_data["mcp_servers"] = ["echo"]
            self.save_json(agent_path, agent_data)

            config = load_project_config(config_dir)
            self.assertEqual(config.agents["general"].skills, ["greeting_skill"])
            self.assertEqual(config.agents["general"].mcp_servers, ["echo"])
            self.assertEqual(config.skills[0]["id"], "greeting_skill")
            self.assertEqual(config.mcp_servers[0]["id"], "echo")

    def test_mcp_server_headers_merged_from_secret_store(self) -> None:
        import src.core.config.mcp_headers as mcp_headers

        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            self.save_json(config_dir / "mcp_servers.json", {
                "servers": [
                    {
                        "id": "remote_api",
                        "name": "远程服务",
                        "transport": "streamablehttp",
                        "url": "https://example.com/mcp",
                        "headers": {},
                        "enabled": True,
                    }
                ]
            })
            headers_file = Path(directory) / "mcp_headers.json"
            agent_path = config_dir / "agents" / "general.json"
            agent_data = self.load_json(agent_path)
            agent_data["mcp_servers"] = ["remote_api"]
            self.save_json(agent_path, agent_data)
            with patch.object(mcp_headers, "MCP_HEADERS_FILE", headers_file):
                mcp_headers.save_headers(
                    "remote_api", {"Authorization": "Bearer secret-token"}
                )
                config = load_project_config(config_dir)
            self.assertEqual(
                config.mcp_servers[0]["headers"],
                {"Authorization": "Bearer secret-token"},
            )

    def test_unknown_skill_reference_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            agent_path = config_dir / "agents" / "general.json"
            agent_data = self.load_json(agent_path)
            agent_data["skills"] = ["missing_skill"]
            self.save_json(agent_path, agent_data)
            with self.assertRaisesRegex(ConfigError, "未知技能.*missing_skill"):
                load_project_config(config_dir)

    def test_unknown_mcp_server_reference_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            agent_path = config_dir / "agents" / "general.json"
            agent_data = self.load_json(agent_path)
            agent_data["mcp_servers"] = ["missing_server"]
            self.save_json(agent_path, agent_data)
            with self.assertRaisesRegex(ConfigError, "未知 MCP 服务.*missing_server"):
                load_project_config(config_dir)

    def test_missing_plugin_binding_remains_visible_without_blocking_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            plugins_path = config_dir / "plugins.json"
            plugins = self.load_json(plugins_path)
            plugins["plugins"].append(
                {
                    "id": "retired_plugin",
                    "enabled": True,
                    "settings": {"legacy": True},
                }
            )
            self.save_json(plugins_path, plugins)
            agent_path = config_dir / "agents" / "general.json"
            agent = self.load_json(agent_path)
            agent.setdefault("plugin_tools", {})["retired_plugin"] = [
                "retired_tool"
            ]
            self.save_json(agent_path, agent)

            config = load_project_config(config_dir)
            self.assertTrue(config.plugins["retired_plugin"].enabled)
            self.assertEqual(
                config.agents["general"].plugin_tools["retired_plugin"],
                ["retired_tool"],
            )

    def test_installed_plugin_binding_rejects_unknown_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            agent_path = config_dir / "agents" / "general.json"
            agent = self.load_json(agent_path)
            agent.setdefault("plugin_tools", {})["todo"] = ["missing_tool"]
            self.save_json(agent_path, agent)
            with self.assertRaisesRegex(
                ConfigError,
                r"plugin_tools\.todo\[0\].*插件中不存在的工具",
            ):
                load_project_config(config_dir)

    def test_plugin_tool_in_tools_array_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            agent_path = config_dir / "agents" / "general.json"
            agent = self.load_json(agent_path)
            agent["tools"] = list(agent.get("tools") or []) + ["todo_manage"]
            self.save_json(agent_path, agent)
            with self.assertRaisesRegex(
                ConfigError,
                r"tools\[\d+\].*请写入 plugin_tools",
            ):
                load_project_config(config_dir)

    def test_invalid_skill_and_mcp_entries_are_rejected_at_load_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            self.save_json(config_dir / "skills.json", {
                "skills": [{"id": "Bad-Id", "name": "问候"}]
            })
            with self.assertRaisesRegex(ConfigError, r"skills\[0\].id"):
                load_project_config(config_dir)

        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            self.save_json(config_dir / "mcp_servers.json", {
                "servers": [{"id": "echo", "name": "Echo", "transport": "stdio"}]
            })
            with self.assertRaisesRegex(ConfigError, "command.*stdio"):
                load_project_config(config_dir)

    def test_runtime_updates_validate_and_mutate_lists_in_place(self) -> None:
        config = load_project_config(SOURCE_CONFIG)
        skills_ref = config.skills
        servers_ref = config.mcp_servers
        config.update_skills(
            [{"id": "greeting_skill", "name": "问候", "prompt": "保持礼貌。", "enabled": True}]
        )
        config.update_mcp_servers(
            [{"id": "echo", "name": "Echo", "transport": "stdio", "command": "echo", "enabled": True}]
        )
        # In-place mutation keeps every holder of the original list up to date.
        self.assertIs(config.skills, skills_ref)
        self.assertIs(config.mcp_servers, servers_ref)
        self.assertEqual([entry["id"] for entry in skills_ref], ["greeting_skill"])
        self.assertEqual([entry["id"] for entry in servers_ref], ["echo"])

    def test_runtime_updates_reject_invalid_entries_without_side_effects(self) -> None:
        config = load_project_config(SOURCE_CONFIG)
        skills_before = list(config.skills)
        servers_before = list(config.mcp_servers)

        invalid_skill_lists = [
            [{"id": "Bad-Id", "name": "问候"}],
            [{"id": "dup", "name": "甲"}, {"id": "dup", "name": "乙"}],
            [{"id": "ok_skill", "name": "问候", "surprise": 1}],
            [{"id": "ok_skill", "name": "问候", "enabled": "yes"}],
            [{"id": "ok_skill", "name": " "}],
        ]
        for skills in invalid_skill_lists:
            with self.subTest(skills=skills):
                with self.assertRaises(ConfigError):
                    config.update_skills(skills)

        invalid_server_lists = [
            [{"id": "echo", "name": "Echo", "transport": "stdio"}],
            [{"id": "echo", "name": "Echo", "transport": "carrier_pigeon", "command": "x"}],
            [{"id": "echo", "name": "Echo", "transport": "sse"}],
            [{"id": "echo", "name": "Echo", "transport": "stdio", "command": "x", "args": [1]}],
            [
                {"id": "echo", "name": "Echo", "transport": "stdio", "command": "x"},
                {"id": "echo", "name": "Echo2", "transport": "stdio", "command": "y"},
            ],
        ]
        for servers in invalid_server_lists:
            with self.subTest(servers=servers):
                with self.assertRaises(ConfigError):
                    config.update_mcp_servers(servers)

        # Failed updates must leave the live lists untouched.
        self.assertEqual(config.skills, skills_before)
        self.assertEqual(config.mcp_servers, servers_before)

    def test_image_schedule_supports_local_path_url_and_optional_caption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "schedules.json"
            data = self.load_json(path)
            data["tasks"][0]["action"] = {
                "type": "image",
                "image_path": "output/status.png",
                "caption": "状态报告",
            }
            data["tasks"][1]["action"] = {
                "type": "image",
                "image_url": "http://127.0.0.1/report.png?token=secret",
            }
            self.save_json(path, data)

            config = load_project_config(config_dir)
            self.assertEqual(
                config.schedules[0].action.image_path,
                str((Path(directory) / "output" / "status.png").resolve()),
            )
            self.assertEqual(config.schedules[0].action.caption, "状态报告")
            self.assertEqual(
                config.schedules[1].action.image_url,
                "http://127.0.0.1/report.png?token=secret",
            )

    def test_image_schedule_rejects_invalid_source_and_caption(self) -> None:
        invalid_actions = [
            ({"type": "image"}, "必须且只能"),
            (
                {
                    "type": "image",
                    "image_path": "one.png",
                    "image_url": "https://example.test/two.png",
                },
                "必须且只能",
            ),
            (
                {"type": "image", "image_url": "file:///tmp/image.png"},
                "HTTP",
            ),
            (
                {"type": "image", "image_url": "https://user:pass@example.test/a.png"},
                "用户名或密码",
            ),
            (
                {"type": "image", "image_path": "one.png", "caption": " "},
                "caption",
            ),
        ]
        for action, expected in invalid_actions:
            with self.subTest(action=action):
                with tempfile.TemporaryDirectory() as directory:
                    config_dir = self.copy_config(directory)
                    path = config_dir / "schedules.json"
                    data = self.load_json(path)
                    data["tasks"][0]["action"] = action
                    self.save_json(path, data)
                    with self.assertRaisesRegex(ConfigError, expected):
                        load_project_config(config_dir)

    def test_inactivity_condition_validation_and_optional_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "schedules.json"
            data = self.load_json(path)
            data["tasks"][0].pop("condition", None)
            self.save_json(path, data)
            config = load_project_config(config_dir)
            self.assertIsNone(config.schedules[0].condition)

        invalid_conditions = [
            {"type": "unknown", "after_hours": 20, "before_hours": 24},
            {"type": "inactivity_once", "after_hours": 0, "before_hours": 24},
            {"type": "inactivity_once", "after_hours": 24, "before_hours": 24},
            {"type": "inactivity_once", "after_hours": 20, "before_hours": 25},
        ]
        for condition in invalid_conditions:
            with self.subTest(condition=condition):
                with tempfile.TemporaryDirectory() as directory:
                    config_dir = self.copy_config(directory)
                    path = config_dir / "schedules.json"
                    data = self.load_json(path)
                    data["tasks"][0]["condition"] = condition
                    self.save_json(path, data)
                    with self.assertRaisesRegex(ConfigError, "condition"):
                        load_project_config(config_dir)

    def test_script_registry_and_schedule_parameters_are_strictly_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = self.script_manifest(directory, "ctsehr")
            data = self.load_json(path)
            data["entrypoint"] = "../ctsoa/monitor.py"
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "脚本目录内"):
                load_project_config(config_dir)

        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "schedules.json"
            data = self.load_json(path)
            task = next(item for item in data["tasks"] if item["id"] == "ctsehr_check")
            task["action"]["parameters"] = {"date": "invalid"}
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "必须是.*日期"):
                load_project_config(config_dir)

        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "schedules.json"
            data = self.load_json(path)
            task = next(item for item in data["tasks"] if item["id"] == "ctsehr_check")
            task["action"]["parameters"] = {"phase": "morning"}
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "未知参数.*phase"):
                load_project_config(config_dir)

        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            duplicate_dir = Path(directory) / "src" / "core" / "jobs" / "zz_duplicate"
            duplicate_dir.mkdir()
            (duplicate_dir / "monitor.py").write_text(
                "print('dup')\n", encoding="utf-8"
            )
            manifest = self.load_json(self.script_manifest(directory, "ctsehr"))
            manifest["entrypoint"] = "monitor.py"
            self.save_json(duplicate_dir / "script.json", manifest)
            with self.assertRaisesRegex(ConfigError, "脚本 id 重复"):
                load_project_config(config_dir)

    def test_model_profile_override_and_disabled_active_validation(self) -> None:
        with patch.dict("os.environ", {"MODEL_PROFILE": "qwen_cloud"}, clear=False):
            config = load_project_config(SOURCE_CONFIG)
            self.assertEqual(config.active_model.id, "qwen_cloud")
        with patch.dict("os.environ", {"MODEL_PROFILE": "ollama_local"}, clear=False):
            config = load_project_config(SOURCE_CONFIG)
            self.assertEqual(config.active_model.id, "ollama_local")
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            models_path = config_dir / "models.json"
            models = self.load_json(models_path)
            models["profiles"]["ollama_local"]["enabled"] = False
            self.save_json(models_path, models)
            with patch.dict("os.environ", {"MODEL_PROFILE": "ollama_local"}, clear=False):
                with self.assertRaisesRegex(ConfigError, "必须启用"):
                    load_project_config(config_dir)

    def test_fallback_reference_and_cooldown_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "app.json"
            data = self.load_json(path)
            data["fallback_model"] = "missing"
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "fallback_model.*missing"):
                load_project_config(config_dir)

        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "app.json"
            data = self.load_json(path)
            data["fallback_cooldown_seconds"] = 0
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "fallback_cooldown_seconds"):
                load_project_config(config_dir)

    def test_invalid_model_url_and_reserved_request_extra_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "models.json"
            data = self.load_json(path)
            data["profiles"]["qwen_cloud"]["base_url"] = "http://remote.example"
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "HTTPS"):
                load_project_config(config_dir)

        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "models.json"
            data = self.load_json(path)
            data["profiles"]["qwen_cloud"]["request_extra"] = {
                "model": "override"
            }
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "不能覆盖核心字段.*model"):
                load_project_config(config_dir)

    def test_modality_defaults_to_chat_when_field_absent(self) -> None:
        config = load_project_config(SOURCE_CONFIG)
        # Existing chat profiles omit the modality field entirely.
        self.assertEqual(config.models["qwen_cloud"].modality, "chat")
        self.assertIsNone(config.models["qwen_cloud"].dimensions)

    def test_embedding_profile_requires_positive_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "models.json"
            data = self.load_json(path)
            del data["profiles"]["bge_m3_local"]["dimensions"]
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "dimensions.*大于 0 的整数"):
                load_project_config(config_dir)

    def test_embedding_profile_rejects_chat_only_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "models.json"
            data = self.load_json(path)
            data["profiles"]["bge_m3_local"]["temperature"] = 0.5
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "包含未知字段.*temperature"):
                load_project_config(config_dir)

    def test_rerank_profile_rejects_ollama_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "models.json"
            data = self.load_json(path)
            data["profiles"]["local_rerank"] = {
                "enabled": False,
                "modality": "rerank",
                "type": "ollama",
                "provider": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "model": "bge-reranker",
                "timeout_seconds": 60,
            }
            self.save_json(path, data)
            with self.assertRaisesRegex(ConfigError, "重排模型仅支持 openai_compatible 或 local_transformers"):
                load_project_config(config_dir)

    def test_local_transformers_rerank_profile_is_valid(self) -> None:
        config = load_project_config(SOURCE_CONFIG)
        profile = config.models["bge_reranker_v2_m3_local"]
        self.assertEqual(profile.modality, "rerank")
        self.assertEqual(profile.type, "local_transformers")
        self.assertEqual(profile.base_url, "local://transformers")

    def test_role_binding_modalities_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "app.json"
            data = self.load_json(path)
            data["vision_model"] = "bge_m3_local"
            self.save_json(path, data)
            with self.assertRaisesRegex(
                ConfigError, "vision_model 必须引用对话（chat）"
            ):
                load_project_config(config_dir)

        with tempfile.TemporaryDirectory() as directory:
            config_dir = self.copy_config(directory)
            path = config_dir / "app.json"
            data = self.load_json(path)
            data["embedding_model"] = "qwen_cloud"
            self.save_json(path, data)
            with self.assertRaisesRegex(
                ConfigError, "embedding_model 必须引用向量（embedding）"
            ):
                load_project_config(config_dir)

    def test_ollama_model_override_skips_embedding_profile(self) -> None:
        with patch.dict("os.environ", {"OLLAMA_MODEL": "custom-chat"}, clear=False):
            config = load_project_config(SOURCE_CONFIG)
        # OLLAMA_MODEL rewrites the chat model but leaves embedding untouched.
        self.assertEqual(config.models["ollama_local"].model, "custom-chat")
        self.assertEqual(config.models["bge_m3_local"].model, "bge-m3")


if __name__ == "__main__":
    unittest.main()
