"""Ollama protocol adapter and local service lifecycle."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

import httpx

from modeling.contracts import (
    CanonicalMessage,
    CanonicalToolCall,
    ModelCapabilities,
    ModelError,
    ModelIdentity,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)


class OllamaAdapter:
    """Translate provider-neutral requests to Ollama's ``/api/chat`` API."""

    def __init__(
        self,
        *,
        profile_id: str,
        provider: str,
        base_url: str,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
        capabilities: ModelCapabilities,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self._identity = ModelIdentity(profile_id, provider, model)
        self._capabilities = capabilities
        self.client = client or httpx.Client(trust_env=False)
        self._owns_client = client is None

    @property
    def identity(self) -> ModelIdentity:
        return self._identity

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _list_models(self, timeout: float = 3.0) -> List[str]:
        response = self.client.get("{}/api/tags".format(self.base_url), timeout=timeout)
        response.raise_for_status()
        data = response.json()
        names: List[str] = []
        for entry in data.get("models") or []:
            name = entry.get("name") or entry.get("model")
            if name:
                names.append(str(name))
        return names

    def ensure_ready(self) -> None:
        try:
            models = self._list_models()
        except (httpx.HTTPError, ValueError):
            raise ModelError(
                "无法连接模型档案 {} 的 Ollama 地址；请先启动 Ollama".format(
                    self.identity.profile_id
                ),
                provider=self.identity.provider,
                retryable=True,
            )

        if self.model not in models:
            raise ModelError(
                "本地未找到模型 {}，请先运行：ollama pull {}".format(
                    self.model, self.model
                ),
                provider=self.identity.provider,
            )

    @staticmethod
    def _tool_name_by_id(messages: List[CanonicalMessage]) -> Dict[str, str]:
        names: Dict[str, str] = {}
        for message in messages:
            for call in message.tool_calls:
                names[call.call_id] = call.name
        return names

    def _serialize_messages(self, request: ModelRequest) -> List[Dict[str, Any]]:
        names = self._tool_name_by_id(request.messages)
        serialized: List[Dict[str, Any]] = []
        for message in request.messages:
            item: Dict[str, Any] = {"role": message.role, "content": message.content}
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                    for call in message.tool_calls
                ]
            if message.role == "assistant" and "thinking" in message.extensions:
                item["thinking"] = message.extensions["thinking"]
            if message.role == "tool" and message.tool_call_id:
                tool_name = names.get(message.tool_call_id)
                if not tool_name:
                    raise ModelError(
                        "工具结果缺少对应的工具调用",
                        provider=self.identity.provider,
                    )
                item["tool_name"] = tool_name
            serialized.append(item)
        if request.image:
            if not self.capabilities.vision:
                raise ModelError(
                    "模型档案 {} 未启用图片能力".format(self.identity.profile_id),
                    provider=self.identity.provider,
                )
            for item in reversed(serialized):
                if item["role"] == "user":
                    item["images"] = [base64.b64encode(request.image).decode("ascii")]
                    break
            else:
                raise ModelError("图片请求缺少用户消息", provider=self.identity.provider)
        return serialized

    @staticmethod
    def _parse_arguments(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("工具参数不是有效 JSON") from exc
            if isinstance(parsed, dict):
                return parsed
        raise ValueError("工具参数必须是 JSON 对象")

    def complete(self, request: ModelRequest) -> ModelResponse:
        temperature = (
            self.temperature
            if request.generation.temperature is None
            else request.generation.temperature
        )
        max_tokens = (
            self.max_tokens
            if request.generation.max_tokens is None
            else request.generation.max_tokens
        )
        reasoning = (
            self.capabilities.reasoning
            if request.generation.reasoning is None
            else request.generation.reasoning
        )
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": self._serialize_messages(request),
            "stream": False,
            "think": reasoning,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if request.tools and self.capabilities.tools:
            payload["tools"] = request.tools
        try:
            response = self.client.post(
                "{}/api/chat".format(self.base_url),
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            raw_message = data.get("message") or {}
            if not isinstance(raw_message, dict):
                raise ValueError("message 不是对象")
            raw_calls = raw_message.get("tool_calls") or []
            if not isinstance(raw_calls, list):
                raise ValueError("tool_calls 不是数组")
            calls: List[CanonicalToolCall] = []
            for index, raw_call in enumerate(raw_calls):
                function = raw_call.get("function") if isinstance(raw_call, dict) else None
                if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                    raise ValueError("工具调用格式无效")
                call_id = raw_call.get("id") or "ollama-call-{}".format(index + 1)
                calls.append(
                    CanonicalToolCall(
                        str(call_id),
                        function["name"],
                        self._parse_arguments(function.get("arguments", {})),
                    )
                )
            content = str(raw_message.get("content") or "").strip()
            thinking = raw_message.get("thinking")
            if not content and not calls and not thinking:
                raise ValueError("模型没有返回文字或工具调用")
            extensions: Dict[str, Any] = {}
            if thinking is not None:
                extensions["thinking"] = str(thinking)
            usage = ModelUsage(
                input_tokens=_optional_int(data.get("prompt_eval_count")),
                output_tokens=_optional_int(data.get("eval_count")),
                total_tokens=_sum_optional(
                    data.get("prompt_eval_count"), data.get("eval_count")
                ),
            )
            return ModelResponse(
                message=CanonicalMessage(
                    role=str(raw_message.get("role") or "assistant"),
                    content=content,
                    tool_calls=calls,
                    extensions=extensions,
                ),
                actual_model=str(data.get("model") or self.model),
                usage=usage,
                request_id=_header_request_id(response),
                finish_reason=(
                    str(data["done_reason"]) if data.get("done_reason") else None
                ),
            )
        except httpx.TimeoutException as exc:
            raise ModelError(
                "模型档案 {} 调用超时".format(self.identity.profile_id),
                provider=self.identity.provider,
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise ModelError(
                "模型档案 {} 调用失败（HTTP {}）".format(
                    self.identity.profile_id, status
                ),
                provider=self.identity.provider,
                status_code=status,
                retryable=status == 429 or status >= 500,
            ) from exc
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ModelError(
                "模型档案 {} 返回无效响应或暂不可用".format(
                    self.identity.profile_id
                ),
                provider=self.identity.provider,
                retryable=True,
            ) from exc


def _optional_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _sum_optional(left: Any, right: Any) -> Optional[int]:
    left_int = _optional_int(left)
    right_int = _optional_int(right)
    if left_int is None or right_int is None:
        return None
    return left_int + right_int


def _header_request_id(response: httpx.Response) -> Optional[str]:
    return response.headers.get("x-request-id") or response.headers.get("request-id")
