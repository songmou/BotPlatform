"""Tenant-isolated script schedule persistence and approval-time validation.

.. deprecated::
    Superseded by ``organization_schedules`` (system C), the production store
    for unattended tasks managed from the Web panel. The
    ``tenant_script_schedules`` table has never been populated in production,
    so this service registers zero scheduler jobs. It is kept only because
    ``SchedulerService`` still wires it for backward compatibility; the chat
    ``list/manage`` tools now read/write through
    ``OrganizationScheduleToolService`` instead. Delete this module together
    with its Web API (``src/api/routers/scripts.py`` ``*script-schedule*``
    endpoints) and the scheduler integration in a dedicated cleanup pass.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from apscheduler.triggers.cron import CronTrigger

from src.core.services.script import ScriptService
from src.core.storage.tenants import TenantContext, TenantRegistry


_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TenantScriptSchedule:
    tenant_id: str
    schedule_id: str
    script_id: str
    parameters: Dict[str, Any]
    crons: List[str]
    enabled: bool
    authorized_sha256: str
    authorized_at: str
    authorized_by: str
    created_at: str
    updated_at: str
    last_run_id: Optional[str] = None
    last_status: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ScriptScheduleStore:
    def __init__(self, registry: TenantRegistry) -> None:
        self.registry = registry

    @staticmethod
    def _row(row: Any) -> TenantScriptSchedule:
        return TenantScriptSchedule(
            tenant_id=str(row["tenant_id"]),
            schedule_id=str(row["schedule_id"]),
            script_id=str(row["script_id"]),
            parameters=json.loads(str(row["parameters_json"])),
            crons=json.loads(str(row["crons_json"])),
            enabled=bool(row["enabled"]),
            authorized_sha256=str(row["authorized_sha256"]),
            authorized_at=str(row["authorized_at"]),
            authorized_by=str(row["authorized_by"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_run_id=row["last_run_id"],
            last_status=row["last_status"],
        )

    def list(self, tenant_id: Optional[str] = None) -> List[TenantScriptSchedule]:
        with self.registry.database.read() as connection:
            if tenant_id is None:
                rows = connection.execute(
                    "SELECT * FROM tenant_script_schedules "
                    "ORDER BY tenant_id, schedule_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM tenant_script_schedules WHERE tenant_id=? "
                    "ORDER BY schedule_id",
                    (tenant_id,),
                ).fetchall()
        return [self._row(row) for row in rows]

    def enabled(self) -> List[TenantScriptSchedule]:
        with self.registry.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM tenant_script_schedules WHERE enabled=1 "
                "ORDER BY tenant_id, schedule_id"
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, tenant_id: str, schedule_id: str) -> Optional[TenantScriptSchedule]:
        with self.registry.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM tenant_script_schedules "
                "WHERE tenant_id=? AND schedule_id=?",
                (tenant_id, schedule_id),
            ).fetchone()
        return self._row(row) if row is not None else None

    def save(self, item: TenantScriptSchedule) -> TenantScriptSchedule:
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO tenant_script_schedules("
                "tenant_id, schedule_id, script_id, parameters_json, crons_json, "
                "enabled, authorized_sha256, authorized_at, authorized_by, "
                "created_at, updated_at, last_run_id, last_status"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, schedule_id) DO UPDATE SET "
                "script_id=excluded.script_id, parameters_json=excluded.parameters_json, "
                "crons_json=excluded.crons_json, enabled=excluded.enabled, "
                "authorized_sha256=excluded.authorized_sha256, "
                "authorized_at=excluded.authorized_at, authorized_by=excluded.authorized_by, "
                "updated_at=excluded.updated_at",
                (
                    item.tenant_id,
                    item.schedule_id,
                    item.script_id,
                    json.dumps(item.parameters, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(item.crons, ensure_ascii=False, separators=(",", ":")),
                    1 if item.enabled else 0,
                    item.authorized_sha256,
                    item.authorized_at,
                    item.authorized_by,
                    item.created_at,
                    item.updated_at,
                    item.last_run_id,
                    item.last_status,
                ),
            )
        return self.get(item.tenant_id, item.schedule_id) or item

    def delete(self, tenant_id: str, schedule_id: str) -> bool:
        with self.registry.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM tenant_script_schedules WHERE tenant_id=? AND schedule_id=?",
                (tenant_id, schedule_id),
            )
        return cursor.rowcount > 0

    def disable(self, tenant_id: str, schedule_id: str, status: str) -> None:
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE tenant_script_schedules SET enabled=0, last_status=?, updated_at=? "
                "WHERE tenant_id=? AND schedule_id=?",
                (status, _utc_now(), tenant_id, schedule_id),
            )

    def mark_run(
        self, tenant_id: str, schedule_id: str, run_id: str, status: str
    ) -> None:
        with self.registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE tenant_script_schedules SET last_run_id=?, last_status=?, updated_at=? "
                "WHERE tenant_id=? AND schedule_id=?",
                (run_id, status, _utc_now(), tenant_id, schedule_id),
            )


class ScriptScheduleService:
    def __init__(
        self,
        registry: TenantRegistry,
        script_service: ScriptService,
        timezone_name: str,
        store: Optional[ScriptScheduleStore] = None,
    ) -> None:
        self.registry = registry
        self.script_service = script_service
        self.timezone_name = timezone_name
        self.store = store or ScriptScheduleStore(registry)
        self._reload_callback: Optional[Callable[[], None]] = None

    def set_reload_callback(self, callback: Callable[[], None]) -> None:
        self._reload_callback = callback

    def reload_scheduler(self) -> None:
        self._reload()

    def list_for_tenant(self, tenant: TenantContext) -> List[Dict[str, Any]]:
        self._require_tenant(tenant)
        return [item.to_dict() for item in self.store.list(tenant.tenant_id)]

    def preview(self, tenant: TenantContext, arguments: Dict[str, Any]) -> str:
        self._require_tenant(tenant)
        action = self._action(arguments)
        schedule_id = self._schedule_id(arguments)
        if action == "delete":
            existing = self._existing(tenant, schedule_id)
            return "删除无人值守脚本计划：{}\n脚本：{}".format(
                schedule_id, existing.script_id
            )
        if action in {"disable"}:
            existing = self._existing(tenant, schedule_id)
            return "停用无人值守脚本计划：{}\n脚本：{}".format(
                schedule_id, existing.script_id
            )
        normalized = self._normalized_change(tenant, arguments)
        lines = [
            "{}无人值守脚本计划：{}".format(
                "创建" if action == "create" else "更新", schedule_id
            ),
            "脚本：{}（{}）".format(normalized["script_name"], normalized["script_id"]),
            "参数：{}".format(
                json.dumps(normalized["parameters"], ensure_ascii=False, sort_keys=True)
            ),
            "时间：{}（{}）".format("；".join(normalized["crons"]), self.timezone_name),
            "脚本版本：{}".format(normalized["sha256"][:12]),
            "审批后将按计划自动执行，触发时不再逐次询问。",
        ]
        return "\n".join(lines)

    def manage(
        self,
        tenant: TenantContext,
        arguments: Dict[str, Any],
        authorized_by: str = "chat",
    ) -> Dict[str, Any]:
        self._require_tenant(tenant)
        action = self._action(arguments)
        schedule_id = self._schedule_id(arguments)
        if action == "delete":
            self._existing(tenant, schedule_id)
            self.store.delete(tenant.tenant_id, schedule_id)
            self._reload()
            return {"schedule_id": schedule_id, "status": "deleted"}
        if action == "disable":
            self._existing(tenant, schedule_id)
            self.store.disable(tenant.tenant_id, schedule_id, "disabled")
            self._reload()
            return {"schedule_id": schedule_id, "status": "disabled"}

        normalized = self._normalized_change(tenant, arguments)
        existing = self.store.get(tenant.tenant_id, schedule_id)
        now = _utc_now()
        item = TenantScriptSchedule(
            tenant_id=tenant.tenant_id,
            schedule_id=schedule_id,
            script_id=normalized["script_id"],
            parameters=normalized["parameters"],
            crons=normalized["crons"],
            enabled=normalized["enabled"],
            authorized_sha256=normalized["sha256"],
            authorized_at=now,
            authorized_by=authorized_by,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            last_run_id=existing.last_run_id if existing else None,
            last_status=existing.last_status if existing else None,
        )
        saved = self.store.save(item)
        self._reload()
        return saved.to_dict()

    def _normalized_change(
        self, tenant: TenantContext, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        action = self._action(arguments)
        schedule_id = self._schedule_id(arguments)
        existing = self.store.get(tenant.tenant_id, schedule_id)
        if action != "create" and existing is None:
            raise ValueError("脚本计划不存在：{}".format(schedule_id))
        if action == "create" and existing is not None:
            raise ValueError("脚本计划已存在：{}".format(schedule_id))
        script_id = arguments.get("script_id") or (existing.script_id if existing else "")
        raw_parameters = (
            arguments["parameters"]
            if "parameters" in arguments
            else (existing.parameters if existing else {})
        )
        definition, parameters = self.script_service.normalize(
            str(script_id), raw_parameters
        )
        raw_crons = (
            arguments["crons"]
            if "crons" in arguments
            else (existing.crons if existing else [])
        )
        crons = self._validate_crons(raw_crons)
        enabled = arguments.get(
            "enabled", existing.enabled if existing is not None else True
        )
        if action == "enable":
            enabled = True
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        return {
            "script_id": definition.id,
            "script_name": definition.name,
            "parameters": parameters,
            "crons": crons,
            "enabled": enabled,
            "sha256": self.script_service.current_hash(definition.id),
        }

    def _validate_crons(self, raw: Any) -> List[str]:
        if not isinstance(raw, list) or not 1 <= len(raw) <= 8 or any(
            not isinstance(item, str) or not item.strip() for item in raw
        ):
            raise ValueError("crons 必须包含 1 到 8 个五段 cron 字符串")
        crons = [item.strip() for item in raw]
        if len(set(crons)) != len(crons):
            raise ValueError("crons 不能包含重复时间")
        for cron in crons:
            try:
                CronTrigger.from_crontab(cron, timezone=self.timezone_name)
            except (TypeError, ValueError) as exc:
                raise ValueError("无效的五段 cron：{}".format(cron)) from exc
        return crons

    @staticmethod
    def _action(arguments: Dict[str, Any]) -> str:
        action = arguments.get("action")
        if action not in {"create", "update", "enable", "disable", "delete"}:
            raise ValueError("action 仅支持 create、update、enable、disable 或 delete")
        return str(action)

    @staticmethod
    def _schedule_id(arguments: Dict[str, Any]) -> str:
        value = arguments.get("schedule_id")
        if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
            raise ValueError("schedule_id 格式无效")
        return value

    def _existing(
        self, tenant: TenantContext, schedule_id: str
    ) -> TenantScriptSchedule:
        item = self.store.get(tenant.tenant_id, schedule_id)
        if item is None:
            raise ValueError("脚本计划不存在：{}".format(schedule_id))
        return item

    def _require_tenant(self, tenant: TenantContext) -> None:
        if self.registry.get(tenant.tenant_id) != tenant:
            raise ValueError("租户身份不匹配")

    def _reload(self) -> None:
        if self._reload_callback is not None:
            self._reload_callback()
