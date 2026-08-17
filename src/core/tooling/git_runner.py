"""Safe git subprocess execution with parameter whitelist and path sandbox."""

from __future__ import annotations

import base64
import os
import re
import signal
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.core.config.git_credentials import load_token
from .models import ToolError

#: Per-command whitelist of allowed flag / option prefixes.
#: Flags not in this list (but starting with ``--`` or ``-``) are rejected.
#: Positional arguments (no leading dash) are always allowed.
_ALLOWED_ARG_PREFIXES: Dict[str, List[str]] = {
    "init": [
        "--initial-branch=", "--bare", "--quiet",
    ],
    "clone": [
        "--depth=", "--branch=", "--single-branch", "--no-single-branch",
        "--no-tags", "--shallow-submodules", "--quiet", "--progress",
    ],
    "status": [
        "--short", "--branch", "--porcelain=", "--ignored",
    ],
    "log": [
        "--oneline", "--graph", "--decorate", "--max-count=",
        "--skip=", "--since=", "--until=", "--author=", "--grep=",
        "--all", "--branches", "--no-merges", "--format=",
        "--diff-filter=", "--name-only", "--name-status", "-p",
        "--pretty=",
    ],
    "diff": [
        "--cached", "--staged", "--stat", "--name-only",
        "--name-status", "--no-color", "--color=never",
        "--ignore-space-change", "--ignore-all-space",
        "--ignore-blank-lines", "-U", "--diff-filter=",
    ],
    "show": [
        "--no-color", "--color=never", "--stat", "--name-only",
        "--format=", "--pretty=",
    ],
    "add": [
        "--all", "-A", "--update", "--intent-to-add",
        "--no-ignore-removal", "--verbose", "--dry-run",
        "--no-all", "--ignore-errors",
    ],
    "commit": [
        "-m", "--message=", "--allow-empty", "--no-verify",
        "--amend", "--no-edit", "--author=", "--date=",
        "--quiet", "--allow-empty-message",
    ],
    "push": [
        "--force", "--force-with-lease", "--set-upstream", "-u",
        "--no-verify", "--quiet", "--progress", "--atomic",
    ],
    "pull": [
        "--rebase", "--no-rebase", "--ff-only", "--no-ff",
        "--squash", "--quiet", "--progress", "--autostash",
    ],
    "branch": [
        "--list", "--show-current", "--delete", "-d", "-D",
        "--move", "-m", "--copy", "-c", "--set-upstream-to=",
        "--unset-upstream", "--no-color", "--all", "-a",
        "--remotes", "-r", "--merged", "--no-merged",
    ],
    "grep": [
        "--line-number", "-n", "--count", "-c", "--files-with-matches", "-l",
        "--ignore-case", "-i", "--word-regexp", "-w", "--fixed-strings", "-F",
        "--max-count=", "-m", "--name-only", "--full-name", "--color=never",
        "--cached", "--untracked", "--exclude-standard", "-e", "-v",
        "--all-match", "--heading", "--break", "--null", "--column",
    ],
    "checkout": [
        "-b", "-B", "--orphan=", "--track", "--no-track",
        "--detach", "--quiet", "--force", "--merge",
        "--conflict=", "--ours", "--theirs",
    ],
    "remote": [
        "--verbose", "-v",
    ],
    "fetch": [
        "--all", "--prune", "--depth=", "--quiet", "--progress",
        "--tags", "--force", "--no-tags",
    ],
}

#: Short flags that consume the following argument as their value.
_SHORT_FLAGS_WITH_VALUE: frozenset[str] = frozenset({
    "-m", "-b", "-B", "-d", "-D", "-e", "-U",
})

#: Flags whose value is free-form text (commit messages, patterns, formats).
#: Those values are only checked for control characters, never for punctuation:
#: execution uses ``shell=False`` so quotes and ``$`` carry no injection risk,
#: and rejecting them would break ordinary Chinese commit messages.
_FREEFORM_VALUE_PREFIXES: Tuple[str, ...] = (
    "--message=", "--grep=", "--author=", "--format=", "--pretty=",
    "--date=", "--since=", "--until=",
)

#: Characters that must never appear in any argument: NUL and newlines would
#: let an argument masquerade as several config lines / refs.
_CONTROL_CHARS = re.compile(r"[\x00\r\n]")

#: Stricter set for structural arguments (paths, refs, remote names, URLs).
_UNSAFE_STRUCTURAL_CHARS = re.compile(r"[;&|$`'\"()<>\x00\r\n]")

#: Commands that never modify the repository (see :func:`is_write_operation`).
READ_ONLY_COMMANDS: frozenset[str] = frozenset({
    "status", "log", "diff", "show", "grep", "branch",
})

#: ``git branch`` flags that mutate refs rather than list them.
_BRANCH_WRITE_FLAGS: Tuple[str, ...] = (
    "--delete", "-d", "-D", "--move", "-m", "--copy", "-c",
    "--set-upstream-to=", "--unset-upstream",
)

#: Network commands that only write inside the sandbox and publish nothing
#: outward, so they are exempt from human approval.  ``push``/``commit``/
#: ``reset`` and friends stay gated because they rewrite history or expose
#: code to a remote.
FETCH_ONLY_COMMANDS: frozenset[str] = frozenset({"clone", "pull", "fetch"})

#: Transports git is allowed to use. ``ext``/``ssh``/``git`` are excluded:
#: ``ext::`` executes arbitrary commands and the others cannot be
#: authenticated from the sandbox anyway.
_ALLOWED_PROTOCOLS = "https:http:file"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_SCP_LIKE_RE = re.compile(r"^[^/\\]+@[^/\\:]+:")

#: Credentials embedded in a URL, e.g. ``https://user:token@host/repo``.
_URL_CREDENTIAL_RE = re.compile(r"(?<=://)[^/@\s]+(?=@)")

#: Per-repository locks: concurrent git commands on one repo collide on
#: ``.git/index.lock``, so serialize them within the process.
_REPO_LOCKS: Dict[str, threading.Lock] = {}
_REPO_LOCKS_GUARD = threading.Lock()


def is_write_operation(command: str, args: Optional[List[str]] = None) -> bool:
    """Return True when ``command`` may modify the repository."""
    if command not in READ_ONLY_COMMANDS:
        return True
    if command != "branch":
        return False
    for arg in args or []:
        if arg.startswith("-"):
            if any(arg == flag or arg.startswith(flag) for flag in _BRANCH_WRITE_FLAGS):
                return True
        else:
            # A positional argument to ``git branch`` creates a branch.
            return True
    return False


def requires_manual_approval(
    command: str, args: Optional[List[str]] = None
) -> bool:
    """Return True when the operation needs explicit human confirmation."""
    if command in FETCH_ONLY_COMMANDS:
        return False
    return is_write_operation(command, args)


def _repo_lock(repo: Path) -> threading.Lock:
    key = str(repo).casefold()
    with _REPO_LOCKS_GUARD:
        lock = _REPO_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _REPO_LOCKS[key] = lock
        return lock


class GitRunner:
    """Execute git commands via subprocess with strict security controls.

    Security layers:
    1. Command name whitelist (must be a known git subcommand).
    2. Per-command flag/option prefix whitelist.
    3. Control-character rejection everywhere, punctuation rejection in
       structural arguments (paths, refs, remotes, URLs).
    4. Path traversal prevention (all repos must live under ``git_root``).
    5. Transport whitelist; local sources must stay inside ``git_root``.
    6. ``shell=False`` — list form only.
    7. Timeout enforcement with process-tree termination.
    8. Output size truncation and credential redaction.
    9. Environment isolation (no user/system gitconfig, no prompts).
    """

    def __init__(
        self,
        git_binary: str,
        git_root: Path,
        author_name: str,
        author_email: str,
        max_output_bytes: int = 65536,
        tenant_id: str | None = None,
        display_root: Path | None = None,
    ) -> None:
        self._git = git_binary
        self._git_root = git_root
        self._author_name = author_name
        self._author_email = author_email
        self._max_output_bytes = max_output_bytes
        self._tenant_id = tenant_id
        # Paths shown to the caller are relative to this root (the tenant's
        # file-library root in web usage), never absolute server paths.
        self._display_root = display_root or git_root

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        command: str,
        args: List[str],
        repo_path: str,
        timeout_seconds: int,
        progress_callback=None,
    ) -> Dict[str, Any]:
        """Run a git command and return a structured result dict.

        Args:
            progress_callback: Optional ``callable(percent: int, detail: str)``
                invoked with progress lines for clone/pull/fetch.

        Returns:
            ``{"command", "repo", "exit_code", "duration_seconds",
              "stdout", "stderr", "success"}``
        """
        if timeout_seconds < 1:
            raise ToolError("timeout_seconds 必须大于 0")

        self._validate_args(command, args)
        resolved_repo = self._resolve_repo_path(repo_path)
        effective_args = list(args)

        if command == "clone":
            self._prepare_clone(resolved_repo, effective_args)
            cwd = self._git_root
        elif command == "init":
            resolved_repo.mkdir(parents=True, exist_ok=True)
            cwd = resolved_repo
        else:
            self._assert_git_repo(resolved_repo)
            cwd = resolved_repo

        streaming = progress_callback is not None and command in ("clone", "pull", "fetch")
        if streaming and not any(a == "--progress" for a in effective_args):
            effective_args.append("--progress")

        argv = [self._git, "-C", str(cwd), command, *effective_args]
        env = self._build_env()
        secrets = self._apply_credentials(command, effective_args, resolved_repo, env)

        started = time.monotonic()
        with _repo_lock(resolved_repo):
            if streaming:
                exit_code, raw_out, raw_err = self._run_streamed(
                    argv, env, str(cwd), timeout_seconds, command, progress_callback
                )
            else:
                exit_code, raw_out, raw_err = self._run_blocking(
                    argv, env, str(cwd), timeout_seconds, command
                )
            if exit_code == 0 and command in ("clone", "pull", "fetch"):
                self._clear_readonly(resolved_repo)

        duration = time.monotonic() - started
        stdout = self._redact(self._truncate(raw_out), secrets)
        stderr = self._redact(self._truncate(raw_err), secrets)
        return {
            "command": command,
            "repo": self._display_path(resolved_repo),
            "exit_code": exit_code,
            "duration_seconds": round(duration, 3),
            "stdout": stdout,
            "stderr": stderr,
            "success": exit_code == 0,
        }

    # ------------------------------------------------------------------
    # Subprocess execution
    # ------------------------------------------------------------------

    def _popen(self, argv: List[str], env: Dict[str, str], cwd: str) -> subprocess.Popen:
        """Start git in its own process group so timeouts kill helpers too."""
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
                **kwargs,
            )
        except OSError as exc:
            raise ToolError("启动 git 失败：{}".format(exc)) from exc

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """Terminate git and any transport helper it spawned."""
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _run_blocking(
        self,
        argv: List[str],
        env: Dict[str, str],
        cwd: str,
        timeout_seconds: int,
        command: str,
    ) -> Tuple[int, bytes, bytes]:
        proc = self._popen(argv, env, cwd)
        try:
            out, err = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            raise ToolError("git {} 执行超时（{} 秒）".format(command, timeout_seconds))
        return proc.returncode, out or b"", err or b""

    def _run_streamed(
        self,
        argv: List[str],
        env: Dict[str, str],
        cwd: str,
        timeout_seconds: int,
        command: str,
        progress_callback,
    ) -> Tuple[int, bytes, bytes]:
        """Run git with --progress, streaming stderr lines to the callback."""
        proc = self._popen(argv, env, cwd)
        stdout_parts: List[bytes] = []
        stderr_parts: List[bytes] = []

        def _drain(stream, parts: List[bytes], report: bool) -> None:
            try:
                for raw in iter(stream.readline, b""):
                    parts.append(raw)
                    if not report:
                        continue
                    match = re.search(rb"(\d+)%", raw)
                    if match:
                        try:
                            progress_callback(
                                int(match.group(1)),
                                raw.decode("utf-8", errors="replace").strip(),
                            )
                        except Exception:
                            pass
            finally:
                stream.close()

        threads = [
            threading.Thread(target=_drain, args=(proc.stdout, stdout_parts, False), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, stderr_parts, True), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            exit_code = proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            for thread in threads:
                thread.join(timeout=2)
            raise ToolError("git {} 执行超时（{} 秒）".format(command, timeout_seconds))
        for thread in threads:
            thread.join(timeout=5)
        return exit_code, b"".join(stdout_parts), b"".join(stderr_parts)

    # ------------------------------------------------------------------
    # Environment
    # ------------------------------------------------------------------

    def _build_env(self) -> Dict[str, str]:
        """Return an environment isolated from user/system git configuration."""
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = self._author_name
        env["GIT_AUTHOR_EMAIL"] = self._author_email
        env["GIT_COMMITTER_NAME"] = self._author_name
        env["GIT_COMMITTER_EMAIL"] = self._author_email
        # HOME alone is not enough on Windows, where git reads USERPROFILE.
        env["HOME"] = str(self._git_root)
        env["USERPROFILE"] = str(self._git_root)
        env.pop("XDG_CONFIG_HOME", None)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = ""
        env["SSH_ASKPASS"] = ""
        env["GIT_ALLOW_PROTOCOL"] = _ALLOWED_PROTOCOLS
        return env

    # ------------------------------------------------------------------
    # Argument validation
    # ------------------------------------------------------------------

    def _validate_args(self, command: str, args: List[str]) -> None:
        """Validate every argument against the security policy."""
        allowed = _ALLOWED_ARG_PREFIXES.get(command, [])
        i = 0
        while i < len(args):
            arg = args[i]
            freeform = arg.startswith(_FREEFORM_VALUE_PREFIXES)
            self._check_chars(command, i, arg, freeform)

            if arg.startswith("-") and len(arg) > 1:
                # log/diff/show accept numeric shorthands such as -1 / -5.
                if command in ("log", "diff", "show") and re.fullmatch(r"-\d+", arg):
                    i += 1
                    continue
                if not any(arg.startswith(p) or arg == p for p in allowed):
                    raise ToolError(
                        "git {} 不允许参数（索引 {}）：{}".format(command, i, arg)
                    )
                if arg in _SHORT_FLAGS_WITH_VALUE:
                    # The value belongs to the flag: free-form text for -m/-e,
                    # structural (refs, paths) otherwise.
                    if i + 1 >= len(args):
                        raise ToolError("git {} 参数 {} 缺少取值".format(command, arg))
                    i += 1
                    self._check_chars(command, i, args[i], arg in ("-m", "-e"))
            i += 1

    def _check_chars(self, command: str, index: int, arg: str, freeform: bool) -> None:
        pattern = _CONTROL_CHARS if freeform else _UNSAFE_STRUCTURAL_CHARS
        if pattern.search(arg):
            raise ToolError(
                "git {} 参数包含危险字符（索引 {}）：{}".format(command, index, arg)
            )

    # ------------------------------------------------------------------
    # Clone source handling
    # ------------------------------------------------------------------

    def _prepare_clone(self, target: Path, args: List[str]) -> None:
        """Validate the clone source and append the sandboxed target dir.

        ``git clone`` derives the target directory from the URL unless one is
        passed explicitly, which would ignore ``repo_path`` and escape the
        caller's intent — so the resolved target is always appended here.
        """
        positional = [a for a in args if not a.startswith("-")]
        if len(positional) != 1:
            raise ToolError(
                "git clone 的 args 必须且只能包含一个仓库地址；"
                "目标目录由 repo_path 指定"
            )
        self._validate_clone_source(positional[0])

        if target == self._git_root:
            raise ToolError("git clone 的 repo_path 必须是 git 根目录下的子目录")
        if target.exists() and any(target.iterdir()):
            raise ToolError("目标目录已存在且非空：{}".format(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        # 大仓库的完整历史常常拖到超时，默认浅克隆；调用方可用
        # --depth= 自定深度，或用 --no-single-branch 要求完整历史。
        if not any(
            arg.startswith("--depth") or arg == "--no-single-branch" for arg in args
        ):
            args.append("--depth=1")
        args.append(str(target))

    def _validate_clone_source(self, source: str) -> None:
        """Allow HTTPS remotes, loopback HTTP and local paths inside git_root."""
        if _SCHEME_RE.match(source):
            scheme = source.split("://", 1)[0].lower()
            if scheme == "https":
                return
            if scheme == "http":
                host = (urllib.parse.urlparse(source).hostname or "").lower()
                if host in _LOOPBACK_HOSTS:
                    return
                raise ToolError("远程仓库地址必须使用 HTTPS（仅本机回环允许 HTTP）")
            if scheme == "file":
                local = urllib.parse.urlparse(source).path
                if os.name == "nt":
                    local = local.lstrip("/")
                self._assert_local_source(local)
                return
            raise ToolError("不支持的仓库地址协议：{}".format(scheme))

        if "::" in source:
            raise ToolError("不支持的仓库地址：{}".format(source))
        windows_drive = len(source) > 1 and source[1] == ":"
        if _SCP_LIKE_RE.match(source) and not windows_drive:
            raise ToolError("不支持 SSH 仓库地址，请使用 HTTPS：{}".format(source))
        self._assert_local_source(source)

    def _assert_local_source(self, raw_path: str) -> None:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self._git_root / candidate
        resolved = candidate.resolve(strict=False)
        if self._git_root not in resolved.parents and resolved != self._git_root:
            raise ToolError(
                "本地仓库地址必须在 git 根目录内：{}（git_root: {}）".format(
                    resolved, self._git_root
                )
            )

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def _display_path(self, path: Path) -> str:
        """Present ``path`` relative to the display root (file-library path)."""
        try:
            return path.resolve().relative_to(self._display_root).as_posix()
        except (ValueError, OSError):
            return str(path)

    def _resolve_repo_path(self, raw_path: str) -> Path:
        """Resolve ``raw_path`` relative to ``git_root`` and validate it."""
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            # repo_path 已相对 git_root；重复带上根目录名会解析出
            # <git_root>/<git_root 名>/... 这种目录，后续报错难以理解。
            first = candidate.parts[0] if candidate.parts else ""
            if first and first.casefold() == self._git_root.name.casefold():
                raise ToolError(
                    "repo_path 只需写仓库目录名（如 code-reviewer），"
                    "不要重复带上 git 根目录名 {}".format(self._git_root.name)
                )
            candidate = self._git_root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            resolved = candidate
        if self._git_root not in resolved.parents and resolved != self._git_root:
            raise ToolError(
                "仓库路径必须在 git 根目录内：{}".format(
                    self._display_path(resolved)
                )
            )
        return resolved

    def _assert_git_repo(self, repo_path: Path) -> None:
        """Check that ``repo_path`` contains a valid git repository."""
        git_dir = repo_path / ".git"
        if not git_dir.is_dir() and not git_dir.is_file():
            raise ToolError(
                "该位置还没有 git 仓库：{}；首次获取远程代码请用 clone"
                "（远程地址写在 args 里，目标目录用 repo_path），"
                "pull/fetch 只能用于已克隆的仓库".format(
                    self._display_path(repo_path)
                )
            )

    # ------------------------------------------------------------------
    # Output handling
    # ------------------------------------------------------------------

    def _truncate(self, data: bytes) -> str:
        """Keep the head and tail of ``data``; git reports errors at the end."""
        limit = max(self._max_output_bytes // 2, 1)
        if len(data) <= limit:
            return data.decode("utf-8", errors="replace")
        head = data[: limit * 2 // 3]
        tail = data[len(data) - (limit - len(head)):]
        return "{}\n...[已截断 {} 字节]...\n{}".format(
            head.decode("utf-8", errors="replace"),
            len(data) - limit,
            tail.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _redact(text: str, secrets: List[str]) -> str:
        """Strip credentials that git may echo back in URLs."""
        if not text:
            return text
        for secret in secrets:
            if secret:
                text = text.replace(secret, "***")
        return _URL_CREDENTIAL_RE.sub("***", text)

    @staticmethod
    def _clear_readonly(repo_root: Path) -> None:
        """Clear read-only attributes inside ``repo_root/.git``.

        git marks pack/idx objects read-only, which blocks deletion of the
        downloaded repository on Windows. Only ``.git`` is walked: the working
        tree is written by git without the read-only bit.
        """
        if os.name != "nt":
            return
        import stat

        git_dir = repo_root / ".git"
        if not git_dir.is_dir():
            return
        for dirpath, _dirnames, filenames in os.walk(str(git_dir)):
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    mode = os.stat(path).st_mode
                    if not mode & stat.S_IWRITE:
                        os.chmod(path, mode | stat.S_IWRITE)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Credential injection
    # ------------------------------------------------------------------

    def _apply_credentials(
        self,
        command: str,
        args: List[str],
        repo: Path,
        env: Dict[str, str],
    ) -> List[str]:
        """Attach stored HTTPS tokens as per-host ``http.extraHeader`` config.

        Tokens are passed through ``GIT_CONFIG_*`` environment variables rather
        than the remote URL: a URL-embedded token would be persisted into
        ``.git/config`` and visible in the process command line.  Returns the
        token values so they can be redacted from the output.
        """
        if command not in ("clone", "fetch", "pull", "push"):
            return []
        if not self._tenant_id:
            return []

        hosts = (
            self._clone_hosts(args)
            if command == "clone"
            else self._remote_hosts(repo, env)
        )
        secrets: List[str] = []
        count = 0
        for host in sorted(hosts):
            token = load_token(self._tenant_id, host)
            if not token:
                continue
            header = base64.b64encode(
                "oauth2:{}".format(token).encode("utf-8")
            ).decode("ascii")
            env["GIT_CONFIG_KEY_{}".format(count)] = (
                "http.https://{}/.extraHeader".format(host)
            )
            env["GIT_CONFIG_VALUE_{}".format(count)] = (
                "Authorization: Basic {}".format(header)
            )
            secrets.append(token)
            count += 1
        if count:
            env["GIT_CONFIG_COUNT"] = str(count)
        return secrets

    @staticmethod
    def _clone_hosts(args: List[str]) -> List[str]:
        for arg in args:
            if arg.startswith("https://"):
                host = urllib.parse.urlparse(arg).hostname
                if host:
                    return [host.lower()]
        return []

    def _remote_hosts(self, repo: Path, env: Dict[str, str]) -> List[str]:
        """Read the repository's HTTPS remote hosts from its config.

        ``git pull/push origin main`` carries no URL, so the host has to come
        from the stored remotes — otherwise no token could ever be attached.
        """
        try:
            proc = subprocess.run(
                [self._git, "-C", str(repo), "config", "--get-regexp", r"^remote\..*\.url$"],
                capture_output=True,
                env=env,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        hosts = set()
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            _, _, url = line.partition(" ")
            if url.startswith("https://"):
                host = urllib.parse.urlparse(url.strip()).hostname
                if host:
                    hosts.add(host.lower())
        return sorted(hosts)
