"""Agent management endpoints with CRUD support."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request

from src.api.deps import get_config
from src.api.schemas import AgentCreate, AgentOut, AgentUpdate
from src.core.config.loader import AgentPreset, Capability
from src.core.paths import CONFIG_DIR

router = APIRouter(prefix="/api/agents", tags=["agents"])

AGENTS_DIR = CONFIG_DIR / "agents"


def _to_out(agent) -> AgentOut:
    return AgentOut(
        id=agent.id,
        name=agent.name,
        role=agent.role,
        description=agent.description,
        system_prompt=agent.system_prompt,
        capabilities=[{"name": c.name, "description": c.description} for c in agent.capabilities],
        tools=agent.tools,
        skills=list(agent.skills),
        mcp_servers=list(agent.mcp_servers),
        model=agent.model,
        greeting=agent.greeting,
        greeting_hints=list(agent.greeting_hints),
        temperature=agent.temperature,
        max_tokens=agent.max_tokens,
    )


def _agent_to_dict(agent) -> dict:
    data = {
        "id": agent.id,
        "name": agent.name,
        "role": agent.role,
        "description": agent.description,
        "system_prompt": agent.system_prompt,
        "capabilities": [{"name": c.name, "description": c.description} for c in agent.capabilities],
        "tools": agent.tools,
        "skills": list(agent.skills),
        "mcp_servers": list(agent.mcp_servers),
    }
    if agent.model:
        data["model"] = agent.model
    if agent.greeting:
        data["greeting"] = agent.greeting
    if agent.greeting_hints:
        data["greeting_hints"] = agent.greeting_hints
    if agent.temperature is not None:
        data["temperature"] = agent.temperature
    if agent.max_tokens is not None:
        data["max_tokens"] = agent.max_tokens
    return data


def _save_agent_file(agent_id: str, data: dict) -> None:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = AGENTS_DIR / "{}.json".format(agent_id)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _delete_agent_file(agent_id: str) -> None:
    path = AGENTS_DIR / "{}.json".format(agent_id)
    if path.exists():
        path.unlink()


def _update_memory(request: Request, agent_id: str, preset: AgentPreset) -> None:
    config = get_config(request)
    config.agents[agent_id] = preset


def _remove_from_memory(request: Request, agent_id: str) -> None:
    config = get_config(request)
    config.agents.pop(agent_id, None)


@router.get("", response_model=list[AgentOut])
def list_agents(request: Request):
    config = get_config(request)
    return [_to_out(agent) for agent in config.agents.values()]


@router.get("/active", response_model=AgentOut)
def active_agent(request: Request):
    config = get_config(request)
    return _to_out(config.active_agent)


@router.get("/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: str, request: Request):
    config = get_config(request)
    agent = config.agents.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return _to_out(agent)


@router.post("", response_model=AgentOut, status_code=201)
def create_agent(body: AgentCreate, request: Request):
    config = get_config(request)
    agent_id = body.id.strip()
    if not agent_id or not re.match(r"^[a-z][a-z0-9_]{0,63}$", agent_id):
        raise HTTPException(status_code=400, detail="ID 只能包含小写字母、数字和下划线，且以字母开头")
    if agent_id in config.agents:
        raise HTTPException(status_code=409, detail="智能体 ID 已存在")
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="名称不能为空")

    preset = AgentPreset(
        id=agent_id,
        name=body.name.strip(),
        role=body.role.strip(),
        description=body.description.strip(),
        system_prompt=body.system_prompt,
        capabilities=[Capability(name=c.name, description=c.description) for c in body.capabilities],
        tools=body.tools,
        skills=body.skills,
        mcp_servers=body.mcp_servers,
        model=body.model or None,
        greeting=body.greeting or None,
        greeting_hints=body.greeting_hints or [],
        temperature=body.temperature,
        max_tokens=body.max_tokens,
    )
    _save_agent_file(agent_id, _agent_to_dict(preset))
    _update_memory(request, agent_id, preset)
    return _to_out(preset)


@router.put("/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: str, body: AgentUpdate, request: Request):
    config = get_config(request)
    existing = config.agents.get(agent_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="智能体不存在")

    preset = AgentPreset(
        id=agent_id,
        name=(body.name if body.name is not None else existing.name).strip() or existing.name,
        role=body.role if body.role is not None else existing.role,
        description=body.description if body.description is not None else existing.description,
        system_prompt=body.system_prompt if body.system_prompt is not None else existing.system_prompt,
        capabilities=(
            [Capability(name=c.name, description=c.description) for c in body.capabilities]
            if body.capabilities is not None
            else existing.capabilities
        ),
        tools=body.tools if body.tools is not None else existing.tools,
        skills=body.skills if body.skills is not None else existing.skills,
        mcp_servers=body.mcp_servers if body.mcp_servers is not None else existing.mcp_servers,
        model=(body.model or None) if body.model is not None else existing.model,
        greeting=(body.greeting or None) if body.greeting is not None else existing.greeting,
        greeting_hints=body.greeting_hints if body.greeting_hints is not None else existing.greeting_hints,
        temperature=body.temperature if body.temperature is not None else existing.temperature,
        max_tokens=body.max_tokens if body.max_tokens is not None else existing.max_tokens,
    )
    _save_agent_file(agent_id, _agent_to_dict(preset))
    _update_memory(request, agent_id, preset)
    return _to_out(preset)


@router.delete("/{agent_id}")
def delete_agent(agent_id: str, request: Request):
    config = get_config(request)
    if agent_id not in config.agents:
        raise HTTPException(status_code=404, detail="智能体不存在")
    if agent_id == config.app.default_agent:
        raise HTTPException(status_code=400, detail="不能删除默认智能体")
    _delete_agent_file(agent_id)
    _remove_from_memory(request, agent_id)
    return {"status": "ok"}
