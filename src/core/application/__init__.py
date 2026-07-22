"""Application entry points and message orchestration."""

from src.core.application.bot import (
    WeChatBot,
    delete_credentials,
    display_qr_code,
    load_credentials,
    print_login_status,
    save_credentials,
)
from src.core.application.bootstrap import run_bot
from src.core.application.cli import main, parse_args, run_notify_command

__all__ = [
    "WeChatBot",
    "delete_credentials",
    "display_qr_code",
    "load_credentials",
    "main",
    "parse_args",
    "print_login_status",
    "run_bot",
    "run_notify_command",
    "save_credentials",
]
