"""Scoped public/organization resource persistence and inheritance resolution."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

from src.core.config.loader import AgentPreset, Capability, ProjectConfig
from src.core.storage.organizations import OrganizationStore


RESOURCE_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,127}$")
RESOURCE_TYPES = {
    "agents",
    "models",
    "skills",
    "plugins",
    "mcp",
    "channels",
    "schedules",
    "tools",
    "scripts",
}
LIST_MODES = {"inherit", "replace", "disable"}
ORGANIZATION_CREATABLE_TYPES = {
    "agents",
    "models",
    "skills",
    "mcp",
    "channels",
    "schedules",
}
SECRET_FIELD_PARTS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
    "credential",
    "headers",
}


class ResourceError(ValueError):
    """Raised when a scoped resource or override is invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _merge_payload(
    base: Dict[str, Any],
    patch: Dict[str, Any],
    list_modes: Dict[str, str],
) -> Dict[str, Any]:
    result = json.loads(json.dumps(base, ensure_ascii=False))
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_payload(result[key], value, {})
        else:
            result[key] = value
    for key, mode in list_modes.items():
        if mode == "inherit":
            continue
        if mode == "disable":
            result[key] = []
        elif mode == "replace":
            replacement = patch.get(key, [])
            if not isinstance(replacement, list):
                raise ResourceError("列表字段 {} 的覆盖值必须是数组".format(key))
            result[key] = replacement
    return result


def _reject_secret_fields(value: Any, path: str = "") -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        normalized = str(key).strip().lower().replace("-", "_")
        secret_field = (
            normalized in SECRET_FIELD_PARTS
            or normalized.endswith("_api_key")
            or normalized.endswith("_password")
            or normalized.endswith("_secret")
            or normalized.endswith("_token")
            or normalized.endswith("_credential")
            or normalized.endswith("_headers")
        )
        if secret_field and item not in (
            None,
            "",
            {},
            [],
        ):
            raise ResourceError(
                "资源配置不得包含密钥字段：{}".format(path + str(key))
            )
        if isinstance(item, dict):
            _reject_secret_fields(item, path + str(key) + ".")


def _validate_organization_payload(
    resource_type: str, payload: Dict[str, Any]
) -> None:
    _reject_secret_fields(payload)
    if resource_type == "mcp":
        transport = str(payload.get("transport") or "").lower()
        url = str(payload.get("url") or "")
        if transport not in {"streamablehttp", "streamable_http"}:
            raise ResourceError("组织 MCP 仅支持远程 Streamable HTTP 连接")
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ResourceError("组织 MCP 地址必须使用 HTTPS")
    if resource_type == "models":
        base_url = str(payload.get("base_url") or "")
        if base_url:
            parsed = urlparse(base_url)
            loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            if parsed.scheme.lower() != "https" and not (
                parsed.scheme.lower() == "http" and loopback
            ):
                raise ResourceError("模型地址必须使用 HTTPS；仅本机回环可使用 HTTP")


class ScopedResourceStore:
    """Store public catalog entries and organization-owned effective resources."""

    def __init__(
        self,
        organizations: OrganizationStore,
        config: Optional[ProjectConfig] = None,
    ) -> None:
        self.organizations = organizations
        self.database = organizations.database
        if config is not None:
            self.bootstrap_public(config)

    @staticmethod
    def _validate_type(resource_type: str) -> str:
        value = resource_type.strip().lower()
        if value not in RESOURCE_TYPES:
            raise ResourceError("不支持的资源类型：{}".format(resource_type))
        return value

    @staticmethod
    def _validate_id(resource_id: str) -> str:
        value = resource_id.strip()
        if not RESOURCE_ID_PATTERN.fullmatch(value):
            raise ResourceError("资源编号格式无效")
        return value

    def bootstrap_public(self, config: ProjectConfig) -> None:
        seeds: Dict[str, Iterable[tuple[str, Any]]] = {
            "agents": config.agents.items(),
            "models": config.models.items(),
            "skills": (
                (str(item.get("id", "")), item) for item in config.skills
            ),
            "plugins": config.plugins.items(),
            "mcp": (
                (str(item.get("id", "")), item) for item in config.mcp_servers
            ),
            "channels": config.channels.items(),
            "schedules": ((item.id, item) for item in config.schedules),
            "scripts": config.scripts.items(),
            "tools": (("platform", config.tools),),
        }
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            for resource_type, entries in seeds.items():
                for resource_id, payload in entries:
                    if not resource_id or not RESOURCE_ID_PATTERN.fullmatch(resource_id):
                        continue
                    connection.execute(
                        "INSERT OR IGNORE INTO scoped_resources("
                        "resource_type, resource_id, scope, organization_id, "
                        "revision, status, payload_json, created_at, updated_at"
                        ") VALUES (?, ?, 'public', NULL, 1, 'published', ?, ?, ?)",
                        (
                            resource_type,
                            resource_id,
                            json.dumps(
                                _json_value(payload),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            timestamp,
                            timestamp,
                        ),
                    )

    @staticmethod
    def _row(row: Any) -> Dict[str, Any]:
        result = dict(row)
        try:
            result["payload"] = json.loads(str(result.pop("payload_json")))
        except (TypeError, ValueError, json.JSONDecodeError):
            result["payload"] = {}
        result["revision"] = int(result["revision"])
        return result

    def get_public(self, resource_type: str, resource_id: str) -> Dict[str, Any]:
        resource_type = self._validate_type(resource_type)
        resource_id = self._validate_id(resource_id)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM scoped_resources WHERE resource_type=? "
                "AND resource_id=? AND scope='public'",
                (resource_type, resource_id),
            ).fetchone()
        if row is None:
            raise ResourceError("公共资源不存在")
        return self._row(row)

    def list_public(
        self, resource_type: str, include_unpublished: bool = False
    ) -> List[Dict[str, Any]]:
        resource_type = self._validate_type(resource_type)
        clause = "" if include_unpublished else " AND status IN ('published','deprecated')"
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM scoped_resources WHERE resource_type=? "
                "AND scope='public'{} ORDER BY resource_id".format(clause),
                (resource_type,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def upsert_public(
        self,
        resource_type: str,
        resource_id: str,
        payload: Dict[str, Any],
        user_id: int,
        status: str = "published",
    ) -> Dict[str, Any]:
        resource_type = self._validate_type(resource_type)
        resource_id = self._validate_id(resource_id)
        if status not in {"draft", "published", "deprecated", "disabled"}:
            raise ResourceError("公共资源状态无效")
        if not isinstance(payload, dict):
            raise ResourceError("资源配置必须是 JSON 对象")
        _reject_secret_fields(payload)
        timestamp = _now()
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO scoped_resources("
                "resource_type, resource_id, scope, organization_id, revision, "
                "status, payload_json, created_by, updated_by, created_at, updated_at"
                ") VALUES (?, ?, 'public', NULL, 1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(resource_type, resource_id) WHERE scope='public' "
                "DO UPDATE SET payload_json=excluded.payload_json, "
                "status=excluded.status, updated_by=excluded.updated_by, "
                "updated_at=excluded.updated_at, revision=revision+1",
                (
                    resource_type,
                    resource_id,
                    status,
                    serialized,
                    user_id,
                    user_id,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_public(resource_type, resource_id)

    def list_effective(
        self, organization_id: str, resource_type: str
    ) -> List[Dict[str, Any]]:
        self.organizations.get(organization_id)
        resource_type = self._validate_type(resource_type)
        public = self.list_public(resource_type)
        with self.database.read() as connection:
            overrides = connection.execute(
                "SELECT * FROM organization_resource_overrides "
                "WHERE organization_id=? AND resource_type=?",
                (organization_id, resource_type),
            ).fetchall()
            owned = connection.execute(
                "SELECT * FROM scoped_resources WHERE organization_id=? "
                "AND resource_type=? AND scope='organization' "
                "AND status != 'disabled' ORDER BY resource_id",
                (organization_id, resource_type),
            ).fetchall()
        override_map = {
            str(row["public_resource_id"]): row for row in overrides
        }
        result: List[Dict[str, Any]] = []
        for item in public:
            override = override_map.get(str(item["resource_id"]))
            if override is not None and not bool(override["enabled"]):
                continue
            payload = item["payload"]
            overridden = override is not None
            if override is not None:
                try:
                    patch = json.loads(str(override["patch_json"]))
                    list_modes = json.loads(str(override["list_modes_json"]))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ResourceError("组织资源覆盖配置损坏") from exc
                payload = _merge_payload(payload, patch, list_modes)
            result.append(
                {
                    **item,
                    "payload": payload,
                    "effective_scope": "public_override" if overridden else "public",
                    "organization_id": organization_id,
                }
            )
        result.extend(
            {
                **self._row(row),
                "effective_scope": "organization",
            }
            for row in owned
        )
        return result

    def effective_agent_presets(
        self, organization_id: str
    ) -> Dict[str, AgentPreset]:
        result: Dict[str, AgentPreset] = {}
        for item in self.list_effective(organization_id, "agents"):
            payload = item["payload"]
            if not bool(payload.get("enabled", True)):
                continue
            capabilities = [
                Capability(
                    name=str(capability.get("name") or ""),
                    description=str(capability.get("description") or ""),
                )
                for capability in payload.get("capabilities", [])
                if isinstance(capability, dict)
            ]
            resource_id = str(item["resource_id"])
            result[resource_id] = AgentPreset(
                id=str(payload.get("id") or resource_id),
                name=str(payload.get("name") or resource_id),
                role=str(payload.get("role") or "assistant"),
                description=str(payload.get("description") or ""),
                system_prompt=str(
                    payload.get("system_prompt") or "你是一个有帮助的助手。"
                ),
                capabilities=capabilities,
                enabled=True,
                image_prompt=payload.get("image_prompt"),
                tools=list(payload.get("tools") or []),
                plugin_tools=dict(payload.get("plugin_tools") or {}),
                skills=list(payload.get("skills") or []),
                mcp_servers=list(payload.get("mcp_servers") or []),
                model=payload.get("model"),
                greeting=payload.get("greeting"),
                greeting_hints=list(payload.get("greeting_hints") or []),
                temperature=payload.get("temperature"),
                max_tokens=payload.get("max_tokens"),
            )
        return result

    def effective_skills(self, organization_id: str) -> List[Dict[str, Any]]:
        return [
            item["payload"]
            for item in self.list_effective(organization_id, "skills")
            if bool(item["payload"].get("enabled", True))
        ]

    def get_effective(
        self,
        organization_id: str,
        resource_type: str,
        resource_id: str,
    ) -> Dict[str, Any]:
        resource_id = self._validate_id(resource_id)
        for item in self.list_effective(organization_id, resource_type):
            if item["resource_id"] == resource_id:
                return item
        raise ResourceError("资源不存在或未向当前组织启用")

    def upsert_organization(
        self,
        organization_id: str,
        resource_type: str,
        resource_id: str,
        payload: Dict[str, Any],
        user_id: int,
        base_resource_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.organizations.get(organization_id)
        resource_type = self._validate_type(resource_type)
        resource_id = self._validate_id(resource_id)
        if resource_type not in ORGANIZATION_CREATABLE_TYPES:
            raise ResourceError(
                "该资源只能由平台发布，组织仅可启停或覆盖已授权公共资源"
            )
        if not isinstance(payload, dict):
            raise ResourceError("资源配置必须是 JSON 对象")
        _validate_organization_payload(resource_type, payload)
        if base_resource_id:
            self.get_public(resource_type, base_resource_id)
        timestamp = _now()
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT resource_pk FROM scoped_resources WHERE resource_type=? "
                "AND resource_id=? AND scope='organization' AND organization_id=?",
                (resource_type, resource_id, organization_id),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO scoped_resources("
                    "resource_type, resource_id, scope, organization_id, "
                    "base_resource_id, revision, status, payload_json, created_by, "
                    "updated_by, created_at, updated_at"
                    ") VALUES (?, ?, 'organization', ?, ?, 1, 'published', ?, ?, ?, ?, ?)",
                    (
                        resource_type,
                        resource_id,
                        organization_id,
                        base_resource_id,
                        serialized,
                        user_id,
                        user_id,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE scoped_resources SET payload_json=?, revision=revision+1, "
                    "updated_by=?, updated_at=? WHERE resource_pk=?",
                    (serialized, user_id, timestamp, int(existing["resource_pk"])),
                )
        return self.get_effective(organization_id, resource_type, resource_id)

    def delete_organization(
        self, organization_id: str, resource_type: str, resource_id: str
    ) -> None:
        resource_type = self._validate_type(resource_type)
        resource_id = self._validate_id(resource_id)
        with self.database.transaction(immediate=True) as connection:
            deleted = connection.execute(
                "DELETE FROM scoped_resources WHERE organization_id=? "
                "AND resource_type=? AND resource_id=? AND scope='organization'",
                (organization_id, resource_type, resource_id),
            )
        if deleted.rowcount == 0:
            raise ResourceError("组织资源不存在")

    def set_override(
        self,
        organization_id: str,
        resource_type: str,
        resource_id: str,
        user_id: int,
        *,
        enabled: bool = True,
        patch: Optional[Dict[str, Any]] = None,
        list_modes: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        self.organizations.get(organization_id)
        public = self.get_public(resource_type, resource_id)
        patch = patch or {}
        list_modes = list_modes or {}
        if not isinstance(patch, dict) or not isinstance(list_modes, dict):
            raise ResourceError("覆盖配置必须是 JSON 对象")
        _reject_secret_fields(patch)
        invalid = sorted(
            key for key, mode in list_modes.items() if mode not in LIST_MODES
        )
        if invalid:
            raise ResourceError("列表覆盖模式无效：{}".format("、".join(invalid)))
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO organization_resource_overrides("
                "organization_id, resource_type, public_resource_id, enabled, "
                "list_modes_json, patch_json, base_revision, updated_by, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(organization_id, resource_type, public_resource_id) "
                "DO UPDATE SET enabled=excluded.enabled, "
                "list_modes_json=excluded.list_modes_json, "
                "patch_json=excluded.patch_json, base_revision=excluded.base_revision, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (
                    organization_id,
                    resource_type,
                    resource_id,
                    1 if enabled else 0,
                    json.dumps(list_modes, ensure_ascii=False),
                    json.dumps(patch, ensure_ascii=False),
                    public["revision"],
                    user_id,
                    _now(),
                ),
            )
        if not enabled:
            return {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "enabled": False,
                "effective_scope": "disabled",
            }
        return self.get_effective(organization_id, resource_type, resource_id)

    def reset_override(
        self, organization_id: str, resource_type: str, resource_id: str
    ) -> Dict[str, Any]:
        resource_type = self._validate_type(resource_type)
        resource_id = self._validate_id(resource_id)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM organization_resource_overrides "
                "WHERE organization_id=? AND resource_type=? "
                "AND public_resource_id=?",
                (organization_id, resource_type, resource_id),
            )
        return self.get_effective(organization_id, resource_type, resource_id)
