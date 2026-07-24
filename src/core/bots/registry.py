"""Bot adapter registry and factory."""

from __future__ import annotations

from typing import Any, Dict

from src.core.bots.adapters.ilink import ILinkAdapter
from src.core.bots.base import BotAdapter

BOT_ADAPTER_TYPES: Dict[str, type] = {
    "ilink": ILinkAdapter,
}


def create_bot_adapter(adapter_type: str, **kwargs: Any) -> BotAdapter:
    cls = BOT_ADAPTER_TYPES.get(adapter_type)
    if cls is None:
        raise ValueError("未知的机器人适配器类型：{}".format(adapter_type))
    return cls(**kwargs)
