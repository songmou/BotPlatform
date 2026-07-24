"""Schedule task management endpoints."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request

from src.api.deps import get_config, get_scheduler
from src.api.schemas import (
    ScheduleTaskCreate,
    ScheduleTaskOut,
    ScheduleTaskUpdate,
    TaskActionOut,
    TaskConditionOut,
)
from src.core.paths import CONFIG_DIR

router = APIRouter(prefix="/api/schedules", tags=["schedules"])

SCHEDULES_FILE = CONFIG_DIR / "schedules.json"

_TASK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ACTION_TYPES = {"text", "agent_prompt", "script", "plugin", "image"}
_CRON_FIELD_PATTERN = re.compile(r"^[0-9*/,-]+$")


def _task_to_out(task) -> ScheduleTaskOut:
    action = TaskActionOut(
        type=task.action.type,
        content=task.action.content,
        agent_id=task.action.agent_id,
        prompt=task.action.prompt,
        image_path=task.action.image_path,
        image_url=task.action.image_url,
        caption=task.action.caption,
        script_id=task.action.script_id,
        plugin_id=task.action.plugin_id,
        tool_name=task.action.tool_name,
    )
    condition = None
    if task.condition:
        condition = TaskConditionOut(
            type=task.condition.type,
            after_hours=task.condition.after_hours,
            before_hours=task.condition.before_hours,
        )
    return ScheduleTaskOut(
        id=task.id,
        enabled=task.enabled,
        cron=task.cron,
        crons=list(task.crons) if task.crons else [],
        target=task.target,
        action=action,
        condition=condition,
    )


def _load_schedules_json() -> dict:
    if SCHEDULES_FILE.exists():
        return json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
    return {"tasks": []}


def _save_schedules_json(data: dict) -> None:
    SCHEDULES_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _find_task(config, task_id: str):
    for task in config.schedules:
        if task.id == task_id:
            return task
    return None


def _reload_scheduler(request: Request) -> None:
    scheduler = get_scheduler(request)
    if scheduler is not None:
        config = get_config(request)
        try:
            scheduler.reload_tasks(list(config.schedules))
        except Exception:
            pass


def _validate_cron(cron: str) -> None:
    parts = cron.strip().split()
    if len(parts) != 5:
        raise HTTPException(status_code=400, detail="cron 表达式必须有 5 个字段（分 时 日 月 周）")
    for part in parts:
        if not _CRON_FIELD_PATTERN.match(part):
            raise HTTPException(status_code=400, detail="cron 字段包含非法字符：{}".format(part))


def _validate_action(action_data: dict) -> None:
    action_type = action_data.get("type")
    if action_type not in _ACTION_TYPES:
        raise HTTPException(status_code=400, detail="动作类型必须是：{}".format("、".join(sorted(_ACTION_TYPES))))
    if action_type == "text" and not action_data.get("content"):
        raise HTTPException(status_code=400, detail="text 动作需要 content 字段")
    if action_type == "agent_prompt":
        if not action_data.get("agent_id"):
            raise HTTPException(status_code=400, detail="agent_prompt 动作需要 agent_id 字段")
        if not action_data.get("prompt"):
            raise HTTPException(status_code=400, detail="agent_prompt 动作需要 prompt 字段")
    if action_type == "script" and not action_data.get("script_id"):
        raise HTTPException(status_code=400, detail="script 动作需要 script_id 字段")
    if action_type == "plugin":
        if not action_data.get("plugin_id"):
            raise HTTPException(status_code=400, detail="plugin 动作需要 plugin_id 字段")
        if not action_data.get("tool_name"):
            raise HTTPException(status_code=400, detail="plugin 动作需要 tool_name 字段")


def _task_to_json(task, action, condition) -> dict:
    task_data = {
        "id": task.id,
        "enabled": task.enabled,
        "target": task.target,
        "action": {
            k: v for k, v in {
                "type": action.type,
                "content": action.content,
                "agent_id": action.agent_id,
                "prompt": action.prompt,
                "image_path": action.image_path,
                "image_url": action.image_url,
                "caption": action.caption,
                "script_id": action.script_id,
                "plugin_id": action.plugin_id,
                "tool_name": action.tool_name,
            }.items() if v is not None
        },
    }
    if task.crons:
        task_data["crons"] = list(task.crons)
    else:
        task_data["cron"] = task.cron
    if condition:
        task_data["condition"] = {
            "type": condition.type,
            "after_hours": condition.after_hours,
            "before_hours": condition.before_hours,
        }
    return task_data


@router.get("", response_model=list[ScheduleTaskOut])
def list_schedules(request: Request):
    config = get_config(request)
    return [_task_to_out(task) for task in config.schedules]


@router.get("/{task_id}", response_model=ScheduleTaskOut)
def get_schedule(task_id: str, request: Request):
    config = get_config(request)
    task = _find_task(config, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return _task_to_out(task)


@router.post("", response_model=ScheduleTaskOut, status_code=201)
def create_schedule(body: ScheduleTaskCreate, request: Request):
    config = get_config(request)
    task_id = body.id.strip()
    if not _TASK_ID_PATTERN.match(task_id):
        raise HTTPException(status_code=400, detail="ID 只能包含小写字母、数字和下划线，且以字母开头")
    if _find_task(config, task_id):
        raise HTTPException(status_code=409, detail="任务 ID 已存在")

    _validate_action(body.action)

    crons = list(body.crons) if body.crons else []
    cron = body.cron
    if not crons and not cron:
        raise HTTPException(status_code=400, detail="必须提供 cron 或 crons 字段")
    if cron:
        _validate_cron(cron)
    for c in crons:
        _validate_cron(c)

    from src.core.config.loader import ScheduledTask, TaskAction, TaskCondition

    action = TaskAction(
        type=body.action.get("type"),
        content=body.action.get("content"),
        agent_id=body.action.get("agent_id"),
        prompt=body.action.get("prompt"),
        image_path=body.action.get("image_path"),
        image_url=body.action.get("image_url"),
        caption=body.action.get("caption"),
        script_id=body.action.get("script_id"),
        plugin_id=body.action.get("plugin_id"),
        tool_name=body.action.get("tool_name"),
    )

    condition = None
    if body.condition:
        condition = TaskCondition(
            type=body.condition.get("type", "inactivity_once"),
            after_hours=float(body.condition.get("after_hours", 0)),
            before_hours=float(body.condition.get("before_hours", 24)),
        )

    task = ScheduledTask(
        id=task_id,
        enabled=body.enabled,
        cron=cron or "",
        target=body.target,
        action=action,
        condition=condition,
        crons=crons,
    )

    data = _load_schedules_json()
    task_data = _task_to_json(task, action, condition)
    data.setdefault("tasks", []).append(task_data)
    _save_schedules_json(data)

    config.schedules.append(task)

    _reload_scheduler(request)

    return _task_to_out(task)


@router.put("/{task_id}", response_model=ScheduleTaskOut)
def update_schedule(task_id: str, body: ScheduleTaskUpdate, request: Request):
    config = get_config(request)
    task = _find_task(config, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")

    from src.core.config.loader import ScheduledTask, TaskAction, TaskCondition

    enabled = body.enabled if body.enabled is not None else task.enabled
    target = body.target if body.target is not None else task.target

    if body.crons is not None:
        crons = list(body.crons)
        for c in crons:
            _validate_cron(c)
        cron = ""
    elif body.cron is not None:
        _validate_cron(body.cron)
        cron = body.cron
        crons = []
    else:
        cron = task.cron
        crons = list(task.crons)

    if body.action is not None:
        _validate_action(body.action)
        action = TaskAction(
            type=body.action.get("type"),
            content=body.action.get("content"),
            agent_id=body.action.get("agent_id"),
            prompt=body.action.get("prompt"),
            image_path=body.action.get("image_path"),
            image_url=body.action.get("image_url"),
            caption=body.action.get("caption"),
            script_id=body.action.get("script_id"),
            plugin_id=body.action.get("plugin_id"),
            tool_name=body.action.get("tool_name"),
        )
    else:
        action = task.action

    condition = task.condition
    if body.condition is not None:
        condition = TaskCondition(
            type=body.condition.get("type", "inactivity_once"),
            after_hours=float(body.condition.get("after_hours", 0)),
            before_hours=float(body.condition.get("before_hours", 24)),
        )

    new_task = ScheduledTask(
        id=task.id,
        enabled=enabled,
        cron=cron,
        target=target,
        action=action,
        condition=condition,
        crons=crons,
    )

    for i, t in enumerate(config.schedules):
        if t.id == task_id:
            config.schedules[i] = new_task
            break

    data = _load_schedules_json()
    for i, t in enumerate(data.get("tasks", [])):
        if t.get("id") == task_id:
            data["tasks"][i] = _task_to_json(new_task, action, condition)
            break
    _save_schedules_json(data)

    _reload_scheduler(request)

    return _task_to_out(new_task)


@router.delete("/{task_id}")
def delete_schedule(task_id: str, request: Request):
    config = get_config(request)
    task = _find_task(config, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")

    config.schedules[:] = [t for t in config.schedules if t.id != task_id]

    data = _load_schedules_json()
    data["tasks"] = [t for t in data.get("tasks", []) if t.get("id") != task_id]
    _save_schedules_json(data)

    _reload_scheduler(request)

    return {"status": "ok"}
