"""Lifecycle and routing for enabled in-process plugins."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .base import PlatformPlugin, PluginContext, PluginError, PluginJobDefinition
from .catalog import PluginCatalog
from .manifest import PluginManifest

logger = logging.getLogger(__name__)


class PluginManager:
    def __init__(
        self,
        catalog: PluginCatalog,
        configs: Mapping[str, Any],
        context: Optional[PluginContext],
        reserved_tools: Iterable[str] = (),
    ) -> None:
        self.catalog = catalog
        self.configs = configs
        self.context = context
        self.plugins: List[PlatformPlugin] = []
        self._by_id: Dict[str, PlatformPlugin] = {}
        self._by_tool: Dict[str, PlatformPlugin] = {}
        self.errors: Dict[str, str] = {}
        self._reserved_tools = set(reserved_tools)
        self._started = False
        self._load_enabled()

    def _load_enabled(self) -> None:
        for plugin_id, config in self.configs.items():
            if not bool(getattr(config, "enabled", False)):
                continue
            manifest = self.catalog.get(plugin_id)
            if manifest is None:
                self.errors[plugin_id] = "插件包未安装"
                continue
            conflicts = sorted(set(manifest.tools) & self._reserved_tools)
            if conflicts:
                self.errors[plugin_id] = "插件工具名称与内置工具冲突：{}".format(
                    "、".join(conflicts)
                )
                continue
            missing = manifest.missing_dependencies
            if missing:
                self.errors[plugin_id] = "缺少依赖：{}".format("、".join(missing))
                continue
            settings = manifest.normalize_settings(
                dict(getattr(config, "settings", {}) or {})
            )
            try:
                manifest.validate_settings(settings)
                plugin_type = self._load_entrypoint(manifest)
                validator = getattr(plugin_type, "validate_settings", None)
                if callable(validator):
                    validator(settings)
                plugin = plugin_type(
                    settings, context=self._context_for(manifest)
                )
                self._register(plugin_id, plugin, manifest)
            except Exception as exc:  # noqa: BLE001 - isolate a broken plugin
                self.errors[plugin_id] = str(exc).strip() or type(exc).__name__
                logger.warning("加载插件 %s 失败", plugin_id, exc_info=True)

    def _context_for(self, manifest: PluginManifest) -> Optional[PluginContext]:
        if self.context is None:
            return None
        has_tenant_storage = "tenant_storage" in manifest.services
        return PluginContext(
            project_root=(
                self.context.project_root
                if "project_paths" in manifest.services
                else None
            ),
            tenant_registry=(
                self.context.tenant_registry if has_tenant_storage else None
            ),
            notification_service=(
                self.context.notification_service
                if "notification" in manifest.services
                else None
            ),
            timezone=self.context.timezone,
            data_root=self.context.data_root if has_tenant_storage else None,
            plugin_id=manifest.id,
        )

    def _register(
        self,
        plugin_id: str,
        plugin: PlatformPlugin,
        manifest: PluginManifest,
    ) -> None:
        if str(getattr(plugin, "id", "")) != plugin_id:
            raise ValueError("插件入口 ID 与清单不一致")
        for tool_name in manifest.tools:
            if tool_name in self._by_tool:
                raise ValueError("平台插件工具名称重复：{}".format(tool_name))
            self._by_tool[tool_name] = plugin
        self.plugins.append(plugin)
        self._by_id[plugin_id] = plugin

    def _remove(self, plugin: PlatformPlugin) -> None:
        self.plugins = [item for item in self.plugins if item is not plugin]
        self._by_id.pop(plugin.id, None)
        for tool_name, owner in list(self._by_tool.items()):
            if owner is plugin:
                self._by_tool.pop(tool_name, None)

    @staticmethod
    def _load_entrypoint(manifest: PluginManifest) -> Any:
        module_name, attribute = manifest.entrypoint.split(":", 1)
        if module_name.startswith("src."):
            module = importlib.import_module(module_name)
        else:
            module = _load_external_module(manifest, module_name)
        value = getattr(module, attribute, None)
        if value is None:
            raise ImportError("插件入口不存在：{}".format(manifest.entrypoint))
        return value

    @property
    def tool_names(self) -> List[str]:
        return list(self._by_tool)

    def manifest_for_tool(self, tool_name: str) -> Optional[PluginManifest]:
        plugin = self._by_tool.get(tool_name)
        return self.catalog.get(plugin.id) if plugin is not None else None

    def definition(self, tool_name: str):
        manifest = self.manifest_for_tool(tool_name)
        return manifest.tools.get(tool_name) if manifest is not None else None

    def get(self, plugin_id: str) -> Optional[PlatformPlugin]:
        return self._by_id.get(plugin_id)

    def is_available(self, tool_name: str, tenant: Any = None) -> bool:
        plugin = self._by_tool.get(tool_name)
        if plugin is None:
            return False
        checker = getattr(plugin, "is_available")
        try:
            return bool(checker(tool_name, tenant))
        except TypeError:
            return bool(checker(tool_name))

    def execute(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> Any:
        plugin = self._by_tool.get(tool_name)
        if plugin is None:
            raise PluginError("插件工具不可用：{}".format(tool_name))
        return plugin.execute(tool_name, arguments, tenant)

    def preview(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> str:
        plugin = self._by_tool.get(tool_name)
        if plugin is None:
            raise PluginError("插件工具不可用：{}".format(tool_name))
        return plugin.preview(tool_name, arguments, tenant)

    def start(self) -> None:
        if self._started:
            return
        for plugin in list(self.plugins):
            starter = getattr(plugin, "start", None)
            try:
                if callable(starter):
                    starter()
            except Exception as exc:  # noqa: BLE001 - isolate plugin startup
                self.errors[plugin.id] = str(exc).strip() or type(exc).__name__
                self._remove(plugin)
                try:
                    plugin.close()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "插件 %s 启动失败后关闭异常", plugin.id, exc_info=True
                    )
                logger.warning("启动插件 %s 失败", plugin.id, exc_info=True)
        self._started = True

    def background_jobs(self) -> List[tuple[str, PluginJobDefinition]]:
        result: List[tuple[str, PluginJobDefinition]] = []
        for plugin in self.plugins:
            manifest = self.catalog.get(plugin.id)
            if manifest:
                result.extend((plugin.id, item) for item in manifest.jobs)
        return result

    def run_background_job(self, plugin_id: str, job_id: str, now: Any = None) -> Any:
        plugin = self._by_id.get(plugin_id)
        runner = getattr(plugin, "run_background_job", None) if plugin else None
        if not callable(runner):
            raise PluginError("插件后台任务不可用：{} / {}".format(plugin_id, job_id))
        return runner(job_id, now)

    def close_tenant(self, tenant_id: str) -> None:
        for plugin in self.plugins:
            try:
                plugin.close_tenant(tenant_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "关闭插件 %s 的租户资源失败", plugin.id, exc_info=True
                )

    def close(self) -> None:
        for plugin in reversed(self.plugins):
            try:
                plugin.close()
            except Exception:  # noqa: BLE001
                logger.warning("关闭插件 %s 失败", plugin.id, exc_info=True)
        self._started = False


def _load_external_module(manifest: PluginManifest, module_name: str) -> ModuleType:
    relative = Path(*module_name.split("."))
    file_path = (manifest.package_root / relative).with_suffix(".py").resolve()
    package_init = (manifest.package_root / relative / "__init__.py").resolve()
    root = manifest.package_root.resolve()
    target = package_init if package_init.is_file() else file_path
    if root not in target.parents or not target.is_file():
        raise ImportError("插件入口必须位于插件目录内")
    namespace_root = "botplatform_external_plugins"
    _ensure_namespace_package(namespace_root, None)
    plugin_namespace = "{}.{}".format(namespace_root, manifest.id)
    _ensure_namespace_package(plugin_namespace, root)
    module_parts = module_name.split(".")
    for index in range(1, len(module_parts)):
        parent_name = "{}.{}".format(
            plugin_namespace,
            ".".join(module_parts[:index]),
        )
        _ensure_namespace_package(
            parent_name,
            root.joinpath(*module_parts[:index]),
        )
    namespace = "{}.{}".format(plugin_namespace, module_name)
    spec = importlib.util.spec_from_file_location(
        namespace,
        target,
        submodule_search_locations=[str(target.parent)] if target.name == "__init__.py" else None,
    )
    if spec is None or spec.loader is None:
        raise ImportError("无法加载插件入口：{}".format(manifest.entrypoint))
    module = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = module
    spec.loader.exec_module(module)
    return module


def _ensure_namespace_package(name: str, path: Optional[Path]) -> None:
    existing = sys.modules.get(name)
    if existing is not None:
        return
    package = ModuleType(name)
    package.__package__ = name
    package.__path__ = [] if path is None else [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = package
