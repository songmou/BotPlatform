"""Workflow DSL v1 validation, normalization and safe variable rendering."""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence
from urllib.parse import urlparse

from apscheduler.triggers.cron import CronTrigger


class WorkflowValidationError(ValueError):
    """Raised when an organization workflow definition is unsafe or invalid."""


def _field(
    key: str,
    label: str,
    field_type: str = "text",
    *,
    required: bool = False,
    default: Any = None,
    help_text: str = "",
    options: Any = None,
    resource: str = "",
    minimum: Any = None,
    maximum: Any = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"key": key, "label": label, "type": field_type}
    if required:
        result["required"] = True
    if default is not None:
        result["default"] = default
    if help_text:
        result["help"] = help_text
    if options is not None:
        result["options"] = options
    if resource:
        result["resource"] = resource
    constraints = {}
    if minimum is not None:
        constraints["min"] = minimum
    if maximum is not None:
        constraints["max"] = maximum
    if constraints:
        result["constraints"] = constraints
    return result


def _node(
    name: str,
    category: str,
    risk: str,
    fields: List[Dict[str, Any]],
    inputs: List[Dict[str, str]],
    outputs: List[Dict[str, str]],
    ports: List[Dict[str, str]],
) -> Dict[str, Any]:
    return {
        "name": name,
        "category": category,
        "risk": risk,
        "config_fields": fields,
        "inputs": inputs,
        "outputs": outputs,
        "output_ports": ports,
        "side_effect": risk in {"write", "dynamic"},
        "supports_retry": risk in {"read", "dynamic"},
    }


_DEFAULT_PORT = [{"key": "default", "label": "继续"}, {"key": "error", "label": "错误"}]
_ANY_INPUT = [{"key": "context", "type": "any", "description": "工作流变量上下文"}]

NODE_CATALOG: Dict[str, Dict[str, Any]] = {
    "start": _node(
        "开始", "基础", "none",
        [],
        [],
        [{"key": "output", "type": "object", "description": "工作流输入"}],
        [{"key": "default", "label": "开始"}],
    ),
    "end": _node(
        "结束", "基础", "none",
        [_field("output", "最终输出", "json", default={})],
        _ANY_INPUT,
        [{"key": "output", "type": "any", "description": "最终工作流输出"}],
        [],
    ),
    "set_variable": _node(
        "设置变量", "基础", "none",
        [_field("values", "变量映射", "json", required=True, default={})],
        _ANY_INPUT,
        [{"key": "output", "type": "object", "description": "变量对象"}],
        _DEFAULT_PORT,
    ),
    "template": _node(
        "文本模板", "基础", "none",
        [_field("text", "模板文本", "textarea", required=True, help_text="支持 {{input.field}} 等安全变量")],
        _ANY_INPUT,
        [{"key": "text", "type": "string", "description": "渲染文本"}],
        _DEFAULT_PORT,
    ),
    "field_map": _node(
        "字段映射", "基础", "none",
        [_field("mapping", "字段映射", "json", required=True, default={})],
        _ANY_INPUT,
        [{"key": "output", "type": "object", "description": "映射结果"}],
        _DEFAULT_PORT,
    ),
    "merge": _node(
        "合并结果", "基础", "none",
        [_field("values", "合并对象", "json", required=True, default={})],
        _ANY_INPUT,
        [{"key": "output", "type": "object", "description": "合并结果"}],
        _DEFAULT_PORT,
    ),
    "delay": _node(
        "延迟等待", "基础", "none",
        [_field("seconds", "等待秒数", "number", required=True, default=60, minimum=1, maximum=2592000)],
        [],
        [{"key": "resumed_at", "type": "string", "description": "恢复时间"}],
        _DEFAULT_PORT,
    ),
    "llm": _node(
        "LLM 生成", "AI", "read",
        [
            _field("prompt", "提示词", "textarea", required=True),
            _field("model", "模型配置", "resource", resource="models"),
        ],
        _ANY_INPUT,
        [{"key": "text", "type": "string", "description": "模型文本"}],
        _DEFAULT_PORT,
    ),
    "extract": _node(
        "字段提取", "AI", "read",
        [
            _field("text", "待提取文本", "textarea", required=True),
            _field("fields", "字段定义", "json", required=True, default=[]),
        ],
        _ANY_INPUT,
        [
            {"key": "data", "type": "object", "description": "结构化字段"},
            {"key": "text", "type": "string", "description": "模型原文"},
        ],
        _DEFAULT_PORT,
    ),
    "classifier": _node(
        "文本分类", "AI", "read",
        [
            _field("text", "待分类文本", "textarea", required=True),
            _field("categories", "分类列表", "json", required=True, default=[]),
        ],
        _ANY_INPUT,
        [{"key": "text", "type": "string", "description": "分类结果"}],
        _DEFAULT_PORT,
    ),
    "agent": _node(
        "组织智能体", "AI", "read",
        [
            _field("agent_id", "智能体", "resource", required=True, resource="agents"),
            _field("prompt", "提示词", "textarea", required=True),
        ],
        _ANY_INPUT,
        [{"key": "text", "type": "string", "description": "智能体回复"}],
        _DEFAULT_PORT,
    ),
    "knowledge": _node(
        "知识库检索", "AI", "read",
        [
            _field("query", "检索问题", "textarea", required=True),
            _field("limit", "返回数量", "number", default=6, minimum=1, maximum=100),
            _field("category_ids", "知识库分类", "json", default=[]),
        ],
        _ANY_INPUT,
        [{"key": "items", "type": "array", "description": "知识条目"}],
        _DEFAULT_PORT,
    ),
    "condition": _node(
        "条件分支", "控制", "none",
        [
            _field("left", "左值", "text", required=True),
            _field(
                "operator", "操作符", "select", required=True, default="equals",
                options=["equals", "not_equals", "contains", "exists", "gt", "gte", "lt", "lte"],
            ),
            _field("right", "右值", "text"),
        ],
        _ANY_INPUT,
        [{"key": "matched", "type": "boolean", "description": "判断结果"}],
        [
            {"key": "true", "label": "成立"},
            {"key": "false", "label": "不成立"},
            {"key": "error", "label": "错误"},
        ],
    ),
    "switch": _node(
        "多路分支", "控制", "none",
        [
            _field("value", "匹配值", "text", required=True),
            _field("cases", "分支列表", "json", required=True, default=[]),
        ],
        _ANY_INPUT,
        [{"key": "value", "type": "any", "description": "匹配值"}],
        [
            {"key": "case:*", "label": "匹配分支", "dynamic": "cases"},
            {"key": "default", "label": "默认"},
            {"key": "error", "label": "错误"},
        ],
    ),
    "for_each": _node(
        "顺序迭代", "控制", "read",
        [
            _field("workflow_id", "子工作流", "resource", required=True, resource="workflows"),
            _field("items", "迭代数组", "json", required=True, default=[]),
        ],
        _ANY_INPUT,
        [{"key": "items", "type": "array", "description": "各项输出"}],
        _DEFAULT_PORT,
    ),
    "subworkflow": _node(
        "子工作流", "控制", "read",
        [
            _field("workflow_id", "子工作流", "resource", required=True, resource="workflows"),
            _field("inputs", "输入映射", "json", default={}),
        ],
        _ANY_INPUT,
        [{"key": "output", "type": "any", "description": "子工作流输出"}],
        _DEFAULT_PORT,
    ),
    "tool": _node(
        "平台工具", "能力", "dynamic",
        [
            _field("tool_name", "工具名称", "resource", required=True, resource="tools"),
            _field("arguments", "参数", "json", default={}),
        ],
        _ANY_INPUT,
        [{"key": "data", "type": "any", "description": "工具结果"}],
        _DEFAULT_PORT,
    ),
    "script": _node(
        "平台脚本", "能力", "dynamic",
        [
            _field("script_id", "脚本", "resource", required=True, resource="scripts"),
            _field("parameters", "脚本参数", "json", default={}),
        ],
        _ANY_INPUT,
        [{"key": "output", "type": "object", "description": "脚本提交结果"}],
        _DEFAULT_PORT,
    ),
    "datasource": _node(
        "只读数据源", "能力", "read",
        [
            _field("datasource_id", "数据源", "resource", required=True, resource="datasources"),
            _field("sql", "只读 SQL", "textarea", required=True, default="SELECT 1"),
            _field("limit", "最大行数", "number", default=100, minimum=1, maximum=1000),
        ],
        [],
        [
            {"key": "rows", "type": "array", "description": "数据行"},
            {"key": "row_count", "type": "integer", "description": "行数"},
        ],
        _DEFAULT_PORT,
    ),
    "http": _node(
        "HTTPS 请求", "外部交互", "dynamic",
        [
            _field(
                "method", "请求方法", "select", required=True, default="GET",
                options=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
            ),
            _field("url", "HTTPS 地址", "text", required=True, default="https://example.com"),
            _field("body", "JSON 请求体", "json"),
            _field("credential_id", "凭据编号", "resource", resource="credentials"),
            _field("timeout_seconds", "超时秒数", "number", default=30, minimum=1, maximum=120),
        ],
        _ANY_INPUT,
        [
            {"key": "status_code", "type": "integer", "description": "HTTP 状态"},
            {"key": "body", "type": "any", "description": "响应体"},
        ],
        _DEFAULT_PORT,
    ),
    "notification": _node(
        "消息通知", "外部交互", "write",
        [_field("message", "通知内容", "textarea", required=True)],
        _ANY_INPUT,
        [
            {"key": "notification_ids", "type": "array", "description": "通知编号"},
            {"key": "status", "type": "string", "description": "投递状态"},
        ],
        _DEFAULT_PORT,
    ),
    "approval": _node(
        "人工审批", "人工介入", "none",
        [
            _field("title", "审批标题", "text", required=True, default="请审批"),
            _field("ttl_seconds", "有效秒数", "number", default=86400, minimum=60, maximum=2592000),
            _field("assignees", "处理人", "json", default={"roles": ["owner", "admin"]}),
            _field("payload", "审批内容", "json"),
        ],
        _ANY_INPUT,
        [{"key": "output", "type": "object", "description": "审批响应对象"}],
        [
            {"key": "approved", "label": "通过"},
            {"key": "rejected", "label": "拒绝"},
            {"key": "error", "label": "错误"},
        ],
    ),
    "human_input": _node(
        "补充输入", "人工介入", "none",
        [
            _field("title", "输入标题", "text", required=True, default="请补充信息"),
            _field("ttl_seconds", "有效秒数", "number", default=86400, minimum=60, maximum=2592000),
            _field("fields", "字段定义", "json", required=True, default=[]),
        ],
        [],
        [{"key": "output", "type": "object", "description": "用户提交字段对象"}],
        _DEFAULT_PORT,
    ),
}

TRIGGER_TYPES = {"manual", "api", "webhook", "schedule"}
INPUT_TYPES = {"string", "number", "integer", "boolean", "object", "array", "file_ref"}
ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
VARIABLE_PATTERN = re.compile(r"{{\s*([^{}]+?)\s*}}")
SECRET_PARTS = {
    "secret", "token", "password", "authorization", "api_key", "apikey",
    "headers", "credential_value", "cookie",
}


def _matches_declared_type(field_type: str, value: Any) -> bool:
    """Return whether a JSON value satisfies a workflow field declaration."""
    return {
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "file_ref": isinstance(value, str),
    }.get(field_type, False)


def validate_field_values(
    fields: Sequence[Mapping[str, Any]], value: Any, *, subject: str
) -> Dict[str, Any]:
    """Validate one JSON object against workflow input-style field metadata."""
    if not isinstance(value, Mapping):
        raise WorkflowValidationError("{}必须是对象".format(subject))
    result = dict(value)
    for field in fields:
        key = str(field.get("key") or "")
        label = str(field.get("label") or key)
        field_value = result.get(key)
        if field.get("required") and (key not in result or field_value in (None, "")):
            raise WorkflowValidationError("{}缺少必填字段：{}".format(subject, label))
        if key not in result or field_value is None:
            continue
        field_type = str(field.get("type") or "string")
        if not _matches_declared_type(field_type, field_value):
            raise WorkflowValidationError(
                "{}字段 {} 的类型必须为 {}".format(subject, label, field_type)
            )
    return result


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized in SECRET_PARTS:
        return True
    return normalized.endswith(("_secret", "_password", "_api_key", "_apikey", "_authorization", "_cookie"))


def empty_definition(name: str = "新工作流") -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "name": name,
        "description": "",
        "inputs": [],
        "outputs": [],
        "triggers": [{"id": "manual", "type": "manual", "config": {}}],
        "nodes": [
            {
                "id": "start", "type": "start", "name": "开始",
                "position": {"x": 80, "y": 160}, "config": {}, "error_policy": {"mode": "stop"},
            },
            {
                "id": "end", "type": "end", "name": "结束",
                "position": {"x": 420, "y": 160}, "config": {}, "error_policy": {"mode": "stop"},
            },
        ],
        "edges": [
            {
                "id": "start-end", "source": "start", "source_port": "default",
                "target": "end", "target_port": "default",
            },
        ],
        "settings": {"timeout_seconds": 86400, "max_steps": 500},
    }


def _reject_secrets(value: Any, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if _is_secret_key(normalized) and item not in (None, "", {}, []):
                raise WorkflowValidationError("工作流定义不得保存密钥字段：{}{}".format(path, key))
            _reject_secrets(item, path + str(key) + ".")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secrets(item, "{}{}.".format(path, index))


def _id(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not ID_PATTERN.fullmatch(result):
        raise WorkflowValidationError("{}格式无效：{}".format(label, result or "空值"))
    return result


def _fields(raw: Any, label: str) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        raise WorkflowValidationError("{}必须是数组".format(label))
    result: List[Dict[str, Any]] = []
    seen = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise WorkflowValidationError("{}字段必须是对象".format(label))
        field = dict(item)
        key = _id(field.get("key"), "{}字段名".format(label))
        if key in seen:
            raise WorkflowValidationError("{}字段重复：{}".format(label, key))
        seen.add(key)
        field_type = str(field.get("type") or "string")
        if field_type not in INPUT_TYPES:
            raise WorkflowValidationError("{}字段类型不支持：{}".format(label, field_type))
        if "default" in field and field["default"] is not None:
            if not _matches_declared_type(field_type, field["default"]):
                raise WorkflowValidationError(
                    "{}字段 {} 的默认值类型必须为 {}".format(label, key, field_type)
                )
        result.append({
            "key": key,
            "label": str(field.get("label") or key)[:128],
            "type": field_type,
            "required": bool(field.get("required", False)),
            **({"default": copy.deepcopy(field["default"])} if "default" in field else {}),
        })
    return result


def _variables(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield from VARIABLE_PATTERN.findall(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _variables(item)
    elif isinstance(value, list):
        for item in value:
            yield from _variables(item)


def _dynamic_value(value: Any) -> bool:
    return isinstance(value, str) and VARIABLE_PATTERN.fullmatch(value.strip()) is not None


def _topological(node_ids: Sequence[str], edges: Sequence[Mapping[str, Any]]) -> List[str]:
    incoming = {node_id: 0 for node_id in node_ids}
    outgoing: Dict[str, List[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        source, target = str(edge["source"]), str(edge["target"])
        outgoing[source].append(target)
        incoming[target] += 1
    queue = sorted(node_id for node_id, count in incoming.items() if count == 0)
    ordered: List[str] = []
    while queue:
        current = queue.pop(0)
        ordered.append(current)
        for target in outgoing[current]:
            incoming[target] -= 1
            if incoming[target] == 0:
                queue.append(target)
    if len(ordered) != len(node_ids):
        raise WorkflowValidationError("工作流必须是无环图，不能连接回已执行节点")
    return ordered


def _require_config(node_id: str, node_type: str, config: Mapping[str, Any]) -> None:
    """Validate node-specific configuration before a draft can be published."""
    spec = NODE_CATALOG[node_type]
    for field in spec.get("config_fields", []):
        key = str(field["key"])
        if field.get("required") and config.get(key) in (None, "", [], {}):
            raise WorkflowValidationError("节点 {} 缺少必填配置：{}".format(node_id, field["label"]))
        if field.get("type") == "resource" and config.get(key) not in (None, ""):
            if not RESOURCE_ID_PATTERN.fullmatch(str(config[key])):
                raise WorkflowValidationError(
                    "节点 {} 的{}资源编号格式无效".format(node_id, field["label"])
                )
        if field.get("type") == "number" and config.get(key) not in (None, ""):
            try:
                number = float(config[key])
            except (TypeError, ValueError) as exc:
                raise WorkflowValidationError(
                    "节点 {} 的{}必须是数字".format(node_id, field["label"])
                ) from exc
            constraints = field.get("constraints") or {}
            if "min" in constraints and number < float(constraints["min"]):
                raise WorkflowValidationError(
                    "节点 {} 的{}不能小于 {}".format(node_id, field["label"], constraints["min"])
                )
            if "max" in constraints and number > float(constraints["max"]):
                raise WorkflowValidationError(
                    "节点 {} 的{}不能大于 {}".format(node_id, field["label"], constraints["max"])
                )
    object_fields = {
        "set_variable": ("values", "变量映射"),
        "field_map": ("mapping", "字段映射"),
        "merge": ("values", "合并对象"),
        "subworkflow": ("inputs", "输入映射"),
        "tool": ("arguments", "工具参数"),
        "script": ("parameters", "脚本参数"),
    }
    if node_type in object_fields:
        key, label = object_fields[node_type]
        value = config.get(key)
        if not isinstance(value, Mapping) and not _dynamic_value(value):
            raise WorkflowValidationError("节点 {} 的{}必须是对象".format(node_id, label))

    if node_type == "condition":
        if str(config.get("operator") or "equals") not in {
            "equals", "not_equals", "contains", "exists", "gt", "gte", "lt", "lte",
        }:
            raise WorkflowValidationError("节点 {} 的条件操作符无效".format(node_id))
    elif node_type == "switch":
        cases = config.get("cases") or []
        if not isinstance(cases, list):
            raise WorkflowValidationError("节点 {} 的分支列表必须是数组".format(node_id))
        keys, values = [], []
        for item in cases:
            if not isinstance(item, Mapping):
                raise WorkflowValidationError("节点 {} 的分支项必须是对象".format(node_id))
            key = str(item.get("key", item.get("value", ""))).strip()
            if not key:
                raise WorkflowValidationError("节点 {} 的分支键不能为空".format(node_id))
            if "value" not in item:
                raise WorkflowValidationError("节点 {} 的分支 {} 缺少匹配值".format(node_id, key))
            keys.append(key)
            values.append(json.dumps(item.get("value"), ensure_ascii=False, sort_keys=True))
        if len(keys) != len(set(keys)):
            raise WorkflowValidationError("节点 {} 的分支键不能重复".format(node_id))
        if len(values) != len(set(values)):
            raise WorkflowValidationError("节点 {} 的分支匹配值不能重复".format(node_id))
    elif node_type in {"delay", "approval", "human_input"}:
        numeric_key = "seconds" if node_type == "delay" else "ttl_seconds"
        try:
            seconds = int(config.get(numeric_key, 60))
        except (TypeError, ValueError) as exc:
            raise WorkflowValidationError("节点 {} 的时间配置必须是整数".format(node_id)) from exc
        if seconds < (1 if node_type == "delay" else 60) or seconds > 30 * 86400:
            raise WorkflowValidationError("节点 {} 的时间配置超出允许范围".format(node_id))
        if node_type == "approval":
            assignees = config.get("assignees") or {}
            if not isinstance(assignees, Mapping):
                raise WorkflowValidationError("节点 {} 的处理人配置必须是对象".format(node_id))
            roles = assignees.get("roles") or []
            users = assignees.get("user_ids") or []
            if not isinstance(roles, list) or not isinstance(users, list):
                raise WorkflowValidationError("节点 {} 的处理人角色和用户必须是数组".format(node_id))
            if any(str(role) not in {"owner", "admin", "member"} for role in roles):
                raise WorkflowValidationError("节点 {} 包含无效审批角色".format(node_id))
    elif node_type == "knowledge":
        categories = config.get("category_ids", [])
        if not isinstance(categories, list) and not _dynamic_value(categories):
            raise WorkflowValidationError("节点 {} 的知识库分类必须是数组".format(node_id))
        try:
            limit = int(config.get("limit", 6))
        except (TypeError, ValueError) as exc:
            raise WorkflowValidationError("节点 {} 的返回数量必须是整数".format(node_id)) from exc
        if limit < 1 or limit > 100:
            raise WorkflowValidationError("节点 {} 的返回数量必须在 1 到 100 之间".format(node_id))
    elif node_type == "datasource":
        sql = str(config.get("sql") or "").lstrip()
        if not re.match(r"(?is)^(select|with)\b", sql):
            raise WorkflowValidationError("节点 {} 只允许只读 SELECT 或 WITH 查询".format(node_id))
        if ";" in sql.rstrip(";"):
            raise WorkflowValidationError("节点 {} 不允许执行多条 SQL".format(node_id))
        # The datasource gateway repeats this check using sqlglot and the
        # datasource's dialect/table allow-list. Keep the DSL layer dependency
        # free while rejecting evident writes, including data-modifying CTEs.
        lexical_sql = re.sub(r"(?s)'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", "", sql)
        if re.search(
            r"(?i)\b(insert|update|delete|merge|create|drop|alter|truncate|replace|"
            r"grant|revoke|copy|call|execute|load|lock|unlock)\b",
            lexical_sql,
        ):
            raise WorkflowValidationError("节点 {} 只允许只读 SQL 查询".format(node_id))
        try:
            limit = int(config.get("limit", 100))
        except (TypeError, ValueError) as exc:
            raise WorkflowValidationError("节点 {} 的数据行数必须是整数".format(node_id)) from exc
        if limit < 1 or limit > 1000:
            raise WorkflowValidationError("节点 {} 的数据行数必须在 1 到 1000 之间".format(node_id))
    elif node_type == "http":
        method = str(config.get("method") or "GET").upper()
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise WorkflowValidationError("节点 {} 的 HTTP 方法无效".format(node_id))
        parsed = urlparse(str(config.get("url") or ""))
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(character.isspace() for character in str(config.get("url") or ""))
        ):
            raise WorkflowValidationError("节点 {} 仅允许有效的 HTTPS 地址".format(node_id))
        try:
            timeout = float(config.get("timeout_seconds", 30))
        except (TypeError, ValueError) as exc:
            raise WorkflowValidationError("节点 {} 的 HTTP 超时必须是数字".format(node_id)) from exc
        if timeout < 1 or timeout > 120:
            raise WorkflowValidationError("节点 {} 的 HTTP 超时必须在 1 到 120 秒之间".format(node_id))
    elif node_type == "for_each":
        items = config.get("items")
        dynamic_items = _dynamic_value(items)
        if not isinstance(items, list) and not dynamic_items:
            raise WorkflowValidationError("节点 {} 的迭代输入必须是数组".format(node_id))
        if isinstance(items, list) and len(items) > 100:
            raise WorkflowValidationError("节点 {} 最多处理 100 项".format(node_id))
    elif node_type in {"extract", "classifier", "human_input"}:
        key = "categories" if node_type == "classifier" else "fields"
        values = config.get(key)
        if not isinstance(values, list):
            raise WorkflowValidationError(
                "节点 {} 的{}必须是数组".format(
                    node_id, NODE_CATALOG[node_type]["config_fields"][-1]["label"]
                )
            )
        if node_type == "human_input":
            _fields(values, "节点 {} 输入".format(node_id))
            return
        identifiers = []
        for index, item in enumerate(values):
            if isinstance(item, Mapping):
                identifier = str(item.get("key") or item.get("id") or item.get("name") or "").strip()
                if not identifier:
                    raise WorkflowValidationError("节点 {} 的第 {} 项缺少编号".format(node_id, index + 1))
            elif node_type == "classifier" and isinstance(item, str):
                identifier = item.strip()
            else:
                raise WorkflowValidationError("节点 {} 的第 {} 项必须是对象".format(node_id, index + 1))
            identifiers.append(identifier)
        if len(identifiers) != len(set(identifiers)):
            raise WorkflowValidationError("节点 {} 的字段或分类编号不能重复".format(node_id))


def validate_definition(raw: Any, *, allow_incomplete: bool = False) -> Dict[str, Any]:
    """Return a normalized, JSON-safe WorkflowDefinition v1."""
    if not isinstance(raw, Mapping):
        raise WorkflowValidationError("工作流定义必须是 JSON 对象")
    value = json.loads(json.dumps(raw, ensure_ascii=False))
    if int(value.get("schema_version", 0)) != 1:
        raise WorkflowValidationError("仅支持 Workflow DSL schema_version=1")
    _reject_secrets(value)
    name = str(value.get("name") or "").strip()
    if not name or len(name) > 128:
        raise WorkflowValidationError("工作流名称不能为空且不能超过 128 字")
    inputs = _fields(value.get("inputs", []), "输入")
    outputs = _fields(value.get("outputs", []), "输出")

    triggers_raw = value.get("triggers", [])
    if not isinstance(triggers_raw, list) or not triggers_raw:
        raise WorkflowValidationError("工作流至少需要一个触发器")
    triggers, trigger_ids = [], set()
    for raw_trigger in triggers_raw:
        if not isinstance(raw_trigger, Mapping):
            raise WorkflowValidationError("触发器必须是对象")
        trigger = dict(raw_trigger)
        trigger_id = _id(trigger.get("id"), "触发器 ID")
        trigger_type = str(trigger.get("type") or "")
        if trigger_id in trigger_ids:
            raise WorkflowValidationError("触发器 ID 重复：{}".format(trigger_id))
        if trigger_type not in TRIGGER_TYPES:
            raise WorkflowValidationError("不支持的触发器类型：{}".format(trigger_type))
        config = trigger.get("config") or {}
        if not isinstance(config, Mapping):
            raise WorkflowValidationError("触发器配置必须是对象")
        if trigger_type == "schedule":
            cron = str(config.get("cron") or "").strip()
            try:
                CronTrigger.from_crontab(cron)
            except (TypeError, ValueError) as exc:
                raise WorkflowValidationError("定时触发器需要有效的五段 cron 表达式") from exc
        trigger_ids.add(trigger_id)
        triggers.append({"id": trigger_id, "type": trigger_type, "config": dict(config)})

    nodes_raw = value.get("nodes", [])
    if not isinstance(nodes_raw, list) or len(nodes_raw) < 2:
        raise WorkflowValidationError("工作流至少需要开始和结束节点")
    if len(nodes_raw) > 500:
        raise WorkflowValidationError("工作流节点不能超过 500 个")
    nodes: List[Dict[str, Any]] = []
    node_ids, node_types = set(), {}
    for raw_node in nodes_raw:
        if not isinstance(raw_node, Mapping):
            raise WorkflowValidationError("节点必须是对象")
        node = dict(raw_node)
        node_id = _id(node.get("id"), "节点 ID")
        node_type = str(node.get("type") or "")
        if node_id in node_ids:
            raise WorkflowValidationError("节点 ID 重复：{}".format(node_id))
        if node_type not in NODE_CATALOG:
            raise WorkflowValidationError("不支持的节点类型：{}".format(node_type))
        config = node.get("config") or {}
        position = node.get("position") or {}
        policy = node.get("error_policy") or {"mode": "stop"}
        if not isinstance(config, Mapping) or not isinstance(position, Mapping) or not isinstance(policy, Mapping):
            raise WorkflowValidationError("节点配置、位置和错误策略必须是对象")
        mode = str(policy.get("mode") or "stop")
        if mode not in {"stop", "retry", "continue", "error_branch"}:
            raise WorkflowValidationError("节点错误策略无效：{}".format(mode))
        try:
            retries = int(policy.get("max_retries", 0))
            x = float(position.get("x", 0))
            y = float(position.get("y", 0))
        except (TypeError, ValueError) as exc:
            raise WorkflowValidationError(
                "节点 {} 的位置或重试次数必须是数字".format(node_id)
            ) from exc
        if retries < 0 or retries > 3:
            raise WorkflowValidationError("节点 {} 的重试次数必须在 0 到 3 之间".format(node_id))
        if mode == "retry" and retries < 1:
            raise WorkflowValidationError("节点 {} 使用重试策略时至少重试 1 次".format(node_id))
        nodes.append({
            "id": node_id,
            "type": node_type,
            "name": str(node.get("name") or NODE_CATALOG[node_type]["name"])[:128],
            "position": {"x": x, "y": y},
            "config": dict(config),
            "error_policy": {"mode": mode, "max_retries": retries},
        })
        node_ids.add(node_id)
        node_types[node_id] = node_type
    starts = [node_id for node_id, node_type in node_types.items() if node_type == "start"]
    ends = [node_id for node_id, node_type in node_types.items() if node_type == "end"]
    if len(starts) != 1 or not ends:
        raise WorkflowValidationError("工作流必须且只能有一个开始节点，并至少有一个结束节点")

    edges_raw = value.get("edges", [])
    if not isinstance(edges_raw, list):
        raise WorkflowValidationError("连线必须是数组")
    edges, edge_ids = [], set()
    outgoing: Dict[str, List[Dict[str, Any]]] = {}
    for raw_edge in edges_raw:
        if not isinstance(raw_edge, Mapping):
            raise WorkflowValidationError("连线必须是对象")
        edge = dict(raw_edge)
        edge_id = _id(edge.get("id"), "连线 ID")
        source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
        if edge_id in edge_ids:
            raise WorkflowValidationError("连线 ID 重复：{}".format(edge_id))
        if source not in node_ids or target not in node_ids:
            raise WorkflowValidationError("连线 {} 引用了不存在的节点".format(edge_id))
        if node_types[source] == "end" or node_types[target] == "start":
            raise WorkflowValidationError("开始节点不能有入线，结束节点不能有出线")
        normalized_edge = {
            "id": edge_id,
            "source": source,
            "source_port": str(edge.get("source_port") or "default"),
            "target": target,
            "target_port": str(edge.get("target_port") or "default"),
        }
        edges.append(normalized_edge)
        edge_ids.add(edge_id)
        outgoing.setdefault(source, []).append(normalized_edge)
    ordered_nodes = _topological(list(node_ids), edges)
    order = {node_id: index for index, node_id in enumerate(ordered_nodes)}
    for node_id, node_type in node_types.items():
        node_edges = outgoing.get(node_id, [])
        normal_count = len([edge for edge in node_edges if edge["source_port"] != "error"])
        if node_type not in {"condition", "switch", "approval", "end"} and normal_count > 1:
            raise WorkflowValidationError("首期不支持并行分支，节点 {} 只能有一条出线".format(node_id))
        if not allow_incomplete and node_type != "end" and not node_edges:
            raise WorkflowValidationError("节点 {} 没有连接到后续节点".format(node_id))
        for edge in node_edges:
            source_port = edge["source_port"]
            allowed = {"default", "error"}
            if node_type == "condition":
                allowed = {"true", "false", "error"}
            elif node_type == "approval":
                allowed = {"approved", "rejected", "error"}
            elif node_type == "switch":
                cases = next(node["config"].get("cases", []) for node in nodes if node["id"] == node_id)
                case_ports = {
                    "case:{}".format(item.get("key", item.get("value")))
                    for item in cases
                    if isinstance(item, Mapping)
                }
                allowed = {"default", "error", *case_ports}
            if source_port not in allowed:
                raise WorkflowValidationError("节点 {} 使用了无效输出端口：{}".format(node_id, source_port))
            if edge["target_port"] != "default":
                raise WorkflowValidationError("首期节点仅支持 default 输入端口：{}".format(edge["target_port"]))

    reachable = {starts[0]}
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if edge["source"] in reachable and edge["target"] not in reachable:
                reachable.add(edge["target"])
                changed = True
    unreachable = sorted(node_ids - reachable)
    if not allow_incomplete and unreachable:
        raise WorkflowValidationError("存在无法从开始节点到达的节点：{}".format("、".join(unreachable)))

    if not allow_incomplete:
        for node in nodes:
            _require_config(node["id"], node["type"], node["config"])
        for node in nodes:
            node_id, node_type = node["id"], node["type"]
            connected_ports = {
                str(edge["source_port"])
                for edge in outgoing.get(node_id, [])
                if str(edge["source_port"]) != "error"
            }
            required_ports: set[str] = set()
            if node_type == "condition":
                required_ports = {"true", "false"}
            elif node_type == "approval":
                required_ports = {"approved", "rejected"}
            elif node_type == "switch":
                required_ports = {"default"} | {
                    "case:{}".format(item.get("key", item.get("value")))
                    for item in node["config"].get("cases", [])
                    if isinstance(item, Mapping)
                }
            missing_ports = sorted(required_ports - connected_ports)
            if missing_ports:
                raise WorkflowValidationError(
                    "节点 {} 缺少必需分支连线：{}".format(
                        node_id, "、".join(missing_ports)
                    )
                )

    input_keys = {field["key"] for field in inputs}
    for node in nodes:
        for expression in _variables(node["config"]):
            root = expression.strip().split(".")
            if root[0] == "input" and (len(root) < 2 or root[1] not in input_keys):
                raise WorkflowValidationError("节点 {} 引用了不存在的输入：{}".format(node["id"], expression))
            if root[0] == "nodes" and (len(root) < 2 or root[1] not in node_ids):
                raise WorkflowValidationError("节点 {} 引用了不存在的节点：{}".format(node["id"], expression))
            if (
                not allow_incomplete
                and root[0] == "nodes"
                and root[1] in node_ids
                and order[root[1]] >= order[node["id"]]
            ):
                raise WorkflowValidationError("节点 {} 只能引用已经执行的上游节点：{}".format(node["id"], expression))
            if root[0] not in {"input", "nodes", "item", "trigger"}:
                raise WorkflowValidationError("不支持的变量引用：{}".format(expression))

    settings = value.get("settings") or {}
    if not isinstance(settings, Mapping):
        raise WorkflowValidationError("工作流设置必须是对象")
    try:
        timeout = int(settings.get("timeout_seconds", 86400))
        max_steps = int(settings.get("max_steps", 500))
    except (TypeError, ValueError) as exc:
        raise WorkflowValidationError("工作流超时和最大步骤必须是整数") from exc
    if timeout < 1 or timeout > 30 * 86400:
        raise WorkflowValidationError("工作流超时必须在 1 到 2592000 秒之间")
    if max_steps < 1 or max_steps > 500:
        raise WorkflowValidationError("工作流最大步骤必须在 1 到 500 之间")
    return {
        "schema_version": 1,
        "name": name,
        "description": str(value.get("description") or "")[:1000],
        "inputs": inputs,
        "outputs": outputs,
        "triggers": triggers,
        "nodes": nodes,
        "edges": edges,
        "settings": {"timeout_seconds": timeout, "max_steps": max_steps},
    }


def resolve_path(context: Mapping[str, Any], expression: str) -> Any:
    current: Any = context
    for part in expression.strip().split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def render_value(value: Any, context: Mapping[str, Any]) -> Any:
    """Render safe references without evaluating arbitrary expressions."""
    if isinstance(value, str):
        matches = list(VARIABLE_PATTERN.finditer(value))
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            return copy.deepcopy(resolve_path(context, matches[0].group(1)))
        return VARIABLE_PATTERN.sub(
            lambda match: "" if (resolved := resolve_path(context, match.group(1))) is None else str(resolved),
            value,
        )
    if isinstance(value, list):
        return [render_value(item, context) for item in value]
    if isinstance(value, Mapping):
        return {str(key): render_value(item, context) for key, item in value.items()}
    return copy.deepcopy(value)


def validate_declared_output(fields: Sequence[Mapping[str, Any]], output: Any) -> Any:
    """Validate a final run output against the workflow's declared fields."""
    if not fields:
        return output
    try:
        return validate_field_values(fields, output, subject="输出")
    except WorkflowValidationError as exc:
        text = str(exc)
        if text == "输出必须是对象":
            raise WorkflowValidationError("工作流声明了输出字段，结束节点必须返回对象") from exc
        if text.startswith("输出缺少必填字段："):
            raise WorkflowValidationError(text.replace("输出缺少必填字段：", "缺少必填输出：", 1)) from exc
        if text.startswith("输出字段 "):
            raise WorkflowValidationError(text.replace("输出字段 ", "输出 ", 1)) from exc
        raise
