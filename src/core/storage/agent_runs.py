"""Durable agent checkpoints, tool journal, approvals and replayable events."""

from __future__ import annotations

import json
import secrets
import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, TypedDict, cast

from src.core.tooling.models import ToolError

if TYPE_CHECKING:
    from src.core.storage.database import Database

AgentRunStatus = Literal[
    "queued", "running", "waiting_approval", "waiting_input", "paused", "succeeded", "failed", "cancelled"
]


class AgentRun(TypedDict):
    run_id: str
    tenant_id: str
    conversation_id: str
    parent_run_id: Optional[str]
    agent_id: str
    source: str
    status: AgentRunStatus
    state: Dict[str, Any]
    cancel_requested: int
    lease_owner: Optional[str]
    lease_until: Optional[float]
    created_at: float
    updated_at: float


TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
WAITING = frozenset({"waiting_approval", "waiting_input", "paused"})


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AgentRunError(ToolError):
    """A scoped run operation was rejected."""


class AgentRunStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> AgentRun:
        if row is None:
            raise AgentRunError("任务不存在或无权访问")
        value = dict(row)
        value["state"] = json.loads(value.pop("state_json"))
        return cast(AgentRun, value)

    def create(
        self,
        tenant_id: str,
        conversation_id: str,
        agent_id: str,
        source: str,
        state: Dict[str, Any],
        parent_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        run_id = uuid.uuid4().hex
        now = time.time()
        with self.database.transaction(immediate=True) as con:
            if parent_run_id is not None:
                parent = con.execute(
                    "SELECT * FROM agent_runs WHERE run_id=? AND tenant_id=? AND conversation_id=?",
                    (parent_run_id, tenant_id, conversation_id),
                ).fetchone()
                if parent is None or parent["cancel_requested"] or parent["status"] in TERMINAL:
                    raise AgentRunError("上级任务已结束或停止，不能创建子任务")
            if parent_run_id is None:
                active = con.execute(
                    "SELECT run_id FROM agent_runs WHERE tenant_id=? AND conversation_id=? "
                    "AND parent_run_id IS NULL AND status NOT IN ('succeeded','failed','cancelled')",
                    (tenant_id, conversation_id),
                ).fetchone()
                if active:
                    raise AgentRunError("当前会话已有未完成任务，请先继续或停止该任务")
            con.execute(
                "INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?,0,NULL,NULL,?,?)",
                (run_id, tenant_id, conversation_id, parent_run_id, agent_id, source, "queued", dump(state), now, now),
            )
            self._event(con, run_id, {"type": "run_state", "status": "queued", "run_id": run_id})
        return self.get(run_id, tenant_id)

    def get(self, run_id: str, tenant_id: Optional[str] = None) -> AgentRun:
        with self.database.read() as con:
            query = "SELECT * FROM agent_runs WHERE run_id=?"
            args: list = [run_id]
            if tenant_id is not None:
                query += " AND tenant_id=?"
                args.append(tenant_id)
            return self._row(con.execute(query, args).fetchone())

    def list(self, tenant_id: str, conversation_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.database.read() as con:
            query = "SELECT * FROM agent_runs WHERE tenant_id=? AND parent_run_id IS NULL"
            args: list = [tenant_id]
            if conversation_id is not None:
                query += " AND conversation_id=?"
                args.append(conversation_id)
            rows = con.execute(query + " ORDER BY created_at DESC LIMIT 100", args).fetchall()
            return [self._row(row) for row in rows]

    @staticmethod
    def notification_version(run: dict) -> str:
        state = run["state"]
        marker = state.get("approval", {}).get("approval_id") or str(state.get("rounds", 0)) + state.get(
            "reason_code", ""
        )
        return run["status"] + ":" + marker

    def notification_recorded(self, run_id: str, version: str) -> bool:
        with self.database.read() as con:
            return (
                con.execute(
                    "SELECT 1 FROM agent_run_events WHERE run_id=? "
                    "AND json_extract(payload_json,'$.type')='notification_queued' "
                    "AND json_extract(payload_json,'$.version')=? LIMIT 1",
                    (run_id, version),
                ).fetchone()
                is not None
            )

    def pending_notifications(self) -> List[Dict[str, Any]]:
        with self.database.read() as con:
            rows = con.execute(
                "SELECT r.* FROM agent_runs r WHERE r.parent_run_id IS NULL "
                "AND r.status IN ('succeeded','failed','cancelled','waiting_approval','waiting_input','paused') "
                "AND (json_extract(r.state_json,'$.notify')=1 OR EXISTS "
                "(SELECT 1 FROM agent_run_subscriptions s WHERE s.run_id=r.run_id)) "
                "AND NOT EXISTS (SELECT 1 FROM agent_run_events e WHERE e.run_id=r.run_id "
                "AND json_extract(e.payload_json,'$.type')='notification_queued' "
                "AND json_extract(e.payload_json,'$.version')=r.status||':'||COALESCE("
                "json_extract(r.state_json,'$.approval.approval_id'),"
                "CAST(json_extract(r.state_json,'$.rounds') AS TEXT)||"
                "COALESCE(json_extract(r.state_json,'$.reason_code'),''))) "
                "ORDER BY r.updated_at LIMIT 20"
            ).fetchall()
        return [self._row(row) for row in rows]

    def unpublished(self) -> List[Dict[str, Any]]:
        with self.database.read() as con:
            rows = con.execute(
                "SELECT r.* FROM agent_runs r WHERE r.status IN ('succeeded','failed','cancelled') AND NOT EXISTS "
                "(SELECT 1 FROM agent_run_publications p WHERE p.run_id=r.run_id) "
                "ORDER BY r.updated_at LIMIT 20"
            ).fetchall()
        return [self._row(row) for row in rows]

    def published(self, run_id: str) -> bool:
        with self.database.read() as con:
            return con.execute("SELECT 1 FROM agent_run_publications WHERE run_id=?", (run_id,)).fetchone() is not None

    def mark_published(self, run_id: str) -> None:
        with self.database.transaction(immediate=True) as con:
            con.execute("INSERT OR IGNORE INTO agent_run_publications VALUES (?)", (run_id,))

    def subscribe_notifications(self, run_id: str) -> None:
        with self.database.transaction(immediate=True) as con:
            con.execute("INSERT OR IGNORE INTO agent_run_subscriptions VALUES (?)", (run_id,))

    def notifications_enabled(self, run_id: str) -> bool:
        with self.database.read() as con:
            return con.execute("SELECT 1 FROM agent_run_subscriptions WHERE run_id=?", (run_id,)).fetchone() is not None

    def children(self, parent_run_id: str) -> List[Dict[str, Any]]:
        with self.database.read() as con:
            rows = con.execute(
                "SELECT * FROM agent_runs WHERE parent_run_id=? ORDER BY created_at", (parent_run_id,)
            ).fetchall()
        return [self._row(row) for row in rows]

    def wake_parent(self, run_id: str) -> None:
        with self.database.transaction(immediate=True) as con:
            con.execute(
                "UPDATE agent_runs SET status='queued',updated_at=? WHERE run_id="
                "(SELECT parent_run_id FROM agent_runs WHERE run_id=?) "
                "AND status IN ('waiting_approval','waiting_input','paused') "
                "AND cancel_requested=0",
                (time.time(), run_id),
            )

    @staticmethod
    def _event(con: Any, run_id: str, payload: Dict[str, Any]) -> None:
        con.execute(
            "INSERT INTO agent_run_events(run_id,payload_json,created_at) VALUES (?,?,?)",
            (run_id, dump(payload), time.time()),
        )

    def emit(self, run_id: str, payload: Dict[str, Any]) -> None:
        with self.database.transaction(immediate=True) as con:
            self._event(con, run_id, payload)

    def events(self, run_id: str, after: int = 0) -> List[Dict[str, Any]]:
        with self.database.read() as con:
            rows = con.execute(
                "SELECT event_id,payload_json FROM agent_run_events WHERE run_id=? AND event_id>? "
                "ORDER BY event_id LIMIT 500",
                (run_id, after),
            ).fetchall()
        return [{"event_id": row["event_id"], **json.loads(row["payload_json"])} for row in rows]

    def cancel_finished_workflows(self) -> None:
        with self.database.read() as con:
            rows = con.execute(
                "SELECT r.run_id,r.tenant_id FROM agent_runs r LEFT JOIN workflow_runs w "
                "ON w.run_id=json_extract(r.state_json,'$.parent_workflow_id') "
                "WHERE r.status NOT IN ('succeeded','failed','cancelled') "
                "AND json_extract(r.state_json,'$.parent_workflow_id') IS NOT NULL "
                "AND (w.run_id IS NULL OR w.status IN ('canceled','failed','timed_out') OR "
                "json_extract(r.state_json,'$.parent_deadline')<=?) LIMIT 20",
                (time.time(),),
            ).fetchall()
        for row in rows:
            self.cancel(row["run_id"], row["tenant_id"])

    def claim(self, owner: str, lease_seconds: float = 600) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self.database.transaction(immediate=True) as con:
            # A claimed run has exclusive ownership across processes. An expired
            # lease is recovered only after the previous worker stopped renewing it.
            row = con.execute(
                "SELECT r.* FROM agent_runs r WHERE (r.status='queued' OR "
                "(r.status='running' AND r.lease_until<?)) AND "
                "NOT EXISTS (SELECT 1 FROM agent_runs x WHERE x.status='running' AND x.lease_until>=? "
                "AND json_extract(r.state_json,'$.resource_key') IS NOT NULL "
                "AND json_extract(x.state_json,'$.resource_key')=json_extract(r.state_json,'$.resource_key')) "
                "ORDER BY r.updated_at LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            con.execute(
                "UPDATE agent_runs SET status='running',lease_owner=?,lease_until=? WHERE run_id=?",
                (owner, now + lease_seconds, row["run_id"]),
            )
            value = self._row(row)
            value.update(status="running", lease_owner=owner, lease_until=now + lease_seconds)
            return value

    def heartbeat(self, run_id: str, owner: str) -> bool:
        with self.database.transaction(immediate=True) as con:
            return (
                con.execute(
                    "UPDATE agent_runs SET lease_until=? WHERE run_id=? AND lease_owner=?",
                    (time.time() + 600, run_id, owner),
                ).rowcount
                == 1
            )

    def checkpoint(self, run_id: str, owner: str, state: Dict[str, Any], status: str) -> None:
        state["resource_key"] = None
        queue = state.get("queue", [])
        index = state.get("queue_index", 0)
        if state.get("phase") == "tools" and index < len(queue):
            call = queue[index]
            name = call["name"]
            arguments = call.get("arguments") or {}
            if name == "db_execute" and isinstance(arguments, dict):
                state["resource_key"] = "datasource:" + str(arguments.get("datasource_id", ""))
            elif "__" in name and not name.startswith("agent__"):
                state["resource_key"] = "mcp:" + name.split("__", 1)[0]
            elif not name.startswith("agent__"):
                state["resource_key"] = "workspace:" + state["tenant"]["tenant_id"]
        with self.database.transaction(immediate=True) as con:
            row = con.execute(
                "SELECT cancel_requested,parent_run_id FROM agent_runs WHERE run_id=? AND lease_owner=?",
                (run_id, owner),
            ).fetchone()
            if row is None:
                raise AgentRunError("任务执行权已失效")
            if row["cancel_requested"]:
                status = "cancelled"
            if status == "cancelled":
                state["answer"] = "任务已停止"
            con.execute(
                "UPDATE agent_runs SET state_json=?,status=?,lease_owner=NULL,lease_until=NULL,updated_at=? "
                "WHERE run_id=? AND lease_owner=?",
                (dump(state), status, time.time(), run_id, owner),
            )
            self._event(
                con,
                run_id,
                {"type": "run_state", "status": status, "run_id": run_id, "reason": state.get("reason", "")},
            )
            if status in TERMINAL and row["parent_run_id"] is None:
                answer = state.get("answer") or state.get("reason") or "任务已结束"
                self._event(con, run_id, {"type": "token", "content": answer})
                self._event(
                    con,
                    run_id,
                    {
                        "type": "done",
                        "full_text": answer,
                        "run_id": state.get("analytics_run_id"),
                        "agent_run_id": run_id,
                        "status": status,
                    },
                )

    def cancel(self, run_id: str, tenant_id: str) -> Dict[str, Any]:
        self.get(run_id, tenant_id)
        with self.database.transaction(immediate=True) as con:
            # Recursion also cancels descendants of a multi-agent parent.
            con.execute(
                "WITH RECURSIVE tree(id) AS (SELECT run_id FROM agent_runs WHERE run_id=? "
                "UNION ALL SELECT r.run_id FROM agent_runs r JOIN tree t ON r.parent_run_id=t.id) "
                "UPDATE agent_runs SET cancel_requested=1, status=CASE WHEN lease_owner IS NULL "
                "THEN 'cancelled' ELSE status END,updated_at=? WHERE run_id IN (SELECT id FROM tree) "
                "AND status NOT IN ('succeeded','failed','cancelled')",
                (run_id, time.time()),
            )
            con.execute(
                "UPDATE agent_runs SET state_json=json_set(state_json,'$.answer','任务已停止') "
                "WHERE cancel_requested=1 AND status='cancelled' AND tenant_id=?",
                (tenant_id,),
            )
            con.execute(
                "UPDATE agent_run_approvals SET status='cancelled' WHERE run_id=? AND status='pending'", (run_id,)
            )
            self._event(con, run_id, {"type": "run_state", "status": "stopping", "run_id": run_id})
            cancelled = self._row(con.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone())
            if cancelled["status"] == "cancelled" and cancelled["parent_run_id"] is None:
                self._event(con, run_id, {"type": "run_state", "status": "cancelled", "run_id": run_id})
                self._event(
                    con,
                    run_id,
                    {
                        "type": "done",
                        "full_text": "任务已停止",
                        "agent_run_id": run_id,
                        "run_id": cancelled["state"].get("analytics_run_id"),
                        "status": "cancelled",
                    },
                )
        return self.get(run_id, tenant_id)

    def resume(
        self, run_id: str, tenant_id: str, user_input: str = "", script_result: Optional[dict] = None
    ) -> Dict[str, Any]:
        with self.database.transaction(immediate=True) as con:
            row = con.execute("SELECT * FROM agent_runs WHERE run_id=? AND tenant_id=?", (run_id, tenant_id)).fetchone()
            run = self._row(row)
            if run["status"] not in {"paused", "waiting_input"}:
                raise AgentRunError("当前任务不能恢复")
            if run["state"].get("reason_code") == "execution_unknown":
                raise AgentRunError("工具执行结果不确定，请先核对并处理该步骤")
            state = run["state"]
            state.update(reason="", reason_code="", repetition_count=0, replan_attempted=False, recent_fingerprints=[])
            state.pop("verification_progress", None)
            state.pop("input_question", None)
            if script_result:
                previous = state.pop("script_input", {})
                state.setdefault("pending_operations", {}).pop(previous.get("run_id"), None)
                state["pending_operations"][script_result["run_id"]] = "run_script"
                state["pending_user_input"] = (
                    "用户已通过脚本输入通道提供所需内容，脚本已续跑。请检查后续任务的实际结果："
                    + dump(script_result)
                )
            if user_input:
                state["pending_user_input"] = user_input
            con.execute(
                "UPDATE agent_runs SET status='queued',state_json=?,updated_at=? WHERE run_id=?",
                (dump(state), time.time(), run_id),
            )
        return self.get(run_id, tenant_id)

    def waiting_script_inputs(self) -> List[Dict[str, Any]]:
        with self.database.read() as con:
            rows = con.execute("SELECT * FROM agent_runs WHERE status='waiting_input'").fetchall()
        return [run for row in rows if (run := self._row(row))["state"].get("script_input")]

    def pause_script_input(self, run_id: str) -> None:
        with self.database.transaction(immediate=True) as con:
            run = self._row(con.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone())
            if run["status"] != "waiting_input":
                return
            state = run["state"]
            state.update(reason="脚本输入已过期、取消或续跑结果不确定，请核对脚本后重新发起任务",
                         reason_code="script_input_unavailable")
            con.execute(
                "UPDATE agent_runs SET status='paused',state_json=?,updated_at=? WHERE run_id=?",
                (dump(state), time.time(), run_id),
            )
            self._event(con, run_id, {"type": "run_state", "status": "paused", "reason": state["reason"]})

    def journal(self, run_id: str, step_key: str, call: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self.database.transaction(immediate=True) as con:
            row = con.execute(
                "SELECT * FROM agent_run_steps WHERE run_id=? AND step_key=?", (run_id, step_key)
            ).fetchone()
            if row is not None:
                if row["status"] == "done":
                    return json.loads(row["result_json"])
                raise AgentRunError("工具执行结果不确定，已暂停，禁止自动重复执行")
            con.execute(
                "INSERT INTO agent_run_steps VALUES (?,?,'started',?,NULL,?)",
                (run_id, step_key, dump(call), time.time()),
            )
        return None

    def journal_result(self, run_id: str, step_key: str, result: Dict[str, Any]) -> None:
        with self.database.transaction(immediate=True) as con:
            con.execute(
                "UPDATE agent_run_steps SET status='done',result_json=?,updated_at=? WHERE run_id=? AND step_key=?",
                (dump(result), time.time(), run_id, step_key),
            )

    def reconcile(self, run_id: str, tenant_id: str, step_key: str, result: Dict[str, Any]) -> None:
        run = self.get(run_id, tenant_id)
        if run["status"] != "paused" or run["state"].get("reason_code") != "execution_unknown":
            raise AgentRunError("任务没有待核对的执行步骤")
        with self.database.transaction(immediate=True) as con:
            changed = con.execute(
                "UPDATE agent_run_steps SET result_json=CASE WHEN status='done' THEN result_json ELSE ? END,"
                "status='done',updated_at=? WHERE run_id=? AND step_key=? AND status IN ('started','done')",
                (dump(result), time.time(), run_id, step_key),
            ).rowcount
            if not changed:
                raise AgentRunError("待核对步骤不存在")
            state = run["state"]
            state.update(reason="", reason_code="")
            con.execute(
                "UPDATE agent_runs SET state_json=?,status='queued',updated_at=? WHERE run_id=?",
                (dump(state), time.time(), run_id),
            )
            self._event(con, run_id, {"type": "reconciled", "step_key": step_key})

    def approval(self, run_id: str, calls: List[Dict[str, Any]], ttl: int) -> Dict[str, Any]:
        approval = {"approval_id": secrets.token_hex(12), "calls": calls, "expires_at": time.time() + ttl}
        with self.database.transaction(immediate=True) as con:
            con.execute(
                "INSERT INTO agent_run_approvals(approval_id,run_id,status,expires_at,payload_json) "
                "VALUES (?,?,'pending',?,?)",
                (approval["approval_id"], run_id, approval["expires_at"], dump(approval)),
            )
            self._event(con, run_id, {"type": "approval", "run_id": run_id, **approval})
        return approval

    def resolve(
        self, run_id: str, tenant_id: str, approval_id: str, approved: bool, actor_user_id: Optional[int] = None
    ) -> None:
        with self.database.transaction(immediate=True) as con:
            run = self._row(
                con.execute("SELECT * FROM agent_runs WHERE run_id=? AND tenant_id=?", (run_id, tenant_id)).fetchone()
            )
            row = con.execute(
                "SELECT * FROM agent_run_approvals WHERE approval_id=? AND run_id=?", (approval_id, run_id)
            ).fetchone()
            if not row or row["status"] != "pending" or run["status"] != "waiting_approval":
                raise AgentRunError("审批不存在、已处理或任务已停止")
            if row["expires_at"] <= time.time():
                raise AgentRunError("确认请求已经过期，请重新申请审批")
            state = run["state"]
            state["approval_decisions"] = {
                call["key"]: {"approved": approved, "call": call["call"]}
                for call in json.loads(row["payload_json"])["calls"]
            }
            state.pop("approval", None)
            self._event(
                con,
                run_id,
                {
                    "type": "approval_resolved",
                    "approval_id": approval_id,
                    "approved": approved,
                    "actor_user_id": actor_user_id,
                },
            )
            con.execute(
                "UPDATE agent_run_approvals SET status=?,resolved_by=?,resolved_at=? WHERE approval_id=?",
                ("approved" if approved else "denied", actor_user_id, time.time(), approval_id),
            )
            con.execute(
                "UPDATE agent_runs SET status='queued',state_json=?,updated_at=? WHERE run_id=?",
                (dump(state), time.time(), run_id),
            )

    def expire_one(self, run_id: str, approval_id: str, now: float) -> bool:
        with self.database.transaction(immediate=True) as con:
            row = con.execute(
                "SELECT * FROM agent_run_approvals WHERE run_id=? AND approval_id=? AND "
                "status='pending' AND expires_at<=?",
                (run_id, approval_id, now),
            ).fetchone()
            if row is None:
                return False
            run = self._row(con.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone())
            if run["status"] != "waiting_approval":
                return False
            state = run["state"]
            state.update(reason="审批已过期，请恢复任务后重新申请审批", reason_code="approval_expired")
            state.pop("approval", None)
            con.execute(
                "UPDATE agent_runs SET status='paused',state_json=?,updated_at=? WHERE run_id=?",
                (dump(state), time.time(), run_id),
            )
            con.execute("UPDATE agent_run_approvals SET status='expired' WHERE approval_id=?", (approval_id,))
            self._event(
                con, run_id, {"type": "run_state", "status": "paused", "reason": state["reason"], "run_id": run_id}
            )
            return True

    def expire_approvals(self) -> None:
        with self.database.transaction(immediate=True) as con:
            rows = con.execute(
                "SELECT * FROM agent_run_approvals WHERE status='pending' AND expires_at<=?", (time.time(),)
            ).fetchall()
            for row in rows:
                run = self._row(con.execute("SELECT * FROM agent_runs WHERE run_id=?", (row["run_id"],)).fetchone())
                if run["status"] == "waiting_approval":
                    state = run["state"]
                    state.update(reason="审批已过期，请恢复任务后重新申请审批", reason_code="approval_expired")
                    state.pop("approval", None)
                    con.execute(
                        "UPDATE agent_runs SET status='paused',state_json=? WHERE run_id=?",
                        (dump(state), run["run_id"]),
                    )
                    self._event(
                        con,
                        run["run_id"],
                        {"type": "run_state", "status": "paused", "reason": state["reason"], "run_id": run["run_id"]},
                    )
                con.execute(
                    "UPDATE agent_run_approvals SET status='expired' WHERE approval_id=?", (row["approval_id"],)
                )
                con.execute(
                    "UPDATE agent_runs SET status='queued',updated_at=? WHERE run_id="
                    "(SELECT parent_run_id FROM agent_runs WHERE run_id=?) "
                    "AND status IN ('waiting_approval','waiting_input','paused') AND cancel_requested=0",
                    (time.time(), row["run_id"]),
                )
