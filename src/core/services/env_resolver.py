"""Resolve environment variables for scripts and plugins across tenant scopes.

Environment variables live in two layers:

* **Global platform values** (``data/system/scripts.env``), managed by the
  platform administrator.
* **Organization-scoped values** stored per tenant in ``tenant_settings.env_json``.

Script and plugin authors declare the variable *names* they need
(``env_allowlist``); the actual values are supplied by the platform or the
organization. At runtime an organization value overrides the global value for
the same name.

Reserved names (``PATH``, ``ILINKBOT_*`` and a few others) may never be claimed
by an author so a script cannot hijack the sandbox environment.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, Iterable, List, Optional

_ENV_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
RESERVED_PREFIXES = ("ILINKBOT_", "PYTHON", "LD_", "DYLD_")
RESERVED_NAMES = frozenset(
    {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "NO_PROXY", "AUTOGEN_ENV_FILE"}
)
_MAX_VARS = 32


class EnvNameError(ValueError):
    """Raised when an environment variable name is invalid or reserved."""


def _reserved(name: str) -> bool:
    upper = name.upper()
    return upper in RESERVED_NAMES or any(
        upper.startswith(prefix) for prefix in RESERVED_PREFIXES
    )


def validate_env_name(name: str) -> str:
    """Validate a single environment variable name, returning it unchanged."""
    if not isinstance(name, str) or not _ENV_PATTERN.fullmatch(name):
        raise EnvNameError(
            "环境变量名必须是大写字母、数字与下划线组合，且以字母或下划线开头"
        )
    if _reserved(name):
        raise EnvNameError("环境变量名 {} 为保留名，禁止声明".format(name))
    return name


def normalize_allowlist(raw: Iterable[str]) -> List[str]:
    """Validate, deduplicate, and bound a declared allowlist of variable names."""
    seen: set = set()
    result: List[str] = []
    for item in raw or []:
        if not isinstance(item, str):
            raise EnvNameError("env_allowlist 只能包含字符串")
        name = validate_env_name(item.strip())
        if name not in seen:
            seen.add(name)
            result.append(name)
    if len(result) > _MAX_VARS:
        raise EnvNameError("env_allowlist 最多包含 {} 个变量".format(_MAX_VARS))
    return result


def mask(value: str) -> str:
    """Mask a secret value for display: keep first/last two chars if long."""
    value = "" if value is None else str(value)
    if not value or len(value) <= 8:
        return "****"
    return value[:2] + "****" + value[-2:]


class EnvResolver:
    """Resolve declared variable names to values for a tenant at runtime."""

    def __init__(
        self,
        settings_store,
        global_loader: Callable[[], Dict[str, str]],
    ) -> None:
        self._settings = settings_store
        self._global_loader = global_loader

    def resolve(self, tenant_id: str, names: Iterable[str]) -> Dict[str, str]:
        """Return ``{name: value}`` for the given names.

        Organization values take precedence over global values; reserved names
        are never returned.
        """
        org = self._settings.env(tenant_id) if self._settings is not None else {}
        glob = self._global_loader() or {}
        result: Dict[str, str] = {}
        for name in names or []:
            if _reserved(name):
                continue
            if name in org:
                result[name] = org[name]
            elif name in glob:
                result[name] = glob[name]
        return result

    def describe(self, tenant_id: str, names: Iterable[str]) -> List[Dict[str, str]]:
        """Describe each name with its source, masked value, and defined flag.

        Used by the web UI to render the read-only env binding table.
        """
        org = self._settings.env(tenant_id) if self._settings is not None else {}
        glob = self._global_loader() or {}
        rows: List[Dict[str, str]] = []
        for name in names or []:
            if _reserved(name):
                rows.append(
                    {
                        "name": name,
                        "source": "reserved",
                        "masked": "",
                        "defined": False,
                    }
                )
                continue
            if name in org:
                source, value = "tenant", org[name]
            elif name in glob:
                source, value = "global", glob[name]
            else:
                source, value = "missing", ""
            rows.append(
                {
                    "name": name,
                    "source": source,
                    "masked": mask(value) if value else "",
                    "defined": bool(value),
                }
            )
        return rows

    def global_describe(self, names: Iterable[str]) -> List[Dict[str, str]]:
        """Describe names against the platform global layer only (no tenant).

        Used by the global admin popups (script/plugin/schedule detail) where a
        specific tenant is not selected; organization overrides are configured
        per tenant on the tenant management page.
        """
        glob = self._global_loader() or {}
        rows: List[Dict[str, str]] = []
        for name in names or []:
            if _reserved(name):
                rows.append(
                    {"name": name, "source": "reserved", "masked": "", "defined": False}
                )
                continue
            if name in glob:
                rows.append(
                    {
                        "name": name,
                        "source": "global",
                        "masked": mask(glob[name]),
                        "defined": True,
                    }
                )
            else:
                rows.append(
                    {"name": name, "source": "missing", "masked": "", "defined": False}
                )
        return rows
