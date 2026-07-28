"""Small, stable interface between platform plugins and the tool runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol


class PluginError(RuntimeError):
    """A safe, user-readable plugin failure."""


@dataclass(frozen=True)
class PluginToolDefinition:
    description: str
    parameters: Dict[str, Any]
    requires_approval: bool = False
    direct_response: bool = False


@dataclass(frozen=True)
class PluginContext:
    """Trusted application services available to bundled platform plugins."""

    project_root: Path
    tenant_registry: Any
    notification_service: Optional[Any] = None
    timezone: str = "UTC"


class PlatformPlugin(Protocol):
    id: str

    @property
    def tool_definitions(self) -> Mapping[str, PluginToolDefinition]: ...

    def is_available(self, tool_name: str) -> bool: ...

    def execute(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> Any: ...

    def preview(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> str: ...

    def close_tenant(self, tenant_id: str) -> None: ...

    def close(self) -> None: ...
