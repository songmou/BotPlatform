#!/usr/bin/env python3
"""CI guard for repository red lines declared in AGENTS.md.

Checks (stdlib only):
1. No credential-looking literals in config/*.json.
2. No non-loopback http:// URLs in config/*.json.
3. No imports from stale top-level src.* packages (only src.core.* / src.api.* allowed).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

CREDENTIAL_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9_\-.=]+"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}"),
    re.compile(r'"(?:api[_-]?key|token|secret|password)"\s*:\s*"[^"]{8,}"', re.IGNORECASE),
]

HTTP_URL_PATTERN = re.compile(r"http://([^/\s\"']+)")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1", "0.0.0.0")

STALE_IMPORT_PATTERN = re.compile(r"^\s*(?:from|import)\s+src\.(\w+)", re.MULTILINE)
ALLOWED_SRC_PACKAGES = {"core", "api"}


def check_config_credentials() -> list:
    errors = []
    for path in sorted(CONFIG_DIR.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        for pattern in CREDENTIAL_PATTERNS:
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                errors.append(
                    "{}:{} 疑似凭证明文（{}...）——密钥不得写入 config/*.json，请改用 data/system/ 下的环境文件".format(
                        path.relative_to(PROJECT_ROOT), line, match.group(0)[:24]
                    )
                )
    return errors


def check_config_http_urls() -> list:
    errors = []
    for path in sorted(CONFIG_DIR.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        for match in HTTP_URL_PATTERN.finditer(text):
            host = match.group(1).split(":")[0]
            if host not in LOOPBACK_HOSTS:
                line = text[: match.start()].count("\n") + 1
                errors.append(
                    "{}:{} 远程地址使用了 HTTP 明文（http://{}）——远程 URL 必须为 HTTPS，仅本机回环可用 HTTP".format(
                        path.relative_to(PROJECT_ROOT), line, match.group(1)
                    )
                )
    return errors


def check_stale_imports() -> list:
    errors = []
    targets = [PROJECT_ROOT / "main.py", PROJECT_ROOT / "web.py"]
    targets.extend(sorted((PROJECT_ROOT / "src").rglob("*.py")))
    targets.extend(sorted((PROJECT_ROOT / "tests").rglob("*.py")))
    for path in targets:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for match in STALE_IMPORT_PATTERN.finditer(text):
            package = match.group(1)
            if package not in ALLOWED_SRC_PACKAGES:
                line = text[: match.start()].count("\n") + 1
                errors.append(
                    "{}:{} 从陈旧路径 src.{} 导入——只允许从 src.core.* 或 src.api.* 导入".format(
                        path.relative_to(PROJECT_ROOT), line, package
                    )
                )
    return errors


def main() -> int:
    errors = check_config_credentials() + check_config_http_urls() + check_stale_imports()
    if errors:
        print("红线检查失败（{} 处）：".format(len(errors)), file=sys.stderr)
        for err in errors:
            print("  - {}".format(err), file=sys.stderr)
        return 1
    print("红线检查通过：config 无凭证明文、无远程 HTTP、无陈旧 src.* 导入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
