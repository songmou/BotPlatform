"""Single-machine durable workflow worker and built-in node executors."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from apscheduler.triggers.cron import CronTrigger

from src.core.datasource.errors import DataSourceError
from src.core.modeling import (
    CanonicalMessage,
    ModelCallContext,
    ModelRequest,
)
from src.core.tooling.models import ToolAuditContext

from .definition import (
    NODE_CATALOG,
    WorkflowValidationError,
    render_value,
    validate_declared_output,
    validate_definition,
)
from .store import WorkflowError, WorkflowStore


@dataclass
class NodeOutcome:
    output: Any
    port: str = "default"
    wait: Optional[Dict[str, Any]] = None


class WorkflowService:
    """Own workflow persistence, schedule polling and bounded worker threads."""

    def __init__(
        self,
        organization_store: Any,
        resource_store: Any = None,
        *,
        model_router: Any = None,
        registry: Any = None,
        tool_runtime: Any = None,
        agent_service: Any = None,
        knowledge_service: Any = None,
        script_service: Any = None,
        datasource_service: Any = None,
        notification_service: Any = None,
        credential_service: Any = None,
        timezone_name: str = "UTC",
        max_workers: int = 4,
    ) -> None:
        self.store = WorkflowStore(organization_store, resource_store)
        self.model_router = model_router
        self.registry = registry
        self.tool_runtime = tool_runtime
        self.agent_service = agent_service
        self.knowledge_service = knowledge_service
        self.script_service = script_service
        self.datasource_service = datasource_service
        self.notification_service = notification_service
        self.credential_service = credential_service
        self.timezone = ZoneInfo(timezone_name)
        self.max_workers = max(1, min(int(max_workers), 16))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: List[threading.Thread] = []
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        for index in range(self.max_workers):
            thread = threading.Thread(
                target=self._worker_loop,
                name="workflow-worker-{}".format(index + 1),
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def shutdown(self) -> None:
        if not self._started:
            return
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._threads.clear()
        self._started = False

    def wake(self) -> None:
        self._wake.set()

    def _worker_loop(self) -> None:
        owner = "{}:{}".format(uuid.uuid4(), threading.current_thread().name)
        last_maintenance = 0.0
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if now - last_maintenance >= 1.0:
                    self.store.expire_waits()
                    self._enqueue_due_schedules()
                    last_maintenance = now
                run = self.store.claim_run(owner, lease_seconds=600)
                if run is not None:
                    self._execute_run(run, owner)
                    continue
            except Exception:
                # One malformed run must never stop the worker pool. The run
                # itself is marked failed by _execute_run whenever possible.
                pass
            self._wake.wait(0.5)
            self._wake.clear()

    def _enqueue_due_schedules(self) -> None:
        now = datetime.now(timezone.utc)
        for trigger in self.store.due_schedules(now.isoformat()):
            config = json.loads(str(trigger["config_json"]) or "{}")
            try:
                cron = CronTrigger.from_crontab(str(config.get("cron") or ""), timezone=self.timezone)
                next_fire = cron.get_next_fire_time(None, now.astimezone(self.timezone))
                if trigger.get("next_fire_at") is not None:
                    self.store.enqueue_run(
                        str(trigger["organization_id"]),
                        str(trigger["workflow_id"]),
                        {"scheduled_at": now.isoformat()},
                        "schedule",
                        str(trigger["trigger_id"]),
                        None,
                        idempotency_key="{}:{}".format(trigger["trigger_id"], str(trigger["next_fire_at"])),
                        version_override=int(trigger["published_version"]),
                    )
                if next_fire is not None:
                    self.store.set_schedule_next_fire(str(trigger["trigger_id"]), next_fire.astimezone(timezone.utc).isoformat())
            except Exception:
                self.store.set_schedule_next_fire(
                    str(trigger["trigger_id"]),
                    (now + timedelta(days=1)).isoformat(),
                )

    def enqueue(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        run = self.store.enqueue_run(*args, **kwargs)
        self.wake()
        return run

    def validate_resources(
        self, organization_id: str, definition: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Validate runtime resource references before publish or test runs."""
        normalized = validate_definition(definition)
        knowledge_ids: Optional[set[str]] = None
        scripts: Optional[Dict[str, Mapping[str, Any]]] = None
        for node in normalized["nodes"]:
            node_type = str(node["type"])
            node_name = str(node.get("name") or node["id"])
            config = node.get("config") or {}
            try:
                if node_type == "agent":
                    if self.store.resources is None:
                        raise WorkflowError("组织资源服务不可用")
                    item = self.store.resources.get_effective(
                        organization_id, "agents", str(config["agent_id"])
                    )
                    if item.get("status") == "disabled" or not bool(
                        (item.get("payload") or {}).get("enabled", True)
                    ):
                        raise WorkflowError("智能体已停用")
                elif node_type in {"llm", "extract", "classifier"}:
                    model_id = str(config.get("model") or "")
                    clients = getattr(self.model_router, "clients", {}) if self.model_router is not None else {}
                    if self.model_router is None or not clients:
                        raise WorkflowError("模型服务不可用")
                    if model_id and model_id not in clients:
                        raise WorkflowError("模型配置不存在或未启用")
                elif node_type == "knowledge" and config.get("category_ids"):
                    if self.knowledge_service is None:
                        raise WorkflowError("知识库服务不可用")
                    if knowledge_ids is None:
                        knowledge_ids = {
                            str(item["category_id"])
                            for item in self.knowledge_service.list_categories(
                                tenant_id=organization_id
                            )
                        }
                    missing = [
                        str(value)
                        for value in config.get("category_ids") or []
                        if str(value) not in knowledge_ids
                    ]
                    if missing:
                        raise WorkflowError(
                            "知识库分类不存在或无权访问：{}".format("、".join(missing))
                        )
                elif node_type == "tool":
                    name = str(config["tool_name"])
                    if self.tool_runtime is None:
                        raise WorkflowError("工具运行时不可用")
                    resolver = getattr(self.tool_runtime, "_definition", None)
                    if not callable(resolver) or resolver(name) is None:
                        raise WorkflowError("工具不存在：{}".format(name))
                    if not self.tool_runtime.is_tool_enabled(name):
                        raise WorkflowError("工具已停用：{}".format(name))
                elif node_type == "script":
                    if self.script_service is None:
                        raise WorkflowError("平台脚本服务不可用")
                    if scripts is None:
                        scripts = {
                            str(item["id"]): item
                            for item in self.script_service.list_scripts()
                        }
                    script_id = str(config["script_id"])
                    if script_id not in scripts:
                        raise WorkflowError("脚本不存在：{}".format(script_id))
                    if not bool(scripts[script_id].get("enabled", True)):
                        raise WorkflowError("脚本已停用：{}".format(script_id))
                elif node_type == "datasource":
                    if self.datasource_service is None:
                        raise WorkflowError("数据源服务不可用")
                    datasource_id = str(config["datasource_id"])
                    item = self.datasource_service.get_config(datasource_id)
                    if not item:
                        raise WorkflowError("数据源不存在：{}".format(datasource_id))
                    if not bool(item.get("enabled", True)):
                        raise WorkflowError("数据源已停用：{}".format(datasource_id))
                    validator = getattr(self.datasource_service, "validate_readonly_query", None)
                    if not callable(validator):
                        raise WorkflowError("数据源查询校验服务不可用")
                    validator(
                        datasource_id,
                        str(config.get("sql") or ""),
                        limit=int(config.get("limit", 100)),
                    )
                elif node_type == "http" and config.get("credential_id"):
                    if self.credential_service is None:
                        raise WorkflowError("工作流凭据服务不可用")
                    self.credential_service.secret_for_resource(
                        organization_id,
                        "workflow_http",
                        str(config["credential_id"]),
                    )
            except (WorkflowError, DataSourceError) as exc:
                raise WorkflowError(
                    "节点“{}”的资源配置无效：{}".format(node_name, exc)
                ) from exc
            except Exception as exc:
                raise WorkflowError(
                    "节点“{}”引用的资源不存在或无权访问".format(node_name)
                ) from exc
        return normalized

    def run_synchronously(self, organization_id: str, run_id: str, timeout: float = 30.0) -> Dict[str, Any]:
        deadline = time.monotonic() + max(0.1, min(timeout, 120.0))
        self.wake()
        while time.monotonic() < deadline:
            run = self.store.get_run(organization_id, run_id)
            if run["status"] not in {"queued", "running"}:
                return run
            time.sleep(0.05)
        return self.store.get_run(organization_id, run_id)

    def _definition_and_dependencies(self, run: Mapping[str, Any]) -> tuple[Dict[str, Any], Dict[str, int]]:
        if int(run["workflow_version"]) == 0:
            state = run.get("state") or {}
            snapshot = state.get("definition_snapshot") if isinstance(state, Mapping) else None
            if snapshot is None:
                raise WorkflowError("试运行草稿快照不存在，请重新发起试运行")
            definition = validate_definition(snapshot)
            dependencies = state.get("dependencies") or {}
            return definition, {
                str(key): int(value) for key, value in dependencies.items()
            }
        snapshot = self.store.get_version(run["organization_id"], run["workflow_id"], int(run["workflow_version"]))
        return validate_definition(snapshot["definition"]), {
            str(key): int(value) for key, value in snapshot.get("dependencies", {}).items()
        }

    def _execute_run(self, run: Dict[str, Any], owner: str) -> None:
        try:
            definition, dependencies = self._definition_and_dependencies(run)
            nodes = {node["id"]: node for node in definition["nodes"]}
            start_id = next(node["id"] for node in definition["nodes"] if node["type"] == "start")
            state = dict(run.get("state") or {})
            state.setdefault("queue", [start_id])
            state.setdefault("completed", [])
            state.setdefault("nodes", {})
            state.setdefault("attempts", {})
            state.setdefault("steps", 0)
            state.setdefault("depth", 0)
            created_at = datetime.fromisoformat(str(run["created_at"]))
            deadline = created_at + timedelta(seconds=int(definition["settings"]["timeout_seconds"]))
            while state["queue"]:
                if datetime.now(timezone.utc) >= deadline:
                    self.store.finish_run(run["run_id"], "timed_out", error={"message": "工作流执行超过超时时间"})
                    return
                current = self.store.get_run(run["organization_id"], run["run_id"])
                if current["status"] == "canceled":
                    return
                node_id = str(state["queue"].pop(0))
                if node_id in state["completed"]:
                    continue
                node = nodes[node_id]
                state["steps"] += 1
                if state["steps"] > definition["settings"]["max_steps"]:
                    raise WorkflowError("工作流执行步骤超过安全上限")
                context = {
                    "input": run.get("input") or {},
                    "trigger": run.get("input") or {},
                    "nodes": {key: {"output": value} for key, value in state["nodes"].items()},
                    "item": state.get("item"),
                }
                rendered = render_value(node.get("config") or {}, context)
                attempt = int(state["attempts"].get(node_id, 0)) + 1
                state["attempts"][node_id] = attempt
                node_run_id = self.store.begin_node(run, node, rendered, attempt)
                try:
                    outcome = self._execute_node(run, node, rendered, state, dependencies, owner)
                except Exception as exc:
                    self.store.finish_node(run, node_run_id, node_id, "failed", error={"message": str(exc)})
                    policy = node.get("error_policy") or {"mode": "stop"}
                    mode = str(policy.get("mode") or "stop")
                    if mode == "retry" and attempt <= int(policy.get("max_retries", 0)):
                        state["queue"].insert(0, node_id)
                        wake_at = (datetime.now(timezone.utc) + timedelta(seconds=min(30, 2 ** attempt))).isoformat()
                        self.store.update_run_state(run["run_id"], state, status="queued", wake_at=wake_at)
                        self.wake()
                        return
                    if mode in {"continue", "error_branch"}:
                        state["nodes"][node_id] = {"ok": False, "error": str(exc)}
                        state["completed"].append(node_id)
                        next_nodes = self._next_nodes(definition, node_id, "error" if mode == "error_branch" else "default")
                        state["queue"].extend(next_nodes)
                        self.store.checkpoint_run(run["run_id"], state, owner)
                        continue
                    raise
                if outcome.wait is not None:
                    state["queue"].insert(0, node_id)
                    self.store.checkpoint_run(run["run_id"], state, owner)
                    self.store.finish_node(run, node_run_id, node_id, "waiting", output={"wait_id": outcome.wait["wait_id"]})
                    self.store.update_run_state(run["run_id"], state, status="waiting")
                    return
                self.store.finish_node(run, node_run_id, node_id, "succeeded", output=outcome.output)
                state["nodes"][node_id] = outcome.output
                state["completed"].append(node_id)
                if node["type"] == "end":
                    final_output = validate_declared_output(definition.get("outputs", []), outcome.output)
                    self.store.finish_run(run["run_id"], "succeeded", output=final_output)
                    return
                next_nodes = self._next_nodes(definition, node_id, outcome.port)
                if not next_nodes and outcome.port != "default":
                    raise WorkflowError("节点 {} 的分支 {} 未连接后续节点".format(node_id, outcome.port))
                state["queue"].extend(next_nodes)
                self.store.checkpoint_run(run["run_id"], state, owner)
            self.store.finish_run(run["run_id"], "succeeded", output=state["nodes"].get(state["completed"][-1], {}) if state["completed"] else {})
        except Exception as exc:
            try:
                self.store.finish_run(run["run_id"], "failed", error={"message": str(exc)})
            except Exception:
                pass

    @staticmethod
    def _next_nodes(definition: Mapping[str, Any], node_id: str, port: str) -> List[str]:
        edges = [edge for edge in definition["edges"] if edge["source"] == node_id]
        selected = [edge for edge in edges if edge["source_port"] == port]
        if not selected and port == "default":
            selected = [edge for edge in edges if edge["source_port"] in {"default", ""}]
        return [str(edge["target"]) for edge in selected]

    def _execute_node(
        self,
        run: Mapping[str, Any],
        node: Mapping[str, Any],
        config: Mapping[str, Any],
        state: Mapping[str, Any],
        dependencies: Mapping[str, int],
        owner: str,
    ) -> NodeOutcome:
        node_type, node_id = str(node["type"]), str(node["id"])
        if node_type == "start":
            return NodeOutcome(dict(run.get("input") or {}))
        if node_type == "end":
            output = config["output"] if "output" in config else dict(state.get("nodes", {}))
            return NodeOutcome(output)
        if node_type in {"set_variable", "field_map", "merge"}:
            return NodeOutcome(config.get("values", config.get("mapping", dict(config))))
        if node_type == "template":
            return NodeOutcome({"text": str(config.get("text") or "")})
        if node_type in {"condition", "switch"}:
            value = config.get("left", config.get("value"))
            if node_type == "switch":
                cases = config.get("cases") or []
                for item in cases:
                    if isinstance(item, Mapping) and value == item.get("value"):
                        return NodeOutcome({"value": value}, "case:{}".format(item.get("key", item.get("value"))))
                return NodeOutcome({"value": value}, "default")
            matched = self._compare(value, str(config.get("operator") or "equals"), config.get("right"))
            return NodeOutcome({"matched": matched}, "true" if matched else "false")
        if node_type == "delay":
            existing = self.store.pending_wait_for_node(run["run_id"], node_id)
            if existing is not None and existing["status"] in {"resolved", "expired"}:
                return NodeOutcome({"resumed_at": datetime.now(timezone.utc).isoformat()})
            seconds = max(1, min(int(config.get("seconds", 1)), 30 * 86400))
            wait = self._create_wait(run, node_id, "delay", {"seconds": seconds}, {}, seconds)
            return NodeOutcome({}, wait=wait)
        if node_type in {"approval", "human_input"}:
            existing = self.store.pending_wait_for_node(run["run_id"], node_id)
            if existing is not None and existing["status"] != "pending":
                if node_type == "approval":
                    approved = existing["status"] == "approved"
                    return NodeOutcome(existing.get("response") or {"approved": approved}, "approved" if approved else "rejected")
                if existing["status"] in {"expired", "rejected"}:
                    raise WorkflowError("补充输入未在有效期内完成")
                resolved = existing.get("response") or {}
                response = resolved.get("response") if isinstance(resolved, Mapping) else None
                return NodeOutcome(dict(response) if isinstance(response, Mapping) else {})
            ttl = max(60, min(int(config.get("ttl_seconds", 86400)), 30 * 86400))
            wait = self._create_wait(
                run,
                node_id,
                "approval" if node_type == "approval" else "input",
                {"title": config.get("title") or node["name"], "fields": config.get("fields") or [], "payload": config.get("payload")},
                config.get("assignees") or {"roles": ["owner", "admin"]},
                ttl,
            )
            return NodeOutcome({}, wait=wait)
        if node_type in {"tool", "script", "http", "notification"}:
            safety = self._safety_required(node_type, config)
            if safety:
                safety_id = node_id + ":safety"
                existing = self.store.pending_wait_for_node(run["run_id"], safety_id)
                if existing is None:
                    wait = self._create_wait(
                        run,
                        safety_id,
                        "approval",
                        {"title": "确认高风险工作流操作", "node": node["name"], "input": config},
                        {"roles": ["owner", "admin"]},
                        86400,
                    )
                    return NodeOutcome({}, wait=wait)
                if existing["status"] != "approved":
                    if existing["status"] == "pending":
                        return NodeOutcome({}, wait=existing)
                    raise WorkflowError("高风险工作流操作未获批准")
            if run.get("test_mode") and not run.get("allow_side_effects"):
                return NodeOutcome({"preview": True, "node_type": node_type, "config": dict(config)})
        if node_type in {"llm", "extract", "classifier"}:
            return NodeOutcome(self._llm(run, node_type, config))
        if node_type == "agent":
            if self.agent_service is None:
                raise WorkflowError("智能体服务不可用")
            return NodeOutcome({"text": self.agent_service.generate(str(config.get("agent_id") or ""), str(config.get("prompt") or ""))})
        if node_type == "knowledge":
            if self.knowledge_service is None:
                raise WorkflowError("知识库服务不可用")
            items = self.knowledge_service.search(
                run["organization_id"],
                str(config.get("query") or ""),
                limit=int(config.get("limit", 6)),
                agent_id=config.get("agent_id"),
                category_ids=config.get("category_ids"),
            )
            return NodeOutcome({"items": items})
        if node_type == "tool":
            return NodeOutcome(self._execute_tool(run, config))
        if node_type == "script":
            if self.script_service is None or self.registry is None:
                raise WorkflowError("平台脚本服务不可用")
            tenant = self._tenant(run)
            result = self.script_service.submit(
                tenant,
                str(config.get("script_id") or ""),
                config.get("parameters") or {},
                trigger="workflow:{}".format(run["run_id"]),
                recipient=None,
            )
            return NodeOutcome(result)
        if node_type == "datasource":
            if self.datasource_service is None:
                raise WorkflowError("数据源服务不可用")
            return NodeOutcome(self.datasource_service.query(str(config.get("datasource_id") or ""), str(config.get("sql") or ""), limit=config.get("limit")))
        if node_type == "http":
            return NodeOutcome(self._http(run, config, node_id))
        if node_type == "notification":
            if self.notification_service is None:
                raise WorkflowError("消息通知服务不可用")
            result = self.notification_service.enqueue_text_to_tenant(
                run["organization_id"],
                str(config.get("message") or ""),
                source_type="workflow",
                source_key="{}:{}".format(run["run_id"], node_id),
                source_ref=run["workflow_id"],
                attempt_immediately=True,
            )
            return NodeOutcome({"notification_ids": list(result.notification_ids), "status": result.status})
        if node_type in {"subworkflow", "for_each"}:
            target = str(config.get("workflow_id") or "")
            version = dependencies.get(target)
            if not target or version is None:
                raise WorkflowError("子工作流未固定到已发布版本")
            if int(state.get("depth", 0)) >= 5:
                raise WorkflowError("子工作流调用深度不能超过 5")
            items = config.get("items") if node_type == "for_each" else [config.get("inputs") or {}]
            if not isinstance(items, list):
                raise WorkflowError("For Each 输入必须是数组")
            if len(items) > 100:
                raise WorkflowError("For Each 最多处理 100 项")
            results = []
            for item in items:
                child_inputs = item if isinstance(item, Mapping) else {"item": item}
                child = self.store.enqueue_run(
                    run["organization_id"],
                    target,
                    child_inputs,
                    "subworkflow",
                    run["run_id"],
                    run.get("initiated_by"),
                    test_mode=bool(run.get("test_mode")),
                    allow_side_effects=bool(run.get("allow_side_effects")),
                    version_override=version,
                )
                child = self.store.start_specific_run(run["organization_id"], child["run_id"], owner)
                child["state"] = {"depth": int(state.get("depth", 0)) + 1}
                self._execute_run(child, owner)
                child = self.store.get_run(run["organization_id"], child["run_id"])
                if child["status"] != "succeeded":
                    raise WorkflowError("子工作流执行未成功：{}".format(child["status"]))
                results.append(child.get("output"))
            return NodeOutcome({"items": results} if node_type == "for_each" else (results[0] if results else {}))
        raise WorkflowError("节点执行器尚未实现：{}".format(node_type))

    def execute_node_for_test(
        self,
        run: Mapping[str, Any],
        node: Mapping[str, Any],
        config: Mapping[str, Any],
        *,
        state: Optional[Mapping[str, Any]] = None,
        dependencies: Optional[Mapping[str, int]] = None,
        owner: str = "workflow-test",
    ) -> NodeOutcome:
        """Execute one node through the production dispatcher for contract tests."""
        return self._execute_node(
            run,
            node,
            config,
            state or {"nodes": {}, "depth": 0},
            dependencies or {},
            owner,
        )

    @staticmethod
    def _compare(left: Any, operator: str, right: Any) -> bool:
        if operator == "equals":
            return left == right
        if operator == "not_equals":
            return left != right
        if operator == "contains":
            return right in left if isinstance(left, (str, list, dict)) else False
        if operator == "exists":
            return left not in (None, "", [], {})
        if operator in {"gt", "gte", "lt", "lte"}:
            try:
                return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[operator]
            except TypeError:
                return False
        raise WorkflowError("不支持的条件操作符：{}".format(operator))

    def _create_wait(self, run: Mapping[str, Any], node_id: str, wait_type: str, payload: Any, assignees: Any, ttl_seconds: int) -> Dict[str, Any]:
        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        wait = self.store.create_wait(run, node_id, wait_type, payload, assignees, expires)
        if wait_type in {"approval", "input"} and self.notification_service is not None:
            try:
                self.notification_service.enqueue_text_to_tenant(
                    run["organization_id"],
                    "【工作流待办】{}\n请登录组织工作台处理，截止时间：{}".format(payload.get("title") or node_id, expires),
                    source_type="workflow_wait",
                    source_key=wait["wait_id"],
                    source_ref=run["run_id"],
                    attempt_immediately=True,
                )
            except Exception:
                pass
        return wait

    def _safety_required(self, node_type: str, config: Mapping[str, Any]) -> bool:
        if node_type == "tool":
            return bool(self.tool_runtime is None or self.tool_runtime.requires_approval(str(config.get("tool_name") or ""), dict(config.get("arguments") or {})))
        if node_type == "script":
            return bool(self.script_service is None or self.script_service.requires_approval(str(config.get("script_id") or "")))
        if node_type == "http":
            return str(config.get("method") or "GET").upper() not in {"GET", "HEAD"}
        return node_type == "notification"

    def _tenant(self, run: Mapping[str, Any]) -> Any:
        if self.registry is None:
            raise WorkflowError("租户服务不可用")
        tenant = self.registry.get(run["organization_id"])
        return replace(tenant, member_user_id=run.get("initiated_by"))

    def _execute_tool(self, run: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
        if self.tool_runtime is None:
            raise WorkflowError("工具运行时不可用")
        tenant = self._tenant(run)
        self.tool_runtime.bind_tenant(tenant)
        datasource_ids = config.get("datasource_ids") or []
        self.tool_runtime.bind_agent_datasources(datasource_ids)
        result = self.tool_runtime.execute(
            str(config.get("tool_name") or ""),
            dict(config.get("arguments") or {}),
            ToolAuditContext(
                user_id=str(run.get("initiated_by") or "workflow"),
                member_user_id=run.get("initiated_by"),
                organization_id=run["organization_id"],
                session_id=run["run_id"],
                agent_id="workflow",
            ),
        )
        if not result.ok:
            raise WorkflowError(result.error or "工作流工具执行失败")
        return {"data": result.data}

    def _llm(self, run: Mapping[str, Any], node_type: str, config: Mapping[str, Any]) -> Dict[str, Any]:
        if self.model_router is None:
            raise WorkflowError("模型服务不可用")
        prompt = str(config.get("prompt") or config.get("text") or "")
        if node_type == "extract":
            prompt = "请只返回 JSON 对象，并按字段要求提取信息。\n字段：{}\n内容：{}".format(json.dumps(config.get("fields") or [], ensure_ascii=False), prompt)
        elif node_type == "classifier":
            prompt = "请只返回最匹配的分类编号。\n分类：{}\n内容：{}".format(json.dumps(config.get("categories") or [], ensure_ascii=False), prompt)
        try:
            session = self.model_router.session(
                "auto", start_profile_id=config.get("model") or None
            )
            response = session.complete(
                ModelRequest(
                    messages=[CanonicalMessage("user", prompt)],
                    context=ModelCallContext(
                        run_id=run["run_id"],
                        tenant_id=run["organization_id"],
                        user_id=run.get("initiated_by"),
                        source="workflow",
                        operation=node_type,
                        agent_id="workflow",
                    ),
                )
            )
            text = str(response.message.content or "").strip()
        except Exception as exc:
            raise WorkflowError("工作流模型节点调用失败，请检查模型服务后重试") from exc
        if not text:
            raise WorkflowError("模型未返回内容")
        if node_type == "extract":
            try:
                return {"data": json.loads(self._json_text(text)), "text": text}
            except ValueError as exc:
                raise WorkflowError("字段提取模型未返回有效 JSON") from exc
        return {"text": text}

    def _http(self, run: Mapping[str, Any], config: Mapping[str, Any], node_id: str) -> Dict[str, Any]:
        url = str(config.get("url") or "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise WorkflowError("工作流 HTTP 节点仅允许有效的 HTTPS 地址")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
            for address in addresses:
                ip = ipaddress.ip_address(address)
                if not ip.is_global:
                    raise WorkflowError("工作流 HTTP 节点禁止访问回环、私网或链路本地地址")
        except socket.gaierror as exc:
            raise WorkflowError("工作流 HTTP 地址无法解析") from exc
        headers: Dict[str, str] = {"Idempotency-Key": "{}:{}".format(run["run_id"], node_id)}
        credential_id = str(config.get("credential_id") or "")
        if credential_id:
            if self.credential_service is None:
                raise WorkflowError("工作流凭据服务不可用")
            raw = self.credential_service.secret_for_resource(run["organization_id"], "workflow_http", credential_id)
            try:
                parsed_secret = json.loads(raw)
            except ValueError:
                parsed_secret = {"Authorization": raw}
            if not isinstance(parsed_secret, dict):
                raise WorkflowError("工作流 HTTPS 凭据格式无效")
            headers.update({str(key): str(value) for key, value in parsed_secret.items()})
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=float(config.get("timeout_seconds", 30))) as client:
                response = client.request(
                    str(config.get("method") or "GET").upper(),
                    url,
                    headers=headers,
                    json=config.get("body"),
                )
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                body: Any = response.json() if "json" in content_type else response.text[:1024 * 1024]
                return {"status_code": response.status_code, "body": body}
        except httpx.HTTPError as exc:
            raise WorkflowError("工作流 HTTPS 请求失败：{}".format(type(exc).__name__)) from exc

    def design_suggestion(self, organization_id: str, instruction: str, current: Mapping[str, Any], user_id: int) -> Dict[str, Any]:
        if self.model_router is None:
            raise WorkflowError("模型服务不可用")
        instruction = instruction.strip()
        if not instruction:
            raise WorkflowError("AI 搭建要求不能为空")
        catalog = [
            {"type": key, "name": value["name"], "category": value["category"]}
            for key, value in NODE_CATALOG.items()
        ]
        prompt = (
            "你是 BotPlatform 工作流设计器。根据要求返回且仅返回 Workflow DSL v1 JSON。"
            "不得输出代码节点、密钥、请求头或任意表达式。节点目录：{}\n当前草稿：{}\n用户要求：{}"
        ).format(json.dumps(catalog, ensure_ascii=False), json.dumps(current, ensure_ascii=False), instruction[:4000])
        try:
            session = self.model_router.session("auto")
            response = session.complete(
                ModelRequest(
                    messages=[CanonicalMessage("user", prompt)],
                    context=ModelCallContext(
                        tenant_id=organization_id,
                        user_id=user_id,
                        source="workflow",
                        operation="workflow_design",
                        agent_id="workflow-designer",
                    ),
                )
            )
        except Exception as exc:
            raise WorkflowError("AI 搭建调用模型失败，请检查模型服务后重试") from exc
        try:
            text = str(response.message.content or "").strip()
        except Exception as exc:
            raise WorkflowError("AI 搭建模型返回格式无效，请调整模型配置后重试") from exc
        try:
            decoded = json.loads(self._json_text(text))
        except (ValueError, TypeError):
            first_error: Exception = WorkflowError("模型返回内容不是有效 JSON")
        else:
            try:
                proposal = validate_definition(decoded)
                return {"proposal": proposal, "summary": "AI 已生成候选草稿，请检查差异后再应用。"}
            except WorkflowValidationError as exc:
                first_error = WorkflowError("候选工作流未通过 DSL 校验：{}".format(exc))
        try:
            repair = session.complete(ModelRequest(
                messages=[
                    CanonicalMessage("user", prompt),
                    CanonicalMessage("assistant", text),
                    CanonicalMessage(
                        "user",
                        "候选 DSL 未通过校验：{}。请修复并仅返回完整 JSON；这是唯一一次自动修复。".format(str(first_error)[:1000]),
                    ),
                ],
                context=ModelCallContext(
                    tenant_id=organization_id,
                    user_id=user_id,
                    source="workflow",
                    operation="workflow_design",
                    agent_id="workflow-designer",
                ),
            ))
        except Exception as exc:
            raise WorkflowError("AI 搭建自动修复失败，请调整描述后重试") from exc
        try:
            repair_text = str(repair.message.content or "").strip()
            if not repair_text:
                raise WorkflowError("模型未返回内容")
            repaired = json.loads(self._json_text(repair_text))
            proposal = validate_definition(repaired)
        except (WorkflowValidationError, WorkflowError) as exc:
            raise WorkflowError("AI 候选工作流未通过 DSL 校验：{}".format(exc)) from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise WorkflowError("AI 模型未返回有效 JSON 工作流") from exc
        return {"proposal": proposal, "summary": "AI 已生成候选草稿，请检查差异后再应用。"}

    @staticmethod
    def _json_text(text: str) -> str:
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        return match.group(1) if match else text
