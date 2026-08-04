"""Manifest-backed plugin discovery and construction helpers."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, Iterable, List, Mapping, Set

from src.core.paths import PROJECT_ROOT, SYSTEM_DATA_DIR

from .base import PlatformPlugin, PluginContext
from .catalog import PluginCatalog
from .manager import PluginManager


@lru_cache(maxsize=1)
def default_catalog() -> PluginCatalog:
    return PluginCatalog.discover(
        PROJECT_ROOT,
        external_root=SYSTEM_DATA_DIR / "plugins",
    )


def refresh_catalog() -> PluginCatalog:
    default_catalog.cache_clear()
    return default_catalog()


def known_plugin_ids(catalog: PluginCatalog | None = None) -> Set[str]:
    return set((catalog or default_catalog()).manifests)


def plugin_tool_names(
    plugin_ids: Iterable[str] | Mapping[str, Any] | None = None,
    catalog: PluginCatalog | None = None,
) -> Set[str]:
    selected = (
        set(plugin_ids)
        if plugin_ids is not None
        else set((catalog or default_catalog()).manifests)
    )
    names: Set[str] = set()
    for plugin_id in selected:
        manifest = (catalog or default_catalog()).get(plugin_id)
        if manifest:
            names.update(manifest.tools)
    return names


def validate_plugin_settings(
    plugin_id: str,
    settings: Mapping[str, Any],
    catalog: PluginCatalog | None = None,
) -> None:
    manifest = (catalog or default_catalog()).get(plugin_id)
    if manifest is None:
        raise ValueError("未知平台插件：{}".format(plugin_id))
    manifest.validate_settings(settings)


def normalize_plugin_settings(
    plugin_id: str,
    settings: Mapping[str, Any],
    catalog: PluginCatalog | None = None,
) -> Dict[str, Any]:
    manifest = (catalog or default_catalog()).get(plugin_id)
    if manifest is None:
        raise ValueError("未知平台插件：{}".format(plugin_id))
    return manifest.normalize_settings(settings)


def build_plugin_manager(
    configs: Mapping[str, Any],
    context: PluginContext | None = None,
    catalog: PluginCatalog | None = None,
) -> PluginManager:
    from src.core.tooling.definitions import TOOL_DEFINITIONS

    return PluginManager(
        catalog or default_catalog(),
        configs,
        context,
        reserved_tools=TOOL_DEFINITIONS,
    )


def build_plugins(
    configs: Mapping[str, Any],
    context: PluginContext | None = None,
) -> List[PlatformPlugin]:
    """Compatibility helper for callers that still consume an instance list."""
    return build_plugin_manager(configs, context=context).plugins
