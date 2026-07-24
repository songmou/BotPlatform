"""Best-effort scheduled delivery for subscribed tenants."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from src.core.plugins.base import PlatformPlugin, PluginError
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
from src.core.services.script import ScriptService
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
        self.tenant_registry = tenant_registry
        self.schedule_store = schedule_store
        self._plugins: Dict[str, PlatformPlugin] = {
            plugin.id: plugin for plugin in (plugins or [])
        }
        if self.tenant_registry is None or self.schedule_store is None:
            raise ValueError("多用户调度器需要租户注册表和订阅存储")
        if credentials is not None and recipient_store is not None:
            self.notification_service = NotificationService(
                credentials_loader=lambda: self.credentials,
                recipient_store=self.recipient_store,
                client_factory=self.client_factory,
                image_loader=image_loader,
            )
        else:
            self.notification_service = None
        self._started = False

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

    def run_task(self, task: ScheduledTask) -> bool:
        tenants = self.schedule_store.enabled_tenants(task.id)
        any_success = False
        for tenant in tenants:
            if self._run_task_for_tenant(task, tenant):
                any_success = True
        if not tenants:
            self.logger(task.id, "跳过", "没有用户订阅此任务", None)
        return any_success

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
            if recipient is None:
                self.logger(task.id, "跳过", "用户尚无有效收件地址", tenant.tenant_id)
                return False
            if task.condition is not None:
                return self._run_conditional_task(
                    task, recipient, tenant_id=tenant.tenant_id, tenant=tenant
                )
            result, detail = self._deliver_action(task, recipient=recipient, tenant=tenant)
            self.logger(task.id, "成功", detail, tenant.tenant_id)
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
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            raise ValueError("插件 {} 未加载或不存在".format(plugin_id))
        try:
            result = plugin.execute(tool_name, task.action.parameters, tenant)
        except PluginError as exc:
            raise ValueError("插件执行失败：{}".format(exc)) from exc
        summary = ""
        if isinstance(result, dict):
            summary = str(result.get("summary", ""))
        elif isinstance(result, str):
            summary = result
        if recipient is not None and summary:
            self.notification_service.send_text_to(recipient, summary)
        self.logger(
            task.id,
            "成功",
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
        outcome = self.agent_service.chat(
            tenant,
            task.action.prompt or "",
            agent_id=task.action.agent_id or None,
        )
        if hasattr(outcome, "approval_id"):
            return "定时任务触发了一个需要确认的操作，请在对话中回复确认或取消：\n\n" + outcome.text
        return outcome.text

    def _deliver_action(self, task: ScheduledTask, recipient: Recipient, tenant: Optional[TenantContext] = None):
        if task.action.type == "image":
            source = (
                ImageSource.local(Path(task.action.image_path or ""))
                if task.action.image_path
                else ImageSource.remote(task.action.image_url or "")
            )
            caption = task.action.caption or ""
            result = self.notification_service.send_image_to(
                recipient,
                source,
                caption=caption,
            )
            detail = "[图片]{}".format(" " + caption if caption else "")
            return result, detail

        message = self._message_for_task(task, tenant)
        result = self.notification_service.send_text_to(recipient, message)
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

        result, detail = self._deliver_action(task, recipient=recipient, tenant=tenant)
        self.logger(task.id, "成功", detail, result.recipient_user_id)
        return True
