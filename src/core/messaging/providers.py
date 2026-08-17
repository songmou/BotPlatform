"""Registry of built-in message-channel providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from src.core.config.loader import ChannelConfig
from src.core.integrations.ilink import Credentials, ILinkClient

from .adapters import FeishuAdapter, WeChatILinkAdapter, WeComAIBotAdapter
from .contracts import MessagingAdapter
from .credentials import validate_channel_credentials


AdapterBuilder = Callable[
    [ChannelConfig, Mapping[str, str], Optional[Any]],
    MessagingAdapter,
]


@dataclass(frozen=True)
class ChannelProvider:
    type: str
    name: str
    credential_fields: Tuple[str, ...]
    secret_fields: Tuple[str, ...]
    builder: AdapterBuilder

    def validate_credentials(self, value: Mapping[str, Any]) -> Dict[str, str]:
        return validate_channel_credentials(self.type, value)

    def build(
        self,
        config: ChannelConfig,
        credentials: Mapping[str, str],
        token_resolver: Optional[Any] = None,
    ) -> MessagingAdapter:
        return self.builder(config, credentials, token_resolver)


def _build_ilink(
    config: ChannelConfig,
    credentials: Mapping[str, str],
    token_resolver: Optional[Any] = None,
) -> MessagingAdapter:
    parsed = Credentials.from_dict(dict(credentials))
    return WeChatILinkAdapter(
        ILinkClient(credentials=parsed),
        channel_id=config.id,
        token_resolver=token_resolver,
    )


def _build_wecom(
    config: ChannelConfig,
    credentials: Mapping[str, str],
    token_resolver: Optional[Any] = None,
) -> MessagingAdapter:
    return WeComAIBotAdapter(
        credentials["bot_id"],
        credentials["secret"],
        channel_id=config.id,
    )


def _build_feishu(
    config: ChannelConfig,
    credentials: Mapping[str, str],
    token_resolver: Optional[Any] = None,
) -> MessagingAdapter:
    return FeishuAdapter(
        credentials["app_id"],
        credentials["app_secret"],
        channel_id=config.id,
    )


_PROVIDERS = {
    "wechat_ilink": ChannelProvider(
        type="wechat_ilink",
        name="微信 iLink",
        credential_fields=("token", "base_url", "bot_id", "user_id"),
        secret_fields=("token",),
        builder=_build_ilink,
    ),
    "wecom_aibot": ChannelProvider(
        type="wecom_aibot",
        name="企业微信",
        credential_fields=("bot_id", "secret"),
        secret_fields=("secret",),
        builder=_build_wecom,
    ),
    "feishu": ChannelProvider(
        type="feishu",
        name="飞书",
        credential_fields=("app_id", "app_secret"),
        secret_fields=("app_secret",),
        builder=_build_feishu,
    ),
}


def channel_provider(channel_type: str) -> ChannelProvider:
    try:
        return _PROVIDERS[channel_type]
    except KeyError as exc:
        raise ValueError("未知消息渠道类型：{}".format(channel_type)) from exc


def list_channel_providers() -> Tuple[ChannelProvider, ...]:
    return tuple(_PROVIDERS[key] for key in sorted(_PROVIDERS))


def build_channel_adapter(
    config: ChannelConfig,
    credentials: Mapping[str, str],
    token_resolver: Optional[Any] = None,
) -> MessagingAdapter:
    provider = channel_provider(config.type)
    normalized = provider.validate_credentials(credentials)
    return provider.build(config, normalized, token_resolver)
