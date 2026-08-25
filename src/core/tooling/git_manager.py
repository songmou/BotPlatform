"""Git binary detection and automatic download across platforms.

Detection priority (fail-closed):
1. ``configured_path`` — from ``config/tools.json`` ``git_binary_path`` (if set).
2. Cached path from a previous successful detection.
3. Platform-specific logic:

   - Windows: ``shutil.which("git")`` (system install first) → auto-download
     MinGit to ``data/system/git/`` when no system git is found.
   - macOS: ``shutil.which("git")`` → hint ``xcode-select --install``.
   - Linux: ``shutil.which("git")`` → hint with package-manager-specific commands.

Note: ``git_binary_path`` takes precedence over all platform detection; if
it points to a nonexistent file the system falls through normally.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path

from src.core.paths import SYSTEM_DATA_DIR

logger = logging.getLogger(__name__)


class GitError(RuntimeError):
    pass


class GitManager:
    """Detect and manage the git binary location."""

    MINGIT_VERSION = "2.47.1"
    MINGIT_URL = (
        "https://github.com/git-for-windows/git/releases/download/"
        "v{MINGIT_VERSION}.windows.1/MinGit-{MINGIT_VERSION}-64-bit.zip"
    ).format(MINGIT_VERSION=MINGIT_VERSION)
    GIT_DIR = SYSTEM_DATA_DIR / "git"
    GIT_EXE = GIT_DIR / "cmd" / "git.exe"

    _cached_path: str | None = None
    _download_lock = threading.Lock()
    _prefetch_guard = threading.Lock()
    _download_thread: threading.Thread | None = None

    @classmethod
    def find_git(cls, configured_path: str | None = None) -> str | None:
        """Return an already usable git path, or None. Never downloads.

        Used for availability probing on request paths, where a multi-megabyte
        MinGit download must never happen synchronously.
        """
        if configured_path:
            candidate = Path(configured_path).expanduser()
            try:
                candidate = candidate.resolve()
            except OSError:
                return None
            # A configured path is never cached: clearing git_binary_path must
            # take effect without a restart.
            return str(candidate) if candidate.is_file() else None

        cached = cls._cached_path
        if cached is not None:
            if Path(cached).is_file():
                return cached
            cls._cached_path = None

        system_git = shutil.which("git")
        if system_git:
            try:
                path = str(Path(system_git).resolve())
            except OSError:
                path = system_git
            cls._cached_path = path
            return path

        if os.name == "nt" and cls.GIT_EXE.is_file():
            path = str(cls.GIT_EXE)
            cls._cached_path = path
            return path
        return None

    @classmethod
    def ensure_git(cls, configured_path: str | None = None) -> str:
        """Return the path to a usable git executable, downloading if needed.

        Args:
            configured_path: Optional override from ``git_binary_path`` config
                (``config/tools.json``).  If set and the file exists, it is
                used immediately.  If it does not exist the method falls
                through to platform detection so a stale path doesn't break
                git discovery.
        """
        found = cls.find_git(configured_path)
        if found is not None:
            return found
        if configured_path:
            # Stale configured path: fall through to platform detection.
            found = cls.find_git()
            if found is not None:
                return found

        if os.name == "nt":
            path = cls._ensure_windows_git()
            cls._cached_path = path
            return path
        cls._raise_platform_install_hint()
        raise GitError("系统未安装 git")

    @classmethod
    def prefetch_async(cls, configured_path: str | None = None) -> None:
        """Start a one-shot background MinGit download on Windows.

        Availability probes call this so the binary becomes usable on a later
        turn without ever blocking the current request.
        """
        if os.name != "nt":
            return
        if cls.find_git(configured_path) is not None:
            return
        with cls._prefetch_guard:
            if cls._download_thread is not None and cls._download_thread.is_alive():
                return

            def _worker() -> None:
                try:
                    cls.ensure_git(configured_path)
                except Exception:
                    logger.warning("MinGit 自动下载失败", exc_info=True)

            cls._download_thread = threading.Thread(
                target=_worker, name="mingit-download", daemon=True
            )
            cls._download_thread.start()

    @classmethod
    def git_version(cls) -> str:
        """Return the ``git --version`` string for the managed binary."""
        git = cls.ensure_git()
        try:
            result = subprocess.run(
                [git, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except OSError as exc:
            raise GitError("无法执行 git：{}".format(exc)) from exc
        if result.returncode != 0:
            raise GitError("git --version 失败：{}".format(result.stderr.strip()))
        return result.stdout.strip()

    @classmethod
    def reset_cache(cls) -> None:
        """Clear cached path (useful for testing)."""
        cls._cached_path = None

    # ------------------------------------------------------------------
    # Platform helpers
    # ------------------------------------------------------------------

    @classmethod
    def _ensure_windows_git(cls) -> str:
        # Serialize: two concurrent downloads would corrupt each other's files.
        with cls._download_lock:
            if cls.GIT_EXE.is_file():
                return str(cls.GIT_EXE)

            cls.GIT_DIR.parent.mkdir(parents=True, exist_ok=True)
            staging = cls.GIT_DIR.parent / "git.download.{}".format(os.getpid())
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            zip_path = staging / "mingit.zip"

            logger.info(
                "正在下载 MinGit %s（~25MB）到 %s ...", cls.MINGIT_VERSION, cls.GIT_DIR
            )
            try:
                urllib.request.urlretrieve(cls.MINGIT_URL, zip_path)
                cls._extract_mingit(zip_path, staging)
            except (OSError, GitError) as exc:
                shutil.rmtree(staging, ignore_errors=True)
                if isinstance(exc, GitError):
                    raise
                raise GitError(
                    "MinGit 下载失败，请从以下方案任选一种：\n"
                    "① 配置 git_binary_path（config/tools.json）指向已有 git 路径\n"
                    "② 手动下载 MinGit 解压到 {}\n"
                    "③ 在系统安装 git（git-scm.com）\n"
                    "下载错误：{}".format(cls.GIT_DIR, exc)
                ) from exc

            zip_path.unlink(missing_ok=True)
            if not (staging / "cmd" / "git.exe").is_file():
                shutil.rmtree(staging, ignore_errors=True)
                raise GitError("MinGit 解压后未找到 git.exe，请重试")

            # Publish atomically so a partial extraction is never observable.
            if cls.GIT_DIR.exists():
                shutil.rmtree(cls.GIT_DIR, ignore_errors=True)
            staging.replace(cls.GIT_DIR)

            try:
                ver = subprocess.run(
                    [str(cls.GIT_EXE), "--version"],
                    capture_output=True, text=True, timeout=10, check=False,
                )
            except OSError:
                pass
            else:
                if ver.returncode == 0:
                    logger.info("MinGit 就绪：%s", ver.stdout.strip())

            return str(cls.GIT_EXE)

    @staticmethod
    def _extract_mingit(zip_path: Path, target: Path) -> None:
        """Extract the archive, rejecting entries that escape ``target``."""
        target_root = target.resolve()
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.namelist():
                    destination = (target_root / member).resolve()
                    if target_root != destination and target_root not in destination.parents:
                        raise GitError("MinGit 压缩包包含非法路径条目：{}".format(member))
                zf.extractall(target_root)
        except zipfile.BadZipFile as exc:
            raise GitError("MinGit 压缩包损坏，请重试") from exc

    @classmethod
    def _raise_platform_install_hint(cls) -> None:
        """Raise GitError with a platform-specific installation hint."""
        if sys.platform == "darwin":
            raise GitError(
                "系统未安装 git。请运行：xcode-select --install\n"
                "或从 https://git-scm.com/download/mac 下载安装。\n"
                "也可在 config/tools.json 中设置 git_binary_path 指定 git 路径。"
            )

        # Linux / other POSIX — detect available package manager
        checks = [
            ("/usr/bin/apt-get", "sudo apt-get install git"),
            ("/usr/bin/dnf",     "sudo dnf install git"),
            ("/usr/bin/yum",     "sudo yum install git"),
            ("/sbin/apk",        "apk add git"),
        ]
        hint = ""
        for cmd, install in checks:
            if Path(cmd).is_file():
                hint = install
                break
        if not hint:
            hint = "使用系统的包管理器安装 git（如 apt install git / dnf install git / pacman -S git）"

        raise GitError(
            "系统未安装 git。请运行：{}\n"
            "也可在 config/tools.json 中设置 git_binary_path 指定 git 路径。".format(hint)
        )
