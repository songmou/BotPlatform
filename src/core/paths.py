"""Canonical filesystem locations used by the application."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
SYSTEM_DATA_DIR = DATA_DIR / "system"
CREDENTIALS_PATH = SYSTEM_DATA_DIR / "credentials.json"
INSTANCE_LOCK_PATH = SYSTEM_DATA_DIR / "bot.lock"


def channel_credentials_path(channel_id: str) -> Path:
    """Keep the default iLink credential location backward compatible."""
    if channel_id == "wechat-main":
        return CREDENTIALS_PATH
    return SYSTEM_DATA_DIR / "channels" / "{}.json".format(channel_id)
