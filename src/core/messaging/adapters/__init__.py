"""Built-in messaging adapters."""

from .feishu import FeishuAdapter
from .wecom_aibot import WeComAIBotAdapter
from .wechat_ilink import WeChatILinkAdapter

__all__ = ["FeishuAdapter", "WeChatILinkAdapter", "WeComAIBotAdapter"]
