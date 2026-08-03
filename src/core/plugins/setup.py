"""Background installer for declared plugin dependencies and components."""

from __future__ import annotations

import importlib
import logging
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional

from .base import PluginError
from .manifest import PluginManifest

logger = logging.getLogger(__name__)


class PluginSetupBusyError(PluginError):
    """Raised when another setup task is already running."""

LOG_TAIL_LINES = 200
DEFAULT_INSTALL_TIMEOUT_SECONDS = 1800

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

STEP_INSTALL = "install_dependencies"
STEP_PREPARE = "prepare_components"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _SetupTask:
    """Mutable per-plugin setup state guarded by the service lock."""

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id
        self.status = STATUS_IDLE
        self.step = ""
        self.error = ""
        self.started_at = ""
        self.finished_at = ""
        self.restart_required = False
        self.log: Deque[str] = deque(maxlen=LOG_TAIL_LINES)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "status": self.status,
            "step": self.step,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "restart_required": self.restart_required,
            "log": list(self.log),
        }


class PluginSetupService:
    """Install manifest-declared pip dependencies and run prepare hooks.

    Only one setup task may run at a time across all plugins. Package names
    and version specifiers come exclusively from validated plugin manifests,
    never from request payloads.
    """

    def __init__(
        self,
        install_timeout_seconds: int = DEFAULT_INSTALL_TIMEOUT_SECONDS,
        pip_runner: Optional[Callable[..., int]] = None,
        reference_loader: Optional[Callable[[PluginManifest, str], Any]] = None,
    ) -> None:
        self._install_timeout = install_timeout_seconds
        self._pip_runner = pip_runner or self._run_pip
        if reference_loader is None:
            from .manager import load_reference

            reference_loader = load_reference
        self._reference_loader = reference_loader
        self._lock = threading.Lock()
        self._tasks: Dict[str, _SetupTask] = {}
        self._active: Optional[str] = None

    # ---- public API ----
    def status(self, plugin_id: str) -> Dict[str, Any]:
        with self._lock:
            task = self._tasks.get(plugin_id)
            if task is None:
                task = _SetupTask(plugin_id)
            return task.snapshot()

    def is_running(self, plugin_id: Optional[str] = None) -> bool:
        with self._lock:
            if plugin_id is None:
                return self._active is not None
            return self._active == plugin_id

    def start(
        self,
        manifest: PluginManifest,
        settings: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Start a background setup task and return its initial snapshot."""
        missing = manifest.missing_dependencies
        if not missing and not manifest.prepare:
            raise PluginError("插件 {} 无需安装依赖或组件".format(manifest.id))
        with self._lock:
            if self._active is not None:
                raise PluginSetupBusyError("已有插件安装任务进行中，请稍后再试")
            task = _SetupTask(manifest.id)
            task.status = STATUS_RUNNING
            task.started_at = _utc_now()
            task.log.append("开始安装插件 {} 的依赖/组件".format(manifest.id))
            self._tasks[manifest.id] = task
            self._active = manifest.id
        worker = threading.Thread(
            target=self._run,
            args=(manifest, dict(settings), task),
            name="plugin-setup-{}".format(manifest.id),
            daemon=True,
        )
        worker.start()
        return task.snapshot()

    # ---- worker internals ----
    def _run(
        self,
        manifest: PluginManifest,
        settings: Dict[str, Any],
        task: _SetupTask,
    ) -> None:
        try:
            self._install_dependencies(manifest, task)
            self._prepare_components(manifest, settings, task)
        except Exception as exc:  # noqa: BLE001 - report to the polling UI
            message = str(exc).strip() or type(exc).__name__
            with self._lock:
                task.status = STATUS_FAILED
                task.error = message
                task.finished_at = _utc_now()
                task.log.append("安装失败：{}".format(message))
                self._active = None
            logger.warning("插件 %s 安装任务失败", manifest.id, exc_info=True)
            return
        with self._lock:
            task.status = STATUS_SUCCEEDED
            task.step = ""
            task.finished_at = _utc_now()
            task.log.append(
                "安装完成，重启后生效" if task.restart_required else "安装完成"
            )
            self._active = None

    def _install_dependencies(
        self, manifest: PluginManifest, task: _SetupTask
    ) -> None:
        missing = manifest.missing_dependencies
        if not missing:
            with self._lock:
                task.log.append("依赖已满足，跳过安装")
            return
        with self._lock:
            task.step = STEP_INSTALL
            task.restart_required = True
        # Requirement strings come from the validated manifest only.
        requirements: List[str] = []
        for dependency in manifest.dependencies:
            requirements.append(dependency.distribution + dependency.version)
        with self._lock:
            task.log.append("安装依赖：{}".format("、".join(requirements)))
        # Keep a copy of pip output so failures can be diagnosed below.
        captured: List[str] = []

        def _log_line(line: str) -> None:
            captured.append(str(line))
            self._append_log(task, line)

        returncode = self._pip_runner(requirements, _log_line)
        if returncode != 0:
            message = "pip 安装失败（退出码 {}）".format(returncode)
            # "from versions: none" means no wheel matches this interpreter,
            # usually because the venv Python version is unsupported upstream.
            if any("from versions: none" in line for line in captured):
                message += (
                    "；当前 Python 版本可能无对应预编译包"
                    "（如 paddlepaddle 仅支持 Python 3.9–3.13），"
                    "请检查虚拟环境 Python 版本"
                )
            raise PluginError(message)
        importlib.invalidate_caches()
        still_missing = manifest.missing_dependencies
        if still_missing:
            raise PluginError(
                "依赖安装后仍缺失：{}".format("、".join(still_missing))
            )

    def _prepare_components(
        self,
        manifest: PluginManifest,
        settings: Dict[str, Any],
        task: _SetupTask,
    ) -> None:
        if not manifest.prepare:
            return
        with self._lock:
            task.step = STEP_PREPARE
            task.log.append("准备插件组件：{}".format(manifest.prepare))
        hook = self._reference_loader(manifest, manifest.prepare)
        if not callable(hook):
            raise PluginError("插件 prepare 钩子不可调用：{}".format(manifest.prepare))
        hook(settings, lambda line: self._append_log(task, line))

    def _append_log(self, task: _SetupTask, line: str) -> None:
        text = str(line).rstrip()
        if not text:
            return
        with self._lock:
            task.log.append(text)

    def _run_pip(
        self, requirements: List[str], log: Callable[[str], None]
    ) -> int:
        command = [sys.executable, "-m", "pip", "install", *requirements]
        log("$ {}".format(" ".join(command)))
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        timer = threading.Timer(self._install_timeout, process.kill)
        timer.start()
        try:
            for line in process.stdout:
                log(line)
            return process.wait()
        finally:
            timer.cancel()
            process.stdout.close()


_default_service: Optional[PluginSetupService] = None
_default_lock = threading.Lock()


def default_setup_service() -> PluginSetupService:
    """Return the process-wide setup service singleton."""
    global _default_service
    with _default_lock:
        if _default_service is None:
            _default_service = PluginSetupService()
        return _default_service
