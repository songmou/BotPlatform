"""Network drive service: public and tenant-scoped file management.

The drive exposes two areas: a global "public" tree rooted at
``data/public`` and a per-tenant tree rooted at the tenant directory
(``data/users/{tenant_id}``), which maps existing subdirectories such as
``workspace`` and ``scripts`` so personal knowledge uploads and scripts
stay visible. All paths are validated against traversal and symlinks;
hidden entries (leading dot, e.g. ``.trash``) are never listed or served.
"""

from __future__ import annotations

import os
import re
import shutil
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.storage.tenants import TenantRegistry, TenantStoreError

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_PREVIEW_BYTES = 256 * 1024

_UNSAFE_SEGMENT = re.compile(r"[\\\x00-\x1f]")

SCOPE_PUBLIC = "public"
SCOPE_TENANT = "tenant"


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(str(path), 0o700)


@dataclass(frozen=True)
class DriveEntry:
    name: str
    path: str
    type: str  # "file" | "folder"
    size: int
    modified_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "type": self.type,
            "size": self.size,
            "modified_at": self.modified_at,
        }


class DriveService:
    """Validated filesystem operations for the drive module."""

    def __init__(
        self,
        tenant_registry: TenantRegistry,
        public_root: Path,
        knowledge_service: Optional[Any] = None,
    ) -> None:
        self.registry = tenant_registry
        self.public_root = public_root.resolve()
        self.knowledge_service = knowledge_service
        _secure_directory(self.public_root)

    def attach_knowledge_service(self, service: Any) -> None:
        self.knowledge_service = service

    def _notify_knowledge(self, method: str, *args: Any) -> None:
        service = self.knowledge_service
        callback = getattr(service, method, None) if service is not None else None
        if not callable(callback):
            return
        try:
            callback(*args)
        except Exception:
            logger.warning("同步网盘知识关联失败：%s", method, exc_info=True)

    # ---- path resolution & safety ----

    def _root(self, scope: str, tenant_id: Optional[str]) -> Path:
        if scope == SCOPE_PUBLIC:
            _secure_directory(self.public_root)
            return self.public_root
        if scope == SCOPE_TENANT:
            if not tenant_id:
                raise ValueError("租户网盘操作必须提供租户编号")
            try:
                self.registry.get(tenant_id)
            except TenantStoreError as exc:
                raise ValueError(str(exc)) from exc
            root = self.registry.tenant_root(tenant_id)
            _secure_directory(root)
            return root.resolve()
        raise ValueError("scope 仅支持 public 或 tenant")

    @staticmethod
    def _split_relative(relative_path: str) -> List[str]:
        """Validate and split a user-supplied relative path into segments."""
        raw = (relative_path or "").strip().strip("/")
        if not raw:
            return []
        if "\x00" in raw:
            raise ValueError("路径包含非法字符")
        segments = []
        for segment in raw.split("/"):
            segment = segment.strip()
            if not segment:
                continue
            if segment in (".", ".."):
                raise ValueError("路径不允许包含 . 或 .. 片段")
            if segment.startswith("."):
                raise ValueError("不允许访问隐藏文件或目录")
            if _UNSAFE_SEGMENT.search(segment):
                raise ValueError("路径包含非法字符")
            segments.append(segment)
        return segments

    def _resolve(
        self, scope: str, tenant_id: Optional[str], relative_path: str
    ) -> Path:
        """Resolve a relative drive path to a real path inside the root."""
        if (relative_path or "").startswith(("/", "\\")) or (
            len(relative_path or "") > 1 and relative_path[1] == ":"
        ):
            raise ValueError("不允许使用绝对路径")
        root = self._root(scope, tenant_id)
        segments = self._split_relative(relative_path)
        target = root
        for segment in segments:
            target = target / segment
            # Reject symlinks on every intermediate component.
            if target.is_symlink():
                raise ValueError("不允许访问符号链接")
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("路径越界，禁止访问")
        return resolved

    @staticmethod
    def _relative(root: Path, target: Path) -> str:
        return target.relative_to(root).as_posix() if target != root else ""

    @staticmethod
    def _validate_name(name: str) -> str:
        cleaned = (name or "").strip()
        if not cleaned or cleaned in (".", ".."):
            raise ValueError("名称无效")
        if cleaned.startswith("."):
            raise ValueError("名称不允许以 . 开头")
        if "/" in cleaned or _UNSAFE_SEGMENT.search(cleaned):
            raise ValueError("名称包含非法字符")
        return cleaned

    # ---- read operations ----

    def list_entries(
        self, scope: str, tenant_id: Optional[str], path: str = ""
    ) -> Dict[str, Any]:
        root = self._root(scope, tenant_id)
        directory = self._resolve(scope, tenant_id, path)
        if not directory.exists():
            raise ValueError("目录不存在")
        if not directory.is_dir():
            raise ValueError("目标不是目录")
        entries: List[DriveEntry] = []
        for child in directory.iterdir():
            if child.name.startswith(".") or child.is_symlink():
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append(
                DriveEntry(
                    name=child.name,
                    path=self._relative(root, child),
                    type="folder" if child.is_dir() else "file",
                    size=0 if child.is_dir() else int(stat.st_size),
                    modified_at=stat.st_mtime,
                )
            )
        entries.sort(key=lambda e: (e.type != "folder", e.name.lower()))
        relative = self._relative(root, directory)
        breadcrumbs = []
        accumulated = []
        for part in relative.split("/") if relative else []:
            accumulated.append(part)
            breadcrumbs.append({"name": part, "path": "/".join(accumulated)})
        return {
            "path": relative,
            "breadcrumbs": breadcrumbs,
            "entries": [entry.to_dict() for entry in entries],
        }

    def read_file(self, scope: str, tenant_id: Optional[str], path: str) -> Path:
        """Validate and return the real path of a file for download."""
        target = self._resolve(scope, tenant_id, path)
        if not target.exists() or not target.is_file():
            raise ValueError("文件不存在")
        return target

    def read_text(
        self,
        scope: str,
        tenant_id: Optional[str],
        path: str,
        max_bytes: int = MAX_PREVIEW_BYTES,
    ) -> Dict[str, Any]:
        target = self.read_file(scope, tenant_id, path)
        size = target.stat().st_size
        limit = max(1, min(int(max_bytes), MAX_PREVIEW_BYTES))
        with open(target, "rb") as handle:
            payload = handle.read(limit)
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("该文件不是 UTF-8 文本，无法预览") from exc
        return {
            "path": path,
            "size": int(size),
            "truncated": size > limit,
            "content": content,
        }

    def stat(self, scope: str, tenant_id: Optional[str], path: str) -> Dict[str, Any]:
        root = self._root(scope, tenant_id)
        target = self._resolve(scope, tenant_id, path)
        if not target.exists():
            raise ValueError("文件或目录不存在")
        info = target.stat()
        return {
            "name": target.name,
            "path": self._relative(root, target),
            "type": "folder" if target.is_dir() else "file",
            "size": 0 if target.is_dir() else int(info.st_size),
            "modified_at": info.st_mtime,
        }

    def usage(self, scope: str, tenant_id: Optional[str]) -> Dict[str, Any]:
        root = self._root(scope, tenant_id)
        total_bytes = 0
        file_count = 0
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if name.startswith("."):
                    continue
                candidate = Path(base) / name
                if candidate.is_symlink():
                    continue
                try:
                    total_bytes += candidate.stat().st_size
                    file_count += 1
                except OSError:
                    continue
        return {"total_bytes": total_bytes, "file_count": file_count}

    # ---- write operations ----

    def create_folder(
        self, scope: str, tenant_id: Optional[str], path: str, name: str
    ) -> Dict[str, Any]:
        parent = self._resolve(scope, tenant_id, path)
        if not parent.exists() or not parent.is_dir():
            raise ValueError("目录不存在")
        folder = parent / self._validate_name(name)
        if folder.exists():
            raise ValueError("同名文件或目录已存在")
        folder.mkdir(mode=0o700)
        root = self._root(scope, tenant_id)
        return {"path": self._relative(root, folder)}

    def save_file(
        self,
        scope: str,
        tenant_id: Optional[str],
        path: str,
        filename: str,
        payload: bytes,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        if len(payload) > MAX_UPLOAD_BYTES:
            raise ValueError("上传的文件不能超过 100 MiB")
        parent = self._resolve(scope, tenant_id, path)
        if not parent.exists() or not parent.is_dir():
            raise ValueError("目录不存在")
        target = parent / self._validate_name(filename)
        existed = target.exists()
        if target.is_dir():
            raise ValueError("目标是一个目录，无法覆盖")
        if target.exists() and not overwrite:
            raise ValueError("同名文件已存在，如需覆盖请显式确认")
        target.write_bytes(payload)
        root = self._root(scope, tenant_id)
        relative = self._relative(root, target)
        if existed:
            self._notify_knowledge("mark_drive_changed", scope, tenant_id, relative)
        return {"path": relative, "size": len(payload)}

    def rename(
        self, scope: str, tenant_id: Optional[str], path: str, new_name: str
    ) -> Dict[str, Any]:
        root = self._root(scope, tenant_id)
        source = self._resolve(scope, tenant_id, path)
        if source == root:
            raise ValueError("不能重命名根目录")
        if not source.exists():
            raise ValueError("文件或目录不存在")
        target = source.parent / self._validate_name(new_name)
        if target.exists():
            raise ValueError("同名文件或目录已存在")
        old_path = self._relative(root, source)
        source.rename(target)
        new_path = self._relative(root, target)
        self._notify_knowledge(
            "move_drive_path", scope, tenant_id, old_path, new_path
        )
        return {"path": new_path}

    def move(
        self, scope: str, tenant_id: Optional[str], path: str, target_dir: str
    ) -> Dict[str, Any]:
        root = self._root(scope, tenant_id)
        source = self._resolve(scope, tenant_id, path)
        if source == root:
            raise ValueError("不能移动根目录")
        if not source.exists():
            raise ValueError("文件或目录不存在")
        destination_dir = self._resolve(scope, tenant_id, target_dir)
        if not destination_dir.exists() or not destination_dir.is_dir():
            raise ValueError("目标目录不存在")
        if source.is_dir() and (
            destination_dir == source or source in destination_dir.parents
        ):
            raise ValueError("不能把目录移动到它自身内部")
        destination = destination_dir / source.name
        if destination.exists():
            raise ValueError("目标位置已存在同名文件或目录")
        old_path = self._relative(root, source)
        shutil.move(str(source), str(destination))
        new_path = self._relative(root, destination)
        self._notify_knowledge(
            "move_drive_path", scope, tenant_id, old_path, new_path
        )
        return {"path": new_path}

    def delete(
        self,
        scope: str,
        tenant_id: Optional[str],
        path: str,
        recursive: bool = False,
    ) -> Dict[str, Any]:
        root = self._root(scope, tenant_id)
        target = self._resolve(scope, tenant_id, path)
        if target == root:
            raise ValueError("不能删除根目录")
        if not target.exists():
            raise ValueError("文件或目录不存在")
        relative = self._relative(root, target)
        if target.is_dir():
            if any(target.iterdir()) and not recursive:
                raise ValueError("目录非空，如需删除请显式确认递归删除")
            shutil.rmtree(str(target))
        else:
            target.unlink()
        self._notify_knowledge("mark_drive_deleted", scope, tenant_id, relative)
        return {"deleted": True, "path": relative}
