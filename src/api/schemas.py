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
    base_url: str
    api_key_env: Optional[str] = None
    model: str
    temperature: float
    max_tokens: int
    timeout_seconds: float
    capabilities: Dict[str, bool]
    billing_currency: str = "CNY"
    pricing: Optional[Dict[str, Optional[str]]] = None
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
    pricing: Optional[Dict[str, Optional[str]]] = None


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
    pricing: Optional[Dict[str, Optional[str]]] = None


class ModelFeedbackIn(BaseModel):
    rating: str
    reasons: List[str] = []
    comment: str = ""


class ModelBudgetIn(BaseModel):
    scope_type: str
    scope_id: str = ""
    monthly_limit_micros: int
    enabled: bool = True


class AgentOut(BaseModel):
    id: str
    name: str
    role: str
    description: str
    system_prompt: str
    capabilities: List[Dict[str, str]]
    tools: List[str]
    skills: List[str] = []
    mcp_servers: List[str] = []
    model: Optional[str] = None
    greeting: Optional[str] = None
    greeting_hints: List[str] = []
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    enabled: bool = True


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
    skills: List[str] = []
    mcp_servers: List[str] = []
    model: Optional[str] = None
    greeting: Optional[str] = None
    greeting_hints: List[str] = []
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    enabled: bool = True


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    capabilities: Optional[List[AgentCapabilityIn]] = None
    tools: Optional[List[str]] = None
    skills: Optional[List[str]] = None
    mcp_servers: Optional[List[str]] = None
    model: Optional[str] = None
    greeting: Optional[str] = None
    greeting_hints: Optional[List[str]] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    enabled: Optional[bool] = None


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


# ---- Schedule Task schemas ----

class TaskActionOut(BaseModel):
    type: str
    content: Optional[str] = None
    agent_id: Optional[str] = None
    prompt: Optional[str] = None
    image_path: Optional[str] = None
    image_url: Optional[str] = None
    caption: Optional[str] = None
    script_id: Optional[str] = None
    plugin_id: Optional[str] = None
    tool_name: Optional[str] = None
    parameters: Dict[str, Any] = {}


class TaskConditionOut(BaseModel):
    type: str
    after_hours: float
    before_hours: float


class ScheduleTaskOut(BaseModel):
    id: str
    enabled: bool
    cron: Optional[str] = None
    crons: List[str] = []
    target: str
    action: TaskActionOut
    condition: Optional[TaskConditionOut] = None


class ScheduleTaskCreate(BaseModel):
    id: str
    enabled: bool = True
    cron: Optional[str] = None
    crons: List[str] = []
    target: str = "last_active_user"
    action: Dict[str, Any]
    condition: Optional[Dict[str, Any]] = None


class ScheduleTaskUpdate(BaseModel):
    enabled: Optional[bool] = None
    cron: Optional[str] = None
    crons: Optional[List[str]] = None
    target: Optional[str] = None
    action: Optional[Dict[str, Any]] = None
    condition: Optional[Dict[str, Any]] = None


# ---- Plugin schemas ----

class PluginToolOut(BaseModel):
    name: str
    description: str
    requires_approval: bool = False
    parameters: Dict[str, Any] = {}


class PluginOut(BaseModel):
    id: str
    enabled: bool
    tool_count: int
    tools: List[PluginToolOut] = []
    settings: Dict[str, Any] = {}


class PluginUpdate(BaseModel):
    enabled: Optional[bool] = None
    settings: Optional[Dict[str, Any]] = None


class ToolStateUpdate(BaseModel):
    enabled: Optional[bool] = None
    require_approval: Optional[bool] = None


class ToolAuditOut(BaseModel):
    id: int
    ts: str
    tenant_id: Optional[str] = None
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    tool_name: str
    status: str
    duration_ms: int
    output_bytes: int
    args_hash: Optional[str] = None
    error: Optional[str] = None


class SkillOut(BaseModel):
    id: str
    name: str
    description: str
    prompt: str
    enabled: bool


class SkillCreate(BaseModel):
    id: str
    name: str
    description: str = ""
    prompt: str
    enabled: bool = True


class SkillUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    enabled: Optional[bool] = None


class McpServerOut(BaseModel):
    id: str
    name: str
    transport: str
    command: Optional[str] = None
    args: List[str] = []
    env: Dict[str, str] = {}
    url: Optional[str] = None
    headers: Dict[str, str] = {}
    enabled: bool


class McpServerCreate(BaseModel):
    id: str
    name: str
    transport: str = "stdio"
    command: Optional[str] = None
    args: List[str] = []
    env: Dict[str, str] = {}
    url: Optional[str] = None
    headers: Dict[str, str] = {}
    enabled: bool = True


class McpServerUpdate(BaseModel):
    name: Optional[str] = None
    transport: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    enabled: Optional[bool] = None


class TenantOverviewOut(BaseModel):
    tenant_id: str
    bot_id: str
    user_id: str
    created_at: str
    message_count: int = 0
    last_active_at: Optional[str] = None
    model_mode: str = "auto"


class TenantDetailOut(TenantOverviewOut):
    schedule_subscriptions: List[Dict[str, Any]] = []
    integrations: List[Dict[str, Any]] = []
    recent_events: List[Dict[str, Any]] = []


class AdminRoleOut(BaseModel):
    role_id: int
    code: str
    name: str
    permissions: List[str]
    builtin: bool


class AdminRoleUpdate(BaseModel):
    permissions: List[str]


class AdminUserOut(BaseModel):
    user_id: int
    username: str
    role: AdminRoleOut
    disabled: bool
    created_at: str
    last_login_at: Optional[str] = None


class AdminUserCreate(BaseModel):
    username: str
    role_id: int
    password: Optional[str] = None


class AdminUserUpdate(BaseModel):
    role_id: Optional[int] = None
    disabled: Optional[bool] = None


class PasswordResetOut(BaseModel):
    user_id: int
    new_password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class MeOut(BaseModel):
    user: AdminUserOut
    permissions: List[str]


class KnowledgeTextIn(BaseModel):
    tenant_id: str
    name: str
    content: str


class KnowledgeReindexIn(BaseModel):
    tenant_id: str


class DriveEntryOut(BaseModel):
    name: str
    path: str
    type: str
    size: int
    modified_at: float


class DriveBreadcrumbOut(BaseModel):
    name: str
    path: str


class DriveListOut(BaseModel):
    path: str
    breadcrumbs: List[DriveBreadcrumbOut]
    entries: List[DriveEntryOut]


class DriveFolderIn(BaseModel):
    scope: str
    tenant_id: Optional[str] = None
    path: str = ""
    name: str


class DriveEntryActionIn(BaseModel):
    scope: str
    tenant_id: Optional[str] = None
    action: str  # rename | move
    path: str
    target: str


class DriveAuditOut(BaseModel):
    id: int
    ts: str
    operator: str
    source: str
    scope: str
    tenant_id: Optional[str] = None
    action: str
    path: str
    target_path: Optional[str] = None
    size_bytes: int
    status: str
    error: Optional[str] = None
