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
    modality: str = "chat"
    dimensions: Optional[int] = None
    temperature: float
    max_tokens: int
    timeout_seconds: float
    capabilities: Dict[str, bool]
    billing_currency: str = "CNY"
    pricing: Optional[Dict[str, Optional[str]]] = None
    is_primary: bool = False
    is_fallback: bool = False
    restart_required: bool = False


class ModelStatusOut(BaseModel):
    primary_profile_id: str
    fallback_profile_id: str
    local_profile_id: Optional[str] = None
    flash_profile_id: Optional[str] = None
    pro_profile_id: Optional[str] = None
    vision_profile_id: Optional[str] = None
    embedding_profile_id: Optional[str] = None
    rerank_profile_id: Optional[str] = None
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
    modality: str = "chat"
    dimensions: Optional[int] = None
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
    modality: Optional[str] = None
    dimensions: Optional[int] = None
    api_key_env: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout_seconds: Optional[float] = None
    enabled: Optional[bool] = None
    capabilities: Optional[Dict[str, bool]] = None
    pricing: Optional[Dict[str, Optional[str]]] = None


class ModelRoleCandidate(BaseModel):
    id: str
    model: str
    enabled: bool


class ModelRolesOut(BaseModel):
    active_model: str
    fallback_model: str
    local_model: str
    flash_model: str
    pro_model: str
    vision_model: str
    embedding_model: str
    rerank_model: str
    chat_candidates: List[ModelRoleCandidate] = []
    vision_candidates: List[ModelRoleCandidate] = []
    embedding_candidates: List[ModelRoleCandidate] = []
    rerank_candidates: List[ModelRoleCandidate] = []


class ModelRolesUpdate(BaseModel):
    vision_model: Optional[str] = None
    embedding_model: Optional[str] = None
    rerank_model: Optional[str] = None


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
    plugin_tools: Dict[str, List[str]] = {}
    skills: List[str] = []
    mcp_servers: List[str] = []
    datasources: List[str] = []
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
    plugin_tools: Dict[str, List[str]] = {}
    skills: List[str] = []
    mcp_servers: List[str] = []
    datasources: List[str] = []
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
    plugin_tools: Optional[Dict[str, List[str]]] = None
    skills: Optional[List[str]] = None
    mcp_servers: Optional[List[str]] = None
    datasources: Optional[List[str]] = None
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
    approval_policy: str = "none"


class PluginOut(BaseModel):
    id: str
    name: str = ""
    version: str = ""
    description: str = ""
    source: str = "bundled"
    icon: str = ""
    color: str = "#6b7280"
    installed: bool = True
    enabled: bool
    configuration_status: str = "valid"
    runtime_status: str = "disabled"
    restart_required: bool = False
    missing_dependencies: List[str] = []
    load_error: Optional[str] = None
    setup_status: Optional[Dict[str, Any]] = None
    tool_count: int
    tools: List[PluginToolOut] = []
    settings: Dict[str, Any] = {}
    settings_schema: Dict[str, Any] = {}
    env_allowlist: List[str] = []


class PluginUpdate(BaseModel):
    enabled: Optional[bool] = None
    settings: Optional[Dict[str, Any]] = None


class PluginPackageIn(BaseModel):
    source_path: str


class PluginDataDeleteIn(BaseModel):
    confirmation: str


class ToolStateUpdate(BaseModel):
    enabled: Optional[bool] = None
    require_approval: Optional[bool] = None


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


class McpTemplateAuth(BaseModel):
    """描述模板创建实例时用户必须补全的鉴权字段（绝不含密钥值）。"""

    kind: str  # "header" / "env" / "query"
    key: str  # 请求头名称、环境变量名或 URL 查询参数名，如 "Authorization" / "NOTION_TOKEN" / "key"
    label: str  # 前端展示的字段名，如 "Token"
    secret: bool = True  # 是否以掩码输入
    placeholder: Optional[str] = None
    help: Optional[str] = None
    prefix: Optional[str] = None  # 写入密钥值前自动拼接的前缀，如 Bearer 类鉴权填 "Bearer "


class McpTemplateOut(BaseModel):
    """一个预设 MCP 服务模板（蓝图），不含任何密钥值。"""

    key: str
    name: str
    description: str = ""
    category: str = ""
    transport: str = "stdio"
    command: Optional[str] = None
    args: List[str] = []
    env: Dict[str, str] = {}
    url: Optional[str] = None
    icon: str = ""
    auth: Optional[McpTemplateAuth] = None
    help_url: Optional[str] = None


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
    tenant_id: Optional[str] = None
    name: str
    content: str
    category_id: Optional[str] = None


class KnowledgeReindexIn(BaseModel):
    tenant_id: str
    category_ids: Optional[List[str]] = None


class KnowledgeCategoryCreateIn(BaseModel):
    scope: str
    name: str
    description: str = ""
    tenant_id: Optional[str] = None


class KnowledgeCategoryUpdateIn(BaseModel):
    name: str
    description: str = ""


class KnowledgeDriveImportIn(BaseModel):
    category_id: str
    scope: str
    tenant_id: Optional[str] = None
    paths: List[str]


class KnowledgeRefreshIn(BaseModel):
    source_ids: List[str]


class KnowledgeMoveIn(BaseModel):
    source_ids: List[str]
    target_category_id: str


class KnowledgeEmbeddingStatusOut(BaseModel):
    bound: bool
    profile_id: Optional[str] = None
    model: Optional[str] = None
    dimensions: Optional[int] = None
    enabled: bool = False
    runtime_enabled: bool = False


class KnowledgeAgentBindingsIn(BaseModel):
    category_ids: List[str]


class DriveEntryOut(BaseModel):
    name: str
    path: str
    type: str
    size: int
    modified_at: float


class DriveBreadcrumbOut(BaseModel):
    name: str
    path: str


class DriveFolderIn(BaseModel):
    scope: str
    tenant_id: Optional[str] = None
    path: str = ""
    name: str
    exist_ok: bool = False


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


class PublishAgentIn(BaseModel):
    agent_id: str


class PublishEnabledIn(BaseModel):
    enabled: bool


class WeComConfigIn(BaseModel):
    bot_id: str
    secret: str


# ------------------------------------------------------------------ Datasource


class DatasourceCreate(BaseModel):
    id: str
    name: str
    engine: str
    host: str
    port: int
    database: str
    username: str = ""
    password: str = ""
    options: Optional[Dict[str, Any]] = None
    enabled: bool = True
    read_only: bool = True
    connect_timeout_seconds: int = 5
    statement_timeout_seconds: int = 15
    pool_size: int = 3
    max_rows: int = 200
    max_result_bytes: int = 262144
    tables: Optional[List[Dict[str, Any]]] = None
    prompt_injection: Optional[Dict[str, Any]] = None


class DatasourceUpdate(BaseModel):
    name: Optional[str] = None
    engine: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None  # null = keep unchanged
    options: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    read_only: Optional[bool] = None
    connect_timeout_seconds: Optional[int] = None
    statement_timeout_seconds: Optional[int] = None
    pool_size: Optional[int] = None
    max_rows: Optional[int] = None
    max_result_bytes: Optional[int] = None
    tables: Optional[List[Dict[str, Any]]] = None
    prompt_injection: Optional[Dict[str, Any]] = None


class DatasourceOut(BaseModel):
    id: str
    name: str
    engine: str
    host: str
    port: int
    database: str
    username: str = ""
    password: str = ""
    password_set: bool = False
    options: Optional[Dict[str, Any]] = None
    enabled: bool = True
    read_only: bool = True
    connect_timeout_seconds: int = 5
    statement_timeout_seconds: int = 15
    pool_size: int = 3
    max_rows: int = 200
    max_result_bytes: int = 262144
    tables: Optional[List[Dict[str, Any]]] = None
    prompt_injection: Optional[Dict[str, Any]] = None
    driver_ready: bool = False
    driver_hint: str = ""


class DatasourceStatusUpdate(BaseModel):
    enabled: bool


class DatasourceTestRequest(BaseModel):
    engine: str
    host: str
    port: int
    database: str
    username: str = ""
    password: str = ""
    options: Optional[Dict[str, Any]] = None
    connect_timeout_seconds: int = 5
    statement_timeout_seconds: int = 15


class DatasourceTestResponse(BaseModel):
    ok: bool
    latency_ms: int = 0
    version: str = ""
    error: str = ""


class DatasourceQueryRequest(BaseModel):
    sql: str
    limit: Optional[int] = None
