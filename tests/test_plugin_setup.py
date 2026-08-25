from __future__ import annotations

import threading
import time
import unittest
from typing import Any, Callable, List

from src.core.plugins.base import PluginError
from src.core.plugins.manifest import PluginDependency
from src.core.plugins.setup import (
    PluginSetupBusyError,
    PluginSetupService,
    STATUS_FAILED,
    STATUS_IDLE,
    STATUS_SUCCEEDED,
)


class FakeManifest:
    """A manifest stub with a mutable ``missing_dependencies`` view."""

    def __init__(
        self,
        plugin_id: str = "sample",
        dependencies=(),
        prepare: str = "",
        missing: List[str] = None,
    ) -> None:
        self.id = plugin_id
        self.dependencies = tuple(dependencies)
        self.prepare = prepare
        self._missing = list(missing if missing is not None else [])

    @property
    def missing_dependencies(self) -> List[str]:
        return list(self._missing)


def wait_done(service: PluginSetupService, plugin_id: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not service.is_running():
            snapshot = service.status(plugin_id)
            if snapshot["status"] != STATUS_IDLE:
                return snapshot
        time.sleep(0.01)
    raise AssertionError("setup task did not finish in time")


class PluginSetupServiceTests(unittest.TestCase):
    def test_install_success_marks_restart_required_and_logs(self) -> None:
        manifest = FakeManifest(
            dependencies=[PluginDependency("demo", "demo", ">=1,<2")],
            missing=["demo>=1,<2"],
        )
        seen: List[List[str]] = []

        def pip_runner(requirements: List[str], log: Callable[[str], None]) -> int:
            seen.append(list(requirements))
            log("Collecting demo")
            manifest._missing = []
            return 0

        service = PluginSetupService(pip_runner=pip_runner)
        service.start(manifest, {})
        snapshot = wait_done(service, "sample")
        self.assertEqual(snapshot["status"], STATUS_SUCCEEDED)
        self.assertTrue(snapshot["restart_required"])
        # Requirement strings are assembled from the manifest only.
        self.assertEqual(seen, [["demo>=1,<2"]])
        self.assertIn("Collecting demo", snapshot["log"])
        self.assertIn("安装完成，重启后生效", snapshot["log"])

    def test_pip_nonzero_exit_marks_failed(self) -> None:
        manifest = FakeManifest(
            dependencies=[PluginDependency("demo", "demo", "")],
            missing=["demo"],
        )
        service = PluginSetupService(pip_runner=lambda reqs, log: 1)
        service.start(manifest, {})
        snapshot = wait_done(service, "sample")
        self.assertEqual(snapshot["status"], STATUS_FAILED)
        self.assertIn("pip 安装失败", snapshot["error"])
        self.assertNotIn("预编译包", snapshot["error"])

    def test_pip_no_matching_version_appends_python_hint(self) -> None:
        manifest = FakeManifest(
            dependencies=[PluginDependency("demo", "demo", ">=3.0,<4.0")],
            missing=["demo>=3.0,<4.0"],
        )

        def pip_runner(requirements: List[str], log: Callable[[str], None]) -> int:
            log(
                "ERROR: Could not find a version that satisfies the "
                "requirement demo<4.0,>=3.0 (from versions: none)"
            )
            return 1

        service = PluginSetupService(pip_runner=pip_runner)
        service.start(manifest, {})
        snapshot = wait_done(service, "sample")
        self.assertEqual(snapshot["status"], STATUS_FAILED)
        self.assertIn("pip 安装失败", snapshot["error"])
        self.assertIn("请检查虚拟环境 Python 版本", snapshot["error"])

    def test_dependency_still_missing_after_install_fails(self) -> None:
        manifest = FakeManifest(
            dependencies=[PluginDependency("demo", "demo", "")],
            missing=["demo"],
        )
        # pip returns success but never clears the missing dependency.
        service = PluginSetupService(pip_runner=lambda reqs, log: 0)
        service.start(manifest, {})
        snapshot = wait_done(service, "sample")
        self.assertEqual(snapshot["status"], STATUS_FAILED)
        self.assertIn("仍缺失", snapshot["error"])

    def test_prepare_hook_runs_after_install(self) -> None:
        manifest = FakeManifest(prepare="pkg:hook", missing=[])
        received: List[Any] = []

        def hook(settings, log):
            received.append(settings)
            log("组件已下载")

        service = PluginSetupService(
            pip_runner=lambda reqs, log: 0,
            reference_loader=lambda m, ref: hook,
        )
        service.start(manifest, {"model_tier": "small"})
        snapshot = wait_done(service, "sample")
        self.assertEqual(snapshot["status"], STATUS_SUCCEEDED)
        self.assertEqual(received, [{"model_tier": "small"}])
        self.assertIn("组件已下载", snapshot["log"])
        # No missing dependencies -> no restart required.
        self.assertFalse(snapshot["restart_required"])

    def test_prepare_hook_failure_marks_failed(self) -> None:
        manifest = FakeManifest(prepare="pkg:hook", missing=[])

        def hook(settings, log):
            raise RuntimeError("下载模型失败")

        service = PluginSetupService(
            pip_runner=lambda reqs, log: 0,
            reference_loader=lambda m, ref: hook,
        )
        service.start(manifest, {})
        snapshot = wait_done(service, "sample")
        self.assertEqual(snapshot["status"], STATUS_FAILED)
        self.assertIn("下载模型失败", snapshot["error"])

    def test_start_requires_dependencies_or_prepare(self) -> None:
        manifest = FakeManifest(missing=[], prepare="")
        service = PluginSetupService()
        with self.assertRaisesRegex(PluginError, "无需安装"):
            service.start(manifest, {})

    def test_global_mutex_rejects_concurrent_start(self) -> None:
        release = threading.Event()
        started = threading.Event()

        def slow_pip(requirements, log):
            started.set()
            release.wait(2)
            manifest._missing = []
            return 0

        manifest = FakeManifest(
            dependencies=[PluginDependency("demo", "demo", "")],
            missing=["demo"],
        )
        service = PluginSetupService(pip_runner=slow_pip)
        service.start(manifest, {})
        self.assertTrue(started.wait(2))
        other = FakeManifest(plugin_id="other", prepare="pkg:hook", missing=[])
        with self.assertRaises(PluginSetupBusyError):
            service.start(other, {})
        release.set()
        snapshot = wait_done(service, "sample")
        self.assertEqual(snapshot["status"], STATUS_SUCCEEDED)

    def test_status_for_unknown_plugin_is_idle(self) -> None:
        service = PluginSetupService()
        snapshot = service.status("nope")
        self.assertEqual(snapshot["status"], STATUS_IDLE)
        self.assertEqual(snapshot["log"], [])


if __name__ == "__main__":
    unittest.main()
