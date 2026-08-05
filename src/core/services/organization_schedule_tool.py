"""Expose organization schedules to chat tools with role enforcement.

The Web panel stores unattended schedules in ``organization_schedules``
(system C). Chat tools previously queried the abandoned
``tenant_script_schedules`` table (system B, always empty), so the agent
"could name scripts but not find the configured schedule". This service
bridges the two: it reads/writes through ``OrganizationControlStore`` so the
live scheduler revision is bumped exactly like the Web panel does, and it
gates every write behind an owner/admin membership.

Writes only touch ``type == "script"`` schedules. Other action types may be
listed, enabled, disabled, or deleted, but their action body is never
rewritten from chat -- that keeps the model from fabricating non-script
tasks.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.core.services.organization_controls import OrganizationControlStore
from src.core.storage.organizations import OrganizationStore


class OrganizationScheduleToolService:
    """Bridge organization schedules to the ``list/manage`` chat tools."""

    def __init__(
        self,
        controls: OrganizationControlStore,
        organization_store: OrganizationStore,
        project_config: Any,
    ) -> None:
        self.controls = controls
        self.organizations = organization_store
        self.config = project_config

    # ---- reads ----------------------------------------------------------

    def list_for_tenant(self, tenant: Any) -> List[Dict[str, Any]]:
        items = self.controls.list_schedules(tenant.tenant_id)
        return [self._summarize(item) for item in items]

    def _summarize(self, schedule: Dict[str, Any]) -> Dict[str, Any]:
        action = schedule.get("action") or {}
        action_type = str(action.get("type") or "")
        script_id = str(action.get("script_id") or "")
        script_name = ""
        if action_type == "script":
            definition = self.config.scripts.get(script_id)
            if definition is not None:
                script_name = getattr(definition, "name", "") or script_id
        return {
            "id": schedule.get("id"),
            "schedule_id": schedule.get("schedule_id"),
            "enabled": bool(schedule.get("enabled")),
            "crons": schedule.get("crons") or [],
            "action_type": action_type,
            "script_id": script_id,
            "script_name": script_name,
            "parameters": action.get("parameters") or {},
            "updated_at": schedule.get("updated_at"),
        }

    # ---- approval preview (Chinese summary for the confirm dialog) ------

    def preview(self, tenant: Any, arguments: Dict[str, Any]) -> str:
        action = self._string(arguments, "action")
        schedule_id = self._string(arguments, "schedule_id")
        if action == "create":
            script_id = self._string(arguments, "script_id")
            crons = arguments.get("crons") or []
            return "新建组织定时任务：\n脚本：{}\ncron：{}\n启用：是".format(
                script_id, "、".join(str(c) for c in crons)
            )
        if action == "delete":
            return "删除组织定时任务：{}".format(schedule_id)
        if action == "enable":
            return "启用组织定时任务：{}".format(schedule_id)
        if action == "disable":
            return "停用组织定时任务：{}".format(schedule_id)
        if action == "update":
            parts = ["更新组织定时任务：{}".format(schedule_id)]
            if arguments.get("crons"):
                parts.append(
                    "cron：{}".format(
                        "、".join(str(c) for c in arguments["crons"])
                    )
                )
            if arguments.get("enabled") is not None:
                parts.append("启用：{}".format("是" if arguments["enabled"] else "否"))
            script_id = self._string(arguments, "script_id")
            if script_id:
                parts.append("脚本：{}".format(script_id))
            return "\n".join(parts)
        raise ValueError("未知的定时任务操作：{}".format(action))

    # ---- writes (role-gated) -------------------------------------------

    def manage(
        self,
        tenant: Any,
        arguments: Dict[str, Any],
        authorized_by: str = "chat",
    ) -> Dict[str, Any]:
        action = self._string(arguments, "action")
        schedule_id = self._string(arguments, "schedule_id")
        if action in {"create", "update"}:
            return self._manage_upsert(tenant, action, schedule_id, arguments)
        if action in {"enable", "disable"}:
            return self._manage_enabled(tenant, schedule_id, action == "enable")
        if action == "delete":
            return self._manage_delete(tenant, schedule_id)
        raise ValueError("未知的定时任务操作：{}".format(action))

    def _require_write_role(self, tenant: Any) -> int:
        actor = tenant.member_user_id
        if actor is None:
            raise ValueError("当前会话无法确认操作人身份，请在 Web 面板中管理定时任务")
        try:
            membership = self.organizations.membership(actor, tenant.tenant_id)
        except Exception:
            raise ValueError("当前账号不属于该组织，无法修改定时任务")
        if membership.get("role") not in {"owner", "admin"}:
            raise ValueError("只有组织所有者或管理员可以修改定时任务")
        return actor

    def _manage_upsert(
        self, tenant: Any, action: str, schedule_id: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        actor = self._require_write_role(tenant)
        if not schedule_id:
            raise ValueError("schedule_id 不能为空")
        script_id = self._string(arguments, "script_id")
        crons = arguments.get("crons") or []
        parameters = arguments.get("parameters") or {}
        existing = None
        if action == "update":
            try:
                existing = self.controls.get_schedule(tenant.tenant_id, schedule_id)
            except Exception:
                existing = None
        if action == "create":
            if not script_id:
                raise ValueError("新建定时任务必须指定 script_id")
            action_body = {
                "type": "script",
                "script_id": script_id,
                "parameters": parameters,
            }
        else:
            if existing is None:
                raise ValueError("定时任务不存在：{}".format(schedule_id))
            current_action = existing.get("action") or {}
            if str(current_action.get("type") or "") != "script":
                raise ValueError(
                    "该定时任务不是脚本类型，无法在对话中修改，请在 Web 面板操作"
                )
            action_body = {
                "type": "script",
                "script_id": script_id or str(current_action.get("script_id") or ""),
                "parameters": parameters or current_action.get("parameters") or {},
            }
        payload = {
            "crons": crons,
            "action": action_body,
            "enabled": bool(arguments.get("enabled", True)),
        }
        result = self.controls.upsert_schedule(
            tenant.tenant_id, schedule_id, payload, actor
        )
        return {"schedule": self._summarize(result), "action": action}

    def _manage_enabled(
        self, tenant: Any, schedule_id: str, enabled: bool
    ) -> Dict[str, Any]:
        actor = self._require_write_role(tenant)
        result = self.controls.set_schedule_enabled(
            tenant.tenant_id, schedule_id, enabled, actor
        )
        return {
            "schedule": self._summarize(result),
            "action": "enable" if enabled else "disable",
        }

    def _manage_delete(self, tenant: Any, schedule_id: str) -> Dict[str, Any]:
        actor = self._require_write_role(tenant)
        self.controls.delete_schedule(tenant.tenant_id, schedule_id)
        return {"deleted": schedule_id, "action": "delete"}

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _string(arguments: Dict[str, Any], key: str, default: str = "") -> str:
        value = arguments.get(key)
        if value is None:
            return default
        return str(value)
