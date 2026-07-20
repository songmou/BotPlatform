"""Cross-platform process lock for the main bot instance."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Optional


@dataclass(frozen=True)
class InstanceInfo:
    pid: Optional[int] = None
    started_at: Optional[str] = None


class AlreadyRunning(RuntimeError):
    """Raised when another process owns the bot instance lock."""

    def __init__(self, info: InstanceInfo) -> None:
        self.info = info
        details = []
        if info.pid is not None:
            details.append("PID={}".format(info.pid))
        if info.started_at:
            details.append("启动时间={}".format(info.started_at))
        message = "机器人已启动，请勿重复运行。"
        if details:
            message = "{} 当前实例：{}。".format(message, "，".join(details))
        super().__init__(message)


class SingleInstanceLock:
    """Hold a non-blocking OS file lock for the lifetime of the process."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: Optional[IO[str]] = None

    @staticmethod
    def _lock(handle: IO[str]) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\n")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: IO[str]) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_info(handle: IO[str]) -> InstanceInfo:
        try:
            handle.seek(0)
            payload = json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError):
            return InstanceInfo()
        if not isinstance(payload, dict):
            return InstanceInfo()

        pid = payload.get("pid")
        started_at = payload.get("started_at")
        return InstanceInfo(
            pid=pid if isinstance(pid, int) and pid > 0 else None,
            started_at=started_at if isinstance(started_at, str) else None,
        )

    def acquire(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except OSError:
            handle.close()
            raise
        try:
            self._lock(handle)
        except OSError:
            info = self._read_info(handle)
            handle.close()
            raise AlreadyRunning(info) from None

        try:
            info = {
                "pid": os.getpid(),
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            handle.seek(0)
            handle.truncate()
            json.dump(info, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            self._unlock(handle)
            handle.close()
            raise

        self._handle = handle
        return self

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            self._unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> "SingleInstanceLock":
        return self.acquire()

    def __exit__(self, *_args: object) -> None:
        self.release()
