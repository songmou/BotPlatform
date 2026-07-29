"""Skill management endpoints."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request

from src.api.deps import get_config
from src.api.schemas import SkillCreate, SkillOut, SkillUpdate
from src.core.config.loader import ConfigError
from src.core.paths import CONFIG_DIR

router = APIRouter(prefix="/api/skills", tags=["skills"])

SKILLS_FILE = CONFIG_DIR / "skills.json"
_TASK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _load() -> list:
    if SKILLS_FILE.exists():
        return json.loads(SKILLS_FILE.read_text(encoding="utf-8")).get("skills", [])
    return []


def _save(skills: list) -> None:
    SKILLS_FILE.write_text(
        json.dumps({"skills": skills}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _apply_and_save(request: Request, skills: list) -> None:
    """Validate, apply to the live config in place, then persist to disk."""
    try:
        get_config(request).update_skills(skills)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _save(skills)


def _to_out(item: dict) -> SkillOut:
    return SkillOut(
        id=item["id"],
        name=item["name"],
        description=item.get("description", ""),
        prompt=item.get("prompt", ""),
        enabled=item.get("enabled", True),
    )


@router.get("", response_model=list[SkillOut])
def list_skills():
    return [_to_out(s) for s in _load()]


@router.post("", response_model=SkillOut, status_code=201)
def create_skill(body: SkillCreate, request: Request):
    if not _TASK_ID_PATTERN.match(body.id):
        raise HTTPException(status_code=400, detail="ID 只能包含小写字母、数字和下划线，且以字母开头")
    skills = _load()
    if any(s["id"] == body.id for s in skills):
        raise HTTPException(status_code=409, detail="技能 ID 已存在")
    item = {
        "id": body.id,
        "name": body.name,
        "description": body.description,
        "prompt": body.prompt,
        "enabled": body.enabled,
    }
    skills.append(item)
    _apply_and_save(request, skills)
    return _to_out(item)


@router.put("/{skill_id}", response_model=SkillOut)
def update_skill(skill_id: str, body: SkillUpdate, request: Request):
    skills = _load()
    for s in skills:
        if s["id"] == skill_id:
            if body.name is not None:
                s["name"] = body.name
            if body.description is not None:
                s["description"] = body.description
            if body.prompt is not None:
                s["prompt"] = body.prompt
            if body.enabled is not None:
                s["enabled"] = body.enabled
            _apply_and_save(request, skills)
            return _to_out(s)
    raise HTTPException(status_code=404, detail="技能不存在")


@router.delete("/{skill_id}")
def delete_skill(skill_id: str, request: Request):
    skills = _load()
    filtered = [s for s in skills if s["id"] != skill_id]
    if len(filtered) == len(skills):
        raise HTTPException(status_code=404, detail="技能不存在")
    _apply_and_save(request, filtered)
    return {"status": "ok"}
