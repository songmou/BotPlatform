"""Cross-platform, per-tenant integration credential storage.

Secrets are stored in a project-local, owner-only JSON file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from src.core.paths import DATA_DIR


class KeychainError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeychainReference:
    service: str
    account: str = "credential"


class KeychainService:
    def __init__(
        self,
        runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        storage_path: Optional[Path] = None,
    ) -> None:
        self.runner = runner
        self.storage_path = (
            storage_path or DATA_DIR / "system" / "integration_credentials.json"
        ).resolve()
        self._lock = threading.RLock()
        # A supplied runner is retained for compatibility with callers testing
        # the old security(1) integration. Normal operation always uses files.
        self._native = None
        if runner is None and self.storage_path.exists():
            # Fail during service startup rather than silently treating an
            # exposed or malformed credential store as "not configured".
            self._read_file()

    @staticmethod
    def _key(reference: KeychainReference) -> str:
        return "{}\n{}".format(reference.service, reference.account)

    def _read_file(self) -> dict[str, str]:
        if not self.storage_path.exists():
            return {}
        if os.name != "nt" and (self.storage_path.stat().st_mode & 0o077):
            raise KeychainError("凭证文件权限必须为 0600：{}".format(self.storage_path))
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise KeychainError("凭证文件无法读取或格式无效") from exc
        if not isinstance(data, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in data.items()
        ):
            raise KeychainError("凭证文件格式无效")
        return data

    @staticmethod
    def _icacls_grant_modify(path: Path, username: str, reset_inheritance: bool) -> int:
        """Grant the current user Modify on ``path`` via icacls.

        Modify (M) includes the delete right, which ``os.replace`` needs to
        overwrite an existing, locked-down credential file. Granting only
        (R,W) — as an earlier version did — removed the delete right and made
        the next atomic replace fail with WinError 5.
        """
        args = ["icacls", str(path)]
        if reset_inheritance:
            args.append("/inheritance:r")
        args += ["/grant:r", "{}:(M)".format(username)]
        completed = subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
        return completed.returncode

    def _write_file(self, values: dict[str, str]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        username = ""
        if os.name == "nt":
            username = os.environ.get("USERNAME", "").strip()
            if not username:
                raise KeychainError("无法确定当前 Windows 用户，不能保护凭证文件")
            # A previously locked-down file may lack the delete right, so make
            # it modifiable first; otherwise the os.replace below is denied.
            if self.storage_path.exists():
                self._icacls_grant_modify(self.storage_path, username, False)
        fd, raw_temp = tempfile.mkstemp(
            prefix=".integration_credentials.", dir=str(self.storage_path.parent)
        )
        temp = Path(raw_temp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(values, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.storage_path)
            os.chmod(self.storage_path, 0o600)
            if os.name == "nt":
                if self._icacls_grant_modify(self.storage_path, username, True) != 0:
                    raise KeychainError("无法设置 Windows 凭证文件访问权限")
        except Exception:
            try:
                temp.unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def reference(tenant_id: str, integration_id: str) -> KeychainReference:
        safe = integration_id.replace("_", "-")
        if not safe or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in safe):
            raise KeychainError("集成编号格式无效")
        return KeychainReference("com.qiao.ilinkbot.{}.{}".format(tenant_id, safe))

    def set_secret(self, reference: KeychainReference, secret: str) -> None:
        if not isinstance(secret, str) or not secret:
            raise KeychainError("凭据不能为空")
        if self.runner is None:
            with self._lock:
                values = self._read_file()
                values[self._key(reference)] = secret
                try:
                    self._write_file(values)
                except OSError as exc:
                    raise KeychainError("无法写入凭证文件") from exc
            return
        try:
            assert self.runner is not None
            completed = self.runner(
                [
                    "/usr/bin/security",
                    "add-generic-password",
                    "-U",
                    "-a",
                    reference.account,
                    "-s",
                    reference.service,
                    "-w",
                ],
                input=secret + "\n",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise KeychainError("无法写入系统钥匙串") from exc
        if completed.returncode != 0:
            raise KeychainError("无法写入系统钥匙串")

    def get_secret(self, reference: KeychainReference) -> str:
        if self.runner is None:
            with self._lock:
                value = self._read_file().get(self._key(reference))
                if value:
                    return value
            raise KeychainError("尚未配置该集成凭据")
        try:
            assert self.runner is not None
            completed = self.runner(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-a",
                    reference.account,
                    "-s",
                    reference.service,
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise KeychainError("无法读取系统钥匙串") from exc
        secret = completed.stdout.rstrip("\r\n")
        if completed.returncode != 0 or not secret:
            raise KeychainError("尚未配置该集成凭据")
        return secret

    def delete_secret(self, reference: KeychainReference) -> None:
        if self.runner is None:
            with self._lock:
                values = self._read_file()
                if values.pop(self._key(reference), None) is not None:
                    try:
                        self._write_file(values)
                    except OSError as exc:
                        raise KeychainError("无法删除凭证文件中的凭据") from exc
            return
        try:
            assert self.runner is not None
            completed = self.runner(
                [
                    "/usr/bin/security",
                    "delete-generic-password",
                    "-a",
                    reference.account,
                    "-s",
                    reference.service,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise KeychainError("无法删除系统钥匙串凭据") from exc
        if completed.returncode not in (0, 44):
            raise KeychainError("无法删除系统钥匙串凭据")

    def exists(self, reference: KeychainReference) -> bool:
        try:
            self.get_secret(reference)
            return True
        except KeychainError:
            return False

    def list_keys(self) -> list[str]:
        """Return all stored reference keys (``service\\naccount`` strings).

        Only supported for the file-backed store; the native keychain path
        has no enumeration API and returns an empty list.
        """
        if self.runner is not None:
            return []
        with self._lock:
            return list(self._read_file().keys())


