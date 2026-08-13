"""Versioned platform catalog and independent organization agents."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

from src.core.config.loader import (
    AgentPreset,
    AppConfig,
    Capability,
    ModelPricing,
    ModelProfile,
    PluginConfig,
    ProjectConfig,
    ScriptDefinition,
    ScriptParameter,
    ToolConfig,
)
from src.core.config.mcp_headers import (
    delete_headers,
    delete_secret,
    merge_headers,
    save_headers,
    save_secret,
)
from src.core.config.model_api_keys import (
    delete_model_api_key,
    model_api_key_set,
    save_model_api_key,
)
from src.core.modeling import ModelCapabilities
from src.core.storage.organizations import OrganizationStore
from src.core.tooling.definitions import TOOL_DEFINITIONS


RESOURCE_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,127}$")
RESOURCE_TYPES = {
    "agents",
    "models",
    "skills",
    "plugins",
    "mcp",
    "tools",
    "scripts",
    "settings",
    "workflows",
}
RESTART_RESOURCE_TYPES = {"plugins", "scripts"}
# settings/runtime fields that cannot be rebound without a process restart.
RESTART_SETTINGS_FIELDS = ("embedding_model", "rerank_model")
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
    """Raised when a catalog or organization agent operation is invalid."""


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


def _is_secret_field(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return (
        normalized in SECRET_FIELD_PARTS
        or normalized.endswith("_api_key")
        or normalized.endswith("_password")
        or normalized.endswith("_secret")
        or normalized.endswith("_token")
        or normalized.endswith("_credential")
        or normalized.endswith("_headers")
    )


def _strip_secret_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ({} if str(key).lower() == "headers" else "")
            if _is_secret_field(key)
            else _strip_secret_values(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_strip_secret_values(item) for item in value]
    return value


def _reject_secret_fields(value: Any, path: str = "") -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if _is_secret_field(key) and item not in (None, "", {}, []):
            raise ResourceError(
                "平台资源不得包含密钥字段：{}".format(path + str(key))
            )
        if isinstance(item, dict):
            _reject_secret_fields(item, path + str(key) + ".")


def _agent_from_payload(resource_id: str, payload: Dict[str, Any]) -> AgentPreset:
    return AgentPreset(
        id=str(payload.get("id") or resource_id),
        name=str(payload.get("name") or resource_id),
        role=str(payload.get("role") or "assistant"),
        description=str(payload.get("description") or ""),
        system_prompt=str(payload.get("system_prompt") or "你是一个有帮助的助手。"),
        capabilities=[
            Capability(
                name=str(item.get("name") or ""),
                description=str(item.get("description") or ""),
            )
            for item in payload.get("capabilities", [])
            if isinstance(item, dict)
        ],
        enabled=bool(payload.get("enabled", True)),
        image_prompt=payload.get("image_prompt"),
        tools=list(payload.get("tools") or []),
        plugin_tools={
            str(key): list(value)
            for key, value in dict(payload.get("plugin_tools") or {}).items()
        },
        skills=list(payload.get("skills") or []),
        mcp_servers=list(payload.get("mcp_servers") or []),
        datasources=list(payload.get("datasources") or []),
        model=payload.get("model"),
        greeting=payload.get("greeting"),
        greeting_hints=list(payload.get("greeting_hints") or []),
        temperature=payload.get("temperature"),
        max_tokens=payload.get("max_tokens"),
    )


def _model_from_payload(resource_id: str, payload: Dict[str, Any]) -> ModelProfile:
    capabilities = dict(payload.get("capabilities") or {})
    pricing_payload = payload.get("pricing")
    pricing = None
    if isinstance(pricing_payload, dict):
        pricing = ModelPricing(
            input_per_million=str(pricing_payload.get("input_per_million") or "0"),
            output_per_million=str(pricing_payload.get("output_per_million") or "0"),
            cached_input_per_million=pricing_payload.get("cached_input_per_million"),
            reasoning_output_per_million=pricing_payload.get(
                "reasoning_output_per_million"
            ),
        )
    return ModelProfile(
        id=str(payload.get("id") or resource_id),
        enabled=bool(payload.get("enabled", True)),
        type=str(payload.get("type") or "openai_compatible"),
        provider=str(payload.get("provider") or ""),
        base_url=str(payload.get("base_url") or ""),
        model=str(payload.get("model") or ""),
        temperature=float(payload.get("temperature", 0.7)),
        max_tokens=int(payload.get("max_tokens", 2048)),
        timeout_seconds=float(payload.get("timeout_seconds", 120)),
        capabilities=ModelCapabilities(
            tools=bool(capabilities.get("tools", False)),
            vision=bool(capabilities.get("vision", False)),
            reasoning=bool(capabilities.get("reasoning", False)),
        ),
        api_key_env=payload.get("api_key_env"),
        request_extra=dict(payload.get("request_extra") or {}),
        assistant_passthrough_fields=list(
            payload.get("assistant_passthrough_fields") or []
        ),
        billing_currency=str(payload.get("billing_currency") or "CNY"),
        pricing=pricing,
        modality=str(payload.get("modality") or "chat"),
        dimensions=payload.get("dimensions"),
    )


class ScopedResourceStore:
    """Database-backed platform catalog and organization-agent repository.

    The historical class name is retained so existing service wiring does not
    need a second compatibility layer. Platform records are versioned; an
    organization owns only its agent snapshots.
    """

    def __init__(
        self,
        organizations: OrganizationStore,
        config: Optional[ProjectConfig] = None,
    ) -> None:
        self.organizations = organizations
        self.database = organizations.database
        self.bootstrap_config = config
        self._activation_lock = threading.RLock()
        self._activation_handler: Optional[
            Callable[[str, str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]], None]
        ] = None
        if config is not None:
            self.bootstrap_public(config)
            self._activate_pending_on_startup()
            self.ensure_all_organization_agents(config.app.default_agent)

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

    def set_activation_handler(
        self,
        handler: Callable[
            [str, str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]], None
        ],
    ) -> None:
        self._activation_handler = handler

    def _seed_entries(
        self, config: ProjectConfig
    ) -> Dict[str, Iterable[tuple[str, Any]]]:
        return {
            "agents": config.agents.items(),
            "models": config.models.items(),
            "skills": ((str(item.get("id", "")), item) for item in config.skills),
            "plugins": config.plugins.items(),
            "mcp": (
                (str(item.get("id", "")), _strip_secret_values(item))
                for item in config.mcp_servers
            ),
            "scripts": config.scripts.items(),
            "tools": (("platform", config.tools),),
            "settings": (("runtime", config.app),),
        }

    def bootstrap_public(self, config: ProjectConfig) -> None:
        """Import validated file configuration only when a resource is absent."""
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            for resource_type, entries in self._seed_entries(config).items():
                for resource_id, raw_payload in entries:
                    if not resource_id or not RESOURCE_ID_PATTERN.fullmatch(resource_id):
                        continue
                    payload = _strip_secret_values(_json_value(raw_payload))
                    if resource_type == "models":
                        payload["api_key_set"] = model_api_key_set(resource_id)
                    serialized = json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    )
                    existing = connection.execute(
                        "SELECT resource_pk FROM platform_resources "
                        "WHERE resource_type=? AND resource_id=?",
                        (resource_type, resource_id),
                    ).fetchone()
                    if existing is not None:
                        continue
                    cursor = connection.execute(
                        "INSERT INTO platform_resources("
                        "resource_type, resource_id, published_revision, "
                        "active_revision, activation_state, created_at, updated_at"
                        ") VALUES (?, ?, 1, 1, 'active', ?, ?)",
                        (resource_type, resource_id, timestamp, timestamp),
                    )
                    connection.execute(
                        "INSERT INTO platform_resource_versions("
                        "resource_pk, revision, lifecycle, payload_json, source, "
                        "created_at, published_at) VALUES (?, 1, 'published', ?, "
                        "'bootstrap', ?, ?)",
                        (cursor.lastrowid, serialized, timestamp, timestamp),
                    )

    def _activate_pending_on_startup(self) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE platform_resources SET active_revision=published_revision, "
                "activation_state='active', activation_error='', updated_at=? "
                "WHERE published_revision IS NOT NULL "
                "AND (active_revision IS NULL OR active_revision != published_revision)",
                (_now(),),
            )

    @staticmethod
    def _catalog_row(row: Any) -> Dict[str, Any]:
        result = dict(row)
        try:
            result["payload"] = json.loads(str(result.pop("payload_json")))
        except (TypeError, ValueError, json.JSONDecodeError):
            result["payload"] = {}
        result["revision"] = int(result["revision"])
        result["scope"] = "public"
        result["status"] = str(result.pop("lifecycle"))
        result.pop("resource_pk", None)
        if result.get("resource_type") == "models" and isinstance(
            result.get("payload"), dict
        ):
            # Surface a non-secret "configured" flag so the model editor can show
            # a 已配置/未配置 badge without ever echoing the raw key.
            result["payload"]["api_key_set"] = model_api_key_set(
                result["resource_id"]
            )
        return result

    def _resource_at_pointer(
        self, resource_type: str, resource_id: str, pointer: str
    ) -> Dict[str, Any]:
        if pointer not in {"active_revision", "published_revision", "draft_revision"}:
            raise ResourceError("资源版本指针无效")
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT r.resource_type, r.resource_id, r.activation_state, "
                "r.activation_error, v.resource_pk, v.revision, v.lifecycle, "
                "v.payload_json, v.source, v.created_at, v.published_at "
                "FROM platform_resources r JOIN platform_resource_versions v "
                "ON v.resource_pk=r.resource_pk AND v.revision=r.{} "
                "WHERE r.resource_type=? AND r.resource_id=?".format(pointer),
                (resource_type, resource_id),
            ).fetchone()
        if row is None:
            raise ResourceError("平台资源不存在")
        return self._catalog_row(row)

    def get_public(self, resource_type: str, resource_id: str) -> Dict[str, Any]:
        resource_type = self._validate_type(resource_type)
        resource_id = self._validate_id(resource_id)
        return self._resource_at_pointer(resource_type, resource_id, "active_revision")

    def list_public(
        self, resource_type: str, include_unpublished: bool = False
    ) -> List[Dict[str, Any]]:
        resource_type = self._validate_type(resource_type)
        pointer = "published_revision" if include_unpublished else "active_revision"
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT r.resource_type, r.resource_id, r.activation_state, "
                "r.activation_error, v.resource_pk, v.revision, v.lifecycle, "
                "v.payload_json, v.source, v.created_at, v.published_at "
                "FROM platform_resources r JOIN platform_resource_versions v "
                "ON v.resource_pk=r.resource_pk AND v.revision=r.{} "
                "WHERE r.resource_type=? ORDER BY r.resource_id".format(pointer),
                (resource_type,),
            ).fetchall()
        return [self._catalog_row(row) for row in rows]

    def save_draft(
        self,
        resource_type: str,
        resource_id: str,
        payload: Dict[str, Any],
        user_id: int,
    ) -> Dict[str, Any]:
        resource_type = self._validate_type(resource_type)
        resource_id = self._validate_id(resource_id)
        if not isinstance(payload, dict):
            raise ResourceError("资源配置必须是 JSON 对象")
        _reject_secret_fields(payload)
        timestamp = _now()
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction(immediate=True) as connection:
            resource = connection.execute(
                "SELECT resource_pk FROM platform_resources "
                "WHERE resource_type=? AND resource_id=?",
                (resource_type, resource_id),
            ).fetchone()
            if resource is None:
                cursor = connection.execute(
                    "INSERT INTO platform_resources("
                    "resource_type, resource_id, activation_state, created_by, "
                    "updated_by, created_at, updated_at) "
                    "VALUES (?, ?, 'inactive', ?, ?, ?, ?)",
                    (resource_type, resource_id, user_id, user_id, timestamp, timestamp),
                )
                resource_pk = int(cursor.lastrowid)
            else:
                resource_pk = int(resource["resource_pk"])
            revision = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 "
                    "FROM platform_resource_versions WHERE resource_pk=?",
                    (resource_pk,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO platform_resource_versions("
                "resource_pk, revision, lifecycle, payload_json, source, "
                "created_by, created_at) VALUES (?, ?, 'draft', ?, 'database', ?, ?)",
                (resource_pk, revision, serialized, user_id, timestamp),
            )
            connection.execute(
                "UPDATE platform_resources SET draft_revision=?, updated_by=?, "
                "updated_at=? WHERE resource_pk=?",
                (revision, user_id, timestamp, resource_pk),
            )
        return self._resource_at_pointer(resource_type, resource_id, "draft_revision")

    def _validate_platform_payload(
        self, resource_type: str, resource_id: str, payload: Dict[str, Any]
    ) -> None:
        _reject_secret_fields(payload)
        if resource_type == "agents":
            self._validate_agent_payload(payload)
            if payload.get("enabled") is False and resource_id == str(
                getattr(self.bootstrap_config.app, "default_agent", "") or ""
            ):
                raise ResourceError("不能禁用默认智能体")
        elif resource_type == "models":
            adapter_type = str(payload.get("type") or "").strip().lower()
            modality = str(payload.get("modality") or "chat").strip().lower()
            if adapter_type == "local_transformers":
                if modality != "rerank":
                    raise ResourceError("local_transformers 仅支持重排模型")
            else:
                base_url = str(payload.get("base_url") or "")
                if base_url:
                    parsed = urlparse(base_url)
                    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
                    if parsed.scheme.lower() != "https" and not (
                        parsed.scheme.lower() == "http" and loopback
                    ):
                        raise ResourceError("模型地址必须使用 HTTPS；仅本机回环可使用 HTTP")
        elif resource_type == "mcp":
            transport = str(payload.get("transport") or "stdio").lower()
            if transport in {"sse", "streamablehttp"}:
                url = urlparse(str(payload.get("url") or ""))
                if url.scheme.lower() != "https" or not url.netloc:
                    raise ResourceError("远程 MCP 地址必须使用 HTTPS")
        elif resource_type == "workflows":
            from src.core.workflows.definition import (
                WorkflowValidationError,
                validate_definition,
            )

            try:
                validate_definition(payload)
            except WorkflowValidationError as exc:
                raise ResourceError(str(exc)) from exc
        if payload.get("enabled") is False:
            # Only block when an *enabled* record is being switched off. A
            # referenced model that is already disabled must still be editable
            # (e.g. re-saving its config without changing the enabled flag).
            existing_enabled = True
            try:
                existing = self.get_public(resource_type, resource_id)
                existing_enabled = (existing or {}).get("payload", {}).get("enabled", True)
            except ResourceError:
                existing_enabled = True
            if existing_enabled is not False:
                self._assert_not_referenced(resource_type, resource_id)

    def _assert_not_referenced(self, resource_type: str, resource_id: str) -> None:
        references = 0
        if resource_type == "models":
            try:
                settings = self.get_public("settings", "runtime")["payload"]
                if resource_id in {
                    settings.get("active_model"),
                    settings.get("fallback_model"),
                    settings.get("local_model"),
                    settings.get("flash_model"),
                    settings.get("pro_model"),
                    settings.get("vision_model"),
                    settings.get("embedding_model"),
                    settings.get("rerank_model"),
                }:
                    references += 1
            except ResourceError:
                pass
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM organization_agents"
            ).fetchall()
            schedule_rows = connection.execute(
                "SELECT action_json FROM organization_schedules"
            ).fetchall()
        for row in rows:
            try:
                agent = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            used = (
                resource_type == "models" and agent.get("model") == resource_id
                or resource_type == "skills" and resource_id in (agent.get("skills") or [])
                or resource_type == "mcp" and resource_id in (agent.get("mcp_servers") or [])
                or resource_type == "plugins" and resource_id in (agent.get("plugin_tools") or {})
                or resource_type == "tools" and bool(agent.get("tools"))
            )
            if used:
                references += 1
        if resource_type == "scripts":
            for row in schedule_rows:
                try:
                    action = json.loads(str(row["action_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if action.get("script_id") == resource_id:
                    references += 1
        if references:
            raise ResourceError(
                "该能力仍被 {} 个组织配置引用，请先完成迁移".format(references)
            )

    def _requires_restart(
        self,
        resource_type: str,
        payload: Dict[str, Any],
        previous: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if resource_type == "settings":
            # First seed has no active revision yet; treating it as
            # restart-required would leave active_revision NULL and break
            # build_project_config on a cold start.
            if previous is None:
                return False
            return any(
                str(payload.get(key) or "") != str(previous.get(key) or "")
                for key in RESTART_SETTINGS_FIELDS
            )
        if resource_type in RESTART_RESOURCE_TYPES:
            return True
        return resource_type == "models" and str(payload.get("modality")) in {
            "embedding", "rerank"
        }

    def publish(
        self,
        resource_type: str,
        resource_id: str,
        user_id: int,
        *,
        revision: Optional[int] = None,
        lifecycle: str = "published",
    ) -> Dict[str, Any]:
        resource_type = self._validate_type(resource_type)
        resource_id = self._validate_id(resource_id)
        if lifecycle not in {"published", "deprecated"}:
            raise ResourceError("发布状态无效")
        with self._activation_lock:
            with self.database.read() as connection:
                resource = connection.execute(
                    "SELECT * FROM platform_resources WHERE resource_type=? "
                    "AND resource_id=?",
                    (resource_type, resource_id),
                ).fetchone()
                if resource is None:
                    raise ResourceError("平台资源不存在")
                selected = revision or resource["draft_revision"]
                version = connection.execute(
                    "SELECT * FROM platform_resource_versions WHERE resource_pk=? "
                    "AND revision=?",
                    (resource["resource_pk"], selected),
                ).fetchone()
            if version is None:
                raise ResourceError("待发布版本不存在")
            payload = json.loads(str(version["payload_json"]))
            self._validate_platform_payload(resource_type, resource_id, payload)
            previous = None
            try:
                previous = self.get_public(resource_type, resource_id)["payload"]
            except ResourceError:
                pass
            restart_required = self._requires_restart(
                resource_type, payload, previous
            )
            if not restart_required and self._activation_handler is not None:
                try:
                    self._activation_handler(
                        resource_type, resource_id, payload, previous
                    )
                except Exception as exc:
                    with self.database.transaction(immediate=True) as connection:
                        connection.execute(
                            "UPDATE platform_resources SET activation_state='failed', "
                            "activation_error=?, updated_at=? WHERE resource_pk=?",
                            (str(exc)[:500], _now(), resource["resource_pk"]),
                        )
                    raise ResourceError("运行时应用失败：{}".format(exc)) from exc
            timestamp = _now()
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE platform_resource_versions SET lifecycle=?, "
                    "published_by=?, published_at=? WHERE resource_pk=? AND revision=?",
                    (lifecycle, user_id, timestamp, resource["resource_pk"], selected),
                )
                connection.execute(
                    "UPDATE platform_resources SET draft_revision=NULL, "
                    "published_revision=?, active_revision=CASE WHEN ? THEN "
                    "active_revision ELSE ? END, activation_state=?, activation_error='', "
                    "updated_by=?, updated_at=? WHERE resource_pk=?",
                    (
                        selected,
                        1 if restart_required else 0,
                        selected,
                        "restart_required" if restart_required else "active",
                        user_id,
                        timestamp,
                        resource["resource_pk"],
                    ),
                )
            return self.activation(resource_type, resource_id)

    def rollback(
        self,
        resource_type: str,
        resource_id: str,
        revision: int,
        user_id: int,
    ) -> Dict[str, Any]:
        return self.publish(
            resource_type,
            resource_id,
            user_id,
            revision=revision,
        )

    def activation(self, resource_type: str, resource_id: str) -> Dict[str, Any]:
        resource_type = self._validate_type(resource_type)
        resource_id = self._validate_id(resource_id)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT resource_type, resource_id, draft_revision, "
                "published_revision, active_revision, activation_state, "
                "activation_error, updated_at FROM platform_resources "
                "WHERE resource_type=? AND resource_id=?",
                (resource_type, resource_id),
            ).fetchone()
        if row is None:
            raise ResourceError("平台资源不存在")
        return dict(row)

    def upsert_public(
        self,
        resource_type: str,
        resource_id: str,
        payload: Dict[str, Any],
        user_id: int,
        status: str = "published",
    ) -> Dict[str, Any]:
        """Save and activate one public resource in a single operation."""
        if resource_type == "mcp":
            # MCP request headers carry secrets.  Persist them in the keychain
            # (never the catalog DB) and store an empty shell, mirroring the
            # legacy /api/mcp behaviour.  The runtime merges them back via
            # merge_headers at read time.
            headers = payload.get("headers")
            if isinstance(headers, dict) and headers:
                save_headers(resource_id, headers)
                payload = {**payload, "headers": {}}
            # A token_provider may embed an app secret (e.g. Feishu app_secret
            # for TAT auto-refresh).  Route it to the keychain too and strip it
            # from the persisted payload so it never lands in the catalog DB.
            provider = payload.get("token_provider")
            if isinstance(provider, dict):
                app_secret = provider.get("app_secret")
                if isinstance(app_secret, str) and app_secret:
                    save_secret(resource_id, app_secret)
                provider = {
                    k: v for k, v in provider.items() if k != "app_secret"
                }
                payload = {**payload, "token_provider": provider}
        elif resource_type == "models":
            # Model API keys are entered on the page and must never be persisted
            # to the catalog DB.  Stash the raw secret in the keychain (keyed by
            # the model id) and store an empty shell; the runtime resolves it via
            # get_model_api_key(profile.id).  A null/empty value means "leave the
            # existing key untouched".
            api_key = payload.get("api_key")
            if isinstance(api_key, str) and api_key:
                save_model_api_key(resource_id, api_key)
                payload = {**payload, "api_key": ""}
            payload = {**payload, "api_key_set": model_api_key_set(resource_id)}
        draft = self.save_draft(resource_type, resource_id, payload, user_id)
        activation = self.publish(
            resource_type,
            resource_id,
            user_id,
            revision=int(draft["revision"]),
            lifecycle="deprecated" if status == "deprecated" else "published",
        )
        try:
            return self.get_public(resource_type, resource_id)
        except ResourceError:
            if activation.get("activation_state") != "restart_required":
                raise
            return {
                **activation,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "revision": int(draft["revision"]),
                "status": status,
                "payload": dict(payload),
            }

    def delete_public(self, resource_type: str, resource_id: str) -> None:
        """Remove a platform resource after checking its live references.

        Resource revisions are an implementation detail of the catalog.  The
        management UI uses direct CRUD, so deletion must remove the resource
        and every retained revision atomically rather than creating a
        user-visible "deprecated" draft.
        """
        resource_type = self._validate_type(resource_type)
        resource_id = self._validate_id(resource_id)
        if (
            resource_type == "agents"
            and self.bootstrap_config is not None
            and resource_id == self.bootstrap_config.app.default_agent
        ):
            raise ResourceError("不能删除默认智能体模板")
        self._assert_not_referenced(resource_type, resource_id)
        with self._activation_lock:
            try:
                previous = self.get_public(resource_type, resource_id)["payload"]
            except ResourceError as exc:
                raise ResourceError("平台资源不存在") from exc
            if self._activation_handler is not None:
                try:
                    self._activation_handler(resource_type, resource_id, None, previous)
                except Exception as exc:
                    raise ResourceError("运行时应用失败：{}".format(exc)) from exc
            if resource_type == "mcp":
                delete_headers(resource_id)
                delete_secret(resource_id)
            elif resource_type == "models":
                delete_model_api_key(resource_id)
            with self.database.transaction(immediate=True) as connection:
                deleted = connection.execute(
                    "DELETE FROM platform_resources WHERE resource_type=? AND resource_id=?",
                    (resource_type, resource_id),
                )
            if deleted.rowcount == 0:
                raise ResourceError("平台资源不存在")

    def ensure_all_organization_agents(self, default_template_id: str) -> None:
        for organization in self.organizations.list_organizations():
            self.ensure_organization_agents(
                str(organization["organization_id"]), default_template_id
            )

    def _insert_agent_row(
        self,
        connection,
        organization_id: str,
        agent_id: str,
        payload: Dict[str, Any],
        *,
        template_resource_id: Optional[str],
        template_revision: Optional[int],
        actor_user_id: Optional[int],
        timestamp: str,
    ) -> None:
        payload = dict(payload)
        payload["id"] = agent_id
        enabled = bool(payload.get("enabled", True))
        connection.execute(
            "INSERT OR IGNORE INTO organization_agents("
            "organization_id, agent_id, revision, enabled, payload_json, "
            "template_resource_id, template_revision, created_by, updated_by, "
            "created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                organization_id,
                agent_id,
                1 if enabled else 0,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                template_resource_id,
                template_revision,
                actor_user_id,
                actor_user_id,
                timestamp,
                timestamp,
            ),
        )

    def ensure_organization_agents(
        self, organization_id: str, default_template_id: str = ""
    ) -> None:
        self.organizations.get(organization_id)
        with self.database.read() as connection:
            exists = connection.execute(
                "SELECT 1 FROM organization_agents WHERE organization_id=? LIMIT 1",
                (organization_id,),
            ).fetchone()
        if exists is not None:
            return
        templates = self.list_public("agents")
        if not templates:
            return
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            for template in templates:
                payload = dict(template["payload"])
                self._insert_agent_row(
                    connection,
                    organization_id,
                    str(template["resource_id"]),
                    payload,
                    template_resource_id=str(template["resource_id"]),
                    template_revision=int(template["revision"]),
                    actor_user_id=None,
                    timestamp=timestamp,
                )
            default_id = default_template_id
            if default_id not in {str(item["resource_id"]) for item in templates}:
                default_id = str(templates[0]["resource_id"])
            connection.execute(
                "INSERT OR IGNORE INTO organization_agent_settings("
                "organization_id, default_agent_id, updated_at) VALUES (?, ?, ?)",
                (organization_id, default_id, timestamp),
            )

    @staticmethod
    def _organization_agent_row(row: Any) -> Dict[str, Any]:
        payload = json.loads(str(row["payload_json"]))
        payload["enabled"] = bool(row["enabled"])
        return {
            "resource_type": "agents",
            "resource_id": str(row["agent_id"]),
            "scope": "organization",
            "organization_id": str(row["organization_id"]),
            "revision": int(row["revision"]),
            "status": "published" if bool(row["enabled"]) else "disabled",
            "payload": payload,
            "effective_scope": "organization",
            "base_resource_id": row["template_resource_id"],
            "template_revision": row["template_revision"],
        }

    def list_organization_agents(self, organization_id: str) -> List[Dict[str, Any]]:
        default_id = ""
        if self.bootstrap_config is not None:
            default_id = self.bootstrap_config.app.default_agent
        self.ensure_organization_agents(organization_id, default_id)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM organization_agents WHERE organization_id=? "
                "ORDER BY agent_id",
                (organization_id,),
            ).fetchall()
        return [self._organization_agent_row(row) for row in rows]

    def list_effective(
        self, organization_id: str, resource_type: str
    ) -> List[Dict[str, Any]]:
        self.organizations.get(organization_id)
        resource_type = self._validate_type(resource_type)
        if resource_type == "agents":
            return [
                item
                for item in self.list_organization_agents(organization_id)
                if item["payload"].get("enabled", True)
            ]
        return [
            {**item, "effective_scope": "public", "organization_id": organization_id}
            for item in self.list_public(resource_type)
        ]

    def get_effective(
        self, organization_id: str, resource_type: str, resource_id: str
    ) -> Dict[str, Any]:
        resource_id = self._validate_id(resource_id)
        if resource_type == "agents":
            for item in self.list_organization_agents(organization_id):
                if item["resource_id"] == resource_id:
                    return item
            raise ResourceError("组织智能体不存在")
        item = self.get_public(resource_type, resource_id)
        return {**item, "effective_scope": "public", "organization_id": organization_id}

    def _validate_agent_payload(self, payload: Dict[str, Any]) -> None:
        if not str(payload.get("name") or "").strip():
            raise ResourceError("智能体名称不能为空")
        model_id = str(payload.get("model") or "")
        if model_id:
            model = self.get_public("models", model_id)
            if model["status"] != "published" or not bool(
                model["payload"].get("enabled", True)
            ):
                raise ResourceError("智能体引用的模型不可用")
        for resource_type, values in (
            ("skills", payload.get("skills") or []),
            ("mcp", payload.get("mcp_servers") or []),
        ):
            if not isinstance(values, list):
                raise ResourceError("智能体能力绑定必须是数组")
            for resource_id in values:
                item = self.get_public(resource_type, str(resource_id))
                if item["status"] != "published":
                    raise ResourceError("智能体引用了未发布的底层能力")
        tools = payload.get("tools") or []
        if not isinstance(tools, list):
            raise ResourceError("内置工具绑定必须是数组")
        unknown_tools = sorted(
            str(name) for name in tools if str(name) not in TOOL_DEFINITIONS
        )
        if unknown_tools:
            raise ResourceError(
                "智能体引用了不存在的内置工具：{}".format("、".join(unknown_tools))
            )
        datasources = payload.get("datasources") or []
        if not isinstance(datasources, list):
            raise ResourceError("数据源绑定必须是数组")
        if datasources:
            available = {
                entry.get("id"): entry
                for entry in (getattr(self.bootstrap_config, "datasources", []) or [])
                if isinstance(entry, dict) and entry.get("id")
            }
            seen: set = set()
            for raw_id in datasources:
                ds_id = str(raw_id or "").strip()
                if not ds_id:
                    raise ResourceError("数据源 ID 不能为空")
                if ds_id in seen:
                    raise ResourceError("数据源绑定不能重复：{}".format(ds_id))
                seen.add(ds_id)
                entry = available.get(ds_id)
                if entry is None:
                    raise ResourceError("智能体引用了不存在的数据源：{}".format(ds_id))
                if not entry.get("enabled", True):
                    raise ResourceError(
                        "智能体引用的数据源已停用：{}".format(
                            entry.get("name") or ds_id
                        )
                    )
        plugin_tools = payload.get("plugin_tools") or {}
        if not isinstance(plugin_tools, dict):
            raise ResourceError("插件工具绑定必须是 JSON 对象")
        for plugin_id, names in plugin_tools.items():
            item = self.get_public("plugins", str(plugin_id))
            if item["status"] != "published" or not isinstance(names, list):
                raise ResourceError("智能体引用的插件工具无效")
            config = self.bootstrap_config.plugins.get(str(plugin_id))
            if config is None or not config.enabled:
                raise ResourceError("智能体引用的插件未启用")
            from src.core.plugins.registry import default_catalog

            manifest = default_catalog().get(str(plugin_id))
            if manifest is None or any(str(name) not in manifest.tools for name in names):
                raise ResourceError("智能体引用了不存在的插件工具")

    def upsert_organization(
        self,
        organization_id: str,
        resource_type: str,
        resource_id: str,
        payload: Dict[str, Any],
        user_id: int,
        base_resource_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self._validate_type(resource_type) != "agents":
            raise ResourceError("组织只能创建智能体")
        resource_id = self._validate_id(resource_id)
        self.organizations.get(organization_id)
        payload = dict(payload)
        payload["id"] = resource_id
        self._validate_agent_payload(payload)
        template_revision = None
        if base_resource_id:
            template = self.get_public("agents", base_resource_id)
            template_revision = int(template["revision"])
        timestamp = _now()
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO organization_agents("
                "organization_id, agent_id, revision, enabled, payload_json, "
                "template_resource_id, template_revision, created_by, updated_by, "
                "created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(organization_id, agent_id) DO UPDATE SET "
                "revision=organization_agents.revision+1, enabled=excluded.enabled, "
                "payload_json=excluded.payload_json, updated_by=excluded.updated_by, "
                "updated_at=excluded.updated_at",
                (
                    organization_id,
                    resource_id,
                    1 if bool(payload.get("enabled", True)) else 0,
                    serialized,
                    base_resource_id,
                    template_revision,
                    user_id,
                    user_id,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_effective(organization_id, "agents", resource_id)

    def copy_template(
        self,
        organization_id: str,
        template_id: str,
        new_id: str,
        user_id: int,
        name: str = "",
    ) -> Dict[str, Any]:
        template = self.get_public("agents", template_id)
        payload = json.loads(json.dumps(template["payload"], ensure_ascii=False))
        payload["id"] = new_id
        if name:
            payload["name"] = name
        return self.upsert_organization(
            organization_id,
            "agents",
            new_id,
            payload,
            user_id,
            template_id,
        )

    def set_organization_agent_enabled(
        self, organization_id: str, agent_id: str, enabled: bool, user_id: int
    ) -> Dict[str, Any]:
        current = self.get_effective(organization_id, "agents", agent_id)
        payload = dict(current["payload"])
        payload["enabled"] = enabled
        return self.upsert_organization(
            organization_id,
            "agents",
            agent_id,
            payload,
            user_id,
            current.get("base_resource_id"),
        )

    def delete_organization(
        self, organization_id: str, resource_type: str, resource_id: str
    ) -> None:
        if self._validate_type(resource_type) != "agents":
            raise ResourceError("组织只能删除自有智能体")
        with self.database.transaction(immediate=True) as connection:
            deleted = connection.execute(
                "DELETE FROM organization_agents WHERE organization_id=? "
                "AND agent_id=?",
                (organization_id, resource_id),
            )
        if deleted.rowcount == 0:
            raise ResourceError("组织智能体不存在")

    def effective_agent_presets(self, organization_id: str) -> Dict[str, AgentPreset]:
        return {
            str(item["resource_id"]): _agent_from_payload(
                str(item["resource_id"]), item["payload"]
            )
            for item in self.list_effective(organization_id, "agents")
        }

    def effective_skills(self, organization_id: str) -> List[Dict[str, Any]]:
        return [
            item["payload"]
            for item in self.list_effective(organization_id, "skills")
            if bool(item["payload"].get("enabled", True))
        ]

    def build_project_config(self, fallback: ProjectConfig) -> ProjectConfig:
        """Create a new immutable runtime snapshot from active DB revisions."""
        models = {
            item["resource_id"]: _model_from_payload(
                item["resource_id"], item["payload"]
            )
            for item in self.list_public("models")
        }
        agents = {
            item["resource_id"]: _agent_from_payload(
                item["resource_id"], item["payload"]
            )
            for item in self.list_public("agents")
        }
        plugins = {
            item["resource_id"]: PluginConfig(
                id=item["resource_id"],
                enabled=bool(item["payload"].get("enabled", True)),
                settings=dict(item["payload"].get("settings") or {}),
            )
            for item in self.list_public("plugins")
        }
        scripts: Dict[str, ScriptDefinition] = {}
        for item in self.list_public("scripts"):
            payload = dict(item["payload"])
            parameters = {
                str(key): ScriptParameter(**dict(value))
                for key, value in dict(payload.pop("parameters", {}) or {}).items()
            }
            payload["id"] = item["resource_id"]
            scripts[item["resource_id"]] = ScriptDefinition(
                **{**payload, "parameters": parameters}
            )
        tools_item = self.get_public("tools", "platform")
        settings_item = self.get_public("settings", "runtime")
        tools = ToolConfig(**dict(tools_item["payload"]))
        app = AppConfig(**dict(settings_item["payload"]))
        skills = [item["payload"] for item in self.list_public("skills")]
        mcp_servers = merge_headers(
            [item["payload"] for item in self.list_public("mcp")]
        )
        return replace(
            fallback,
            app=app,
            models=models,
            tools=tools,
            plugins=plugins,
            agents=agents,
            scripts=scripts,
            skills=skills,
            mcp_servers=mcp_servers,
        )
