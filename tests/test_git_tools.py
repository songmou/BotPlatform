"""Tests for the built-in git tool (GitRunner + GitManager)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.tooling.definitions import APPROVAL_TOOLS, TOOL_DEFINITIONS
from src.core.tooling.git_manager import GitError, GitManager
from src.core.tooling.git_runner import (
    GitRunner,
    is_write_operation,
    requires_manual_approval,
)
from src.core.tooling.models import ToolError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _system_git(
    *args: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run raw git for test setup (assumes system git)."""
    return subprocess.run(
        ["git", *args],
        capture_output=True, text=True,
        cwd=cwd,
        env=env,
        check=False,
    )


def _setup_repo_with_commit(repo: Path) -> None:
    """Create a git repo at ``repo`` with one commit."""
    _system_git("init", str(repo))
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    _system_git("-C", str(repo), "add", ".")
    _system_git(
        "-C", str(repo), "commit",
        "-m", "initial",
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.local",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.local",
        },
    )


# ---------------------------------------------------------------------------
# GitManager tests
# ---------------------------------------------------------------------------

class GitManagerTests(unittest.TestCase):
    def tearDown(self) -> None:
        GitManager.reset_cache()

    def test_ensure_git_on_posix(self) -> None:
        """On POSIX, ensure_git() should find system git via shutil.which."""
        if os.name == "nt":
            self.skipTest("POSIX-only test")
        path = GitManager.ensure_git()
        self.assertTrue(os.path.isfile(path), "git binary not found: {}".format(path))
        # Should cache the result
        cached_path = GitManager.ensure_git()
        self.assertEqual(path, cached_path)

    def test_git_version(self) -> None:
        """git_version() should return a version string."""
        if os.name == "nt":
            self.skipTest("POSIX-only test (Windows MinGit download requires network)")
        version = GitManager.git_version()
        self.assertIn("git version", version)

    @patch("src.core.tooling.git_manager.shutil.which", return_value=None)
    def test_ensure_git_raises_on_missing(self, mock_which) -> None:
        """On POSIX without git, should raise GitError."""
        if os.name == "nt":
            self.skipTest("POSIX-only test")
        with self.assertRaisesRegex(RuntimeError, "git"):
            GitManager.ensure_git()

    def test_reset_cache(self) -> None:
        """reset_cache should clear the cached path."""
        if os.name == "nt":
            self.skipTest("POSIX-only test")
        _ = GitManager.ensure_git()
        self.assertIsNotNone(GitManager._cached_path)
        GitManager.reset_cache()
        self.assertIsNone(GitManager._cached_path)

    def test_configured_path_takes_priority(self) -> None:
        """ensure_git(configured_path) returns the configured path when it exists."""
        if os.name == "nt":
            self.skipTest("POSIX-only test")
        GitManager.reset_cache()
        with tempfile.TemporaryDirectory() as tmp:
            fake_git = Path(tmp) / "mygit"
            fake_git.write_text("#!/bin/sh\necho fake-git\n", encoding="utf-8")
            fake_git.chmod(0o755)
            path = GitManager.ensure_git(str(fake_git))
            self.assertEqual(path, str(fake_git.resolve()))

    def test_configured_path_does_not_poison_cache(self) -> None:
        """Clearing git_binary_path must take effect without a restart."""
        GitManager.reset_cache()
        with tempfile.TemporaryDirectory() as tmp:
            fake_git = Path(tmp) / "mygit.exe"
            fake_git.write_text("stub", encoding="utf-8")
            configured = GitManager.find_git(str(fake_git))
            self.assertEqual(configured, str(fake_git.resolve()))
            self.assertIsNone(GitManager._cached_path)

    def test_configured_path_fallback_when_not_found(self) -> None:
        """ensure_git(nonexistent_path) fallthrough to system git."""
        if os.name == "nt":
            self.skipTest("POSIX-only test")
        GitManager.reset_cache()
        bogus = "/nonexistent/git/binary"
        path = GitManager.ensure_git(bogus)
        # Should fallthrough to real system git
        self.assertTrue(os.path.isfile(path), "fallback git not found: {}".format(path))

    def test_find_git_never_downloads(self) -> None:
        """find_git() must stay side-effect free when no git is present."""
        GitManager.reset_cache()
        with patch("src.core.tooling.git_manager.shutil.which", return_value=None), \
                patch.object(GitManager, "_ensure_windows_git") as download:
            with patch.object(Path, "is_file", return_value=False):
                self.assertIsNone(GitManager.find_git())
            download.assert_not_called()

    @patch("src.core.tooling.git_manager.shutil.which", return_value=None)
    @patch("sys.platform", "darwin")
    def test_macos_hint(self, mock_which) -> None:
        """On macOS without git, error should mention xcode-select."""
        if os.name == "nt":
            self.skipTest("POSIX-only test")
        GitManager.reset_cache()
        with self.assertRaisesRegex(GitError, "xcode-select"):
            GitManager.ensure_git()

    @patch("src.core.tooling.git_manager.shutil.which", return_value=None)
    @patch("sys.platform", "linux")
    @patch("src.core.tooling.git_manager.Path.is_file", return_value=True)
    def test_linux_hint_with_apt(self, mock_is_file, mock_which) -> None:
        """On Linux without git but with apt-get, error should mention apt-get."""
        if os.name == "nt":
            self.skipTest("POSIX-only test")
        # The first Path.is_file called in _raise_platform_install_hint would be
        # /usr/bin/apt-get (since we patched sys.platform=linux and all is_file
        # return True).
        GitManager.reset_cache()
        with self.assertRaisesRegex(GitError, "apt-get"):
            GitManager.ensure_git()


# ---------------------------------------------------------------------------
# GitRunner unit tests
# ---------------------------------------------------------------------------

class GitRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("需要系统 git")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.git_root = Path(self.temp.name).resolve() / "git_root"
        self.git_root.mkdir()

        self.runner = GitRunner(
            git_binary=shutil.which("git"),
            git_root=self.git_root,
            author_name="Test Agent",
            author_email="agent@test.local",
            max_output_bytes=65536,
        )

    # ------------------------------------------------------------------
    # init / status / add / commit / log
    # ------------------------------------------------------------------

    def test_init_creates_repository(self) -> None:
        """init must work on a path that is not a repository yet."""
        repo = self.git_root / "myrepo"
        result = self.runner.execute("init", [], str(repo), 30)
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertTrue((repo / ".git").is_dir())
        self.assertEqual(result["repo"], "myrepo")

        result = self.runner.execute("status", ["--short"], str(repo), 30)
        self.assertEqual(result["exit_code"], 0, result["stderr"])

    def test_add_commit_log(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)

        result = self.runner.execute("log", ["--oneline"], str(repo), 30)
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertIn("initial", result["stdout"])

        # Second commit
        (repo / "file2.txt").write_text("world\n", encoding="utf-8")
        result = self.runner.execute("add", ["-A"], str(repo), 30)
        self.assertEqual(result["exit_code"], 0, result["stderr"])

        result = self.runner.execute(
            "commit", ["-m", "second commit"], str(repo), 30,
        )
        self.assertEqual(result["exit_code"], 0, result["stderr"])

        result = self.runner.execute("log", ["--oneline"], str(repo), 30)
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertIn("second commit", result["stdout"])

    def test_commit_message_accepts_punctuation(self) -> None:
        """shell=False 下引号/括号无注入风险，不能拒绝正常提交说明。"""
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        (repo / "extra.txt").write_text("x\n", encoding="utf-8")
        self.runner.execute("add", ["-A"], str(repo), 30)
        result = self.runner.execute(
            "commit", ["-m", 'fix(工具): 修复 "引号" 与 (括号) 场景'], str(repo), 30,
        )
        self.assertEqual(result["exit_code"], 0, result["stderr"])

    def test_diff(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        (repo / "file.txt").write_text("hello\nmodified\n", encoding="utf-8")

        result = self.runner.execute("diff", ["--no-color"], str(repo), 30)
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertIn("modified", result["stdout"])

    # ------------------------------------------------------------------
    # branch / checkout
    # ------------------------------------------------------------------

    def test_branch_and_checkout(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)

        # Create a branch
        result = self.runner.execute("branch", ["feature"], str(repo), 30)
        self.assertEqual(result["exit_code"], 0, result["stderr"])

        # Switch to it
        result = self.runner.execute("checkout", ["feature"], str(repo), 30)
        self.assertEqual(result["exit_code"], 0, result["stderr"])

        result = self.runner.execute(
            "branch", ["--show-current"], str(repo), 30,
        )
        self.assertIn("feature", result["stdout"].strip())

    # ------------------------------------------------------------------
    # grep（只读代码搜索）
    # ------------------------------------------------------------------

    def test_grep_search(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        (repo / "app.py").write_text("def main():\n    return 'hello'\n", encoding="utf-8")
        self.runner.execute("add", ["-A"], str(repo), 30)
        self.runner.execute("commit", ["-m", "add app"], str(repo), 30)

        result = self.runner.execute(
            "grep", ["-n", "hello"], str(repo), 30,
        )
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertIn("hello", result["stdout"])

    def test_grep_count_and_files_with_matches(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        (repo / "a.py").write_text("todo: fix me\n", encoding="utf-8")
        (repo / "b.py").write_text("no match here\n", encoding="utf-8")
        self.runner.execute("add", ["-A"], str(repo), 30)
        self.runner.execute("commit", ["-m", "add files"], str(repo), 30)

        result = self.runner.execute(
            "grep", ["--files-with-matches", "todo"], str(repo), 30,
        )
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertIn("a.py", result["stdout"])
        self.assertNotIn("b.py", result["stdout"])

    def test_grep_no_match_returns_nonzero(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        result = self.runner.execute(
            "grep", ["nonexistent_pattern_xyz"], str(repo), 30,
        )
        self.assertNotEqual(result["exit_code"], 0)

    def test_grep_dangerous_flags_rejected(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        with self.assertRaises(ToolError):
            self.runner.execute(
                "grep", ["--open-files-in-pager", "x"], str(repo), 30,
            )
        with self.assertRaises(ToolError):
            self.runner.execute(
                "grep", ["--textconv", "x"], str(repo), 30,
            )

    # ------------------------------------------------------------------
    # push / clone（本地仓库，离线）
    # ------------------------------------------------------------------

    def test_push_and_clone(self) -> None:
        repo = self.git_root / "myrepo"
        bare = self.git_root / "bare.git"
        bare.mkdir()
        _system_git("init", "--bare", str(bare))

        _setup_repo_with_commit(repo)
        _system_git("-C", str(repo), "remote", "add", "origin", str(bare))
        head = _system_git("-C", str(repo), "branch", "--show-current").stdout.strip()

        result = self.runner.execute(
            "push", ["-u", "origin", head], str(repo), 30,
        )
        self.assertEqual(result["exit_code"], 0, result["stderr"])

        # Clone the bare repo — the target comes from repo_path, not args.
        result = self.runner.execute("clone", [str(bare)], "clone", 60)
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        clone = self.git_root / "clone"
        self.assertTrue((clone / ".git").is_dir())
        self.assertTrue((clone / "file.txt").exists())
        self.assertEqual(result["repo"], "clone")

    def test_clone_rejects_target_in_args(self) -> None:
        """目标目录只能来自 repo_path，否则 repo_path 会被静默忽略。"""
        src = self.git_root / "src_repo"
        _setup_repo_with_commit(src)
        with self.assertRaises(ToolError):
            self.runner.execute("clone", [str(src), "other_target"], "target", 30)

    def test_clone_rejects_non_empty_target(self) -> None:
        src = self.git_root / "src_repo"
        _setup_repo_with_commit(src)
        target = self.git_root / "occupied"
        target.mkdir()
        (target / "keep.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(ToolError):
            self.runner.execute("clone", [str(src)], "occupied", 30)

    def test_clone_defaults_to_shallow(self) -> None:
        """默认浅克隆，避免大仓库拉全量历史超时。"""
        src = self.git_root / "deep_repo"
        _setup_repo_with_commit(src)
        (src / "file.txt").write_text("second\n", encoding="utf-8")
        _system_git("-C", str(src), "add", ".")
        _system_git(
            "-C", str(src), "commit", "-m", "second",
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@test.local",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@test.local",
            },
        )

        result = self.runner.execute("clone", [src.as_uri()], "shallow", 60)
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        log = _system_git(
            "-C", str(self.git_root / "shallow"), "log", "--oneline"
        ).stdout.strip().splitlines()
        self.assertEqual(len(log), 1)

    def test_clone_full_history_when_requested(self) -> None:
        src = self.git_root / "deep_repo2"
        _setup_repo_with_commit(src)
        (src / "file.txt").write_text("second\n", encoding="utf-8")
        _system_git("-C", str(src), "add", ".")
        _system_git(
            "-C", str(src), "commit", "-m", "second",
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@test.local",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@test.local",
            },
        )

        result = self.runner.execute(
            "clone", [src.as_uri(), "--no-single-branch"], "full", 60
        )
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        log = _system_git(
            "-C", str(self.git_root / "full"), "log", "--oneline"
        ).stdout.strip().splitlines()
        self.assertEqual(len(log), 2)

    def test_clone_with_progress_callback(self) -> None:
        src = self.git_root / "src_repo"
        _setup_repo_with_commit(src)
        seen: list = []
        result = self.runner.execute(
            "clone", [str(src)], "clone_progress",
            60, progress_callback=lambda p, d: seen.append((p, d)),
        )
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertTrue((self.git_root / "clone_progress" / ".git").is_dir())
        # 回调应可调用（本地克隆可能无百分比行，但机制不能报错）
        self.assertIsInstance(seen, list)

    # ------------------------------------------------------------------
    # Security: clone source restrictions
    # ------------------------------------------------------------------

    def test_clone_rejects_ext_transport(self) -> None:
        with self.assertRaises(ToolError):
            self.runner.execute("clone", ["ext::sh -c whoami"], "evil", 30)

    def test_clone_rejects_ssh_source(self) -> None:
        with self.assertRaises(ToolError):
            self.runner.execute("clone", ["git@github.com:foo/bar.git"], "evil", 30)

    def test_clone_rejects_plain_http_remote(self) -> None:
        with self.assertRaises(ToolError):
            self.runner.execute("clone", ["http://example.com/x.git"], "evil", 30)

    def test_clone_allows_loopback_http(self) -> None:
        """回环 HTTP 允许通过校验（连接失败与否由 git 决定）。"""
        try:
            self.runner.execute("clone", ["http://127.0.0.1:1/x.git"], "loopback", 5)
        except ToolError as exc:
            self.assertNotIn("HTTPS", str(exc))

    def test_clone_rejects_local_source_outside_root(self) -> None:
        outside = Path(self.temp.name).resolve() / "outside_repo"
        _setup_repo_with_commit(outside)
        with self.assertRaises(ToolError):
            self.runner.execute("clone", [str(outside)], "stolen", 30)

    def test_clone_rejects_file_url_outside_root(self) -> None:
        outside = Path(self.temp.name).resolve() / "outside_repo2"
        _setup_repo_with_commit(outside)
        url = "file:///{}".format(str(outside).replace("\\", "/").lstrip("/"))
        with self.assertRaises(ToolError):
            self.runner.execute("clone", [url], "stolen2", 30)

    # ------------------------------------------------------------------
    # Security: dangerous characters
    # ------------------------------------------------------------------

    def test_control_chars_rejected(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        with self.assertRaises(ToolError):
            self.runner.execute("commit", ["-m", "bad\nmessage"], str(repo), 30)

    def test_structural_arg_metachars_rejected(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        with self.assertRaises(ToolError):
            self.runner.execute("checkout", ["-b", "feat;rm -rf /"], str(repo), 30)
        with self.assertRaises(ToolError):
            self.runner.execute("log", ["$(cat /etc/passwd)"], str(repo), 30)

    # ------------------------------------------------------------------
    # Security: unknown flag rejection
    # ------------------------------------------------------------------

    def test_unknown_flag_rejected(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        with self.assertRaises(ToolError):
            self.runner.execute("log", ["--output=/tmp/evil"], str(repo), 30)

    def test_unknown_short_flag_rejected(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        with self.assertRaises(ToolError):
            self.runner.execute("log", ["-Z"], str(repo), 30)

    def test_log_numeric_shortcut_allowed(self) -> None:
        """数字简写必须放行且不能让参数校验陷入死循环。"""
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        result = self.runner.execute(
            "log", ["-5", "--oneline"], str(repo), 30
        )
        self.assertEqual(result["exit_code"], 0, result["stderr"])

    def test_short_flag_without_value_rejected(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        with self.assertRaises(ToolError):
            self.runner.execute("commit", ["-m"], str(repo), 30)

    def test_remote_verbose_allowed(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        self.runner.execute("remote", ["add", "origin", "https://example.com/x.git"], str(repo), 30)
        result = self.runner.execute("remote", ["-v"], str(repo), 30)
        self.assertEqual(result["exit_code"], 0, result["stderr"])

    def test_fetch_allowed(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        # fetch 对无远程的仓库会 git 自身报错，但不应因参数白名单被拒绝
        result = self.runner.execute("fetch", ["--all"], str(repo), 30)
        self.assertEqual(result["stderr"].find("不允许参数"), -1, result["stderr"])

    def test_numeric_shortcut_rejected_for_other_commands(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        with self.assertRaises(ToolError):
            self.runner.execute("status", ["-5"], str(repo), 30)

    # ------------------------------------------------------------------
    # Security: path traversal
    # ------------------------------------------------------------------

    def test_path_traversal_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.runner.execute("init", [], "../outside", 30)

    def test_absolute_path_outside_git_root_rejected(self) -> None:
        outside = str(Path(self.temp.name).resolve() / "evil")
        with self.assertRaises(ToolError):
            self.runner.execute("init", [], outside, 30)

    def test_repo_path_repeating_root_name_is_rejected(self) -> None:
        """repo_path 重复带上根目录名会解析出 git_repos/git_repos/...。"""
        with self.assertRaises(ToolError) as ctx:
            self.runner.execute(
                "init", [], "{}/code-reviewer".format(self.git_root.name), 30
            )
        self.assertIn("不要重复带上", str(ctx.exception))

    def test_pull_on_missing_repo_suggests_clone(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.runner.execute("pull", [], "not-cloned-yet", 30)
        self.assertIn("clone", str(ctx.exception))

    # ------------------------------------------------------------------
    # Security: timeout
    # ------------------------------------------------------------------

    def test_zero_timeout_rejected(self) -> None:
        repo = self.git_root / "myrepo"
        with self.assertRaises(ToolError):
            self.runner.execute("init", [], str(repo), -1)

    # ------------------------------------------------------------------
    # Git repo validation
    # ------------------------------------------------------------------

    def test_command_fails_on_non_repo(self) -> None:
        repo = self.git_root / "not_a_repo"
        repo.mkdir()
        with self.assertRaises(ToolError):
            self.runner.execute("status", [], str(repo), 30)

    # ------------------------------------------------------------------
    # Allowed args acceptance
    # ------------------------------------------------------------------

    def test_allowed_args_are_accepted(self) -> None:
        repo = self.git_root / "myrepo"
        _setup_repo_with_commit(repo)
        result = self.runner.execute(
            "log",
            ["--oneline", "--graph", "--max-count=5", "--no-merges"],
            str(repo), 30,
        )
        self.assertEqual(result["exit_code"], 0, result["stderr"])

    # ------------------------------------------------------------------
    # Output handling
    # ------------------------------------------------------------------

    def test_truncation_keeps_tail(self) -> None:
        """git 的错误信息在末尾，截断必须保留尾部。"""
        runner = GitRunner(
            git_binary=self.runner._git,
            git_root=self.git_root,
            author_name="Test",
            author_email="test@test.local",
            max_output_bytes=100,
        )
        text = runner._truncate(b"HEAD" + b"x" * 5000 + b"TAIL")
        self.assertIn("HEAD", text)
        self.assertIn("TAIL", text)
        self.assertIn("已截断", text)

    def test_output_redacts_url_credentials(self) -> None:
        redacted = GitRunner._redact(
            "fatal: https://oauth2:secret-token@github.com/x/y.git not found",
            ["secret-token"],
        )
        self.assertNotIn("secret-token", redacted)

    # ------------------------------------------------------------------
    # Result format structure
    # ------------------------------------------------------------------

    def test_result_format(self) -> None:
        repo = self.git_root / "myrepo"
        result = self.runner.execute("init", [], str(repo), 30)
        self.assertIsInstance(result, dict)
        for key in ("command", "repo", "exit_code", "duration_seconds",
                    "stdout", "stderr", "success"):
            self.assertIn(key, result)


# ---------------------------------------------------------------------------
# Write/read classification
# ---------------------------------------------------------------------------

class GitWriteClassificationTests(unittest.TestCase):
    def test_read_only_commands(self) -> None:
        for command in ("status", "log", "diff", "show", "grep"):
            self.assertFalse(is_write_operation(command, []))

    def test_write_commands(self) -> None:
        for command in ("init", "clone", "add", "commit", "push", "pull",
                        "checkout", "remote", "fetch"):
            self.assertTrue(is_write_operation(command, []))

    def test_branch_listing_is_read_only(self) -> None:
        self.assertFalse(is_write_operation("branch", ["--list"]))
        self.assertFalse(is_write_operation("branch", ["--show-current"]))

    def test_branch_mutation_requires_approval(self) -> None:
        self.assertTrue(is_write_operation("branch", ["-D", "feature"]))
        self.assertTrue(is_write_operation("branch", ["--delete", "feature"]))
        self.assertTrue(is_write_operation("branch", ["new-feature"]))

    def test_fetch_only_commands_skip_approval(self) -> None:
        """clone/pull/fetch 只写沙箱、不对外发布，免人工确认。"""
        for command in ("clone", "pull", "fetch"):
            self.assertTrue(is_write_operation(command, []))
            self.assertFalse(requires_manual_approval(command, []))

    def test_publishing_commands_still_require_approval(self) -> None:
        for command in ("init", "add", "commit", "push", "checkout", "remote"):
            self.assertTrue(requires_manual_approval(command, []))
        self.assertTrue(requires_manual_approval("branch", ["-D", "feature"]))
        self.assertFalse(requires_manual_approval("branch", ["--list"]))
        self.assertFalse(requires_manual_approval("status", []))


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------

class GitCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "git_credentials.json"
        patcher = patch(
            "src.core.config.git_credentials.GIT_CREDENTIALS_FILE", self.store
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_roundtrip_and_listing(self) -> None:
        from src.core.config import git_credentials

        git_credentials.save_token("tenant-a", "GitHub.com", "token-a")
        self.assertEqual(git_credentials.load_token("tenant-a", "github.com"), "token-a")
        self.assertEqual(git_credentials.list_hosts("tenant-a"), ["github.com"])

    def test_tenant_isolation(self) -> None:
        from src.core.config import git_credentials

        git_credentials.save_token("tenant-a", "github.com", "token-a")
        self.assertEqual(git_credentials.load_token("tenant-b", "github.com"), "")
        self.assertEqual(git_credentials.list_hosts("tenant-b"), [])

    def test_invalid_host_rejected(self) -> None:
        from src.core.config import git_credentials
        from src.core.integrations.keychain import KeychainError

        for host in ("https://github.com", "github.com/foo", "user@github.com", ""):
            with self.assertRaises(KeychainError):
                git_credentials.save_token("tenant-a", host, "token")

    def test_empty_token_deletes(self) -> None:
        from src.core.config import git_credentials

        git_credentials.save_token("tenant-a", "github.com", "token-a")
        git_credentials.save_token("tenant-a", "github.com", "")
        self.assertEqual(git_credentials.load_token("tenant-a", "github.com"), "")


# ---------------------------------------------------------------------------
# Tool definition & approval tests
# ---------------------------------------------------------------------------

class GitToolDefinitionTests(unittest.TestCase):
    def test_git_in_tool_definitions(self) -> None:
        self.assertIn("git", TOOL_DEFINITIONS)

    def test_git_in_approval_tools(self) -> None:
        self.assertIn("git", APPROVAL_TOOLS)

    def test_git_definition_has_required_params(self) -> None:
        definition = TOOL_DEFINITIONS["git"]
        params = definition["parameters"]
        properties = params["properties"]
        self.assertIn("command", properties)
        self.assertIn("repo_path", properties)
        self.assertEqual(params["required"], ["command", "repo_path"])

    def test_git_command_enum_matches_config_whitelist(self) -> None:
        from src.core.config.loader import KNOWN_GIT_COMMANDS

        enum = TOOL_DEFINITIONS["git"]["parameters"]["properties"]["command"]["enum"]
        self.assertEqual(sorted(enum), sorted(KNOWN_GIT_COMMANDS))


# ---------------------------------------------------------------------------
# git_root 解析：仓库必须落在租户文件库内
# ---------------------------------------------------------------------------

class GitRootResolutionTests(unittest.TestCase):
    """$TENANT_WORKSPACE 必须解析到租户目录，且未绑定租户时 fail closed。"""

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("系统未安装 git")
        from dataclasses import replace as dataclass_replace

        from src.core.config.loader import load_project_config
        from src.core.storage.tenants import TenantRegistry
        from src.core.tooling import ToolRuntime

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_root = Path(self.temp.name) / "data"
        self.registry = TenantRegistry(self.data_root)
        self.tenant = self.registry.resolve("ilink", "wxid_demo")

        source_config = Path(__file__).resolve().parents[1] / "config"
        original = load_project_config(source_config).tools
        config = dataclass_replace(
            original,
            default_working_directory=str(self.data_root),
            allowed_roots=[str(self.data_root)],
            git_root="$TENANT_WORKSPACE/git_repos",
        )
        self.runtime = ToolRuntime(
            config,
            "Asia/Shanghai",
            tenant_registry=self.registry,
        )

    def test_default_config_is_tenant_scoped(self) -> None:
        """缺少 git_root 配置时也必须落在租户工作区，而非跨租户共享目录。"""
        from src.core.config.loader import ToolConfig

        self.assertEqual(
            ToolConfig.__dataclass_fields__["git_root"].default,
            "$TENANT_WORKSPACE/git_repos",
        )

    def test_repo_lands_in_tenant_file_library(self) -> None:
        self.runtime.bind_tenant(self.tenant)
        result = self.runtime.execute("git", {"command": "init", "repo_path": "demo"})
        self.assertTrue(result.ok, result.error)
        # 展示给用户的是文件库内路径，物理落点在租户目录下。
        self.assertEqual(result.data["repo"], "workspace/git_repos/demo")
        expected = (
            self.registry.tenant_root(self.tenant.tenant_id)
            / "workspace" / "git_repos" / "demo"
        )
        self.assertTrue((expected / ".git").is_dir())

    def test_unbound_tenant_fails_closed(self) -> None:
        """未绑定租户时必须报错，不能悄悄写到跨租户目录。"""
        result = self.runtime.execute("git", {"command": "init", "repo_path": "demo"})
        self.assertFalse(result.ok)
        self.assertFalse((self.data_root / "git_repos").exists())


if __name__ == "__main__":
    unittest.main()
