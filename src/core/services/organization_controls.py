"""Typed organization channel and schedule persistence."""

from __future__ import annotations

import json
import hashlib
import re
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.core.messaging.providers import channel_provider
from src.core.services.resources import ScopedResourceStore
from src.core.storage.organizations import OrganizationStore


CONTROL_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
CRON_FIELD = re.compile(r"^[0-9*/,-]+$")
# Run history is diagnostic data; keep it bounded per organization.
RUN_HISTORY_LIMIT = 500
RUN_STATUSES = ("running", "succeeded", "failed", "skipped")


class OrganizationControlError(ValueError):
    """Raised when organization-owned runtime configuration is invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


class OrganizationControlStore:
    """Persist typed organization controls and publish runtime revisions."""

    def __init__(
        self,
        organizations: OrganizationStore,
        resources: ScopedResourceStore,
        config: Any,
    ) -> None:
        self.organizations = organizations
        self.resources = resources
        self.config = config
        self.database = organizations.database

    @staticmethod
    def _validate_key(value: str, label: str) -> str:
        normalized = value.strip()
        if not CONTROL_ID.fullmatch(normalized):
            raise OrganizationControlError(
                "{}只能以小写字母开头，并包含小写字母、数字、下划线或连字符".format(
                    label
                )
            )
        return normalized

    def _validate_agent(self, organization_id: str, agent_id: str) -> str:
        normalized = agent_id.strip()
        try:
            item = self.resources.get_effective(
                organization_id, "agents", normalized
            )
        except Exception as exc:
            raise OrganizationControlError("绑定的智能体不存在或已暂停") from exc
        if not bool(item.get("payload", {}).get("enabled", True)):
            raise OrganizationControlError("绑定的智能体不存在或已暂停")
        return normalized

    def _bump(self, connection: Any, organization_id: str, field: str) -> None:
        if field not in {"channels_revision", "schedules_revision"}:
            raise ValueError("未知运行时版本字段")
        connection.execute(
            "INSERT INTO organization_runtime_revisions("
            "organization_id, {}, updated_at) VALUES (?, 1, ?) ".format(field)
            + "ON CONFLICT(organization_id) DO UPDATE SET "
            + "{}={}+1, updated_at=excluded.updated_at".format(field, field),
            (organization_id, _now()),
        )

    def runtime_revisions(self) -> Dict[str, Dict[str, Any]]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM organization_runtime_revisions"
            ).fetchall()
        return {str(row["organization_id"]): dict(row) for row in rows}

    def bump_channels_revision(self, organization_id: str) -> None:
        """Trigger a channel runtime rebuild for externally updated credentials."""
        self.organizations.get(organization_id)
        with self.database.transaction(immediate=True) as connection:
            self._bump(connection, organization_id, "channels_revision")

    @staticmethod
    def _channel_row(row: Any, configured: bool = False) -> Dict[str, Any]:
        return {
            "channel_instance_id": str(row["channel_instance_id"]),
            "organization_id": str(row["organization_id"]),
            "id": str(row["channel_id"]),
            "type": str(row["channel_type"]),
            "agent_id": str(row["agent_id"]),
            "enabled": bool(row["enabled"]),
            "settings": _loads(row["settings_json"], {}),
            "migration_error": str(row["migration_error"] or ""),
            "revision": int(row["revision"]),
            "credential_configured": configured,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def list_channels(self, organization_id: str) -> List[Dict[str, Any]]:
        self.organizations.get(organization_id)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT c.*, EXISTS(SELECT 1 FROM credential_metadata m "
                "WHERE m.organization_id=c.organization_id "
                "AND m.resource_type='channels' "
                "AND m.resource_id=c.channel_instance_id) AS configured "
                "FROM organization_channels c WHERE c.organization_id=? "
                "ORDER BY c.channel_id",
                (organization_id,),
            ).fetchall()
        return [self._channel_row(row, bool(row["configured"])) for row in rows]

    def get_channel(
        self, organization_id: str, channel_id: str
    ) -> Dict[str, Any]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT c.*, EXISTS(SELECT 1 FROM credential_metadata m "
                "WHERE m.organization_id=c.organization_id "
                "AND m.resource_type='channels' "
                "AND m.resource_id=c.channel_instance_id) AS configured "
                "FROM organization_channels c WHERE c.organization_id=? "
                "AND c.channel_id=?",
                (organization_id, channel_id),
            ).fetchone()
        if row is None:
            raise OrganizationControlError("消息渠道不存在")
        return self._channel_row(row, bool(row["configured"]))

    def upsert_channel(
        self,
        organization_id: str,
        channel_id: str,
        payload: Mapping[str, Any],
        actor_user_id: int,
    ) -> Dict[str, Any]:
        self.organizations.get(organization_id)
        channel_id = self._validate_key(channel_id, "渠道编号")
        channel_type = str(payload.get("type") or "").strip()
        try:
            channel_provider(channel_type)
        except ValueError as exc:
            raise OrganizationControlError(str(exc)) from exc
        agent_id = self._validate_agent(
            organization_id, str(payload.get("agent_id") or "")
        )
        settings = payload.get("settings") or {}
        if not isinstance(settings, dict):
            raise OrganizationControlError("渠道设置必须是 JSON 对象")
        unexpected = sorted(set(settings) - {"group_policy"})
        if unexpected:
            raise OrganizationControlError(
                "渠道设置包含不允许的字段：{}".format("、".join(unexpected))
            )
        group_policy = settings.get("group_policy", "private_only")
        if group_policy not in {"private_only", "mention_only"}:
            raise OrganizationControlError("群聊策略无效")
        settings = {"group_policy": group_policy}
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT channel_instance_id FROM organization_channels "
                "WHERE organization_id=? AND channel_id=?",
                (organization_id, channel_id),
            ).fetchone()
            instance_id = (
                str(existing["channel_instance_id"])
                if existing is not None
                else str(uuid.uuid4())
            )
            connection.execute(
                "INSERT INTO organization_channels("
                "channel_instance_id, organization_id, channel_id, channel_type, "
                "agent_id, enabled, settings_json, revision, created_by, updated_by, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?) "
                "ON CONFLICT(organization_id, channel_id) DO UPDATE SET "
                "channel_type=excluded.channel_type, agent_id=excluded.agent_id, "
                "enabled=excluded.enabled, settings_json=excluded.settings_json, "
                "migration_error='', "
                "revision=organization_channels.revision+1, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (
                    instance_id,
                    organization_id,
                    channel_id,
                    channel_type,
                    agent_id,
                    1 if bool(payload.get("enabled", True)) else 0,
                    json.dumps(settings, ensure_ascii=False),
                    actor_user_id,
                    actor_user_id,
                    timestamp,
                    timestamp,
                ),
            )
            self._bump(connection, organization_id, "channels_revision")
        return self.get_channel(organization_id, channel_id)

    def set_channel_enabled(
        self, organization_id: str, channel_id: str, enabled: bool, actor_user_id: int
    ) -> Dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            updated = connection.execute(
                "UPDATE organization_channels SET enabled=?, revision=revision+1, "
                "updated_by=?, updated_at=? WHERE organization_id=? AND channel_id=?",
                (
                    1 if enabled else 0,
                    actor_user_id,
                    _now(),
                    organization_id,
                    channel_id,
                ),
            )
            if updated.rowcount == 0:
                raise OrganizationControlError("消息渠道不存在")
            self._bump(connection, organization_id, "channels_revision")
        return self.get_channel(organization_id, channel_id)

    def delete_channel(self, organization_id: str, channel_id: str) -> str:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT channel_instance_id FROM organization_channels "
                "WHERE organization_id=? AND channel_id=?",
                (organization_id, channel_id),
            ).fetchone()
            if row is None:
                raise OrganizationControlError("消息渠道不存在")
            connection.execute(
                "DELETE FROM organization_channels WHERE channel_instance_id=?",
                (str(row["channel_instance_id"]),),
            )
            self._bump(connection, organization_id, "channels_revision")
        return str(row["channel_instance_id"])

    @staticmethod
    def _validate_crons(raw: Any) -> List[str]:
        if not isinstance(raw, list) or not raw:
            raise OrganizationControlError("至少需要一个 cron 表达式")
        result: List[str] = []
        for value in raw:
            cron = str(value).strip()
            parts = cron.split()
            if len(parts) != 5 or any(not CRON_FIELD.fullmatch(part) for part in parts):
                raise OrganizationControlError("cron 表达式格式无效：{}".format(cron))
            result.append(cron)
        return result

    def _validate_action(
        self, organization_id: str, raw: Any
    ) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise OrganizationControlError("任务动作必须是 JSON 对象")
        action = dict(raw)
        action_type = str(action.get("type") or "")
        if action_type == "text":
            if not str(action.get("content") or "").strip():
                raise OrganizationControlError("文本任务内容不能为空")
        elif action_type == "agent_prompt":
            action["agent_id"] = self._validate_agent(
                organization_id, str(action.get("agent_id") or "")
            )
            if not str(action.get("prompt") or "").strip():
                raise OrganizationControlError("智能体任务提示词不能为空")
        elif action_type == "script":
            script_id = str(action.get("script_id") or "")
            if script_id not in self.config.scripts:
                raise OrganizationControlError("引用的平台脚本不存在")
            definition = self.config.scripts[script_id]
            if not definition.enabled or definition.requires_approval:
                raise OrganizationControlError("该平台脚本未声明允许无人值守")
        elif action_type == "plugin":
            plugin_id = str(action.get("plugin_id") or "")
            if plugin_id not in self.config.plugins:
                raise OrganizationControlError("引用的平台插件不存在")
            plugin = self.config.plugins[plugin_id]
            tool_name = str(action.get("tool_name") or "")
            if not plugin.enabled:
                raise OrganizationControlError("引用的平台插件未启用")
            if not tool_name:
                raise OrganizationControlError("插件任务缺少工具名称")
            from src.core.plugins.registry import default_catalog

            manifest = default_catalog().get(plugin_id)
            if manifest is None or tool_name not in manifest.tools:
                raise OrganizationControlError("插件工具不存在或不属于所选插件")
        else:
            raise OrganizationControlError(
                "任务动作只支持文本、智能体、平台脚本或平台插件"
            )
        parameters = action.get("parameters", {})
        if not isinstance(parameters, dict):
            raise OrganizationControlError("任务参数必须是 JSON 对象")
        return action

    def dependency_revision(self, action: Mapping[str, Any]) -> str:
        """Return the approved immutable version for unattended dependencies."""
        action_type = str(action.get("type") or "")
        if action_type == "script":
            definition = self.config.scripts.get(str(action.get("script_id") or ""))
            if definition is None or not definition.enabled:
                raise OrganizationControlError("引用的平台脚本不存在或已停用")
            if definition.external:
                revision = str(definition.sha256 or "")
            else:
                try:
                    revision = hashlib.sha256(
                        Path(definition.entrypoint).read_bytes()
                    ).hexdigest()
                except OSError as exc:
                    raise OrganizationControlError("无法读取平台脚本版本") from exc
            if not revision:
                raise OrganizationControlError("平台脚本缺少可确认的版本")
            return revision
        if action_type == "plugin":
            plugin = self.config.plugins.get(str(action.get("plugin_id") or ""))
            if plugin is None or not plugin.enabled:
                raise OrganizationControlError("引用的平台插件不存在或已停用")
            payload = (
                asdict(plugin)
                if is_dataclass(plugin) and not isinstance(plugin, type)
                else dict(plugin)
            )
            return hashlib.sha256(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, default=str
                ).encode("utf-8")
            ).hexdigest()
        return ""

    @staticmethod
    def _schedule_row(row: Any) -> Dict[str, Any]:
        return {
            "schedule_id": str(row["schedule_id"]),
            "organization_id": str(row["organization_id"]),
            "id": str(row["schedule_key"]),
            "enabled": bool(row["enabled"]),
            "crons": _loads(row["crons_json"], []),
            "target": str(row["target"]),
            "action": _loads(row["action_json"], {}),
            "condition": _loads(row["condition_json"], None),
            "dependency_revision": str(row["dependency_revision"]),
            "revision": int(row["revision"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def list_schedules(self, organization_id: str) -> List[Dict[str, Any]]:
        self.organizations.get(organization_id)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM organization_schedules WHERE organization_id=? "
                "ORDER BY schedule_key",
                (organization_id,),
            ).fetchall()
        return [self._schedule_row(row) for row in rows]

    def get_schedule(
        self, organization_id: str, schedule_key: str
    ) -> Dict[str, Any]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM organization_schedules WHERE organization_id=? "
                "AND schedule_key=?",
                (organization_id, schedule_key),
            ).fetchone()
        if row is None:
            raise OrganizationControlError("定时任务不存在")
        return self._schedule_row(row)

    def upsert_schedule(
        self,
        organization_id: str,
        schedule_key: str,
        payload: Mapping[str, Any],
        actor_user_id: int,
    ) -> Dict[str, Any]:
        self.organizations.get(organization_id)
        schedule_key = self._validate_key(schedule_key, "任务编号")
        crons = self._validate_crons(payload.get("crons"))
        action = self._validate_action(organization_id, payload.get("action"))
        condition = payload.get("condition")
        if condition is not None and not isinstance(condition, dict):
            raise OrganizationControlError("任务条件必须是 JSON 对象")
        timestamp = _now()
        dependency_revision = self.dependency_revision(action)
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT schedule_id FROM organization_schedules "
                "WHERE organization_id=? AND schedule_key=?",
                (organization_id, schedule_key),
            ).fetchone()
            schedule_id = (
                str(existing["schedule_id"])
                if existing is not None
                else str(uuid.uuid4())
            )
            connection.execute(
                "INSERT INTO organization_schedules("
                "schedule_id, organization_id, schedule_key, enabled, crons_json, "
                "target, action_json, condition_json, dependency_revision, revision, "
                "created_by, updated_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'last_active_user', ?, ?, ?, 1, ?, ?, ?, ?) "
                "ON CONFLICT(organization_id, schedule_key) DO UPDATE SET "
                "enabled=excluded.enabled, crons_json=excluded.crons_json, "
                "action_json=excluded.action_json, condition_json=excluded.condition_json, "
                "dependency_revision=excluded.dependency_revision, "
                "revision=organization_schedules.revision+1, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (
                    schedule_id,
                    organization_id,
                    schedule_key,
                    1 if bool(payload.get("enabled", True)) else 0,
                    json.dumps(crons, ensure_ascii=False),
                    json.dumps(action, ensure_ascii=False),
                    (
                        json.dumps(condition, ensure_ascii=False)
                        if condition is not None
                        else None
                    ),
                    dependency_revision,
                    actor_user_id,
                    actor_user_id,
                    timestamp,
                    timestamp,
                ),
            )
            self._bump(connection, organization_id, "schedules_revision")
        return self.get_schedule(organization_id, schedule_key)

    def set_schedule_enabled(
        self, organization_id: str, schedule_key: str, enabled: bool, actor_user_id: int
    ) -> Dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT action_json FROM organization_schedules "
                "WHERE organization_id=? AND schedule_key=?",
                (organization_id, schedule_key),
            ).fetchone()
            if row is None:
                raise OrganizationControlError("定时任务不存在")
            dependency_revision = (
                self.dependency_revision(_loads(row["action_json"], {}))
                if enabled
                else None
            )
            updated = connection.execute(
                "UPDATE organization_schedules SET enabled=?, revision=revision+1, "
                "dependency_revision=COALESCE(?, dependency_revision), "
                "updated_by=?, updated_at=? WHERE organization_id=? AND schedule_key=?",
                (
                    1 if enabled else 0,
                    dependency_revision,
                    actor_user_id,
                    _now(),
                    organization_id,
                    schedule_key,
                ),
            )
            if updated.rowcount == 0:
                raise OrganizationControlError("定时任务不存在")
            self._bump(connection, organization_id, "schedules_revision")
        return self.get_schedule(organization_id, schedule_key)

    def enabled_schedules(self) -> List[Dict[str, Any]]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM organization_schedules WHERE enabled=1 "
                "ORDER BY organization_id, schedule_key"
            ).fetchall()
        return [self._schedule_row(row) for row in rows]

    def pause_schedule(self, schedule_id: str, reason: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT organization_id FROM organization_schedules WHERE schedule_id=?",
                (schedule_id,),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE organization_schedules SET enabled=0, revision=revision+1, "
                "updated_at=? WHERE schedule_id=?",
                (_now(), schedule_id),
            )
            self._bump(
                connection, str(row["organization_id"]), "schedules_revision"
            )
            self._record_run_in_connection(
                connection,
                schedule_id,
                str(row["organization_id"]),
                "failed",
                reason,
            )

    @staticmethod
    def _record_run_in_connection(
        connection: Any,
        schedule_id: str,
        organization_id: str,
        status: str,
        detail: str,
        run_id: Optional[str] = None,
    ) -> str:
        run_id = run_id or str(uuid.uuid4())
        timestamp = _now()
        connection.execute(
            "INSERT INTO organization_schedule_runs("
            "run_id, schedule_id, organization_id, status, detail, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                schedule_id,
                organization_id,
                status,
                detail,
                timestamp,
                None if status == "running" else timestamp,
            ),
        )
        connection.execute(
            "DELETE FROM organization_schedule_runs WHERE organization_id=? "
            "AND run_id NOT IN (SELECT run_id FROM organization_schedule_runs "
            "WHERE organization_id=? ORDER BY started_at DESC LIMIT ?)",
            (organization_id, organization_id, RUN_HISTORY_LIMIT),
        )
        return run_id

    def record_schedule_run(
        self,
        schedule_id: str,
        organization_id: str,
        status: str,
        detail: str,
        run_id: Optional[str] = None,
    ) -> str:
        if status not in {"running", "succeeded", "failed", "skipped"}:
            raise OrganizationControlError("定时任务运行状态无效")
        with self.database.transaction(immediate=True) as connection:
            return self._record_run_in_connection(
                connection,
                schedule_id,
                organization_id,
                status,
                detail,
                run_id,
            )

    def finish_schedule_run(
        self,
        run_id: str,
        status: str,
        detail: str,
        script_run_id: Optional[str] = None,
    ) -> None:
        if status not in {"succeeded", "failed", "skipped"}:
            raise OrganizationControlError("定时任务完成状态无效")
        with self.database.transaction(immediate=True) as connection:
            if script_run_id:
                connection.execute(
                    "UPDATE organization_schedule_runs SET status=?, detail=?, "
                    "finished_at=?, script_run_id=? WHERE run_id=?",
                    (status, detail, _now(), script_run_id, run_id),
                )
                return
            connection.execute(
                "UPDATE organization_schedule_runs SET status=?, detail=?, finished_at=? "
                "WHERE run_id=?",
                (status, detail, _now(), run_id),
            )

    def delete_schedule(self, organization_id: str, schedule_key: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            deleted = connection.execute(
                "DELETE FROM organization_schedules WHERE organization_id=? "
                "AND schedule_key=?",
                (organization_id, schedule_key),
            )
            if deleted.rowcount == 0:
                raise OrganizationControlError("定时任务不存在")
            self._bump(connection, organization_id, "schedules_revision")

    @staticmethod
    def _run_filters(
        organization_id: str,
        schedule_key: Optional[str] = None,
        status: Optional[str] = None,
    ) -> tuple[str, List[Any]]:
        """Build the shared WHERE clause; the organization is never optional."""
        clauses = ["r.organization_id=?"]
        params: List[Any] = [organization_id]
        if schedule_key:
            clauses.append("s.schedule_key=?")
            params.append(str(schedule_key))
        if status:
            if status not in RUN_STATUSES:
                raise OrganizationControlError("定时任务运行状态无效")
            clauses.append("r.status=?")
            params.append(status)
        return " WHERE " + " AND ".join(clauses), params

    def list_schedule_runs(
        self,
        organization_id: str,
        limit: int = 100,
        offset: int = 0,
        schedule_key: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        where, params = self._run_filters(organization_id, schedule_key, status)
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT r.*, s.schedule_key, s.action_json "
                "FROM organization_schedule_runs r "
                "JOIN organization_schedules s ON s.schedule_id=r.schedule_id"
                + where
                + " ORDER BY r.started_at DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(int(limit), 500)), max(0, int(offset))),
            ).fetchall()
        items: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            action = _loads(item.pop("action_json", "{}"), {})
            item["action_type"] = str(action.get("type") or "")
            item["script_id"] = str(action.get("script_id") or "")
            items.append(item)
        return items

    def count_schedule_runs(
        self,
        organization_id: str,
        schedule_key: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        where, params = self._run_filters(organization_id, schedule_key, status)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM organization_schedule_runs r "
                "JOIN organization_schedules s ON s.schedule_id=r.schedule_id" + where,
                tuple(params),
            ).fetchone()
        return int(row["total"]) if row is not None else 0
