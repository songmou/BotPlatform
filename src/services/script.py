"""Registered background script execution and result delivery."""

from __future__ import annotations

import json
import hashlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config.loader import ScriptDefinition, validate_script_parameters
from src.integrations.keychain import KeychainReference, KeychainService
from src.integrations.ilink import Credentials
from src.integrations.images import ImageSource, ImageSourceError, ImageSourceLoader
from src.services.notification import (
    NotificationError,
    NotificationService,
    Recipient,
    TenantRecipientStore,
)
from src.storage.tenants import IntegrationStore, TenantContext, TenantRegistry


FINAL_STATUSES = {"success", "failed", "skipped", "timed_out", "cancelled"}
_RUN_ID = re.compile(r"^[a-z0-9_]+-[0-9]{8}T[0-9]{6}-[0-9a-f]{8}$")


@dataclass
class ScriptRun:
    run_id: str
    script_id: str
    script_name: str
    trigger: str
    parameters: Dict[str, str]
    status: str
    summary: str
    tenant_id: str
    artifacts: List[str] = field(default_factory=list)
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    notification_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize(value: object, limit: int = 2000) -> str:
    text = str(value or "").replace("\x00", "").strip()
    text = re.sub(r"([?&](?:token|key|password|secret)=)[^&\s]+", r"\1***", text, flags=re.I)
    text = re.sub(r"(?i)(password|secret|api[_-]?key)\s*[:=]\s*\S+", r"\1=***", text)
    encoded = text.encode("utf-8")
    if len(encoded) > limit:
        text = encoded[:limit].decode("utf-8", errors="ignore") + "……"
    return text


class ScriptService:
    def __init__(
        self,
        definitions: Dict[str, ScriptDefinition],
        credentials: Credentials,
        recipient_store: TenantRecipientStore,
        project_root: Path,
        tenant_registry: TenantRegistry,
        integration_store: Optional[IntegrationStore] = None,
        python_executable: Optional[str] = None,
        notification_service: Optional[NotificationService] = None,
        image_loader: Optional[ImageSourceLoader] = None,
        keychain_service: Optional[KeychainService] = None,
    ) -> None:
        self.definitions = dict(definitions)
        self.credentials = credentials
        self.recipient_store = recipient_store
        self.project_root = project_root.resolve()
        self.python_executable = python_executable or sys.executable
        self.notification_service = notification_service or NotificationService(
            credentials_loader=lambda: self.credentials,
            recipient_store=self.recipient_store,
        )
        self.image_loader = image_loader or ImageSourceLoader()
        self.tenant_registry = tenant_registry
        self.integration_store = integration_store or IntegrationStore(tenant_registry)
        self.keychain_service = keychain_service or KeychainService()
        self._lock = threading.RLock()
        self._active: Dict[str, str] = {}
        self._processes: Dict[str, subprocess.Popen] = {}
        self._recipients: Dict[str, Recipient] = {}
        self._cancelled = set()
        self._shutting_down = False
        self._executor = ThreadPoolExecutor(
            max_workers=max(2, len(self.definitions)),
            thread_name_prefix="ilinkbot-script",
        )

    @property
    def script_ids(self) -> List[str]:
        return sorted(self.definitions)

    def requires_approval(self, script_id: object) -> bool:
        """Fail closed for missing or unknown scripts."""
        if not isinstance(script_id, str):
            return True
        definition = self.definitions.get(script_id)
        return True if definition is None else definition.requires_approval

    def has_approval_required_scripts(self) -> bool:
        return any(
            definition.requires_approval for definition in self.definitions.values()
        )

    def list_scripts(self) -> List[Dict[str, Any]]:
        result = []
        for definition in sorted(self.definitions.values(), key=lambda item: item.id):
            parameters = {}
            for name, spec in definition.parameters.items():
                parameters[name] = {
                    "type": spec.type,
                    "required": spec.required,
                    "choices": list(spec.choices),
                }
            result.append(
                {
                    "id": definition.id,
                    "name": definition.name,
                    "description": definition.description,
                    "requires_approval": definition.requires_approval,
                    "parameters": parameters,
                }
            )
        return result

    def normalize(self, script_id: str, parameters: object) -> tuple[ScriptDefinition, Dict[str, str]]:
        if not isinstance(script_id, str) or script_id not in self.definitions:
            raise ValueError("未知脚本：{}".format(script_id or "<空>"))
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise ValueError("parameters 必须是 JSON 对象")
        definition = self.definitions[script_id]
        return definition, validate_script_parameters(definition, parameters)

    def preview(self, script_id: str, parameters: object) -> str:
        definition, normalized = self.normalize(script_id, parameters)
        detail = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        return "运行固定脚本：{}（{}）\n参数：{}".format(
            definition.name, definition.id, detail
        )

    def submit_for_tenant(
        self, tenant: TenantContext, script_id: str, parameters: object
    ) -> Dict[str, Any]:
        registered = self.tenant_registry.get(tenant.tenant_id)
        if registered != tenant:
            raise ValueError("租户身份不匹配")
        recipient = self.recipient_store.load(tenant.tenant_id)
        return self.submit(
            tenant,
            script_id,
            parameters,
            trigger="model",
            recipient=recipient,
        )

    def submit(
        self,
        tenant: TenantContext,
        script_id: str,
        parameters: object,
        trigger: str,
        recipient: Optional[Recipient] = None,
    ) -> Dict[str, Any]:
        if self.tenant_registry.get(tenant.tenant_id) != tenant:
            raise ValueError("租户身份不匹配")
        self._require_integration(tenant, script_id)
        definition, normalized = self.normalize(script_id, parameters)
        now = datetime.now(timezone.utc)
        run_id = "{}-{}-{}".format(
            definition.id,
            now.strftime("%Y%m%dT%H%M%S"),
            uuid.uuid4().hex[:8],
        )
        run = ScriptRun(
            run_id=run_id,
            script_id=definition.id,
            script_name=definition.name,
            trigger=trigger,
            parameters=normalized,
            status="running",
            summary="任务已提交，正在后台执行。",
            created_at=now.isoformat(),
            tenant_id=tenant.tenant_id,
        )
        with self._lock:
            if self._shutting_down:
                raise ValueError("脚本服务正在关闭，不能提交新任务")
            active_key = self._active_key(run)
            existing = self._active.get(active_key)
            if existing:
                run.status = "skipped"
                run.summary = "已有同一脚本正在运行，本次触发已跳过。"
                run.finished_at = _utc_now()
                self._persist(run)
                if recipient:
                    self._recipients[run_id] = recipient
                self._executor.submit(self._notify, run)
                return run.to_dict()
            self._active[active_key] = run_id
            if recipient:
                self._recipients[run_id] = recipient
            self._persist(run)
            self._executor.submit(self._execute, definition, run)
        return run.to_dict()

    @staticmethod
    def _integration_id(script_id: str) -> Optional[str]:
        return {
            "ctsehr_check": "ctsehr",
            "autogen_monitor": "autogen",
        }.get(script_id)

    def _require_integration(self, tenant: TenantContext, script_id: str) -> None:
        integration_id = self._integration_id(script_id)
        if integration_id is None:
            return
        metadata = self.integration_store.get(tenant.tenant_id, integration_id)
        if not metadata or not self.keychain_service.exists(
            KeychainReference(
                str(metadata.get("keychain_service", "")),
                str(metadata.get("keychain_account", "credential")),
            )
        ):
            raise ValueError(
                "尚未配置 {}，请先使用 /integration setup {}".format(
                    integration_id, integration_id
                )
            )

    @staticmethod
    def _active_key(run: ScriptRun) -> str:
        return "{}:{}".format(run.tenant_id, run.script_id)

    def _roots_for(self, run: ScriptRun) -> tuple[Path, Path]:
        root = self.tenant_registry.tenant_root(run.tenant_id)
        runs_root = root / "scripts" / ".runtime"
        outputs_root = root / "scripts"
        runs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        outputs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(runs_root), 0o700)
        os.chmod(str(outputs_root), 0o700)
        return runs_root, outputs_root

    def get_run(self, tenant: TenantContext, run_id: str) -> Dict[str, Any]:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("任务编号格式无效")
        if self.tenant_registry.get(tenant.tenant_id) != tenant:
            raise ValueError("租户身份不匹配")
        with self.tenant_registry.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM script_runs WHERE run_id=? AND tenant_id=?",
                (run_id, tenant.tenant_id),
            ).fetchone()
            if row is None:
                raise ValueError("未找到脚本任务：{}".format(run_id))
            artifact_rows = connection.execute(
                "SELECT relative_path FROM script_run_artifacts "
                "WHERE run_id=? ORDER BY position",
                (run_id,),
            ).fetchall()
        root = self.tenant_registry.tenant_root(tenant.tenant_id)
        return {
            "run_id": str(row["run_id"]),
            "script_id": str(row["script_id"]),
            "script_name": str(row["script_name"]),
            "trigger": str(row["trigger"]),
            "parameters": json.loads(str(row["parameters_json"])),
            "status": str(row["status"]),
            "summary": str(row["summary"]),
            "tenant_id": str(row["tenant_id"]),
            "artifacts": [str(root / str(item["relative_path"])) for item in artifact_rows],
            "created_at": str(row["created_at"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "exit_code": row["exit_code"],
            "error": row["error"],
            "notification_error": row["notification_error"],
        }

    def _persist(self, run: ScriptRun) -> None:
        root = self.tenant_registry.tenant_root(run.tenant_id).resolve()
        artifacts = []
        for position, raw in enumerate(run.artifacts):
            path = Path(raw).resolve()
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            digest = None
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                pass
            artifacts.append((run.run_id, position, str(relative), digest))
        parameters = json.dumps(run.parameters, ensure_ascii=False, separators=(",", ":"))
        with self.tenant_registry.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO script_runs(run_id, tenant_id, script_id, script_name, trigger, "
                "parameters_json, status, summary, created_at, started_at, finished_at, exit_code, "
                "error, notification_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, summary=excluded.summary, "
                "started_at=excluded.started_at, finished_at=excluded.finished_at, "
                "exit_code=excluded.exit_code, error=excluded.error, "
                "notification_error=excluded.notification_error",
                (
                    run.run_id, run.tenant_id, run.script_id, run.script_name, run.trigger,
                    parameters, run.status, run.summary, run.created_at, run.started_at,
                    run.finished_at, run.exit_code, run.error, run.notification_error,
                ),
            )
            connection.execute("DELETE FROM script_run_artifacts WHERE run_id=?", (run.run_id,))
            connection.executemany(
                "INSERT INTO script_run_artifacts(run_id, position, relative_path, content_hash) "
                "VALUES (?, ?, ?, ?)",
                artifacts,
            )

    def _argv(self, definition: ScriptDefinition, parameters: Dict[str, str]) -> List[str]:
        positional: List[str] = []
        flagged: List[str] = []
        for name, spec in definition.parameters.items():
            if name not in parameters:
                continue
            if spec.positional:
                positional.append(parameters[name])
            else:
                flagged.extend([spec.flag or "", parameters[name]])
        return [self.python_executable, definition.entrypoint, *positional, *flagged]

    def _environment(self, run: ScriptRun, result_path: Path) -> Dict[str, str]:
        environment = {}
        for name in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "NO_PROXY", "no_proxy"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        environment.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        environment.setdefault("LANG", "en_US.UTF-8")
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONPATH"] = str(self.project_root)
        environment["ILINKBOT_SCRIPT_RESULT_FILE"] = str(result_path)
        definition = self.definitions[run.script_id]
        _, outputs_root = self._roots_for(run)
        environment["ILINKBOT_SCRIPT_DATA_ROOT"] = str(
            outputs_root / (definition.data_directory or definition.id)
        )
        environment["ILINKBOT_TENANT_ID"] = run.tenant_id
        environment["ILINKBOT_DATABASE_PATH"] = str(self.tenant_registry.database_path)
        integration_id = self._integration_id(run.script_id)
        if integration_id:
            metadata = self.integration_store.get(run.tenant_id, integration_id)
            if not metadata:
                raise ValueError("租户集成配置缺失")
            environment["ILINKBOT_INTEGRATION_ID"] = integration_id
            environment["ILINKBOT_INTEGRATION_ACCOUNT"] = str(
                metadata.get("account", "")
            )
            environment["ILINKBOT_KEYCHAIN_SERVICE"] = str(
                metadata.get("keychain_service", "")
            )
            environment["ILINKBOT_KEYCHAIN_ACCOUNT"] = str(
                metadata.get("keychain_account", "credential")
            )
        if run.script_id == "autogen_monitor":
            environment["AUTOGEN_ENV_FILE"] = str(outputs_root / "autogen.env")
        return environment

    def _execute(self, definition: ScriptDefinition, run: ScriptRun) -> None:
        runs_root, _ = self._roots_for(run)
        child_result = runs_root / ("." + run.run_id + ".child.json")
        try:
            with self._lock:
                if self._shutting_down:
                    self._cancelled.add(run.run_id)
                cancelled = run.run_id in self._cancelled
            if cancelled:
                run.status = "cancelled"
                run.summary = "iLinkBot 已关闭，任务未启动。"
                return
            run.started_at = _utc_now()
            self._persist(run)
            process = subprocess.Popen(
                self._argv(definition, run.parameters),
                cwd=str(Path(definition.entrypoint).parent),
                env=self._environment(run, child_result),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            with self._lock:
                self._processes[run.run_id] = process
                if run.run_id in self._cancelled:
                    self._stop_process(process)
            try:
                stdout, stderr = process.communicate(timeout=definition.timeout_seconds)
            except subprocess.TimeoutExpired:
                self._stop_process(process)
                stdout, stderr = process.communicate()
                run.status = "timed_out"
                run.summary = "脚本运行超过 {} 秒，已终止。".format(definition.timeout_seconds)
                run.error = _sanitize(stderr or stdout)
            else:
                with self._lock:
                    cancelled = run.run_id in self._cancelled
                if cancelled:
                    run.status = "cancelled"
                    run.summary = "iLinkBot 关闭时已终止脚本任务。"
                else:
                    self._apply_child_result(run, child_result, process.returncode, stdout, stderr)
            run.exit_code = process.returncode
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            run.status = "failed"
            run.summary = "无法完成脚本任务。"
            run.error = _sanitize(exc)
        finally:
            run.finished_at = _utc_now()
            try:
                child_result.unlink()
            except FileNotFoundError:
                pass
            with self._lock:
                self._processes.pop(run.run_id, None)
                active_key = self._active_key(run)
                if self._active.get(active_key) == run.run_id:
                    self._active.pop(active_key, None)
            self._persist(run)
            self._notify(run)

    def _apply_child_result(
        self,
        run: ScriptRun,
        result_path: Path,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        payload: Dict[str, Any] = {}
        if result_path.is_file():
            try:
                raw = json.loads(result_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    payload = raw
            except (OSError, ValueError):
                payload = {}
        requested_status = payload.get("status")
        run.status = requested_status if requested_status in {"success", "failed", "skipped"} else (
            "success" if returncode == 0 else "failed"
        )
        summary = payload.get("summary")
        run.summary = _sanitize(summary or stdout or stderr or "脚本运行结束。")
        raw_artifacts = payload.get("artifacts", [])
        artifacts: List[str] = []
        if isinstance(raw_artifacts, list):
            definition = self.definitions[run.script_id]
            _, outputs_root = self._roots_for(run)
            allowed_root = (
                outputs_root / (definition.data_directory or definition.id)
            ).resolve()
            for raw_path in raw_artifacts:
                if not isinstance(raw_path, str):
                    continue
                try:
                    path = Path(raw_path).expanduser().resolve(strict=True)
                except OSError:
                    continue
                if path.is_file() and (path == allowed_root or allowed_root in path.parents):
                    if "image" not in definition.artifact_types:
                        continue
                    try:
                        self.image_loader.load(ImageSource.local(path))
                    except ImageSourceError:
                        continue
                    artifacts.append(str(path))
        run.artifacts = artifacts
        error = payload.get("error")
        if run.status != "success":
            run.error = _sanitize(error or stderr or stdout)

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass

    def _notify(self, run: ScriptRun) -> None:
        with self._lock:
            recipient = self._recipients.pop(run.run_id, None)
        if recipient is None:
            return
        labels = {
            "success": "成功",
            "failed": "失败",
            "skipped": "跳过",
            "timed_out": "超时",
            "cancelled": "取消",
            "running": "运行中",
        }
        message = "【固定脚本结果】\n任务：{}\n状态：{}\n编号：{}\n{}".format(
            run.script_name,
            labels.get(run.status, run.status),
            run.run_id,
            run.summary,
        )
        errors: List[str] = []
        try:
            self.notification_service.send_text_to(recipient, message)
        except NotificationError as exc:
            errors.append(_sanitize(exc, 500))
        for artifact in run.artifacts:
            try:
                self.notification_service.send_image_to(
                    recipient, ImageSource.local(Path(artifact)), caption=""
                )
            except NotificationError as exc:
                errors.append(_sanitize(exc, 500))
        if errors:
            run.notification_error = "；".join(errors)
            self._persist(run)

    def shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
            processes = list(self._processes.items())
            self._cancelled.update(self._active.values())
        for _, process in processes:
            self._stop_process(process)
        self._executor.shutdown(wait=True)

    def cancel_tenant(self, tenant_id: str) -> None:
        """Cancel only one tenant's active scripts before deleting its data."""
        prefix = tenant_id + ":"
        with self._lock:
            run_ids = [
                run_id
                for key, run_id in self._active.items()
                if key.startswith(prefix)
            ]
            self._cancelled.update(run_ids)
            processes = [
                self._processes[run_id]
                for run_id in run_ids
                if run_id in self._processes
            ]
            for run_id in run_ids:
                self._recipients.pop(run_id, None)
        for process in processes:
            self._stop_process(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self._lock:
                if not any(key.startswith(prefix) for key in self._active):
                    return
            time.sleep(0.05)
        raise ValueError("仍有用户脚本正在结束，请稍后重试删除")
