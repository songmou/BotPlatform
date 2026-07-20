"""Generic OpenAI Chat Completions compatible protocol adapter."""

from __future__ import annotations

import base64
import io
import json
from typing import Any, Dict, List, Optional

import httpx
from PIL import Image, UnidentifiedImageError

from src.modeling.contracts import (
    CanonicalMessage,
    CanonicalToolCall,
    ModelCapabilities,
    ModelError,
    ModelIdentity,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)


class OpenAICompatibleAdapter:
    """Use the portable subset of OpenAI's Chat Completions protocol."""

    def __init__(
        self,
        *,
        profile_id: str,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
        capabilities: ModelCapabilities,
        request_extra: Optional[Dict[str, Any]] = None,
        assistant_passthrough_fields: Optional[List[str]] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.request_extra = dict(request_extra or {})
        self.assistant_passthrough_fields = list(assistant_passthrough_fields or [])
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

    def ensure_ready(self) -> None:
        if not self.api_key:
            raise ModelError(
                "模型档案 {} 缺少 API Key 环境变量".format(
                    self.identity.profile_id
                ),
                provider=self.identity.provider,
            )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _serialize_message(self, message: CanonicalMessage) -> Dict[str, Any]:
        item: Dict[str, Any] = {"role": message.role, "content": message.content}
        if message.role == "assistant":
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments, ensure_ascii=False, separators=(",", ":")
                            ),
                        },
                    }
                    for call in message.tool_calls
                ]
            for field in self.assistant_passthrough_fields:
                if field in message.extensions:
                    item[field] = message.extensions[field]
        elif message.role == "tool":
            if not message.tool_call_id:
                raise ModelError(
                    "工具结果缺少 tool_call_id",
                    provider=self.identity.provider,
                )
            item["tool_call_id"] = message.tool_call_id
        return item

    def _serialize_messages(self, request: ModelRequest) -> List[Dict[str, Any]]:
        messages = [self._serialize_message(message) for message in request.messages]
        if not request.image:
            return messages
        if not self.capabilities.vision:
            raise ModelError(
                "模型档案 {} 未启用图片能力".format(self.identity.profile_id),
                provider=self.identity.provider,
            )
        data_url = _image_data_url(request.image, self.identity.provider)
        for message in reversed(messages):
            if message["role"] == "user":
                text = str(message.get("content") or "")
                message["content"] = [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]
                return messages
        raise ModelError("图片请求缺少用户消息", provider=self.identity.provider)

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
        payload: Dict[str, Any] = dict(self.request_extra)
        if (
            request.generation.reasoning is not None
            and isinstance(payload.get("thinking"), dict)
        ):
            payload["thinking"] = {
                "type": (
                    "enabled" if request.generation.reasoning else "disabled"
                )
            }
            if not request.generation.reasoning:
                payload.pop("reasoning_effort", None)
        payload.update(
            {
                "model": self.model,
                "messages": self._serialize_messages(request),
                "stream": False,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if request.tools and self.capabilities.tools:
            payload["tools"] = request.tools
        headers = {
            "Authorization": "Bearer {}".format(self.api_key),
            "Content-Type": "application/json",
        }
        try:
            response = self.client.post(
                "{}/chat/completions".format(self.base_url),
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            return self._parse_response(data, response)
        except httpx.TimeoutException as exc:
            raise ModelError(
                "模型档案 {} 调用超时".format(self.identity.profile_id),
                provider=self.identity.provider,
                retryable=True,
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise self._status_error(exc.response.status_code) from exc
        except httpx.HTTPError as exc:
            raise ModelError(
                "模型档案 {} 网络连接失败".format(self.identity.profile_id),
                provider=self.identity.provider,
                retryable=True,
            ) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise ModelError(
                "模型档案 {} 返回了无效响应".format(self.identity.profile_id),
                provider=self.identity.provider,
            ) from exc

    def _status_error(self, status: int) -> ModelError:
        labels = {
            401: "认证失败",
            402: "账户余额或配额不足",
            429: "请求过于频繁",
        }
        detail = labels.get(status)
        if detail is None:
            detail = "服务暂不可用" if status >= 500 else "请求失败"
        return ModelError(
            "模型档案 {} {}（HTTP {}）".format(
                self.identity.profile_id, detail, status
            ),
            provider=self.identity.provider,
            status_code=status,
            retryable=status == 429 or status >= 500,
        )

    def _parse_response(
        self, data: Dict[str, Any], response: httpx.Response
    ) -> ModelResponse:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("choices 为空")
        raw_message = choices[0].get("message")
        if not isinstance(raw_message, dict):
            raise ValueError("message 不是对象")
        raw_calls = raw_message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ValueError("tool_calls 不是数组")
        calls: List[CanonicalToolCall] = []
        seen_call_ids = set()
        for raw_call in raw_calls:
            function = raw_call.get("function") if isinstance(raw_call, dict) else None
            call_id = raw_call.get("id") if isinstance(raw_call, dict) else None
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
            ):
                raise ValueError("工具调用格式无效")
            if call_id in seen_call_ids:
                raise ValueError("工具调用 ID 重复")
            seen_call_ids.add(call_id)
            arguments = function.get("arguments", "{}")
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象")
            calls.append(CanonicalToolCall(call_id, function["name"], arguments))
        content = str(raw_message.get("content") or "").strip()
        extensions = {
            field: raw_message[field]
            for field in self.assistant_passthrough_fields
            if field in raw_message
        }
        if not content and not calls and not any(
            str(value).strip() for value in extensions.values()
        ):
            raise ValueError("模型没有返回文字、思考或工具调用")
        raw_usage = data.get("usage")
        usage: Optional[ModelUsage] = None
        if isinstance(raw_usage, dict):
            usage = ModelUsage(
                input_tokens=_optional_int(raw_usage.get("prompt_tokens")),
                output_tokens=_optional_int(raw_usage.get("completion_tokens")),
                total_tokens=_optional_int(raw_usage.get("total_tokens")),
            )
        request_id = (
            response.headers.get("x-request-id")
            or response.headers.get("request-id")
            or (str(data["id"]) if data.get("id") else None)
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
            request_id=request_id,
            finish_reason=(
                str(choices[0]["finish_reason"])
                if choices[0].get("finish_reason")
                else None
            ),
        )


def _optional_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _image_data_url(image_bytes: bytes, provider: str) -> str:
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image_format = (image.format or "").upper()
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ModelError("图片格式无效或不受支持", provider=provider) from exc
    mime_by_format = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "GIF": "image/gif",
        "WEBP": "image/webp",
    }
    mime = mime_by_format.get(image_format)
    if not mime:
        raise ModelError("图片格式 {} 暂不支持".format(image_format), provider=provider)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return "data:{};base64,{}".format(mime, encoded)
