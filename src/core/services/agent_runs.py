"""Durable, fair background execution and dependency-aware agent coordination."""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.core.modeling import CanonicalMessage, GenerationOptions, ModelCallContext, ModelError, ModelRequest
from src.core.services.react import REACT_PROMPT, ReactEngine, decode_message, encode_message, new_state
from src.core.storage.agent_runs import AgentRunError, AgentRunStore, TERMINAL, WAITING
from src.core.storage.tenants import TenantContext
from src.core.tooling.models import ApprovalRequired, FinalAnswer, ToolError

logger = logging.getLogger(__name__)


class AgentRunService:
    def __init__(
        self,
        agent_service: Any,
        registry: Any,
        organizations: Any = None,
        notification_service: Any = None,
        max_workers: int = 4,
    ) -> None:
        self.owner = agent_service
        self.registry = registry
        self.organizations = organizations or getattr(registry, "organization_store", None)
        self.notifications = notification_service
        self.store = AgentRunStore(registry.database)
        self.max_workers = max_workers
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: List[threading.Thread] = []
        self._start_lock = threading.Lock()
        self._notification_attempts: Dict[str, float] = {}

    def start(self) -> None:
        with self._start_lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            if self._threads:
                return
            self._stop.clear()
            for index in range(self.max_workers):
                thread = threading.Thread(target=self._worker, name="agent-worker-{}".format(index), daemon=True)
                self._threads.append(thread)
                thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        # Checkpointed in-flight steps keep their lease until they finish. A
        # process restart recovers them when the lease expires.
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads = [thread for thread in self._threads if thread.is_alive()]

    def preset(self, tenant: TenantContext, agent_id: str):
        resources = self.owner.resource_store
        try:
            if resources is None:
                presets = self.owner.agents
            else:
                presets = resources.effective_agent_presets(tenant.tenant_id)
            preset = presets.get(agent_id)
            if preset is None or not preset.enabled:
                raise AgentRunError("智能体不存在、已停用或未获组织授权")
            if resources is not None:
                plugins = {item["resource_id"] for item in resources.list_effective(tenant.tenant_id, "plugins")}
                mcp = {item["resource_id"] for item in resources.list_effective(tenant.tenant_id, "mcp")}
                preset = replace(
                    preset,
                    plugin_tools={key: names for key, names in preset.plugin_tools.items() if key in plugins},
                    mcp_servers=[key for key in preset.mcp_servers if key in mcp],
                )
            return preset
        except AgentRunError:
            raise
        except Exception as exc:
            raise AgentRunError("无法读取组织智能体授权，已暂停执行") from exc

    def submit(
        self,
        tenant: TenantContext,
        question: str,
        agent_id: Optional[str] = None,
        conversation_id: str = "direct",
        source: str = "internal",
        *,
        agent_ids: Optional[List[str]] = None,
        allow_tools: bool = True,
        allow_private_context: bool = True,
        image_bytes: Optional[bytes] = None,
        history: Optional[list] = None,
        regenerate: bool = False,
        parent_run_id: Optional[str] = None,
        task_id: str = "",
        model_mode: Optional[str] = None,
        completion_criteria: Optional[List[str]] = None,
        notify: bool = False,
        notification_endpoint_id: Optional[str] = None,
        parent_workflow_id: Optional[str] = None,
        parent_deadline: Optional[float] = None,
        schedule_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        from src.core.services.agent_tools import resolve_tool_names

        preset = self.preset(tenant, agent_id or self.owner.app_config.default_agent)
        self._check_member(tenant, source)
        if self.owner.tool_runtime is not None:
            self.owner.tool_runtime.bind_tenant(tenant)
            self.owner.tool_runtime.bind_agent_datasources(preset.datasources)
        mode = model_mode or self.owner._mode_for(tenant)
        model = self.owner.model_router.session(mode, has_image=bool(image_bytes), start_profile_id=preset.model)
        ocr_context, ocr_error = self.owner._automatic_ocr_context(image_bytes, preset)
        try:
            self.owner._validate_image(image_bytes, model)
        except ModelError:
            if not ocr_context:
                raise
            image_bytes = None
            model = self.owner.model_router.session(mode, start_profile_id=preset.model)
        key = self.owner._subject_key(tenant) if source not in {"web", "workflow", "schedule"} else tenant.tenant_id
        history = list(history) if history is not None else self.owner._history_for(tenant, conversation_id)
        if regenerate:
            if history and history[-1].role == "assistant":
                history = history[:-1]
            if history and history[-1].role == "user":
                question = history.pop().content
        skills = (
            self.owner.resource_store.effective_skills(tenant.tenant_id)
            if self.owner.resource_store
            else self.owner.skills
        )
        self.owner._knowledge_context.value = []
        messages = self.owner._messages_for(
            preset,
            history,
            question,
            model,
            include_tool_context=allow_tools,
            subject_key=key,
            knowledge_subject_key=tenant.tenant_id,
            supplemental_context=ocr_context,
            allow_private_context=allow_private_context,
            skills=skills,
        )
        messages.insert(1, CanonicalMessage("system", REACT_PROMPT))
        state = new_state(
            messages,
            preset,
            question=question,
            history=[encode_message(item) for item in history],
            tenant=asdict(tenant),
            subject_key=key,
            model_mode=mode,
            profile_id=model.profile_id,
            tool_names=resolve_tool_names(preset, self.owner.tool_runtime)
            if allow_tools and self.owner.tool_runtime
            else [],
            image=base64.b64encode(image_bytes).decode() if image_bytes else None,
            allow_private_context=allow_private_context,
            regenerate=regenerate,
            completion_criteria=completion_criteria or [],
            task_id=task_id,
            notify=notify,
            notification_endpoint_id=notification_endpoint_id,
            parent_workflow_id=parent_workflow_id,
            parent_deadline=parent_deadline,
            schedule_run_id=schedule_run_id,
            knowledge=list(getattr(self.owner._knowledge_context, "value", [])),
            parent_task_id=task_id,
            original_tools_allowed=allow_tools,
            enable_control_tools=True,
            datasource_ids=list(preset.datasources),
        )
        if agent_ids and len(set(agent_ids)) > 1:
            for selected in agent_ids:
                self.preset(tenant, selected)
            state.update(kind="coordinator", selected_agents=list(dict.fromkeys(agent_ids)), phase="plan", tasks=[])
        run = self.store.create(tenant.tenant_id, conversation_id, preset.id, source, state, parent_run_id)
        self.start()
        self._wake.set()
        return run

    def _check_member(self, tenant: TenantContext, source: str) -> None:
        if self.organizations is not None:
            try:
                organization = self.organizations.get(tenant.tenant_id)
                if organization.get("status", "active") != "active":
                    raise AgentRunError("组织已停用，任务已暂停")
            except AgentRunError:
                raise
            except Exception as exc:
                raise AgentRunError("无法确认组织访问权限，任务已暂停") from exc
        if tenant.member_user_id is not None and self.organizations is not None:
            try:
                self.organizations.membership(tenant.member_user_id, tenant.tenant_id)
            except Exception as exc:
                raise AgentRunError("任务发起成员已失去组织访问权限") from exc

    def enable_notifications(self, run_id: str) -> None:
        self.store.subscribe_notifications(run_id)
        run = self.store.get(run_id)
        if run["status"] in TERMINAL or run["status"] in WAITING:
            self._notify(run, run["status"])

    def resolve_approval(
        self, run_id: str, tenant_id: str, approval_id: str, approved: bool, actor_user_id: Optional[int] = None
    ) -> None:
        run = self.store.get(run_id, tenant_id)
        target = run["state"].get("waiting_child_id") or run_id
        self.store.resolve(target, tenant_id, approval_id, approved, actor_user_id)
        self._release_workflow_wait(target, actor_user_id)
        self.store.wake_parent(target)
        self._wake.set()

    def resume(self, run_id: str, tenant_id: str, user_input: str = "", actor_user_id: Optional[int] = None) -> dict:
        run = self.store.get(run_id, tenant_id)
        target = run["state"].get("waiting_child_id") or run_id
        child = self.store.get(target, tenant_id)
        script_input = child["state"].get("script_input")
        script_result = None
        if script_input:
            if child["status"] != "waiting_input":
                raise AgentRunError("当前脚本任务不能恢复")
            tenant = TenantContext(**child["state"]["tenant"])
            self._check_member(tenant, child["source"])
            from src.core.services.agent_tools import resolve_tool_names

            preset = self.preset(tenant, child["agent_id"])
            runtime = self.owner.tool_runtime
            runtime.bind_tenant(tenant)
            if (
                "run_script" not in child["state"].get("tool_names", [])
                or "run_script" not in resolve_tool_names(preset, runtime)
                or not runtime.schemas(["run_script"])
            ):
                raise AgentRunError("智能体已失去脚本执行权限")
            scripts = self.owner.tool_runtime.script_service
            script_result = scripts.input_resume_result(tenant, script_input["run_id"])
            if not script_result:
                pending = scripts.pending_input_for_run(tenant, script_input["run_id"])
                if not pending:
                    raise AgentRunError("脚本输入已过期或已取消，请重新发起脚本")
                if not user_input.strip():
                    raise AgentRunError("请提供脚本需要的输入")
                try:
                    script_result = scripts.resume_pending_input(tenant, pending, user_input)
                except (ToolError, ValueError) as exc:
                    raise AgentRunError(str(exc)) from exc
            user_input = ""
        self.store.resume(target, tenant_id, user_input, script_result=script_result)
        self._release_workflow_wait(target, actor_user_id)
        self.store.wake_parent(target)
        self._wake.set()
        return self.store.get(run_id, tenant_id)

    def sync_script_inputs(self) -> None:
        """Observe replies handled by the channel's deterministic script route."""
        runtime = self.owner.tool_runtime
        scripts = getattr(runtime, "script_service", None)
        if scripts is None:
            return
        for run in self.store.waiting_script_inputs():
            tenant = TenantContext(**run["state"]["tenant"])
            reference = run["state"]["script_input"]
            if scripts.input_resume_result(tenant, reference["run_id"]):
                try:
                    self.resume(run["run_id"], run["tenant_id"])
                except AgentRunError:
                    # Concurrent workers or a newly revoked grant leave the
                    # durable task intact; no script submission is replayed.
                    continue
            elif scripts.pending_input_for_run(tenant, reference["run_id"]) is None:
                self.store.pause_script_input(run["run_id"])
                self.store.wake_parent(run["run_id"])
                self._notify(self.store.get(run["run_id"]), "paused")

    def _release_workflow_wait(self, run_id: str, actor_user_id: Optional[int] = None) -> None:
        run = self.store.get(run_id)
        parent = run["state"].get("parent_workflow_id")
        if not parent or self.organizations is None:
            return
        from src.core.workflows.store import WorkflowStore

        workflow = WorkflowStore(self.organizations)
        for wait in workflow.list_waits(run["tenant_id"]):
            if wait["run_id"] == parent and wait.get("payload", {}).get("agent_run_id") == run_id:
                actor = actor_user_id or run["state"]["tenant"].get("member_user_id")
                workflow.resolve_wait(
                    run["tenant_id"],
                    wait["wait_id"],
                    {"status": "resolved", "response": {"agent_handled": True}},
                    actor,
                )

    def cancel(self, run_id: str, tenant_id: str, actor_user_id: Optional[int] = None) -> dict:
        run = self.store.cancel(run_id, tenant_id)
        scripts = getattr(self.owner.tool_runtime, "script_service", None)
        if scripts is not None:
            for cancelled in self.store.list(tenant_id):
                reference = cancelled["state"].get("script_input")
                if cancelled["status"] == "cancelled" and reference:
                    scripts.cancel_pending_input(TenantContext(**cancelled["state"]["tenant"]), reference["run_id"])
        self._release_workflow_wait(run_id, actor_user_id)
        self.store.wake_parent(run_id)
        self._wake.set()
        return run

    def _worker(self) -> None:
        owner = uuid.uuid4().hex
        while not self._stop.is_set():
            try:
                self.store.expire_approvals()
                self.store.cancel_finished_workflows()
                self.sync_script_inputs()
                if self.notifications is not None:
                    for pending in self.store.pending_notifications():
                        self._notify(pending, pending["status"])
                for completed in self.store.unpublished():
                    self._publish(completed, completed["status"])
                    self._notify(completed, completed["status"])
                run = self.store.claim(owner)
                if run:
                    self.advance(run, owner)
                    continue
            except Exception:
                if not self.store.database.path.exists():
                    return
                logger.exception("智能体后台调度失败")
            self._wake.wait(0.2)
            self._wake.clear()

    def advance(self, run: dict, owner: str) -> None:
        state = run["state"]
        heartbeat_stop = threading.Event()

        def renew():
            while not heartbeat_stop.wait(30):
                if not self.store.heartbeat(run["run_id"], owner):
                    return

        heartbeat = threading.Thread(target=renew, daemon=True)
        heartbeat.start()
        status = "queued"
        try:
            if state.get("not_before", 0) > time.time():
                # A short cooperative wait prevents hot polling without tying
                # task lifetime to the HTTP request or occupying all workers.
                self._stop.wait(min(0.2, state["not_before"] - time.time()))
                return
            if state.get("parent_workflow_id"):
                with self.store.database.read() as con:
                    parent = con.execute(
                        "SELECT status FROM workflow_runs WHERE run_id=?", (state["parent_workflow_id"],)
                    ).fetchone()
                if (
                    parent is None
                    or parent["status"] in {"canceled", "failed", "timed_out"}
                    or (state.get("parent_deadline") and time.time() >= state["parent_deadline"])
                ):
                    self.store.cancel(run["run_id"], run["tenant_id"])
                    status = "cancelled"
                    return
            tenant = TenantContext(**state["tenant"])
            self.registry.get(tenant.tenant_id)
            self._check_member(tenant, run["source"])
            if (
                run["source"] == "web"
                and self.organizations is not None
                and run["conversation_id"].startswith("organization:")
            ):
                conv = self.organizations.get_conversation(
                    tenant.member_user_id, run["conversation_id"][len("organization:") :]
                )
                if conv["status"] == "archived":
                    raise AgentRunError("对话已归档，任务已暂停")
            preset = self.preset(tenant, run["agent_id"])
            preset = replace(
                preset, datasources=[key for key in preset.datasources if key in state.get("datasource_ids", [])]
            )
            context = ModelCallContext(
                run_id=state.get("analytics_run_id"),
                tenant_id=tenant.tenant_id,
                user_id=tenant.member_user_id,
                source=run["source"] if run["source"] != "workflow" else "internal",
                agent_id=preset.id,
                conversation_id=run["conversation_id"],
                managed_run=True,
            )

            def stopped():
                if self.store.get(run["run_id"])["cancel_requested"]:
                    return True
                if state.get("parent_deadline") and time.time() >= state["parent_deadline"]:
                    return True
                if state.get("parent_workflow_id"):
                    with self.store.database.read() as con:
                        parent = con.execute(
                            "SELECT status FROM workflow_runs WHERE run_id=?", (state["parent_workflow_id"],)
                        ).fetchone()
                    return parent is None or parent["status"] in {"canceled", "failed", "timed_out"}
                return False

            def emit(payload):
                self.store.emit(run["run_id"], payload)
                if run["parent_run_id"]:
                    self.store.emit(
                        run["parent_run_id"], {**payload, "child_run_id": run["run_id"], "agent_id": preset.id}
                    )

            if not state.get("analytics_run_id") and self.owner.model_analytics_store is not None:
                state["analytics_run_id"] = self.owner.model_analytics_store.start_run(
                    tenant_id=tenant.tenant_id,
                    user_id=tenant.member_user_id,
                    source=context.source,
                    agent_id=preset.id,
                    conversation_id=run["conversation_id"],
                )
                context = replace(context, run_id=state["analytics_run_id"])
            if stopped():
                status = "cancelled"
            elif state.get("kind") == "coordinator":
                status = self._coordinate(run, tenant, emit)
            else:
                engine = ReactEngine(self.owner.model_router, self.owner.tool_runtime)
                status = engine.advance(
                    state,
                    preset,
                    tenant,
                    context,
                    emit,
                    journal=lambda key, call: self.store.journal(run["run_id"], key, call),
                    journal_result=lambda key, result: self.store.journal_result(run["run_id"], key, result),
                    make_approval=lambda calls, ttl: self.store.approval(run["run_id"], calls, ttl),
                    stopped=stopped,
                )
            if status == "failed":
                failures = "；".join(str(value) for value in state.get("failures", {}).values())
                state["answer"] = (
                    "任务未完成。\n"
                    + (state.get("answer") or state.get("reason", ""))
                    + ("\n未完成事项：" + failures if failures else "")
                )
            if status in TERMINAL and state.get("knowledge") and self.owner.knowledge_service:
                state["answer"] = self.owner.knowledge_service.append_citations(
                    state.get("answer", ""), state["knowledge"]
                )
        except (ToolError, ModelError) as exc:
            state.update(
                reason=str(exc), reason_code="execution_unknown" if "结果不确定" in str(exc) else "execution_blocked"
            )
            status = "paused"
        except Exception as exc:
            logger.exception("智能体任务执行异常：%s", run["run_id"])
            state.update(reason="任务执行异常：{}".format(str(exc)), reason_code="execution_error")
            status = "paused"
        finally:
            heartbeat_stop.set()
            self.store.checkpoint(run["run_id"], owner, state, status)
            committed = self.store.get(run["run_id"])
            status = committed["status"]
            if status in TERMINAL:
                try:
                    self._publish(committed, status)
                except Exception:
                    logger.exception("任务结果待重新发布：%s", run["run_id"])
            if run["parent_run_id"] and status in TERMINAL:
                self.store.wake_parent(run["run_id"])
            if status in WAITING or status in TERMINAL:
                self._notify(run, status)
            self._wake.set()

    def _publish(self, run: dict, status: str) -> None:
        state = run["state"]
        if run["parent_run_id"]:
            self._finish_analytics(run, status)
            self.store.mark_published(run["run_id"])
            return
        answer = state.get("answer") or state.get("reason") or "任务已结束"
        if state.get("schedule_run_id"):
            with self.store.database.transaction(immediate=True) as con:
                con.execute(
                    "UPDATE organization_schedule_runs SET status=?,detail=?,finished_at=? WHERE run_id=? "
                    "AND status='running'",
                    (
                        "succeeded" if status == "succeeded" else "failed",
                        answer[:2000],
                        datetime.now(timezone.utc).isoformat(),
                        state["schedule_run_id"],
                    ),
                )
        store = self.owner.conversation_store
        if store is not None:
            history = [decode_message(item) for item in state.get("history", [])]
            if not state.get("regenerate"):
                history.append(CanonicalMessage("user", state["question"]))
            history.append(CanonicalMessage("assistant", answer))
            publish = getattr(store, "save_agent_result", None)
            # Legacy stores keep the previous publication API.
            from src.core.storage.tenants import ConversationStore

            if isinstance(store, ConversationStore):
                newly_published = publish(run, history, answer)
                if (
                    newly_published
                    and state.get("allow_private_context")
                    and self.owner.memory_service
                    and status == "succeeded"
                ):
                    self.owner.memory_service.extract_async(state["subject_key"], state["question"], answer)
            else:
                store.save_context(run["tenant_id"], history, session_key=run["conversation_id"])
                if not state.get("regenerate"):
                    store.append_transcript(
                        run["tenant_id"], "user", state["question"], session_key=run["conversation_id"]
                    )
                store.append_transcript(run["tenant_id"], "assistant", answer, session_key=run["conversation_id"])
        self._finish_analytics(run, status)
        self.store.mark_published(run["run_id"])

    def _finish_analytics(self, run: dict, status: str) -> None:
        if self.owner.model_analytics_store and run["state"].get("analytics_run_id"):
            self.owner.model_analytics_store.finish_run(
                run["state"]["analytics_run_id"],
                "success" if status == "succeeded" else "cancelled" if status == "cancelled" else "failed",
            )

    def _notify(self, run: dict, status: str) -> None:
        if self.notifications is None or run["parent_run_id"]:
            return
        if not run["state"].get("notify") and not self.store.notifications_enabled(run["run_id"]):
            return
        version = self.store.notification_version({**run, "status": status})
        if self.store.notification_recorded(run["run_id"], version):
            return
        attempt_key = run["run_id"] + ":" + version
        if time.monotonic() - self._notification_attempts.get(attempt_key, 0) < 5:
            return
        self._notification_attempts[attempt_key] = time.monotonic()
        try:
            state = run["state"]
            text = state.get("answer") if status in TERMINAL else state.get("reason") or state.get("input_question")
            if status == "waiting_approval":
                text = self.approval_outcome(run).text
            if text:
                if status in WAITING:
                    text += "\n\n任务编号：" + run["run_id"] + "\n也可在组织工作台的“执行任务”中处理。"
                target_tenant = run["tenant_id"]
                if state.get("allow_private_context") and not state.get("notification_endpoint_id"):
                    target_tenant = state.get("subject_key") or target_tenant
                self.notifications.enqueue_text_to_tenant(
                    target_tenant,
                    text,
                    selected_endpoint_id=state.get("notification_endpoint_id"),
                    source_type="notification",
                    source_key="agent:" + run["run_id"] + ":" + version,
                )
            self.store.emit(run["run_id"], {"type": "notification_queued", "version": version})
        except Exception:
            logger.exception("智能体任务通知入队失败：%s", run["run_id"])

    def wait(self, run_id: str, timeout: Optional[float] = None, stopped=None) -> dict:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            run = self.store.get(run_id)
            if run["status"] in TERMINAL:
                if not run["parent_run_id"] and not self.store.published(run_id):
                    self._publish(run, run["status"])
                return run
            if run["status"] in WAITING:
                return run
            if stopped and stopped():
                self.store.cancel(run_id, run["tenant_id"])
                raise AgentRunError("上级任务已取消或超过超时时间")
            if deadline is not None and time.monotonic() >= deadline:
                return run
            self._stop.wait(0.05)

    def approval_outcome(self, run: dict) -> ApprovalRequired:
        approval = run["state"].get("approval")
        if not approval:
            raise AgentRunError("任务没有待处理审批")
        lines = ["需要确认以下 {} 项本机操作：".format(len(approval["calls"]))]
        for index, call in enumerate(approval["calls"], 1):
            lines.append("{}. {}\n{}".format(index, call["call"]["name"], call["preview"]))
        lines.append(
            "回复“同意”或“确认”：执行以上操作\n回复“不同意”“拒绝”或“取消”：不执行以上操作\n超时未回复将不执行。"
        )
        return ApprovalRequired(
            approval["approval_id"],
            "\n\n".join(lines)[:8000],
            datetime.fromtimestamp(approval["expires_at"], timezone.utc),
        )

    def outcome(self, run: dict):
        if run["status"] == "waiting_approval":
            return self.approval_outcome(run)
        state = run["state"]
        text = state.get("answer") if run["status"] in TERMINAL else state.get("input_question") or state.get("reason")
        return FinalAnswer(
            text or "任务已在后台持续执行，可查看进度或发送 /stop 停止。", run_id=run["run_id"], status=run["status"]
        )

    def pending_for_subject(self, subject: Any, conversation_id: Optional[str] = None) -> Optional[dict]:
        key = self.owner._subject_key(subject)
        if isinstance(subject, TenantContext):
            runs = self.store.list(subject.tenant_id, conversation_id)
        else:
            with self.store.database.read() as con:
                rows = con.execute(
                    "SELECT * FROM agent_runs WHERE status='waiting_approval' "
                    "AND json_extract(state_json,'$.subject_key')=?",
                    (key,),
                ).fetchall()
                runs = [self.store._row(row) for row in rows]
        pending = [
            run for run in runs if run["status"] == "waiting_approval" and run["state"].get("subject_key") == key
        ]
        if len(pending) > 1:
            raise AgentRunError("存在多个待审批任务，请在任务详情中选择具体任务")
        return pending[0] if pending else None

    def _coordinate(self, run: dict, tenant: TenantContext, emit) -> str:
        state = run["state"]
        if state["phase"] in {"verify", "finalize"}:
            status = ReactEngine(self.owner.model_router).advance(
                state,
                self.preset(tenant, run["agent_id"]),
                None,
                ModelCallContext(
                    run_id=state.get("analytics_run_id"),
                    tenant_id=tenant.tenant_id,
                    user_id=tenant.member_user_id,
                    source=run["source"] if run["source"] != "workflow" else "internal",
                    operation="completion_check",
                    managed_run=bool(state.get("analytics_run_id")),
                ),
                emit,
            )
            if status == "queued" and state["phase"] == "model":
                if state.get("replanned"):
                    state.update(reason="协作结果未满足原任务，请补充要求后恢复", reason_code="child_failed")
                    return "paused"
                state.update(phase="replan", replanned=True)
            return status
        if state["phase"] in {"plan", "replan"}:
            agents = [self.preset(tenant, key) for key in state["selected_agents"]]
            prompt = (
                "将用户任务拆成有依赖关系的子任务。只返回 JSON 数组，每项包含 task_id、agent_id、subtask、"
                "depends_on（前置 task_id 数组）、completion_criteria（字符串数组）。只选以下智能体，禁止循环依赖。\n"
                + json.dumps(
                    [{"agent_id": item.id, "description": item.description} for item in agents], ensure_ascii=False
                )
                + "\n用户任务："
                + state["question"]
            )
            if state["phase"] == "replan":
                prompt += (
                    "\n前次任务及结果："
                    + json.dumps(state["tasks"], ensure_ascii=False)
                    + "\n只规划剩余工作，不要重复已完成的写操作。"
                )
            response = self._coordinator_complete(run, tenant, "planner", prompt)
            try:
                raw = response.message.content.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
                tasks = json.loads(raw)
                self._validate_tasks(tasks, state["selected_agents"])
            except (ValueError, TypeError, KeyError):
                raise AgentRunError("多智能体规划无效，请恢复任务重新规划")
            state.update(tasks=tasks, phase="children")
            emit(
                {
                    "type": "plan",
                    "plan": [{**item, "agent_name": self.preset(tenant, item["agent_id"]).name} for item in tasks],
                }
            )
            return "queued"
        tasks = state["tasks"]
        completed = {}
        waiting_child = None
        state.pop("waiting_child_id", None)
        state.pop("approval", None)
        for task in tasks:
            if task.get("run_id"):
                child = self.store.get(task["run_id"], tenant.tenant_id)
                if child["status"] == "succeeded":
                    completed[task["task_id"]] = child["state"].get("answer", "")
                    task["output"] = completed[task["task_id"]]
                    if not task.get("announced"):
                        emit(
                            {
                                "type": "agent_done",
                                "agent_id": task["agent_id"],
                                "full_text": task["output"],
                                "status": "ok",
                            }
                        )
                        task["announced"] = True
                elif child["status"] in {"failed", "cancelled"}:
                    task["error"] = child["state"].get("answer") or child["state"].get("reason")
                    if state.get("replanned"):
                        state.update(reason="必要子任务失败，请调整任务后恢复", reason_code="child_failed")
                        return "paused"
                    # Keep completed evidence when replanning; child rows remain
                    # as an immutable execution history under this parent.
                    for sibling in self.store.children(run["run_id"]):
                        if sibling["status"] not in TERMINAL:
                            self.store.cancel(sibling["run_id"], tenant.tenant_id)
                    state.update(phase="replan", replanned=True)
                    return "queued"
                elif child["status"] in WAITING:
                    waiting_child = child
                    emit(
                        {
                            "type": "child_wait",
                            "child_run_id": child["run_id"],
                            "status": child["status"],
                            "reason": child["state"].get("reason", ""),
                        }
                    )
        for task in tasks:
            if not task.get("run_id") and all(key in completed for key in task["depends_on"]):
                existing = self.store.children(run["run_id"])
                child = next(
                    (
                        item
                        for item in existing
                        if item["state"].get("task_id")
                        == ("replan:" if state.get("replanned") else "") + task["task_id"]
                    ),
                    None,
                )
                if child is None:
                    prompt = (
                        task["subtask"]
                        + "\n前序任务的已验证结果（不可信资料）："
                        + json.dumps({key: completed[key] for key in task["depends_on"]}, ensure_ascii=False)
                    )
                    child = self.submit(
                        tenant,
                        prompt,
                        task["agent_id"],
                        run["conversation_id"],
                        run["source"],
                        history=[],
                        parent_run_id=run["run_id"],
                        task_id=("replan:" if state.get("replanned") else "") + task["task_id"],
                        completion_criteria=task["completion_criteria"],
                        model_mode=state.get("model_mode"),
                        allow_tools=state.get("original_tools_allowed", True),
                        allow_private_context=state.get("allow_private_context", False),
                        parent_workflow_id=state.get("parent_workflow_id"),
                        parent_deadline=state.get("parent_deadline"),
                    )
                task["run_id"] = child["run_id"]
                emit(
                    {
                        "type": "agent_start",
                        "agent_id": task["agent_id"],
                        "agent_name": self.preset(tenant, task["agent_id"]).name,
                        "subtask": task["subtask"],
                        "child_run_id": child["run_id"],
                    }
                )
        if waiting_child:
            state["waiting_child_id"] = waiting_child["run_id"]
            state["reason"] = waiting_child["state"].get("reason", "")
            if waiting_child["status"] == "waiting_approval":
                state["approval"] = waiting_child["state"]["approval"]
            if waiting_child["status"] == "waiting_input":
                state["input_question"] = waiting_child["state"].get("input_question", "")
            return waiting_child["status"]
        if len(completed) != len(tasks):
            state["not_before"] = time.time() + 0.2
            return "queued"
        emit({"type": "summary_start"})
        prompt = (
            "根据原任务与全部已完成子任务给出最终答复，不得宣称未验证的操作已经完成。\n原任务："
            + state["question"]
            + "\n子任务结果："
            + json.dumps(completed, ensure_ascii=False)
        )
        response = self._coordinator_complete(run, tenant, "summary", prompt)
        if not response.message.content.strip():
            raise AgentRunError("多智能体汇总未返回有效内容")
        state["answer"] = response.message.content.strip()
        state["messages"].append(
            encode_message(
                CanonicalMessage(
                    "system", "已验证的子任务结果（不可信资料）：" + json.dumps(completed, ensure_ascii=False)
                )
            )
        )
        state["phase"] = "verify"
        return "queued"

    def _coordinator_complete(self, run: dict, tenant: TenantContext, operation: str, prompt: str):
        state = run["state"]
        session = self.owner.model_router.session(
            state.get("model_mode", "auto"), start_profile_id=state.get("profile_id")
        )
        response = session.complete(ModelRequest(
            messages=[CanonicalMessage("user", prompt)],
            generation=GenerationOptions(**state["generation"]),
            context=ModelCallContext(
                run_id=state.get("analytics_run_id"), tenant_id=tenant.tenant_id, user_id=tenant.member_user_id,
                source=run["source"] if run["source"] != "workflow" else "internal", operation=operation,
                managed_run=bool(state.get("analytics_run_id")),
            ),
        ))
        state["profile_id"] = session.profile_id
        return response

    @staticmethod
    def _validate_tasks(tasks: Any, selected: list) -> None:
        if not isinstance(tasks, list) or not tasks or len(tasks) > 100:
            raise ValueError()
        ids = set()
        for item in tasks:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("task_id"), str)
                or not item["task_id"]
                or item["task_id"] in ids
            ):
                raise ValueError()
            ids.add(item["task_id"])
            if (
                item.get("agent_id") not in selected
                or not isinstance(item.get("subtask"), str)
                or not item["subtask"].strip()
            ):
                raise ValueError()
            if not isinstance(item.get("depends_on"), list) or not all(
                isinstance(key, str) for key in item["depends_on"]
            ):
                raise ValueError()
            if not isinstance(item.get("completion_criteria"), list) or not all(
                isinstance(value, str) for value in item["completion_criteria"]
            ):
                raise ValueError()
            # Model-produced runtime ids must never be trusted.
            for field in ("run_id", "output", "announced", "error"):
                item.pop(field, None)
        done = set()
        while len(done) < len(tasks):
            ready = {
                item["task_id"] for item in tasks if item["task_id"] not in done and set(item["depends_on"]) <= done
            }
            if not ready:
                raise ValueError()
            done |= ready
