from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.core.config.loader import load_project_config
from src.core.storage.tenants import TenantRegistry
from src.core.tooling import ToolAuditContext, ToolError, ToolRuntime


SOURCE_CONFIG = Path(__file__).resolve().parents[1] / "config"


class FakeScriptService:
    script_ids = ["protected_check", "todo_manager"]

    def __init__(self) -> None:
        self.calls = []
        from types import SimpleNamespace

        self.definitions = {
            "protected_check": SimpleNamespace(
                id="protected_check", name="受保护检查", description=""
            ),
            "todo_manager": SimpleNamespace(
                id="todo_manager", name="待办管理", description=""
            ),
        }

    def list_scripts(self):
        return [
            {
                "id": "protected_check",
                "name": "受保护检查",
                "requires_approval": True,
                "parameters": {},
            },
            {
                "id": "todo_manager",
                "name": "待办管理",
                "requires_approval": False,
                "parameters": {},
            },
        ]

    def requires_approval(self, script_id):
        return script_id != "todo_manager"

    def has_approval_required_scripts(self):
        return True

    def preview(self, script_id, parameters):
        return "运行固定脚本：{} {}".format(script_id, parameters)

    def submit_for_tenant(self, tenant, script_id, parameters):
        self.calls.append((tenant, script_id, parameters))
        return {"run_id": "todo_manager-20260717T120000-12345678", "status": "running"}

    def get_run(self, tenant, run_id):
        return {"run_id": run_id, "status": "success"}


class ToolRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir()
        self.trash = Path(self.temp.name) / "trash"
        original = load_project_config(SOURCE_CONFIG).tools
        config = replace(
            original,
            default_working_directory=str(self.root),
            allowed_roots=[str(self.root)],
            denied_globs=[".git/**", "**/.git/**", ".env", "**/.env", "**/*.key"],
            max_read_bytes=1024,
            max_write_bytes=1024,
            max_command_output_bytes=64,
        )
        self.runtime = ToolRuntime(
            config,
            "Asia/Shanghai",
            trash_directory=self.trash,
            sandbox_available=True,
        )

    def test_read_list_find_and_search(self) -> None:
        (self.root / "notes.txt").write_text("第一行\nhello world\n", encoding="utf-8")
        (self.root / "folder").mkdir()
        (self.root / "folder" / "code.py").write_text("print('hello')\n", encoding="utf-8")

        listed = self.runtime.execute("list_directory", {"path": ".", "depth": 2})
        self.assertTrue(listed.ok)
        paths = [item["path"] for item in listed.data["items"]]
        self.assertIn("notes.txt", paths)
        self.assertIn("folder/code.py", paths)

        found = self.runtime.execute("find_files", {"query": "*.py"})
        self.assertEqual(Path(found.data["results"][0]["path"]).name, "code.py")
        searched = self.runtime.execute("search_text", {"query": "hello"})
        self.assertEqual(len(searched.data["results"]), 2)
        read = self.runtime.execute(
            "read_text_file", {"path": "notes.txt", "start_line": 2, "max_lines": 1}
        )
        self.assertEqual(read.data["content"], "hello world")

    def test_audit_logger_receives_model_context_without_output_body(self) -> None:
        logs = []
        self.runtime.audit_logger = lambda *values: logs.append(values)
        context = ToolAuditContext("user", "deepseek", "cloud", "actual-model")
        result = self.runtime.execute("get_current_time", {}, context)
        self.assertTrue(result.ok)
        self.assertEqual(logs[0][0], context)
        self.assertEqual(logs[0][1], "get_current_time")
        self.assertEqual(logs[0][2], "成功")
        self.assertIsInstance(logs[0][4], int)
        self.assertNotIn(str(result.data), repr(logs[0]))

    def test_path_escape_symlink_and_sensitive_files_are_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (self.root / "escape").symlink_to(outside)
        (self.root / ".env").write_text("TOKEN=secret", encoding="utf-8")
        (self.root / "private.key").write_text("secret", encoding="utf-8")

        for path in ["../outside.txt", "escape", ".env", "private.key"]:
            result = self.runtime.execute("read_text_file", {"path": path})
            self.assertFalse(result.ok, path)

    def test_binary_and_oversized_files_are_rejected(self) -> None:
        (self.root / "binary.bin").write_bytes(b"a\x00b")
        (self.root / "large.txt").write_text("x" * 1025, encoding="utf-8")
        self.assertFalse(
            self.runtime.execute("read_text_file", {"path": "binary.bin"}).ok
        )
        self.assertFalse(
            self.runtime.execute("read_text_file", {"path": "large.txt"}).ok
        )

    def test_confirmed_file_changes_and_trash(self) -> None:
        write_args = {"path": "draft.txt", "content": "hello\n", "mode": "create"}
        preview = self.runtime.preview("write_text_file", write_args)
        self.assertIn("draft.txt", preview)
        result = self.runtime.execute("write_text_file", write_args)
        self.assertTrue(result.ok)
        self.assertEqual((self.root / "draft.txt").read_text(encoding="utf-8"), "hello\n")
        self.assertEqual(os.stat(self.root / "draft.txt").st_mode & 0o777, 0o600)

        replace_args = {
            "path": "draft.txt",
            "old_text": "hello",
            "new_text": "你好",
            "expected_count": 1,
        }
        self.assertIn("替换次数：1", self.runtime.preview("replace_text", replace_args))
        self.assertTrue(self.runtime.execute("replace_text", replace_args).ok)

        self.assertTrue(
            self.runtime.execute(
                "copy_path", {"source": "draft.txt", "destination": "copy.txt"}
            ).ok
        )
        self.assertTrue(
            self.runtime.execute(
                "move_path", {"source": "copy.txt", "destination": "moved.txt"}
            ).ok
        )
        trashed = self.runtime.execute("move_to_trash", {"path": "moved.txt"})
        self.assertTrue(trashed.ok)
        self.assertFalse((self.root / "moved.txt").exists())
        self.assertTrue(Path(trashed.data["trash_path"]).exists())

    def test_command_profiles_reject_eval_and_shell_strings(self) -> None:
        with self.assertRaisesRegex(ToolError, "禁止"):
            self.runtime.command_runner.prepare(
                {"profile": "python", "args": ["-c", "print('bad')"]}
            )
        with self.assertRaises(ToolError):
            self.runtime.command_runner.prepare(
                {"profile": "workspace_script", "args": ["../outside.sh"]}
            )
        with self.assertRaisesRegex(ToolError, "branch"):
            self.runtime.command_runner.prepare(
                {"profile": "git_readonly", "args": ["branch", "new-branch"]}
            )
        result = self.runtime.execute("get_system_info", {"unexpected": True})
        self.assertFalse(result.ok)
        self.assertIn("未知参数", result.error)

    def test_workspace_script_runs_without_shell_interpretation_and_truncates(self) -> None:
        script = self.root / "output.sh"
        script.write_text(
            "#!/bin/sh\nprintf '"
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789extra'\n",
            encoding="utf-8",
        )
        script.chmod(0o700)
        prepared = self.runtime.command_runner.prepare(
            {
                "profile": "workspace_script",
                "args": [str(script), "; touch should-not-exist"],
                "timeout_seconds": 5,
            }
        )
        class FakeProcess:
            pid = 123
            returncode = 0

            def communicate(self, timeout=None):
                return b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789extra", b""

        with patch("src.core.tooling.commands.subprocess.Popen", return_value=FakeProcess()) as popen:
            result = self.runtime.command_runner.execute(prepared)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["output_truncated"])
        self.assertFalse((self.root / "should-not-exist").exists())
        launched = popen.call_args.args[0]
        self.assertIn("; touch should-not-exist", launched)

    def test_registered_script_tools_use_per_script_approval(self) -> None:
        scripts = FakeScriptService()
        registry = TenantRegistry(Path(self.temp.name) / "data")
        tenant = registry.resolve("bot", "user")
        runtime = ToolRuntime(
            self.runtime.config,
            "Asia/Shanghai",
            trash_directory=self.trash,
            sandbox_available=True,
            script_service=scripts,
            tenant_registry=registry,
        )
        runtime.bind_tenant(tenant)
        schema = next(
            item for item in runtime.schemas(["run_script"])
            if item["function"]["name"] == "run_script"
        )
        self.assertEqual(
            schema["function"]["parameters"]["properties"]["script_id"]["enum"],
            ["protected_check", "todo_manager"],
        )
        self.assertTrue(runtime.requires_approval("run_script"))
        for action in (
            "list", "add", "edit", "complete", "reopen", "remind", "archive"
        ):
            self.assertFalse(
                runtime.requires_approval(
                    "run_script",
                    {
                        "script_id": "todo_manager",
                        "parameters": {"action": action},
                    },
                ),
                action,
            )
        self.assertTrue(
            runtime.requires_approval(
                "run_script", {"script_id": "protected_check", "parameters": {}}
            )
        )
        self.assertTrue(runtime.requires_approval("run_script", {}))
        self.assertTrue(
            runtime.requires_approval("run_script", {"script_id": "unknown"})
        )
        automatic_scripts, approval_scripts = runtime.script_approval_groups()
        self.assertEqual(automatic_scripts, ["待办管理（todo_manager）"])
        self.assertEqual(approval_scripts, ["受保护检查（protected_check）"])
        self.assertIn(
            "todo_manager",
            runtime.preview("run_script", {"script_id": "todo_manager"}),
        )
        context = ToolAuditContext(user_id="user")
        result = runtime.execute(
            "run_script",
            {"script_id": "todo_manager", "parameters": {}},
            context,
        )
        self.assertTrue(result.ok)
        self.assertEqual(scripts.calls, [(tenant, "todo_manager", {})])
        self.assertTrue(runtime.execute("list_scripts", {}).ok)
        self.assertTrue(
            runtime.execute(
                "get_script_run",
                {"run_id": "todo_manager-20260717T120000-12345678"},
            ).ok
        )


if __name__ == "__main__":
    unittest.main()
