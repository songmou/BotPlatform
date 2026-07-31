"""Secure persistence for per-channel credentials."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from src.core.paths import channel_credentials_path


class ChannelCredentialError(ValueError):
    """Raised when a channel credential file is missing or malformed."""


_FIELDS = {
    "wechat_ilink": {"token", "base_url", "bot_id", "user_id"},
    "wecom_aibot": {"bot_id", "secret"},
    "feishu": {"app_id", "app_secret"},
}

_REQUIRED = {
    "wechat_ilink": {"token", "base_url", "bot_id"},
    "wecom_aibot": {"bot_id", "secret"},
    "feishu": {"app_id", "app_secret"},
}


def validate_channel_credentials(
    channel_type: str,
    value: Mapping[str, Any],
) -> Dict[str, str]:
    """Validate and normalize a provider credential payload."""
    allowed = _FIELDS.get(channel_type)
    if allowed is None:
        raise ChannelCredentialError("未知消息渠道类型：{}".format(channel_type))
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ChannelCredentialError(
            "凭据包含未知字段：{}".format("、".join(unknown))
        )
    normalized = {
        key: str(item).strip()
        for key, item in value.items()
        if isinstance(item, (str, int)) and str(item).strip()
    }
    missing = sorted(_REQUIRED[channel_type] - set(normalized))
    if missing:
        raise ChannelCredentialError(
            "凭据缺少字段：{}".format("、".join(missing))
        )
    if channel_type == "wechat_ilink":
        base_url = normalized["base_url"]
        if not (
            base_url.startswith("https://")
            or base_url.startswith("http://127.0.0.1")
            or base_url.startswith("http://localhost")
        ):
            raise ChannelCredentialError("微信 iLink 服务地址必须使用 HTTPS")
    return normalized


class ChannelCredentialStore:
    """Read and atomically write channel credentials with mode ``0600``."""

    def path(self, channel_id: str) -> Path:
        return channel_credentials_path(channel_id)

    def configured(self, channel_id: str) -> bool:
        return self.path(channel_id).is_file()

    def load(
        self,
        channel_id: str,
        channel_type: str,
        *,
        required: bool = False,
    ) -> Optional[Dict[str, str]]:
        path = self.path(channel_id)
        if not path.exists():
            if required:
                raise ChannelCredentialError(
                    "渠道 {} 尚未配置凭据".format(channel_id)
                )
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ChannelCredentialError(
                "渠道 {} 的凭据文件无效".format(channel_id)
            ) from exc
        if not isinstance(payload, dict):
            raise ChannelCredentialError(
                "渠道 {} 的凭据必须是 JSON 对象".format(channel_id)
            )
        return validate_channel_credentials(channel_type, payload)

    def save(
        self,
        channel_id: str,
        channel_type: str,
        credentials: Mapping[str, Any],
    ) -> None:
        normalized = validate_channel_credentials(channel_type, credentials)
        path = self.path(channel_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(str(path.parent), 0o700)
        temporary = path.with_suffix(path.suffix + ".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        descriptor = os.open(str(temporary), flags, 0o600)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(normalized, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(str(temporary), str(path))
            if os.name != "nt":
                os.chmod(str(path), 0o600)
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    def delete(self, channel_id: str) -> None:
        try:
            self.path(channel_id).unlink()
        except FileNotFoundError:
            return
