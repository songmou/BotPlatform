"""Pydantic request/response models for the web API."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str = "ok"


class StatusResponse(BaseModel):
    model_ready: bool
    active_model: str
    cooling_down: bool
    agents_count: int
    default_agent: str


class ModelProfileOut(BaseModel):
    id: str
    enabled: bool
    type: str
    provider: str
    model: str
    temperature: float
    max_tokens: int
    timeout_seconds: float
    capabilities: Dict[str, bool]
    is_primary: bool = False
    is_fallback: bool = False


class ModelStatusOut(BaseModel):
    primary_profile_id: str
    fallback_profile_id: str
    local_profile_id: Optional[str] = None
    flash_profile_id: Optional[str] = None
    pro_profile_id: Optional[str] = None
    vision_profile_id: Optional[str] = None
    cooling_down: bool
    last_primary_error: Optional[str] = None


class ModelSwitchRequest(BaseModel):
    profile_id: str


class ModelCreate(BaseModel):
    id: str
    type: str = "openai_compatible"
    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key_env: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 2048
    timeout_seconds: float = 120
    enabled: bool = True
    capabilities: Dict[str, bool] = {"tools": False, "vision": False, "reasoning": False}


class ModelUpdate(BaseModel):
    type: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key_env: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout_seconds: Optional[float] = None
    enabled: Optional[bool] = None
    capabilities: Optional[Dict[str, bool]] = None


class AgentOut(BaseModel):
    id: str
    name: str
    role: str
    description: str
    system_prompt: str
    capabilities: List[Dict[str, str]]
    tools: List[str]
    model: Optional[str] = None
    greeting: Optional[str] = None
    greeting_hints: List[str] = []
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class AgentCapabilityIn(BaseModel):
    name: str
    description: str


class AgentCreate(BaseModel):
    id: str
    name: str
    role: str = ""
    description: str = ""
    system_prompt: str = ""
    capabilities: List[AgentCapabilityIn] = []
    tools: List[str] = []
    model: Optional[str] = None
    greeting: Optional[str] = None
    greeting_hints: List[str] = []
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    capabilities: Optional[List[AgentCapabilityIn]] = None
    tools: Optional[List[str]] = None
    model: Optional[str] = None
    greeting: Optional[str] = None
    greeting_hints: Optional[List[str]] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class ChatRequest(BaseModel):
    message: str
    agent_id: Optional[str] = None
    agent_ids: Optional[List[str]] = None
    regenerate: bool = False
    conversation_id: Optional[str] = None


class ChatHistoryItem(BaseModel):
    role: str
    content: str


class ChatHistoryResponse(BaseModel):
    messages: List[ChatHistoryItem]
