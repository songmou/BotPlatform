"""Validated, approved command execution inside a constrained macOS sandbox."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List

from src.core.config.loader import ToolConfig
from .models import ToolError


@dataclass(frozen=True)
class PreparedCommand:
    profile: str
    executable: str
    arguments: List[str]
    cwd: Path
    timeout_seconds: int

    @property
    def argv(self) -> List[str]:
        return [self.executable, *self.arguments]

    def preview(self) -> str:
        rendered = " ".join(_display_argument(item) for item in self.argv)
        return "命令：{}\n工作目录：{}\n超时：{} 秒".format(
            rendered, self.cwd, self.timeout_seconds
        )


def _display_argument(value: str) -> str:
    if value and all(character.isalnum() or character in "-._/:=@+" for character in value):
        return value
    return json.dumps(value, ensure_ascii=False)


class CommandRunner:
    def __init__(
        self,
        config: ToolConfig,
        resolve_path: Callable[..., Path],
        sandbox_executable: str = "/usr/bin/sandbox-exec",
        sandbox_available: Any = None,
    ) -> None:
        self.config = config
        self.resolve_path = resolve_path
        self.sandbox_executable = sandbox_executable
        self._sandbox_available = sandbox_available

    @property
    def available(self) -> bool:
        if self._sandbox_available is not None:
            return bool(self._sandbox_available)
        if not Path(self.sandbox_executable).is_file() or not os.access(
            self.sandbox_executable, os.X_OK
        ):
            self._sandbox_available = False
            return False
        try:
            probe = subprocess.run(
                [
                    self.sandbox_executable,
                    "-p",
                    "(version 1)\n(allow default)",
                    "/usr/bin/true",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
            self._sandbox_available = probe.returncode == 0
        except (OSError, subprocess.SubprocessError):
            self._sandbox_available = False
        return bool(self._sandbox_available)

    def prepare(self, arguments: Dict[str, Any]) -> PreparedCommand:
        if not self.available:
            raise ToolError("macOS 命令沙箱不可用，run_command 已禁用")
        profile = arguments.get("profile")
        if not isinstance(profile, str) or profile not in self.config.enabled_command_profiles:
            raise ToolError("命令档案未启用或不存在：{}".format(profile))
        raw_args = arguments.get("args", [])
        if not isinstance(raw_args, list) or any(not isinstance(item, str) for item in raw_args):
            raise ToolError("args 必须是字符串数组")
        if len(raw_args) > 50 or any(len(item) > 4096 for item in raw_args):
            raise ToolError("命令参数过多或单个参数过长")
        raw_cwd = arguments.get("cwd", self.config.default_working_directory)
        if not isinstance(raw_cwd, str):
            raise ToolError("cwd 必须是字符串")
        cwd = self.resolve_path(raw_cwd, must_exist=True)
        if not cwd.is_dir():
            raise ToolError("命令工作目录不是文件夹：{}".format(cwd))
        timeout = arguments.get(
            "timeout_seconds", self.config.default_command_timeout_seconds
        )
        if not isinstance(timeout, int) or isinstance(timeout, bool):
            raise ToolError("timeout_seconds 必须是整数")
        if timeout < 1 or timeout > self.config.max_command_timeout_seconds:
            raise ToolError(
                "timeout_seconds 必须在 1 到 {} 之间".format(
                    self.config.max_command_timeout_seconds
                )
            )

        executable, validated_args = self._prepare_profile(profile, raw_args, cwd)
        return PreparedCommand(profile, executable, validated_args, cwd, timeout)

    def _which(self, name: str) -> str:
        executable = shutil.which(name)
        if not executable:
            raise ToolError("系统中未找到命令：{}".format(name))
        return str(Path(executable).resolve())

    def _script_path(self, raw: str, cwd: Path, executable: bool = False) -> Path:
        path = self.resolve_path(raw, base=cwd, must_exist=True)
        if not path.is_file():
            raise ToolError("脚本不是普通文件：{}".format(path))
        if executable and not os.access(str(path), os.X_OK):
            raise ToolError("脚本没有执行权限：{}".format(path))
        return path

    def _prepare_profile(
        self, profile: str, args: List[str], cwd: Path
    ) -> tuple[str, List[str]]:
        if profile == "python":
            if not args:
                raise ToolError("python 档案需要脚本路径或 -m 模块")
            local_python = cwd / ".venv" / "bin" / "python"
            executable = (
                str(local_python.resolve())
                if local_python.is_file() and os.access(str(local_python), os.X_OK)
                else self._which("python3")
            )
            if args[0] == "-m":
                if len(args) < 2 or args[1] not in {"unittest", "pytest", "compileall"}:
                    raise ToolError("python -m 仅允许 unittest、pytest 或 compileall")
                return executable, list(args)
            if args[0] == "--version" and len(args) == 1:
                return executable, list(args)
            if args[0].startswith("-"):
                raise ToolError("python 档案禁止 -c、标准输入代码和其他解释器开关")
            script = self._script_path(args[0], cwd)
            return executable, [str(script), *args[1:]]

        if profile == "git_readonly":
            if not args or args[0] not in {
                "status", "diff", "log", "show", "branch", "grep", "ls-files"
            }:
                raise ToolError("git_readonly 不允许该子命令")
            if args[0] == "branch":
                branch_args = args[1:]
                if branch_args == ["--show-current"] or not branch_args:
                    pass
                elif branch_args[0] == "--list" and not any(
                    item.startswith("-") for item in branch_args[1:]
                ):
                    pass
                else:
                    raise ToolError(
                        "git_readonly 的 branch 仅允许无参数、--show-current 或 --list [模式]"
                    )
            forbidden_prefixes = (
                "--output=", "--ext-diff", "--textconv", "--open-files-in-pager"
            )
            if any(item.startswith(forbidden_prefixes) for item in args[1:]):
                raise ToolError("git_readonly 禁止输出重定向或外部差异程序")
            return self._which("git"), list(args)

        if profile == "node":
            if not args or args[0].startswith("-"):
                raise ToolError("node 档案只允许运行开放目录中的脚本")
            script = self._script_path(args[0], cwd)
            return self._which("node"), [str(script), *args[1:]]

        if profile == "npm_script":
            if not args or args[0] not in {"test", "run"}:
                raise ToolError("npm_script 仅允许 test 或 run")
            package_path = self.resolve_path("package.json", base=cwd, must_exist=True)
            try:
                package = json.loads(package_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ToolError("无法读取 package.json：{}".format(exc)) from exc
            scripts = package.get("scripts") if isinstance(package, dict) else None
            if not isinstance(scripts, dict):
                scripts = {}
            script_name = "test" if args[0] == "test" else (args[1] if len(args) > 1 else "")
            if not script_name or script_name not in scripts:
                raise ToolError("package.json 中不存在脚本：{}".format(script_name or "<空>"))
            return self._which("npm"), list(args)

        if profile == "ollama_readonly":
            if not args or args[0] not in {"list", "ps", "show"}:
                raise ToolError("ollama_readonly 仅允许 list、ps 或 show")
            if args[0] == "show" and len(args) != 2:
                raise ToolError("ollama show 必须且只能指定一个模型")
            return self._which("ollama"), list(args)

        if profile == "workspace_script":
            if not args:
                raise ToolError("workspace_script 需要脚本路径")
            script = self._script_path(args[0], cwd, executable=True)
            return str(script), list(args[1:])

        raise ToolError("未知命令档案：{}".format(profile))

    def _sandbox_policy(self, temp_directory: Path, profile: str) -> str:
        lines = [
            "(version 1)",
            "(allow default)",
            "(deny network*)",
        ]
        restricted_roots = {
            Path("/Users"),
            Path("/Volumes"),
            Path("/Network"),
            Path("/private/tmp"),
            Path(tempfile.gettempdir()).resolve(),
        }
        for root in sorted(restricted_roots, key=lambda item: str(item)):
            quoted = _sandbox_quote(str(root))
            lines.append('(deny file-read* (subpath "{}"))'.format(quoted))
            lines.append('(deny file-write* (subpath "{}"))'.format(quoted))
        for allowed_root in [*self.config.allowed_roots, str(temp_directory)]:
            quoted = _sandbox_quote(str(Path(allowed_root).resolve()))
            lines.append('(allow file-read* (subpath "{}"))'.format(quoted))
            lines.append('(allow file-write* (subpath "{}"))'.format(quoted))
        if profile == "ollama_readonly":
            lines.append(
                '(allow network-outbound (remote ip "localhost:11434"))'
            )
        return "\n".join(lines)

    def execute(self, prepared: PreparedCommand) -> Dict[str, Any]:
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="ilinkbot-tool-") as temp_name:
            temp_directory = Path(temp_name).resolve()
            policy = self._sandbox_policy(temp_directory, prepared.profile)
            command = [
                self.sandbox_executable,
                "-p",
                policy,
                *prepared.argv,
            ]
            environment = {
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
                "HOME": str(temp_directory),
                "TMPDIR": str(temp_directory),
                "LANG": "en_US.UTF-8",
                "LC_ALL": "en_US.UTF-8",
            }
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(prepared.cwd),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                raise ToolError("启动命令失败：{}".format(exc)) from exc
            timed_out = False
            try:
                stdout, stderr = process.communicate(timeout=prepared.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate()

        output_limit = self.config.max_command_output_bytes
        stdout, stdout_truncated = _truncate(stdout, output_limit // 2)
        stderr, stderr_truncated = _truncate(stderr, output_limit - len(stdout))
        return {
            "profile": prepared.profile,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "output_truncated": stdout_truncated or stderr_truncated,
        }


def _sandbox_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _truncate(value: bytes, limit: int) -> tuple[bytes, bool]:
    if len(value) <= max(0, limit):
        return value, False
    return value[: max(0, limit)], True
