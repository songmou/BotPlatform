"""Durably queue scheduled delivery for subscribed tenants."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.core.plugins.base import PlatformPlugin, PluginError
from src.core.plugins.manager import PluginManager
from src.core.services.agent import AgentService
from src.core.config.loader import ScheduledTask
from src.core.infrastructure.logging import log_scheduled_task
from src.core.integrations.ilink import Credentials, ILinkClient
from src.core.integrations.images import ImageSource, ImageSourceLoader
from src.core.services.notification import (
    NotificationService,
    Recipient,
    TenantRecipientStore,
)
from src.core.services.memory import MemoryService
from src.core.services.script import ScriptService
from src.core.services.script_schedule import (
    ScriptScheduleService,
    TenantScriptSchedule,
)
from src.core.services.organization_controls import OrganizationControlStore
from src.core.storage.tenants import ScheduleStore, TenantContext, TenantRegistry


class SchedulerService:
    def __init__(
        self,
        credentials: Optional[Credentials] = None,
        tasks: Optional[List[ScheduledTask]] = None,
        timezone_name: str = "UTC",
        agent_service: Optional[AgentService] = None,
        recipient_store: Optional[TenantRecipientStore] = None,
        client_factory: Callable[[Credentials], ILinkClient] = lambda credentials: ILinkClient(
            credentials=credentials
        ),
        scheduler: Optional[Any] = None,
        logger: Callable[[str, str, str, Optional[str]], None] = log_scheduled_task,
        now_provider: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        image_loader: Optional[ImageSourceLoader] = None,
        script_service: Optional[ScriptService] = None,
        tenant_registry: TenantRegistry = None,
        schedule_store: ScheduleStore = None,
        plugins: Optional[List[PlatformPlugin]] = None,
        plugin_manager: Optional[PluginManager] = None,
        memory_service: Optional[MemoryService] = None,
        notification_service: Optional[NotificationService] = None,
        script_schedule_service: Optional[ScriptScheduleService] = None,
        organization_control_store: Optional[OrganizationControlStore] = None,
    ) -> None:
        self.credentials = credentials
        self.tasks = tasks or []
        self.timezone_name = timezone_name
        self.agent_service = agent_service
        self.recipient_store = recipient_store
        self.client_factory = client_factory
        self.scheduler = scheduler or BackgroundScheduler(
            timezone=timezone_name,
            job_defaults={
                "coalesce": False,
                "max_instances": 1,
                "misfire_grace_time": 1,
            },
        )
        self.logger = logger
        self.now_provider = now_provider
        self.script_service = script_service
        self.script_schedule_service = script_schedule_service
        self.organization_control_store = organization_control_store
        self.tenant_registry = tenant_registry
        self.schedule_store = schedule_store
        self.memory_service = memory_service
        self.plugin_manager = plugin_manager
        self._plugins: Dict[str, PlatformPlugin] = {
            plugin.id: plugin
            for plugin in (plugins or [])
        }
        if self.tenant_registry is None or self.schedule_store is None:
            raise ValueError("多用户调度器需要租户注册表和订阅存储")
        if notification_service is not None:
            self.notification_service = notification_service
        elif recipient_store is not None:
            self.notification_service = NotificationService(
                credentials_loader=lambda: self.credentials,
                recipient_store=self.recipient_store,
                client_factory=self.client_factory,
                image_loader=image_loader,
            )
        else:
            self.notification_service = None
        self._started = False
        self._script_schedule_job_ids: set[str] = set()
        self._organization_schedule_job_ids: set[str] = set()
        self._organization_schedule_revisions: Dict[str, int] = {}
        if self.script_schedule_service is not None:
            self.script_schedule_service.set_reload_callback(
                self.reload_script_schedules
            )
        if self.script_service is not None:
            add_listener = getattr(
                self.script_service, "add_completion_listener", None
            )
            if callable(add_listener):
                add_listener(self._on_script_run_complete)

    @property
    def enabled_count(self) -> int:
        return sum(1 for task in self.tasks if task.enabled)

    def start(self) -> None:
        for task in self.tasks:
            if not task.enabled:
                continue
            crons = task.crons or [task.cron]
            for index, cron in enumerate(crons):
                trigger = CronTrigger.from_crontab(cron, timezone=self.timezone_name)
                job_id = task.id if len(crons) == 1 else "{}#{}".format(task.id, index + 1)
                self.scheduler.add_job(
                    self.run_task,
                    trigger=trigger,
                    args=[task],
                    id=job_id,
                    replace_existing=True,
                    coalesce=False,
                    max_instances=1,
                    misfire_grace_time=1,
                )
        for plugin_id, job in self._plugin_background_jobs():
            self.scheduler.add_job(
                self.run_plugin_background_job,
                trigger=IntervalTrigger(
                    seconds=job.interval_seconds,
                    timezone=self.timezone_name,
                ),
                args=[plugin_id, job.id],
                id="plugin:{}:{}".format(plugin_id, job.id),
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=job.interval_seconds,
            )
        if self.memory_service is not None:
            self.memory_service.recover_dirty()
            self.scheduler.add_job(
                self.memory_service.run_daily_maintenance,
                trigger=CronTrigger(
                    hour=3, minute=10, timezone=self.timezone_name
                ),
                id="soul_daily_maintenance",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=3600,
            )
            self.scheduler.add_job(
                self.memory_service.run_weekly_compaction,
                trigger=CronTrigger(
                    day_of_week="sun",
                    hour=3,
                    minute=30,
                    timezone=self.timezone_name,
                ),
                id="soul_weekly_compaction",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=3600,
            )
        self._register_script_schedules()
        self._register_organization_schedules()
        if self.organization_control_store is not None:
            self.scheduler.add_job(
                self._refresh_organization_schedules,
                trigger=IntervalTrigger(seconds=5, timezone=self.timezone_name),
                id="organization-schedules:refresh",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
        self.scheduler.start()
        self._started = True

    def shutdown(self) -> None:
        if not self._started:
            return
        self.scheduler.shutdown(wait=True)
        self._started = False

    def reload_tasks(self, tasks: List[ScheduledTask]) -> None:
        self.tasks = tasks
        if not self._started:
            return
        self.scheduler.remove_all_jobs()
        self._script_schedule_job_ids.clear()
        self._organization_schedule_job_ids.clear()
        for task in self.tasks:
            if not task.enabled:
                continue
            crons = task.crons or [task.cron]
            for index, cron in enumerate(crons):
                trigger = CronTrigger.from_crontab(cron, timezone=self.timezone_name)
                job_id = task.id if len(crons) == 1 else "{}#{}".format(task.id, index + 1)
                self.scheduler.add_job(
                    self.run_task,
                    trigger=trigger,
                    args=[task],
                    id=job_id,
                    replace_existing=True,
                    coalesce=False,
                    max_instances=1,
                    misfire_grace_time=1,
                )
        self._register_script_schedules()
        self._register_organization_schedules()

    @staticmethod
    def _script_schedule_job_id(item: TenantScriptSchedule, index: int) -> str:
        return "tenant-script:{}:{}:{}".format(
            item.tenant_id, item.schedule_id, index + 1
        )

    def _register_script_schedules(self) -> None:
        if self.script_schedule_service is None or self.script_service is None:
            return
        for item in self.script_schedule_service.store.enabled():
            try:
                current_hash = self.script_service.current_hash(item.script_id)
                if current_hash != item.authorized_sha256:
                    raise ValueError("脚本版本已变化，原定时授权失效")
            except Exception as exc:
                self._pause_script_schedule(item, str(exc))
                continue
            for index, cron in enumerate(item.crons):
                job_id = self._script_schedule_job_id(item, index)
                self.scheduler.add_job(
                    self.run_script_schedule,
                    trigger=CronTrigger.from_crontab(
                        cron, timezone=self.timezone_name
                    ),
                    args=[item.tenant_id, item.schedule_id],
                    id=job_id,
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=30,
                )
                self._script_schedule_job_ids.add(job_id)

    def reload_script_schedules(self) -> None:
        if not self._started:
            return
        for job_id in list(self._script_schedule_job_ids):
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass
        self._script_schedule_job_ids.clear()
        self._register_script_schedules()

    def run_script_schedule(self, tenant_id: str, schedule_id: str) -> bool:
        if self.script_schedule_service is None or self.script_service is None:
            return False
        item = self.script_schedule_service.store.get(tenant_id, schedule_id)
        if item is None or not item.enabled:
            return False
        try:
            current_hash = self.script_service.current_hash(item.script_id)
            if current_hash != item.authorized_sha256:
                raise ValueError("脚本版本已变化，原定时授权失效")
            tenant = self.tenant_registry.get(tenant_id)
            if tenant is None:
                raise ValueError("租户不存在")
            recipient = self.recipient_store.load(tenant_id)
            result = self.script_service.submit(
                tenant,
                item.script_id,
                item.parameters,
                trigger="tenant_schedule:{}".format(schedule_id),
                recipient=recipient,
            )
            status = str(result.get("status", "running"))
            self.script_schedule_service.store.mark_run(
                tenant_id, schedule_id, str(result.get("run_id", "")), status
            )
            self.logger(
                schedule_id,
                "跳过" if status == "skipped" else "已提交",
                "脚本={}，任务={}".format(
                    item.script_id, result.get("run_id", "-")
                ),
                tenant_id,
            )
            return status != "skipped"
        except Exception as exc:
            self._pause_script_schedule(item, str(exc))
            self.reload_script_schedules()
            return False

    def _pause_script_schedule(
        self, item: TenantScriptSchedule, detail: str
    ) -> None:
        reason = "脚本计划已暂停：{}".format(detail)
        self.script_schedule_service.store.disable(
            item.tenant_id, item.schedule_id, reason
        )
        if self.notification_service is not None:
            self.notification_service.enqueue_text_to_tenant(
                item.tenant_id,
                "【定时脚本】计划 {} 已暂停。\n{}".format(
                    item.schedule_id, detail
                ),
                source_type="schedule",
                source_key="script-schedule:{}:paused".format(item.schedule_id),
            )
        self.logger(item.schedule_id, "失败", reason, item.tenant_id)

    def _on_script_run_complete(self, run) -> None:
        prefix = "tenant_schedule:"
        if (
            self.script_schedule_service is None
            or not run.trigger.startswith(prefix)
        ):
            return
        schedule_id = run.trigger[len(prefix):]
        self.script_schedule_service.store.mark_run(
            run.tenant_id, schedule_id, run.run_id, run.status
        )

    @staticmethod
    def _organization_schedule_job_id(item: Dict[str, Any], index: int) -> str:
        return "organization-schedule:{}:{}".format(item["schedule_id"], index + 1)

    def _schedule_revision_snapshot(self) -> Dict[str, int]:
        if self.organization_control_store is None:
            return {}
        return {
            organization_id: int(row.get("schedules_revision", 0))
            for organization_id, row in
            self.organization_control_store.runtime_revisions().items()
        }

    def _register_organization_schedules(self) -> None:
        controls = self.organization_control_store
        if controls is None:
            return
        for item in controls.enabled_schedules():
            action = item["action"]
            try:
                current = controls.dependency_revision(action)
                if current != item["dependency_revision"]:
                    raise ValueError("依赖版本已变化，需要组织管理员重新确认")
            except Exception as exc:
                controls.pause_schedule(
                    item["schedule_id"], "定时任务已暂停：{}".format(exc)
                )
                continue
            for index, cron in enumerate(item["crons"]):
                job_id = self._organization_schedule_job_id(item, index)
                self.scheduler.add_job(
                    self.run_organization_schedule,
                    trigger=CronTrigger.from_crontab(
                        cron, timezone=self.timezone_name
                    ),
                    args=[item["organization_id"], item["id"]],
                    id=job_id,
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=30,
                )
                self._organization_schedule_job_ids.add(job_id)
        self._organization_schedule_revisions = self._schedule_revision_snapshot()

    def _refresh_organization_schedules(self) -> None:
        current = self._schedule_revision_snapshot()
        if current == self._organization_schedule_revisions:
            return
        for job_id in list(self._organization_schedule_job_ids):
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass
        self._organization_schedule_job_ids.clear()
        self._register_organization_schedules()

    def reload_organization_schedules(self) -> None:
        """Force an immediate reload after panel-side database migration."""
        self._organization_schedule_revisions = {}
        if self._started:
            self._refresh_organization_schedules()

    def run_organization_schedule(
        self, organization_id: str, schedule_key: str
    ) -> bool:
        controls = self.organization_control_store
        if controls is None or self.notification_service is None:
            return False
        item = controls.get_schedule(organization_id, schedule_key)
        if not item["enabled"]:
            return False
        run_id = controls.record_schedule_run(
            item["schedule_id"], organization_id, "running", "任务开始执行"
        )
        try:
            action = item["action"]
            current = controls.dependency_revision(action)
            if current != item["dependency_revision"]:
                controls.finish_schedule_run(
                    run_id, "failed", "依赖版本已变化，需要组织管理员重新确认"
                )
                controls.pause_schedule(
                    item["schedule_id"],
                    "依赖版本已变化，需要组织管理员重新确认",
                )
                self._refresh_organization_schedules()
                return False
            address_store = getattr(self.notification_service, "address_store", None)
            endpoint = (
                address_store.latest_endpoint(organization_id)
                if address_store is not None
                else None
            )
            if endpoint is None:
                controls.finish_schedule_run(
                    run_id, "skipped", "当前组织没有有效的最近活跃渠道用户"
                )
                return False
            message = ""
            action_type = str(action.get("type") or "")
            tenant = self.tenant_registry.get(organization_id)
            if action_type == "text":
                message = str(action.get("content") or "")
            elif action_type == "agent_prompt":
                if self.agent_service is None:
                    raise ValueError("智能体服务不可用")
                outcome = self.agent_service.chat(
                    tenant,
                    str(action.get("prompt") or ""),
                    agent_id=str(action.get("agent_id") or ""),
                    source="schedule",
                    allow_tools=True,
                    allow_private_context=False,
                )
                message = outcome.text
            elif action_type == "script":
                if self.script_service is None:
                    raise ValueError("平台脚本服务不可用")
                result = self.script_service.submit(
                    tenant,
                    str(action.get("script_id") or ""),
                    action.get("parameters", {}),
                    trigger="organization_schedule:{}".format(item["schedule_id"]),
                    recipient=None,
                )
                status = str(result.get("status") or "running")
                controls.finish_schedule_run(
                    run_id,
                    "skipped" if status == "skipped" else "succeeded",
                    "脚本任务已提交：{}".format(result.get("run_id", "-")),
                )
                return status != "skipped"
            elif action_type == "plugin":
                if self.plugin_manager is None:
                    raise ValueError("平台插件服务不可用")
                result = self.plugin_manager.execute(
                    str(action.get("tool_name") or ""),
                    action.get("parameters", {}),
                    tenant,
                )
                if isinstance(result, dict):
                    message = str(result.get("summary") or "")
                elif isinstance(result, str):
                    message = result
                if not message:
                    controls.finish_schedule_run(
                        run_id, "succeeded", "插件任务执行成功，无需发送消息"
                    )
                    return True
            else:
                raise ValueError("不支持的组织定时任务动作")
            self.notification_service.enqueue_text_to_tenant(
                organization_id,
                message,
                source_type="organization_schedule",
                source_ref=item["schedule_id"],
                attempt_immediately=True,
            )
            controls.finish_schedule_run(run_id, "succeeded", "消息已入队")
            return True
        except Exception as exc:
            controls.finish_schedule_run(run_id, "failed", str(exc))
            self.logger(schedule_key, "失败", str(exc), organization_id)
            return False

    def run_task(self, task: ScheduledTask) -> bool:
        tenants = self.schedule_store.enabled_tenants(task.id)
        any_success = False
        for tenant in tenants:
            if self._run_task_for_tenant(task, tenant):
                any_success = True
        if not tenants:
            self.logger(task.id, "跳过", "没有用户订阅此任务", None)
        return any_success

    def _plugin_background_jobs(self):
        if self.plugin_manager is not None:
            return self.plugin_manager.background_jobs()
        result = []
        for plugin in self._plugins.values():
            for job in getattr(plugin, "background_jobs", []):
                result.append((plugin.id, job))
        return result

    def run_plugin_background_job(self, plugin_id: str, job_id: str) -> bool:
        try:
            if self.plugin_manager is not None:
                result = self.plugin_manager.run_background_job(
                    plugin_id, job_id, self.now_provider()
                )
            else:
                plugin = self._plugins.get(plugin_id)
                runner = getattr(plugin, "run_background_job", None) if plugin else None
                if not callable(runner):
                    return False
                result = runner(job_id, self.now_provider())
            self.logger(
                "plugin:{}:{}".format(plugin_id, job_id),
                "完成",
                "插件后台任务已执行",
                None,
            )
            return bool(result)
        except Exception as exc:
            self.logger(
                "plugin:{}:{}".format(plugin_id, job_id),
                "失败",
                str(exc),
                None,
            )
            return False

    def _run_task_for_tenant(
        self, task: ScheduledTask, tenant: TenantContext
    ) -> bool:
        recipient: Optional[Recipient] = None
        try:
            recipient = self.recipient_store.load(tenant.tenant_id)
            if task.action.type == "script":
                if self.script_service is None:
                    raise ValueError("固定脚本服务不可用")
                result = self.script_service.submit(
                    tenant,
                    task.action.script_id or "",
                    task.action.parameters,
                    trigger="schedule",
                    recipient=recipient,
                )
                status = str(result.get("status", "running"))
                self.logger(
                    task.id,
                    "跳过" if status == "skipped" else "已提交",
                    "脚本={}，任务={}".format(
                        task.action.script_id, result.get("run_id", "-")
                    ),
                    tenant.tenant_id,
                )
                return status != "skipped"
            if task.action.type == "plugin":
                return self._run_plugin_task(task, tenant, recipient)
            if task.condition is not None:
                if recipient is None:
                    self.logger(task.id, "跳过", "用户尚无有效收件地址", tenant.tenant_id)
                    return False
                return self._run_conditional_task(
                    task, recipient, tenant_id=tenant.tenant_id, tenant=tenant
                )
            _, detail = self._deliver_action(
                task, tenant_id=tenant.tenant_id, tenant=tenant
            )
            self.logger(task.id, "已入队", detail, tenant.tenant_id)
            return True
        except Exception as exc:
            self.logger(task.id, "失败", str(exc), tenant.tenant_id)
            return False

    def _run_plugin_task(
        self,
        task: ScheduledTask,
        tenant: TenantContext,
        recipient: Optional[Recipient],
    ) -> bool:
        plugin_id = task.action.plugin_id or ""
        tool_name = task.action.tool_name or ""
        try:
            if self.plugin_manager is not None:
                manifest = self.plugin_manager.manifest_for_tool(tool_name)
                if manifest is None or manifest.id != plugin_id:
                    raise ValueError("插件 {} 未加载或不存在".format(plugin_id))
                result = self.plugin_manager.execute(
                    tool_name,
                    task.action.parameters,
                    tenant,
                )
            else:
                plugin = self._plugins.get(plugin_id)
                if plugin is None:
                    raise ValueError("插件 {} 未加载或不存在".format(plugin_id))
                result = plugin.execute(tool_name, task.action.parameters, tenant)
        except PluginError as exc:
            raise ValueError("插件执行失败：{}".format(exc)) from exc
        summary = ""
        if isinstance(result, dict):
            summary = str(result.get("summary", ""))
        elif isinstance(result, str):
            summary = result
        if summary:
            self.notification_service.enqueue_text_to_tenant(
                tenant.tenant_id,
                summary,
                source_type="schedule",
            )
        self.logger(
            task.id,
            "已入队" if summary else "成功",
            "插件={}，工具={}".format(plugin_id, tool_name),
            tenant.tenant_id,
        )
        return True

    def _message_for_task(self, task: ScheduledTask, tenant: Optional[TenantContext] = None) -> str:
        if task.action.type == "text":
            return task.action.content or ""
        if tenant is None or self.agent_service is None:
            return self.agent_service.generate(
                task.action.agent_id or "", task.action.prompt or ""
            ) if self.agent_service else ""
        chat_options = {"agent_id": task.action.agent_id or None}
        if hasattr(self.agent_service, "model_analytics_store"):
            chat_options["source"] = "schedule"
        outcome = self.agent_service.chat(
            tenant,
            task.action.prompt or "",
            **chat_options,
        )
        if hasattr(outcome, "approval_id"):
            return "定时任务触发了一个需要确认的操作，请在对话中回复确认或取消：\n\n" + outcome.text
        return outcome.text

    def _deliver_action(
        self,
        task: ScheduledTask,
        tenant_id: str,
        tenant: Optional[TenantContext] = None,
    ):
        if task.action.type == "image":
            source = (
                ImageSource.local(Path(task.action.image_path or ""))
                if task.action.image_path
                else ImageSource.remote(task.action.image_url or "")
            )
            caption = task.action.caption or ""
            result = self.notification_service.enqueue_image_to_tenant(
                tenant_id,
                source,
                caption,
                source_type="schedule",
            )
            detail = "[图片]{}".format(" " + caption if caption else "")
            return result, detail

        message = self._message_for_task(task, tenant)
        result = self.notification_service.enqueue_text_to_tenant(
            tenant_id,
            message,
            source_type="schedule",
        )
        return result, message

    def _run_conditional_task(
        self,
        task: ScheduledTask,
        recipient: Recipient,
        tenant_id: str,
        tenant: Optional[TenantContext] = None,
    ) -> bool:
        condition = task.condition
        if condition is None or condition.type != "inactivity_once":
            raise ValueError("不支持的定时任务条件")

        updated_at = datetime.fromisoformat(recipient.updated_at.replace("Z", "+00:00"))
        if updated_at.tzinfo is None:
            raise ValueError("用户收件地址 updated_at 必须包含时区")
        now = self.now_provider()
        if now.tzinfo is None:
            raise ValueError("当前时间必须包含时区")
        inactive_hours = (
            now.astimezone(timezone.utc) - updated_at.astimezone(timezone.utc)
        ).total_seconds() / 3600
        if inactive_hours < 0:
            raise ValueError("用户收件地址 updated_at 晚于当前时间")
        if inactive_hours < condition.after_hours:
            self.logger(
                task.id,
                "跳过",
                "用户静默时间尚未达到 {} 小时".format(condition.after_hours),
                recipient.user_id,
            )
            return False

        claimed = self.recipient_store.claim_task_attempt(
            tenant_id, task.id, recipient
        )
        if not claimed:
            self.logger(task.id, "跳过", "本轮静默提醒已处理或用户已更新", recipient.user_id)
            return False

        if inactive_hours >= condition.before_hours:
            self.logger(
                task.id,
                "跳过",
                "用户静默已达到 {} 小时，超过主动回复窗口".format(
                    condition.before_hours
                ),
                recipient.user_id,
            )
            return False

        _, detail = self._deliver_action(task, tenant_id=tenant_id, tenant=tenant)
        self.logger(task.id, "已入队", detail, tenant_id)
        return True
