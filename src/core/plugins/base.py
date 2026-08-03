"""Small, stable interface between platform plugins and the tool runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol


class PluginError(RuntimeError):
    """A safe, user-readable plugin failure."""


@dataclass(frozen=True)
class PluginToolDefinition:
    description: str
    parameters: Dict[str, Any]
    requires_approval: bool = False
    direct_response: bool = False
    approval_policy: str = "none"

    def __post_init__(self) -> None:
        policy = self.approval_policy
        if self.requires_approval and policy == "none":
            object.__setattr__(self, "approval_policy", "required")
        elif policy not in {"none", "optional", "required"}:
            raise ValueError("插件工具审批策略无效：{}".format(policy))


@dataclass(frozen=True)
class PluginContext:
    """Trusted application services available to bundled platform plugins."""

    project_root: Optional[Path]
    tenant_registry: Optional[Any]
    notification_service: Optional[Any] = None
    timezone: str = "UTC"
    data_root: Optional[Path] = None
    plugin_id: str = ""

    def tenant_data_dir(
        self,
        tenant_or_plugin_id: str,
        tenant_id: Optional[str] = None,
    ) -> Path:
        """Return the plugin's tenant-owned data directory."""
        if tenant_id is None:
            plugin_id = self.plugin_id
            tenant_id = tenant_or_plugin_id
        else:
            plugin_id = tenant_or_plugin_id
        if not plugin_id or self.tenant_registry is None:
            raise PluginError("插件未声明 tenant_storage 平台服务")
        if self.plugin_id and plugin_id != self.plugin_id:
            raise PluginError("插件不能访问其他插件的数据目录")
        root = self.tenant_registry.tenant_root(tenant_id) / "plugins" / plugin_id
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    def global_data_dir(self, requested_plugin_id: str = "") -> Path:
        """Return the plugin's non-tenant data directory."""
        plugin_id = requested_plugin_id or self.plugin_id
        if not plugin_id:
            raise PluginError("插件上下文未绑定插件 ID")
        if self.plugin_id and plugin_id != self.plugin_id:
            raise PluginError("插件不能访问其他插件的数据目录")
        if self.data_root is None and self.project_root is None:
            raise PluginError("插件未声明存储平台服务")
        base = self.data_root or (self.project_root / "data" / "plugins")
        root = base / plugin_id
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root


@dataclass(frozen=True)
class PluginJobDefinition:
    id: str
    interval_seconds: int


class PlatformPlugin(Protocol):
    id: str

    @property
    def tool_definitions(self) -> Mapping[str, PluginToolDefinition]: ...

    def is_available(self, tool_name: str) -> bool: ...

    def execute(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> Any: ...

    def preview(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> str: ...

    def close_tenant(self, tenant_id: str) -> None: ...

    def close(self) -> None: ...

    def start(self) -> None: ...

    @property
    def background_jobs(self) -> List[PluginJobDefinition]: ...

    def run_background_job(self, job_id: str, now: Any = None) -> Any: ...
