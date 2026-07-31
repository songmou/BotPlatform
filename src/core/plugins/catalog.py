"""Discover bundled and locally installed plugin packages."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Optional

from .manifest import PluginManifest, PluginManifestError, load_manifest


class PluginCatalog:
    def __init__(self, manifests: Iterable[PluginManifest] = ()) -> None:
        self._manifests: Dict[str, PluginManifest] = {}
        self.errors: Dict[str, str] = {}
        for manifest in manifests:
            self.add(manifest)

    @property
    def manifests(self) -> Dict[str, PluginManifest]:
        return dict(self._manifests)

    def add(self, manifest: PluginManifest) -> None:
        if manifest.id in self._manifests:
            raise PluginManifestError("插件 ID 重复：{}".format(manifest.id))
        existing_tools = {
            name: item.id
            for item in self._manifests.values()
            for name in item.tools
        }
        duplicate = sorted(set(existing_tools) & set(manifest.tools))
        if duplicate:
            raise PluginManifestError(
                "插件工具名称重复：{}（{} / {}）".format(
                    duplicate[0], existing_tools[duplicate[0]], manifest.id
                )
            )
        self._manifests[manifest.id] = manifest

    def get(self, plugin_id: str) -> Optional[PluginManifest]:
        return self._manifests.get(plugin_id)

    @classmethod
    def discover(cls, project_root: Path, external_root: Optional[Path] = None) -> "PluginCatalog":
        catalog = cls()
        roots = [
            ("bundled", project_root / "src" / "core" / "plugins" / "bundled"),
            ("external", external_root or project_root / "data" / "system" / "plugins"),
        ]
        for source, root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*/plugin.json")):
                try:
                    catalog.add(load_manifest(path, source))
                except PluginManifestError as exc:
                    catalog.errors[str(path)] = str(exc)
        return catalog
