"""SQLite persistence for workflow definitions, releases, runs and waits."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from .definition import empty_definition, validate_definition


class WorkflowError(ValueError):
    """Raised when a workflow operation is invalid or unauthorized."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_LOG_SECRET_KEYS = {
    "authorization", "cookie", "password", "secret", "token", "api_key", "apikey",
}


def _redact_log_value(value: Any, depth: int = 0) -> Any:
    """Redact common secret fields and bound log nesting."""
    if depth >= 12:
        return "[内容层级过深，已截断]"
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _LOG_SECRET_KEYS):
                result[str(key)] = "[已脱敏]"
            else:
                result[str(key)] = _redact_log_value(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_redact_log_value(item, depth + 1) for item in value[:1000]]
    if isinstance(value, str) and len(value) > 65536:
        return value[:65536] + "[已截断]"
    return value


def redact_workflow_value(value: Any) -> Any:
    """Return a bounded copy suitable for workflow audit API responses."""
    return _redact_log_value(value)


def _log_dump(value: Any, max_bytes: int = 256 * 1024) -> str:
    redacted = _redact_log_value(value)
    serialized = _dump(redacted)
    if len(serialized.encode("utf-8")) <= max_bytes:
        return serialized
    return _dump({"truncated": True, "preview": serialized[: max_bytes // 2]})


class WorkflowStore:
    def __init__(self, organization_store: Any, resource_store: Any = None) -> None:
        self.organizations = organization_store
        self.database = organization_store.database
        self.resources = resource_store

    @staticmethod
    def _workflow_row(row: Any, include_draft: bool = False) -> Dict[str, Any]:
        result = {
            "workflow_id": str(row["workflow_id"]),
            "organization_id": str(row["organization_id"]),
            "id": str(row["workflow_key"]),
            "name": str(row["name"]),
            "description": str(row["description"]),
            "status": str(row["status"]),
            "draft_revision": int(row["draft_revision"]),
            "published_version": (
                int(row["published_version"])
                if row["published_version"] is not None
                else None
            ),
            "template_resource_id": row["template_resource_id"],
            "template_revision": row["template_revision"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        if include_draft:
            result["definition"] = _load(row["draft_json"], {})
        return result

    def list_workflows(self, organization_id: str, include_archived: bool = False) -> List[Dict[str, Any]]:
        self.organizations.get(organization_id)
        where = "organization_id=?"
        if not include_archived:
            where += " AND status<>'archived'"
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM organization_workflows WHERE " + where + " ORDER BY updated_at DESC",
                (organization_id,),
            ).fetchall()
        return [self._workflow_row(row) for row in rows]

    def get_workflow(self, organization_id: str, workflow_id: str, include_draft: bool = True) -> Dict[str, Any]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM organization_workflows WHERE organization_id=? AND workflow_id=?",
                (organization_id, workflow_id),
            ).fetchone()
        if row is None:
            raise WorkflowError("工作流不存在")
        return self._workflow_row(row, include_draft=include_draft)

    def create_workflow(
        self,
        organization_id: str,
        workflow_key: str,
        name: str,
        actor_user_id: int,
        definition: Optional[Mapping[str, Any]] = None,
        *,
        template_resource_id: Optional[str] = None,
        template_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.organizations.get(organization_id)
        workflow_key = str(workflow_key or "").strip()
        if not workflow_key or len(workflow_key) > 128:
            raise WorkflowError("工作流 ID 不能为空且不能超过 128 字")
        normalized = validate_definition(definition or empty_definition(name))
        workflow_id, timestamp = str(uuid.uuid4()), _now()
        try:
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO organization_workflows("
                    "workflow_id, organization_id, workflow_key, name, description, status, "
                    "draft_json, draft_revision, template_resource_id, template_revision, "
                    "created_by, updated_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'draft', ?, 1, ?, ?, ?, ?, ?, ?)",
                    (
                        workflow_id,
                        organization_id,
                        workflow_key,
                        normalized["name"],
                        normalized["description"],
                        _dump(normalized),
                        template_resource_id,
                        template_revision,
                        actor_user_id,
                        actor_user_id,
                        timestamp,
                        timestamp,
                    ),
                )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise WorkflowError("工作流 ID 已存在") from exc
            raise
        return self.get_workflow(organization_id, workflow_id)

    def save_draft(
        self,
        organization_id: str,
        workflow_id: str,
        definition: Mapping[str, Any],
        base_revision: int,
        actor_user_id: int,
    ) -> Dict[str, Any]:
        normalized = validate_definition(definition, allow_incomplete=True)
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE organization_workflows SET draft_json=?, draft_revision=draft_revision+1, "
                "name=?, description=?, updated_by=?, updated_at=? "
                "WHERE organization_id=? AND workflow_id=? AND draft_revision=? AND status<>'archived'",
                (
                    _dump(normalized),
                    normalized["name"],
                    normalized["description"],
                    actor_user_id,
                    timestamp,
                    organization_id,
                    workflow_id,
                    int(base_revision),
                ),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT draft_revision FROM organization_workflows WHERE organization_id=? AND workflow_id=?",
                    (organization_id, workflow_id),
                ).fetchone()
                if row is None:
                    raise WorkflowError("工作流不存在")
                raise WorkflowError("工作流草稿已被其他成员更新，请刷新后重试")
        return self.get_workflow(organization_id, workflow_id)

    def _dependencies(
        self,
        organization_id: str,
        definition: Mapping[str, Any],
        root_workflow_id: str,
    ) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for node in definition.get("nodes", []):
            if node.get("type") not in {"subworkflow", "for_each"}:
                continue
            target = str((node.get("config") or {}).get("workflow_id") or "")
            if not target:
                raise WorkflowError("子工作流节点缺少 workflow_id")
            child = self.get_workflow(organization_id, target, include_draft=False)
            version = child.get("published_version")
            if child.get("status") != "published" or version is None:
                raise WorkflowError("子工作流 {} 尚未发布".format(target))
            result[target] = int(version)
        self._assert_no_dependency_cycle(organization_id, root_workflow_id, result)
        return result

    def dependencies_for_definition(
        self,
        organization_id: str,
        workflow_id: str,
        definition: Mapping[str, Any],
    ) -> Dict[str, int]:
        """Resolve published child versions for validation and draft test runs."""
        return self._dependencies(organization_id, definition, workflow_id)

    def _assert_no_dependency_cycle(
        self,
        organization_id: str,
        root_workflow_id: str,
        dependencies: Mapping[str, int],
    ) -> None:
        visiting, visited = {root_workflow_id}, set()

        def visit(workflow_id: str, depth: int) -> None:
            if depth > 5:
                raise WorkflowError("子工作流调用深度不能超过 5")
            if workflow_id in visiting:
                raise WorkflowError("子工作流不能形成递归调用")
            if workflow_id in visited:
                return
            visiting.add(workflow_id)
            version = dependencies.get(workflow_id)
            if version is not None:
                data = self.get_version(organization_id, workflow_id, int(version))
                for child_id in data.get("dependencies", {}):
                    visit(str(child_id), depth + 1)
            visiting.remove(workflow_id)
            visited.add(workflow_id)

        for workflow_id in dependencies:
            visit(workflow_id, 1)
        visiting.remove(root_workflow_id)

    def publish(self, organization_id: str, workflow_id: str, actor_user_id: int) -> Dict[str, Any]:
        item = self.get_workflow(organization_id, workflow_id)
        definition = validate_definition(item["definition"])
        dependencies = self._dependencies(organization_id, definition, workflow_id)
        payload = _dump(definition)
        digest = _hash(payload)
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM organization_workflow_versions WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            version = int(row[0]) + 1
            connection.execute(
                "INSERT INTO organization_workflow_versions("
                "workflow_id, version, definition_json, definition_hash, dependency_json, published_by, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (workflow_id, version, payload, digest, _dump(dependencies), actor_user_id, timestamp),
            )
            connection.execute(
                "UPDATE organization_workflows SET status='published', published_version=?, updated_by=?, updated_at=? "
                "WHERE organization_id=? AND workflow_id=?",
                (version, actor_user_id, timestamp, organization_id, workflow_id),
            )
            existing = {
                str(row["trigger_key"]): row
                for row in connection.execute(
                    "SELECT * FROM workflow_trigger_bindings WHERE workflow_id=?",
                    (workflow_id,),
                ).fetchall()
            }
            active_keys = set()
            for trigger in definition["triggers"]:
                key, trigger_type = trigger["id"], trigger["type"]
                active_keys.add(key)
                previous = existing.get(key)
                trigger_id = str(previous["trigger_id"]) if previous is not None else str(uuid.uuid4())
                secret_hash = str(previous["secret_hash"]) if previous is not None else ""
                enabled = bool(previous["enabled"]) if previous is not None else trigger_type in {"manual", "schedule"}
                connection.execute(
                    "INSERT INTO workflow_trigger_bindings("
                    "trigger_id, workflow_id, organization_id, trigger_key, trigger_type, config_json, "
                    "published_version, enabled, secret_hash, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(workflow_id, trigger_key) DO UPDATE SET "
                    "trigger_type=excluded.trigger_type, config_json=excluded.config_json, "
                    "published_version=excluded.published_version, enabled=excluded.enabled, "
                    "updated_at=excluded.updated_at",
                    (
                        trigger_id,
                        workflow_id,
                        organization_id,
                        key,
                        trigger_type,
                        _dump(trigger["config"]),
                        version,
                        int(enabled),
                        secret_hash,
                        timestamp,
                        timestamp,
                    ),
                )
            for key in set(existing) - active_keys:
                connection.execute(
                    "UPDATE workflow_trigger_bindings SET enabled=0, updated_at=? "
                    "WHERE workflow_id=? AND trigger_key=?",
                    (timestamp, workflow_id, key),
                )
        return self.get_workflow(organization_id, workflow_id)

    def unpublish(self, organization_id: str, workflow_id: str, actor_user_id: int) -> Dict[str, Any]:
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE organization_workflows SET status='disabled', updated_by=?, updated_at=? "
                "WHERE organization_id=? AND workflow_id=? AND published_version IS NOT NULL",
                (actor_user_id, timestamp, organization_id, workflow_id),
            )
            if cursor.rowcount != 1:
                raise WorkflowError("工作流不存在或尚未发布")
            connection.execute(
                "UPDATE workflow_trigger_bindings SET enabled=0, updated_at=? "
                "WHERE workflow_id=?",
                (timestamp, workflow_id),
            )
        return self.get_workflow(organization_id, workflow_id)

    def archive(self, organization_id: str, workflow_id: str, actor_user_id: int) -> Dict[str, Any]:
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            active = connection.execute(
                "SELECT COUNT(*) FROM workflow_runs WHERE workflow_id=? AND status IN ('queued','running','waiting')",
                (workflow_id,),
            ).fetchone()[0]
            if active:
                raise WorkflowError("工作流仍有运行中任务，不能归档")
            cursor = connection.execute(
                "UPDATE organization_workflows SET status='archived', updated_by=?, updated_at=? "
                "WHERE organization_id=? AND workflow_id=?",
                (actor_user_id, timestamp, organization_id, workflow_id),
            )
            if cursor.rowcount != 1:
                raise WorkflowError("工作流不存在")
            connection.execute(
                "UPDATE workflow_trigger_bindings SET enabled=0, updated_at=? "
                "WHERE workflow_id=?",
                (timestamp, workflow_id),
            )
        return self.get_workflow(organization_id, workflow_id)

    def list_versions(self, organization_id: str, workflow_id: str) -> List[Dict[str, Any]]:
        self.get_workflow(organization_id, workflow_id, include_draft=False)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT version, definition_hash, dependency_json, published_by, published_at "
                "FROM organization_workflow_versions WHERE workflow_id=? ORDER BY version DESC",
                (workflow_id,),
            ).fetchall()
        return [{
            "version": int(row["version"]),
            "definition_hash": str(row["definition_hash"]),
            "dependencies": _load(row["dependency_json"], {}),
            "published_by": row["published_by"],
            "published_at": str(row["published_at"]),
        } for row in rows]

    def get_version(self, organization_id: str, workflow_id: str, version: int) -> Dict[str, Any]:
        self.get_workflow(organization_id, workflow_id, include_draft=False)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM organization_workflow_versions WHERE workflow_id=? AND version=?",
                (workflow_id, int(version)),
            ).fetchone()
        if row is None:
            raise WorkflowError("工作流版本不存在")
        return {
            "workflow_id": workflow_id,
            "version": int(row["version"]),
            "definition": _load(row["definition_json"], {}),
            "definition_hash": str(row["definition_hash"]),
            "dependencies": _load(row["dependency_json"], {}),
            "published_at": str(row["published_at"]),
        }

    def rollback(self, organization_id: str, workflow_id: str, version: int, actor_user_id: int) -> Dict[str, Any]:
        snapshot = self.get_version(organization_id, workflow_id, version)
        current = self.get_workflow(organization_id, workflow_id)
        self.save_draft(
            organization_id,
            workflow_id,
            snapshot["definition"],
            current["draft_revision"],
            actor_user_id,
        )
        return self.publish(organization_id, workflow_id, actor_user_id)

    def copy_platform_template(
        self,
        organization_id: str,
        template_id: str,
        workflow_key: str,
        actor_user_id: int,
        name: str = "",
    ) -> Dict[str, Any]:
        if self.resources is None:
            raise WorkflowError("平台工作流模板服务不可用")
        self.organizations.get(organization_id)
        templates: Dict[str, Dict[str, Any]] = {}
        visiting: List[str] = []

        def collect(resource_id: str, depth: int) -> None:
            if depth > 5:
                raise WorkflowError("平台模板子工作流深度不能超过 5")
            if resource_id in visiting:
                raise WorkflowError("平台模板子工作流不能形成循环依赖")
            if resource_id in templates:
                return
            visiting.append(resource_id)
            try:
                resource = self.resources.get_public("workflows", resource_id)
            except Exception as exc:
                raise WorkflowError("平台子工作流模板不存在：{}".format(resource_id)) from exc
            definition = validate_definition(resource.get("payload") or {})
            templates[resource_id] = {
                "definition": definition,
                "revision": int(resource.get("revision") or 1),
            }
            for node in definition["nodes"]:
                if node["type"] not in {"subworkflow", "for_each"}:
                    continue
                child_id = str(node["config"].get("workflow_id") or "")
                if not child_id:
                    raise WorkflowError("平台子工作流节点缺少 workflow_id")
                collect(child_id, depth + 1)
            visiting.pop()

        collect(template_id, 1)
        workflow_key = str(workflow_key or "").strip()
        if not workflow_key or len(workflow_key) > 128:
            raise WorkflowError("工作流 ID 不能为空且不能超过 128 字")
        workflow_ids = {resource_id: str(uuid.uuid4()) for resource_id in templates}
        definitions: Dict[str, Dict[str, Any]] = {}
        dependency_maps: Dict[str, Dict[str, int]] = {}
        for resource_id, template in templates.items():
            definition = copy.deepcopy(template["definition"])
            dependencies: Dict[str, int] = {}
            for node in definition["nodes"]:
                if node["type"] not in {"subworkflow", "for_each"}:
                    continue
                child_template_id = str(node["config"]["workflow_id"])
                child_workflow_id = workflow_ids[child_template_id]
                node["config"]["workflow_id"] = child_workflow_id
                dependencies[child_workflow_id] = 1
            if resource_id == template_id and name:
                definition["name"] = str(name)[:128]
            definitions[resource_id] = validate_definition(definition)
            dependency_maps[resource_id] = dependencies
        timestamp = _now()
        try:
            with self.database.transaction(immediate=True) as connection:
                for resource_id, template in templates.items():
                    is_root = resource_id == template_id
                    item_key = workflow_key if is_root else self._dependency_workflow_key(workflow_key, resource_id)
                    definition = definitions[resource_id]
                    connection.execute(
                        "INSERT INTO organization_workflows("
                        "workflow_id, organization_id, workflow_key, name, description, status, "
                        "draft_json, draft_revision, "
                        "published_version, template_resource_id, template_revision, created_by, "
                        "updated_by, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            workflow_ids[resource_id], organization_id, item_key,
                            definition["name"], definition["description"],
                            "draft" if is_root else "published", _dump(definition), None if is_root else 1,
                            resource_id, template["revision"], actor_user_id, actor_user_id, timestamp, timestamp,
                        ),
                    )
                for resource_id in templates:
                    if resource_id == template_id:
                        continue
                    definition = definitions[resource_id]
                    payload = _dump(definition)
                    connection.execute(
                        "INSERT INTO organization_workflow_versions("
                        "workflow_id, version, definition_json, definition_hash, dependency_json, "
                        "published_by, published_at) "
                        "VALUES (?, 1, ?, ?, ?, ?, ?)",
                        (
                            workflow_ids[resource_id], payload, _hash(payload), _dump(dependency_maps[resource_id]),
                            actor_user_id, timestamp,
                        ),
                    )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise WorkflowError("复制模板生成的工作流 ID 已存在") from exc
            raise
        return self.get_workflow(organization_id, workflow_ids[template_id])

    @staticmethod
    def _dependency_workflow_key(root_key: str, template_id: str) -> str:
        digest = _hash(template_id)[:8]
        template_part = template_id[:40]
        available = max(1, 128 - len(template_part) - len(digest) - 3)
        return "{}__{}_{}".format(root_key[:available], template_part, digest)

    def issue_access_token(
        self,
        organization_id: str,
        workflow_id: str,
        label: str,
        actor_user_id: int,
    ) -> Dict[str, Any]:
        workflow = self.get_workflow(organization_id, workflow_id, include_draft=False)
        if workflow["status"] != "published":
            raise WorkflowError("工作流尚未发布或已停用")
        token_id, secret = str(uuid.uuid4()), "bpwf_" + secrets.token_urlsafe(32)
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            api_trigger = connection.execute(
                "SELECT trigger_id FROM workflow_trigger_bindings WHERE workflow_id=? AND trigger_type='api' LIMIT 1",
                (workflow_id,),
            ).fetchone()
            if api_trigger is None:
                raise WorkflowError("工作流未配置 API 触发器")
            connection.execute(
                "INSERT INTO workflow_access_tokens("
                "token_id, workflow_id, organization_id, label, token_hash, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_id, workflow_id, organization_id, str(label)[:128], _hash(secret), actor_user_id, timestamp),
            )
            connection.execute(
                "UPDATE workflow_trigger_bindings SET enabled=1, updated_at=? "
                "WHERE workflow_id=? AND trigger_type='api'",
                (timestamp, workflow_id),
            )
        return {"token_id": token_id, "token": secret, "created_at": timestamp}

    def list_trigger_bindings(self, organization_id: str, workflow_id: str) -> List[Dict[str, Any]]:
        self.get_workflow(organization_id, workflow_id, include_draft=False)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT trigger_id, trigger_key, trigger_type, config_json, published_version, enabled, "
                "next_fire_at, created_at, updated_at FROM workflow_trigger_bindings "
                "WHERE organization_id=? AND workflow_id=? ORDER BY trigger_key",
                (organization_id, workflow_id),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["config"] = _load(item.pop("config_json"), {})
            item["enabled"] = bool(item["enabled"])
            result.append(item)
        return result

    def list_access_tokens(self, organization_id: str, workflow_id: str) -> List[Dict[str, Any]]:
        self.get_workflow(organization_id, workflow_id, include_draft=False)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT token_id, label, created_by, created_at, revoked_at, last_used_at "
                "FROM workflow_access_tokens WHERE organization_id=? AND workflow_id=? ORDER BY created_at DESC",
                (organization_id, workflow_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def issue_webhook_secret(self, organization_id: str, workflow_id: str, trigger_id: str) -> Dict[str, Any]:
        self.get_workflow(organization_id, workflow_id, include_draft=False)
        secret = "bpwh_" + secrets.token_urlsafe(32)
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE workflow_trigger_bindings SET enabled=1, secret_hash=?, updated_at=? "
                "WHERE organization_id=? AND workflow_id=? AND trigger_id=? AND trigger_type='webhook'",
                (_hash(secret), timestamp, organization_id, workflow_id, trigger_id),
            )
            if cursor.rowcount != 1:
                raise WorkflowError("Webhook 触发器不存在")
        return {"trigger_id": trigger_id, "token": secret, "created_at": timestamp}

    def revoke_webhook_secret(self, organization_id: str, workflow_id: str, trigger_id: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE workflow_trigger_bindings SET enabled=0, secret_hash='', updated_at=? "
                "WHERE organization_id=? AND workflow_id=? AND trigger_id=? AND trigger_type='webhook'",
                (_now(), organization_id, workflow_id, trigger_id),
            )
            if cursor.rowcount != 1:
                raise WorkflowError("Webhook 触发器不存在")

    def revoke_access_token(self, organization_id: str, workflow_id: str, token_id: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE workflow_access_tokens SET revoked_at=? "
                "WHERE organization_id=? AND workflow_id=? AND token_id=? AND revoked_at IS NULL",
                (_now(), organization_id, workflow_id, token_id),
            )
            if cursor.rowcount != 1:
                raise WorkflowError("访问令牌不存在或已撤销")

    def authenticate_token(self, workflow_id: str, secret: str) -> Optional[Dict[str, Any]]:
        digest = _hash(secret)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT token.token_id, token.workflow_id, token.organization_id, binding.trigger_id "
                "FROM workflow_access_tokens token "
                "JOIN workflow_trigger_bindings binding ON binding.workflow_id=token.workflow_id "
                "JOIN organization_workflows workflow ON workflow.workflow_id=token.workflow_id "
                "WHERE token.workflow_id=? AND token.token_hash=? AND token.revoked_at IS NULL "
                "AND binding.trigger_type='api' AND binding.enabled=1 AND workflow.status='published' "
                "ORDER BY binding.trigger_key LIMIT 1",
                (workflow_id, digest),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE workflow_access_tokens SET last_used_at=? WHERE token_id=?",
                    (_now(), row["token_id"]),
                )
        return dict(row) if row is not None else None

    def authenticate_run_token(self, run_id: str, secret: str) -> Optional[Dict[str, Any]]:
        digest = _hash(secret)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT token.token_id, token.workflow_id, token.organization_id, run.run_id "
                "FROM workflow_access_tokens token "
                "JOIN workflow_runs run ON run.workflow_id=token.workflow_id "
                "WHERE run.run_id=? AND token.token_hash=? AND token.revoked_at IS NULL LIMIT 1",
                (run_id, digest),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE workflow_access_tokens SET last_used_at=? WHERE token_id=?",
                    (_now(), row["token_id"]),
                )
        return dict(row) if row is not None else None

    def authenticate_webhook(self, trigger_id: str, secret: str) -> Optional[Dict[str, Any]]:
        digest = _hash(secret)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_trigger_bindings WHERE trigger_id=? AND trigger_type='webhook' AND enabled=1",
                (trigger_id,),
            ).fetchone()
        if row is None or not hmac.compare_digest(str(row["secret_hash"]), digest):
            return None
        return dict(row)

    def _definition_for_run(self, workflow_id: str, version: int) -> Dict[str, Any]:
        with self.database.read() as connection:
            if version == 0:
                row = connection.execute(
                    "SELECT draft_json FROM organization_workflows WHERE workflow_id=?",
                    (workflow_id,),
                ).fetchone()
                payload = row["draft_json"] if row is not None else None
            else:
                row = connection.execute(
                    "SELECT definition_json FROM organization_workflow_versions WHERE workflow_id=? AND version=?",
                    (workflow_id, version),
                ).fetchone()
                payload = row["definition_json"] if row is not None else None
        if payload is None:
            raise WorkflowError("工作流运行版本不存在")
        return _load(payload, {})

    def enqueue_run(
        self,
        organization_id: str,
        workflow_id: str,
        inputs: Mapping[str, Any],
        trigger_type: str,
        trigger_ref: str,
        initiated_by: Optional[int],
        *,
        idempotency_key: str = "",
        test_mode: bool = False,
        allow_side_effects: bool = False,
        version_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        workflow = self.get_workflow(organization_id, workflow_id)
        version = int(
            version_override if version_override is not None
            else (0 if test_mode else workflow.get("published_version") or 0)
        )
        if not test_mode and (workflow["status"] != "published" or version <= 0):
            raise WorkflowError("工作流尚未发布或已停用")
        definition = validate_definition(self._definition_for_run(workflow_id, version))
        initial_state: Dict[str, Any] = {}
        if test_mode and version == 0:
            initial_state = {
                "definition_snapshot": definition,
                "dependencies": self.dependencies_for_definition(
                    organization_id, workflow_id, definition
                ),
            }
        normalized_inputs = dict(inputs or {})
        for field in definition["inputs"]:
            if field["key"] not in normalized_inputs and "default" in field:
                normalized_inputs[field["key"]] = field["default"]
            if field["required"] and normalized_inputs.get(field["key"]) in (None, ""):
                raise WorkflowError("缺少必填输入：{}".format(field["label"]))
            if field["key"] in normalized_inputs and normalized_inputs[field["key"]] is not None:
                self._validate_input_type(field, normalized_inputs[field["key"]])
        if len(_dump(normalized_inputs).encode("utf-8")) > 1024 * 1024:
            raise WorkflowError("工作流输入不能超过 1 MiB")
        run_id, timestamp = str(uuid.uuid4()), _now()
        try:
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO workflow_runs("
                    "run_id, workflow_id, organization_id, workflow_version, trigger_type, trigger_ref, "
                    "idempotency_key, status, input_json, state_json, initiated_by, "
                    "test_mode, allow_side_effects, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        workflow_id,
                        organization_id,
                        version,
                        trigger_type,
                        trigger_ref,
                        str(idempotency_key or "")[:256],
                        _dump(normalized_inputs),
                        _dump(initial_state),
                        initiated_by,
                        int(test_mode),
                        int(allow_side_effects),
                        timestamp,
                    ),
                )
                self._event(connection, run_id, organization_id, "run.queued", "", {"trigger_type": trigger_type})
        except Exception as exc:
            if idempotency_key and "UNIQUE" in str(exc):
                with self.database.read() as connection:
                    row = connection.execute(
                        "SELECT run_id FROM workflow_runs WHERE workflow_id=? AND trigger_ref=? AND idempotency_key=?",
                        (workflow_id, trigger_ref, idempotency_key),
                    ).fetchone()
                if row is not None:
                    return self.get_run(organization_id, str(row["run_id"]))
            raise
        return self.get_run(organization_id, run_id)

    @staticmethod
    def _validate_input_type(field: Mapping[str, Any], value: Any) -> None:
        field_type = str(field["type"])
        valid = {
            "string": isinstance(value, str),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "object": isinstance(value, Mapping),
            "array": isinstance(value, list),
            "file_ref": isinstance(value, str),
        }.get(field_type, False)
        if not valid:
            raise WorkflowError("输入 {} 的类型必须为 {}".format(field["label"], field_type))

    @staticmethod
    def _run_row(row: Any) -> Dict[str, Any]:
        result = dict(row)
        for key in ("input_json", "output_json", "state_json", "error_json"):
            result[key[:-5] if key.endswith("_json") else key] = _load(
                result.pop(key, None), None if key in {"output_json", "error_json"} else {}
            )
        result["test_mode"] = bool(result["test_mode"])
        result["allow_side_effects"] = bool(result["allow_side_effects"])
        return result

    def get_run(self, organization_id: str, run_id: str) -> Dict[str, Any]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_runs WHERE organization_id=? AND run_id=?",
                (organization_id, run_id),
            ).fetchone()
            nodes = (
                connection.execute(
                    "SELECT * FROM workflow_node_runs WHERE run_id=? ORDER BY started_at, node_run_id",
                    (run_id,),
                ).fetchall()
                if row is not None
                else []
            )
        if row is None:
            raise WorkflowError("工作流运行记录不存在")
        result = self._run_row(row)
        result["node_runs"] = [dict(item) for item in nodes]
        for item in result["node_runs"]:
            item["input"] = _load(item.pop("input_json"), {})
            item["output"] = _load(item.pop("output_json"), None)
            item["error"] = _load(item.pop("error_json"), None)
        return result

    def list_runs(
        self,
        organization_id: str,
        workflow_id: str = "",
        status: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        where = ["organization_id=?"]
        params: List[Any] = [organization_id]
        if workflow_id:
            where.append("workflow_id=?")
            params.append(workflow_id)
        if status:
            where.append("status=?")
            params.append(status)
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_runs WHERE {} ORDER BY created_at DESC LIMIT ? OFFSET ?".format(
                    " AND ".join(where)
                ),
                tuple(params),
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def claim_run(self, owner: str, lease_seconds: int = 60) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT r.run_id, r.organization_id, r.status FROM workflow_runs r WHERE "
                "(r.status='queued' OR (r.status='running' AND r.lease_expires_at<=?)) "
                "AND (r.wake_at IS NULL OR r.wake_at<=?) "
                "AND (r.lease_expires_at IS NULL OR r.lease_expires_at<=?) "
                "AND (SELECT COUNT(*) FROM workflow_runs active "
                "     WHERE active.organization_id=r.organization_id AND active.status='running' "
                "     AND active.lease_expires_at>?) < 2 "
                "ORDER BY r.created_at LIMIT 1",
                (now_text, now_text, now_text, now_text),
            ).fetchone()
            if row is None:
                return None
            if str(row["status"]) == "running":
                unfinished = connection.execute(
                    "SELECT node_run_id, node_id, node_type FROM workflow_node_runs "
                    "WHERE run_id=? AND status='running' ORDER BY started_at DESC LIMIT 1",
                    (row["run_id"],),
                ).fetchone()
                if (
                    unfinished is not None
                    and str(unfinished["node_type"]) in {"tool", "script", "http", "notification"}
                ):
                    self._mark_uncertain_write(connection, row, unfinished, now_text)
                    return None
            connection.execute(
                "UPDATE workflow_runs SET status='running', lease_owner=?, lease_expires_at=?, "
                "started_at=COALESCE(started_at, ?) WHERE run_id=?",
                (owner, expires, now_text, row["run_id"]),
            )
            self._event(connection, str(row["run_id"]), str(row["organization_id"]), "run.running", "", {})
        return self.get_run(str(row["organization_id"]), str(row["run_id"]))

    def _mark_uncertain_write(self, connection: Any, run: Any, node_run: Any, timestamp: str) -> None:
        node_id = str(node_run["node_id"])
        detail = {
            "message": "外部写操作在进程中断后无法确认结果，需要管理员处置",
            "node_id": node_id,
            "actions": ["retry", "skip", "terminate"],
        }
        connection.execute(
            "UPDATE workflow_node_runs SET status='needs_attention', error_json=?, finished_at=? WHERE node_run_id=?",
            (_log_dump(detail), timestamp, node_run["node_run_id"]),
        )
        connection.execute(
            "UPDATE workflow_runs SET status='needs_attention', error_json=?, lease_owner=NULL, lease_expires_at=NULL "
            "WHERE run_id=?",
            (_log_dump(detail), run["run_id"]),
        )
        existing = connection.execute(
            "SELECT wait_id FROM workflow_waits "
            "WHERE run_id=? AND node_id=? AND wait_type='attention' AND status='pending'",
            (run["run_id"], node_id),
        ).fetchone()
        if existing is None:
            wait_id = str(uuid.uuid4())
            payload = _log_dump(detail)
            connection.execute(
                "INSERT INTO workflow_waits(wait_id, run_id, organization_id, node_id, wait_type, assignees_json, "
                "payload_json, payload_hash, created_at) VALUES (?, ?, ?, ?, 'attention', ?, ?, ?, ?)",
                (
                    wait_id, run["run_id"], run["organization_id"], node_id,
                    _dump({"roles": ["owner", "admin"]}), payload, _hash(payload), timestamp,
                ),
            )
        self._event(connection, str(run["run_id"]), str(run["organization_id"]), "run.needs_attention", node_id, detail)

    def start_specific_run(
        self,
        organization_id: str,
        run_id: str,
        owner: str,
        lease_seconds: int = 600,
    ) -> Dict[str, Any]:
        timestamp = _now()
        expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE workflow_runs SET status='running', lease_owner=?, lease_expires_at=?, "
                "started_at=COALESCE(started_at, ?) WHERE organization_id=? AND run_id=? AND status='queued'",
                (owner, expires, timestamp, organization_id, run_id),
            )
            if cursor.rowcount != 1:
                raise WorkflowError("子工作流运行无法启动")
            self._event(connection, run_id, organization_id, "run.running", "", {"source": "subworkflow"})
        return self.get_run(organization_id, run_id)

    def checkpoint_run(self, run_id: str, state: Mapping[str, Any], lease_owner: str, lease_seconds: int = 600) -> None:
        expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE workflow_runs SET state_json=?, lease_expires_at=? "
                "WHERE run_id=? AND status='running' AND lease_owner=?",
                (_dump(state), expires, run_id, lease_owner),
            )
            if cursor.rowcount != 1:
                raise WorkflowError("工作流运行租约已经失效")

    def update_run_state(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        status: str = "running",
        wake_at: Optional[str] = None,
    ) -> None:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute("SELECT organization_id FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise WorkflowError("工作流运行记录不存在")
            connection.execute(
                "UPDATE workflow_runs SET state_json=?, status=?, wake_at=?, "
                "lease_owner=NULL, lease_expires_at=NULL WHERE run_id=?",
                (_dump(state), status, wake_at, run_id),
            )
            self._event(connection, run_id, str(row["organization_id"]), "run." + status, "", {})

    def finish_run(self, run_id: str, status: str, *, output: Any = None, error: Any = None) -> None:
        if status not in {"succeeded", "failed", "canceled", "timed_out", "needs_attention"}:
            raise WorkflowError("无效的工作流结束状态")
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute("SELECT organization_id FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise WorkflowError("工作流运行记录不存在")
            connection.execute(
                "UPDATE workflow_runs SET status=?, output_json=?, error_json=?, finished_at=?, "
                "lease_owner=NULL, lease_expires_at=NULL WHERE run_id=?",
                (
                    status,
                    _dump(output) if output is not None else None,
                    _dump(error) if error is not None else None,
                    timestamp,
                    run_id,
                ),
            )
            self._event(connection, run_id, str(row["organization_id"]), "run." + status, "", error or {})

    def resolve_attention(
        self,
        organization_id: str,
        run_id: str,
        action: str,
        actor_user_id: int,
        comment: str = "",
    ) -> Dict[str, Any]:
        if action not in {"retry", "skip", "terminate"}:
            raise WorkflowError("人工处置动作必须为 retry、skip 或 terminate")
        run = self.get_run(organization_id, run_id)
        if run["status"] != "needs_attention":
            raise WorkflowError("工作流运行当前不需要人工处置")
        affected = next(
            (item for item in reversed(run["node_runs"]) if item["status"] == "needs_attention"),
            None,
        )
        if affected is None:
            raise WorkflowError("未找到需要人工处置的节点")
        node_id = str(affected["node_id"])
        state = dict(run.get("state") or {})
        state["queue"] = list(state.get("queue") or [])
        state["completed"] = list(state.get("completed") or [])
        state["nodes"] = dict(state.get("nodes") or {})
        if action == "retry" and node_id not in state["queue"]:
            state["queue"].insert(0, node_id)
        elif action == "skip":
            state["queue"] = [item for item in state["queue"] if item != node_id]
            if node_id not in state["completed"]:
                state["completed"].append(node_id)
            state["nodes"][node_id] = {"skipped": True, "reason": comment[:1000]}
            definition = validate_definition(self._definition_for_run(run["workflow_id"], int(run["workflow_version"])))
            next_nodes = [
                str(edge["target"])
                for edge in definition["edges"]
                if edge["source"] == node_id and edge["source_port"] in {"default", ""}
            ]
            for next_node in next_nodes:
                if next_node not in state["queue"]:
                    state["queue"].append(next_node)
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE workflow_waits SET status=?, response_json=?, resolved_by=?, resolved_at=? "
                "WHERE run_id=? AND node_id=? AND wait_type='attention' AND status='pending'",
                (
                    "rejected" if action == "terminate" else "resolved",
                    _log_dump({"action": action, "comment": comment[:1000]}),
                    actor_user_id,
                    timestamp,
                    run_id,
                    node_id,
                ),
            )
            if action == "terminate":
                connection.execute(
                    "UPDATE workflow_runs SET status='failed', error_json=?, finished_at=? "
                    "WHERE organization_id=? AND run_id=? AND status='needs_attention'",
                    (
                        _log_dump({"message": "管理员终止了无法确认结果的外部操作", "comment": comment[:1000]}),
                        timestamp, organization_id, run_id,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE workflow_runs SET status='queued', state_json=?, error_json=NULL, "
                    "wake_at=NULL WHERE organization_id=? AND run_id=? AND status='needs_attention'",
                    (_dump(state), organization_id, run_id),
                )
                if action == "skip":
                    connection.execute(
                        "UPDATE workflow_node_runs SET status='skipped' WHERE node_run_id=?",
                        (affected["node_run_id"],),
                    )
            self._event(
                connection, run_id, organization_id, "attention." + action,
                node_id, {"comment": comment[:1000]},
            )
        return self.get_run(organization_id, run_id)

    def cancel_run(self, organization_id: str, run_id: str) -> Dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE workflow_runs SET status='canceled', finished_at=?, lease_owner=NULL, lease_expires_at=NULL "
                "WHERE organization_id=? AND run_id=? AND status IN ('queued','running','waiting','needs_attention')",
                (_now(), organization_id, run_id),
            )
            if cursor.rowcount != 1:
                raise WorkflowError("工作流运行不存在或已结束")
            connection.execute(
                "UPDATE workflow_waits SET status='canceled', resolved_at=? "
                "WHERE run_id=? AND status='pending'",
                (_now(), run_id),
            )
        return self.get_run(organization_id, run_id)

    def begin_node(self, run: Mapping[str, Any], node: Mapping[str, Any], rendered_input: Any, attempt: int) -> str:
        node_run_id = str(uuid.uuid4())
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO workflow_node_runs("
                "node_run_id, run_id, node_id, node_type, attempt, status, input_json, "
                "operation_key, started_at) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)",
                (
                    node_run_id, run["run_id"], node["id"], node["type"], attempt,
                    _log_dump(rendered_input), "{}:{}".format(run["run_id"], node["id"]), _now(),
                ),
            )
            self._event(
                connection, run["run_id"], run["organization_id"], "node.running",
                node["id"], {"attempt": attempt},
            )
        return node_run_id

    def finish_node(
        self,
        run: Mapping[str, Any],
        node_run_id: str,
        node_id: str,
        status: str,
        output: Any = None,
        error: Any = None,
    ) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE workflow_node_runs SET status=?, output_json=?, "
                "error_json=?, finished_at=? WHERE node_run_id=?",
                (
                    status,
                    _log_dump(output) if output is not None else None,
                    _log_dump(error) if error is not None else None,
                    _now(),
                    node_run_id,
                ),
            )
            self._event(connection, run["run_id"], run["organization_id"], "node." + status, node_id, error or {})

    def create_wait(
        self,
        run: Mapping[str, Any],
        node_id: str,
        wait_type: str,
        payload: Any,
        assignees: Any,
        expires_at: Optional[str],
    ) -> Dict[str, Any]:
        wait_id, timestamp = str(uuid.uuid4()), _now()
        payload_json = _log_dump(payload)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO workflow_waits("
                "wait_id, run_id, organization_id, node_id, wait_type, assignees_json, "
                "payload_json, payload_hash, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    wait_id, run["run_id"], run["organization_id"], node_id, wait_type,
                    _dump(assignees), payload_json, _hash(payload_json), expires_at, timestamp,
                ),
            )
            self._event(
                connection, run["run_id"], run["organization_id"], "wait.created",
                node_id, {"wait_id": wait_id, "wait_type": wait_type},
            )
        return self.get_wait(run["organization_id"], wait_id)

    @staticmethod
    def _wait_row(row: Any) -> Dict[str, Any]:
        result = dict(row)
        result["assignees"] = _load(result.pop("assignees_json"), {})
        result["payload"] = _load(result.pop("payload_json"), {})
        result["response"] = _load(result.pop("response_json"), None)
        return result

    def get_wait(self, organization_id: str, wait_id: str) -> Dict[str, Any]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_waits WHERE organization_id=? AND wait_id=?",
                (organization_id, wait_id),
            ).fetchone()
        if row is None:
            raise WorkflowError("工作流待办不存在")
        return self._wait_row(row)

    def pending_wait_for_node(self, run_id: str, node_id: str) -> Optional[Dict[str, Any]]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_waits WHERE run_id=? AND node_id=? ORDER BY created_at DESC LIMIT 1",
                (run_id, node_id),
            ).fetchone()
        return self._wait_row(row) if row is not None else None

    def list_waits(self, organization_id: str, status: str = "pending") -> List[Dict[str, Any]]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_waits WHERE organization_id=? AND (?='' OR status=?) "
                "ORDER BY created_at DESC LIMIT 200",
                (organization_id, status, status),
            ).fetchall()
        return [self._wait_row(row) for row in rows]

    def resolve_wait(
        self,
        organization_id: str,
        wait_id: str,
        response: Mapping[str, Any],
        actor_user_id: int,
    ) -> Dict[str, Any]:
        status = str(response.get("status") or "resolved")
        if status not in {"approved", "rejected", "resolved"}:
            raise WorkflowError("待办处理状态无效")
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM workflow_waits "
                "WHERE organization_id=? AND wait_id=? AND status='pending'",
                (organization_id, wait_id),
            ).fetchone()
            if row is None:
                raise WorkflowError("工作流待办不存在或已处理")
            if row["expires_at"] and str(row["expires_at"]) <= timestamp:
                connection.execute(
                    "UPDATE workflow_waits SET status='expired', resolved_at=? WHERE wait_id=?",
                    (timestamp, wait_id),
                )
                raise WorkflowError("工作流待办已经过期")
            connection.execute(
                "UPDATE workflow_waits SET status=?, response_json=?, resolved_by=?, resolved_at=? WHERE wait_id=?",
                (status, _dump(dict(response)), actor_user_id, timestamp, wait_id),
            )
            connection.execute(
                "UPDATE workflow_runs SET status='queued', wake_at=NULL "
                "WHERE run_id=? AND status='waiting'",
                (row["run_id"],),
            )
            self._event(
                connection, str(row["run_id"]), organization_id, "wait." + status,
                str(row["node_id"]), {"wait_id": wait_id},
            )
        return self.get_wait(organization_id, wait_id)

    def expire_waits(self) -> int:
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT wait_id, run_id, organization_id, node_id FROM workflow_waits "
                "WHERE status='pending' AND expires_at IS NOT NULL AND expires_at<=?",
                (timestamp,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE workflow_waits SET status='expired', resolved_at=? WHERE wait_id=?",
                    (timestamp, row["wait_id"]),
                )
                connection.execute(
                    "UPDATE workflow_runs SET status='queued', wake_at=NULL "
                    "WHERE run_id=? AND status='waiting'",
                    (row["run_id"],),
                )
                self._event(
                    connection, str(row["run_id"]), str(row["organization_id"]),
                    "wait.expired", str(row["node_id"]), {"wait_id": row["wait_id"]},
                )
        return len(rows)

    def list_events(self, organization_id: str, run_id: str, after: int = 0) -> List[Dict[str, Any]]:
        self.get_run(organization_id, run_id)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_events WHERE run_id=? AND event_id>? ORDER BY event_id LIMIT 500",
                (run_id, max(0, after)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = _load(item.pop("detail_json"), {})
            result.append(item)
        return result

    @staticmethod
    def _event(connection: Any, run_id: str, organization_id: str, event_type: str, node_id: str, detail: Any) -> None:
        connection.execute(
            "INSERT INTO workflow_events("
            "run_id, organization_id, event_type, node_id, detail_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, organization_id, event_type, node_id, _log_dump(detail, 64 * 1024), _now()),
        )

    def due_schedules(self, timestamp: str) -> List[Dict[str, Any]]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_trigger_bindings WHERE trigger_type='schedule' AND enabled=1 "
                "AND (next_fire_at IS NULL OR next_fire_at<=?) ORDER BY updated_at LIMIT 100",
                (timestamp,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_schedule_next_fire(self, trigger_id: str, next_fire_at: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE workflow_trigger_bindings SET next_fire_at=?, updated_at=? WHERE trigger_id=?",
                (next_fire_at, _now(), trigger_id),
            )
