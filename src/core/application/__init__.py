"""Application entry points and message orchestration."""

from src.core.application.bot import (
    MessageBot,
    WeChatBot,
    delete_credentials,
    display_qr_code,
    load_credentials,
    print_login_status,
    save_credentials,
)
from src.core.application.bootstrap import run_bot
from src.core.application.cli import (
    main,
    parse_args,
    run_channel_command,
    run_codex_hook_command,
    run_notify_command,
)

__all__ = [
    "WeChatBot",
    "MessageBot",
    "delete_credentials",
    "display_qr_code",
    "load_credentials",
    "main",
    "parse_args",
    "print_login_status",
    "run_bot",
    "run_channel_command",
    "run_codex_hook_command",
    "run_notify_command",
    "save_credentials",
]
