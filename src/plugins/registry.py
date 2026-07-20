"""Registry for trusted, in-tree platform plugins."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Set

from .base import PlatformPlugin, PluginContext
from .browser_automation import BrowserAutomationPlugin
from .codex_tasks import CodexTasksPlugin


PLUGIN_TYPES = {
    BrowserAutomationPlugin.id: BrowserAutomationPlugin,
    CodexTasksPlugin.id: CodexTasksPlugin,
}


def known_plugin_ids() -> Set[str]:
    return set(PLUGIN_TYPES)


def plugin_tool_names(plugin_ids: Iterable[str] | None = None) -> Set[str]:
    selected = set(plugin_ids) if plugin_ids is not None else set(PLUGIN_TYPES)
    names: Set[str] = set()
    for plugin_id in selected:
        plugin_type = PLUGIN_TYPES.get(plugin_id)
        if plugin_type:
            names.update(plugin_type.TOOL_DEFINITIONS)
    return names


def validate_plugin_settings(plugin_id: str, settings: Mapping[str, Any]) -> None:
    plugin_type = PLUGIN_TYPES.get(plugin_id)
    if plugin_type is None:
        raise ValueError("未知平台插件：{}".format(plugin_id))
    plugin_type.validate_settings(settings)


def build_plugins(
    configs: Mapping[str, Any],
    context: PluginContext | None = None,
) -> List[PlatformPlugin]:
    plugins: List[PlatformPlugin] = []
    for plugin_id, config in configs.items():
        enabled = bool(getattr(config, "enabled", False))
        if not enabled:
            continue
        settings: Dict[str, Any] = dict(getattr(config, "settings", {}) or {})
        plugin_type = PLUGIN_TYPES.get(plugin_id)
        if plugin_type is None:
            continue
        plugins.append(plugin_type(settings, context=context))
    return plugins
