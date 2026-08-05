"""Throwaway smoke launcher for scrollbar position check."""
import os
import sys
from pathlib import Path

REPRO_DATA = Path(os.environ.get("TEMP", "/tmp")) / "bp_repro_data"
REPRO_DATA.mkdir(parents=True, exist_ok=True)
(REPRO_DATA / "system").mkdir(exist_ok=True)

import src.core.paths as paths

paths.DATA_DIR = REPRO_DATA
paths.SYSTEM_DATA_DIR = REPRO_DATA / "system"
paths.INSTANCE_LOCK_PATH = REPRO_DATA / "system" / "bot.lock"
paths.CREDENTIALS_PATH = REPRO_DATA / "system" / "credentials.json"

sys.argv = ["web.py", "--panel-only", "--host", "127.0.0.1", "--port", "8124"]

import web

raise SystemExit(web.main())
