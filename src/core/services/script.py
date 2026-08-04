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
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.core.config.loader import (
    ScriptDefinition,
    load_project_config,
    validate_script_parameters,
)
from src.core.integrations.keychain import KeychainReference, KeychainService
from src.core.integrations.ilink import Credentials
from src.core.integrations.images import ImageSource, ImageSourceError, ImageSourceLoader
from src.core.services.notification import (
    NotificationError,
    NotificationService,
    Recipient,
    TenantRecipientStore,
)
from src.core.services.script_registry import ExternalScriptRegistry, file_sha256
from src.core.services.env_resolver import EnvResolver
from src.core.storage.tenants import IntegrationStore, TenantContext, TenantRegistry


FINAL_STATUSES = {"success", "failed", "skipped", "timed_out", "cancelled"}
_RUN_ID = re.compile(r"^[a-z0-9_]+-[0-9]{8}T[0-9]{6}-[0-9a-f]{8}$")


@dataclass
class ScriptRun:
    run_id: str
    script_id: str
    script_name: str
    trigger: str
    parameters: Dict[str, Any]
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


def _redact_values(value: object, secrets: List[str]) -> str:
    text = str(value or "")
    for secret in sorted(
        {item for item in secrets if item}, key=len, reverse=True
    ):
        text = text.replace(secret, "***")
    return text


class ScriptService:
    def __init__(
        self,
        definitions: Dict[str, ScriptDefinition],
        credentials: Optional[Credentials],
        recipient_store: TenantRecipientStore,
        project_root: Path,
        tenant_registry: TenantRegistry,
        integration_store: Optional[IntegrationStore] = None,
        python_executable: Optional[str] = None,
        notification_service: Optional[NotificationService] = None,
        image_loader: Optional[ImageSourceLoader] = None,
        keychain_service: Optional[KeychainService] = None,
        external_registry: Optional[ExternalScriptRegistry] = None,
        env_resolver: Optional[EnvResolver] = None,
        address_store: Optional[Any] = None,
    ) -> None:
        self.builtin_definitions = dict(definitions)
        self.external_registry = external_registry
        self.env_resolver = env_resolver
        self.definitions = self._merged_definitions()
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
        # Duck-typed ChannelAddressStore; kept loose to avoid an import cycle.
        self.address_store = address_store
        self._lock = threading.RLock()
        self._active: Dict[str, str] = {}
        self._active_run_keys: Dict[str, str] = {}
        self._processes: Dict[str, subprocess.Popen] = {}
        self._recipients: Dict[str, Recipient] = {}
        self._credential_tenants: Dict[str, str] = {}
        self._cancelled = set()
        self._completion_listeners: List[Callable[[ScriptRun], None]] = []
        self._shutting_down = False
        self._executor = ThreadPoolExecutor(
            max_workers=max(2, len(self.definitions)),
            thread_name_prefix="ilinkbot-script",
        )

    @property
    def script_ids(self) -> List[str]:
        return sorted(self.definitions)

    def _merged_definitions(self) -> Dict[str, ScriptDefinition]:
        definitions = dict(self.builtin_definitions)
        if self.external_registry is not None:
            for script_id, definition in self.external_registry.definitions.items():
                if script_id in definitions:
                    raise ValueError("外部脚本与内置脚本 ID 冲突：{}".format(script_id))
                definitions[script_id] = definition
        return definitions

    def reload_external_definitions(self) -> None:
        if self.external_registry is None:
            return
        self.external_registry.reload()
        definitions = self._merged_definitions()
        with self._lock:
            self.definitions = definitions

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
                    "positional": spec.positional,
                    "flag": spec.flag,
                }
            result.append(
                {
                    "id": definition.id,
                    "name": definition.name,
                    "description": definition.description,
                    "requires_approval": definition.requires_approval,
                    "parameters": parameters,
                    "runtime": definition.runtime,
                    "sha256": definition.sha256,
                    "sha256_short": definition.sha256[:12] if definition.sha256 else "",
                    "enabled": definition.enabled,
                    "external": definition.external,
                    "env_allowlist": list(definition.env_allowlist),
                }
            )
        return result

    def add_completion_listener(self, listener: Callable[[ScriptRun], None]) -> None:
        with self._lock:
            self._completion_listeners.append(listener)

    def normalize(
        self, script_id: str, parameters: object
    ) -> tuple[ScriptDefinition, Dict[str, Any]]:
        if not isinstance(script_id, str) or script_id not in self.definitions:
            raise ValueError("未知脚本：{}".format(script_id or "<空>"))
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise ValueError("parameters 必须是 JSON 对象")
        definition = self.definitions[script_id]
        if not definition.enabled:
            raise ValueError("脚本已被禁用：{}".format(script_id))
        self.verify_definition(definition)
        return definition, validate_script_parameters(definition, parameters)

    def verify_definition(self, definition: ScriptDefinition) -> str:
        if definition.external:
            if self.external_registry is None:
                raise ValueError("外部脚本注册表不可用")
            return self.external_registry.verify(definition)
        return file_sha256(Path(definition.entrypoint))

    def current_hash(self, script_id: str) -> str:
        definition = self.definitions.get(script_id)
        if definition is None:
            raise ValueError("未知脚本：{}".format(script_id))
        if not definition.enabled:
            raise ValueError("脚本已被禁用：{}".format(script_id))
        return self.verify_definition(definition)

    def preview(self, script_id: str, parameters: object) -> str:
        definition, normalized = self.normalize(script_id, parameters)
        detail = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        return "运行固定脚本：{}（{}）\n参数：{}".format(
            definition.name, definition.id, detail
        ) + (
            "\n版本：{}".format(definition.sha256[:12])
            if definition.sha256
            else ""
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
        credential_tenant_id = self._require_integration(tenant, script_id)
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
            active_key = self._active_key(run, definition)
            existing = self._active.get(active_key)
            if existing:
                run.status = "skipped"
                run.summary = "已有同一脚本正在运行，本次触发已跳过。"
                run.finished_at = _utc_now()
                self._persist(run)
                if recipient:
                    self._recipients[run_id] = recipient
                self._emit_completion(run)
                self._executor.submit(self._notify, run)
                return run.to_dict()
            self._active[active_key] = run_id
            self._active_run_keys[run_id] = active_key
            if recipient:
                self._recipients[run_id] = recipient
            if credential_tenant_id:
                self._credential_tenants[run_id] = credential_tenant_id
            self._persist(run)
            self._executor.submit(self._execute, definition, run)
        return run.to_dict()

    @staticmethod
    def _integration_id(script_id: str) -> Optional[str]:
        return {
            "ctsehr_check": "ctsehr",
            "ctsoa_check": "ctsoa",
            "autogen_monitor": "autogen",
        }.get(script_id)

    def _credential_tenant_ids(
        self, tenant_id: Optional[str], personal_tenant_id: Optional[str] = None
    ) -> List[str]:
        """Rank credential owners: personal tenant first, organization last.

        Integration secrets are written under the personal tenant (see
        ``IntegrationService._tenant_id``), while organization schedules run
        under the organization tenant. Probing both keeps the two ends aligned.
        """
        ids: List[str] = []
        if personal_tenant_id:
            ids.append(personal_tenant_id)
        elif tenant_id and self.address_store is not None:
            try:
                endpoint = self.address_store.latest_endpoint(tenant_id)
                derived = (
                    self.address_store.personal_tenant_for_endpoint(
                        tenant_id, endpoint.endpoint_id
                    )
                    if endpoint is not None
                    else None
                )
                if derived:
                    ids.append(derived)
            except Exception:
                # A missing route must never break credential resolution.
                pass
        if tenant_id and tenant_id not in ids:
            ids.append(tenant_id)
        return ids

    def _resolve_integration_metadata(
        self,
        tenant_id: Optional[str],
        integration_id: str,
        personal_tenant_id: Optional[str] = None,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """Return the first tenant whose integration binding is complete."""
        partial: Optional[Tuple[str, Dict[str, Any]]] = None
        for candidate in self._credential_tenant_ids(tenant_id, personal_tenant_id):
            metadata = self.integration_store.get(candidate, integration_id)
            if not metadata:
                continue
            if partial is None:
                partial = (candidate, dict(metadata))
            service = str(metadata.get("keychain_service", ""))
            try:
                ready = bool(service) and self.keychain_service.exists(
                    KeychainReference(
                        service, str(metadata.get("keychain_account", "credential"))
                    )
                )
            except Exception:
                ready = False
            if ready:
                return candidate, dict(metadata)
        return partial or (None, {})

    def _require_integration(
        self, tenant: TenantContext, script_id: str
    ) -> Optional[str]:
        """Validate the integration binding and return the credential tenant."""
        integration_id = self._integration_id(script_id)
        if integration_id is None:
            return None
        credential_tenant_id, metadata = self._resolve_integration_metadata(
            tenant.tenant_id, integration_id, tenant.personal_tenant_id
        )
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
        return credential_tenant_id

    def integration_status(
        self, tenant_id: Optional[str], script_id: str
    ) -> Optional[Dict[str, Any]]:
        """Describe the integration credential binding for a script.

        Scripts that map to a platform integration (ctsehr/ctsoa/autogen) get
        their account and password from the per-tenant integration store and
        the Keychain, not from the org-env store. This surfaces that binding so
        the web UI can show whether credentials are configured before a run.
        """
        integration_id = self._integration_id(script_id)
        if integration_id is None:
            return None
        _, metadata = (
            self._resolve_integration_metadata(tenant_id, integration_id)
            if tenant_id
            else (None, {})
        )
        account = metadata.get("account", "")
        keychain_service = metadata.get("keychain_service", "")
        keychain_account = metadata.get("keychain_account", "credential")
        secret_present = False
        if keychain_service:
            try:
                secret_present = self.keychain_service.exists(
                    KeychainReference(keychain_service, keychain_account)
                )
            except Exception:
                secret_present = False
        return {
            "integration_id": integration_id,
            "requires_credentials": True,
            "account_set": bool(account),
            "keychain_secret_set": bool(secret_present),
            "ready": bool(account) and bool(secret_present),
            "injected": [
                "ILINKBOT_INTEGRATION_ACCOUNT",
                "ILINKBOT_KEYCHAIN_SERVICE",
                "ILINKBOT_KEYCHAIN_ACCOUNT",
            ],
        }

    @staticmethod
    def _active_key(run: ScriptRun, definition: ScriptDefinition) -> str:
        key = definition.concurrency_key or definition.id
        if definition.concurrency_scope == "global":
            return "global:{}".format(key)
        return "{}:{}".format(run.tenant_id, key)

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
        log_path = root / "scripts" / "logs" / (run_id + ".log")
        log_tail = ""
        if log_path.is_file():
            try:
                log_tail = log_path.read_text(encoding="utf-8")[-8192:]
            except OSError:
                log_tail = ""
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
            "log_tail": log_tail,
        }

    def list_runs(
        self,
        tenant: TenantContext,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        if self.tenant_registry.get(tenant.tenant_id) != tenant:
            raise ValueError("租户身份不匹配")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit 必须是 1 到 200 的整数")
        with self.tenant_registry.database.read() as connection:
            rows = connection.execute(
                "SELECT run_id FROM script_runs WHERE tenant_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (tenant.tenant_id, limit),
            ).fetchall()
        return [self.get_run(tenant, str(row["run_id"])) for row in rows]

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

    def _argv(self, definition: ScriptDefinition, parameters: Dict[str, Any]) -> List[str]:
        positional: List[str] = []
        flagged: List[str] = []
        for name, spec in definition.parameters.items():
            if name not in parameters:
                continue
            value = parameters[name]
            if spec.type == "boolean":
                if value:
                    flagged.append(spec.flag or "")
                continue
            if spec.positional:
                positional.append(str(value))
            else:
                flagged.extend([spec.flag or "", str(value)])
        prefix = (
            [self.python_executable, definition.entrypoint]
            if definition.runtime == "python"
            else [definition.entrypoint]
        )
        return [*prefix, *positional, *flagged]

    def _environment(
        self,
        run: ScriptRun,
        result_path: Path,
        definition: Optional[ScriptDefinition] = None,
    ) -> Dict[str, str]:
        definition = definition or self.definitions[run.script_id]
        environment = {}
        allowed = ["PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "NO_PROXY", "no_proxy"]
        if os.name == "nt":
            allowed += [
                "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
                "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
                "TEMP", "TMP", "USERNAME",
            ]
        for name in allowed:
            value = os.environ.get(name)
            if value:
                environment[name] = value
        environment.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        environment.setdefault("LANG", "en_US.UTF-8")
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONPATH"] = str(self.project_root)
        environment["ILINKBOT_SCRIPT_RESULT_FILE"] = str(result_path)
        _, outputs_root = self._roots_for(run)
        environment["ILINKBOT_SCRIPT_DATA_ROOT"] = str(
            outputs_root / (definition.data_directory or definition.id)
        )
        environment["ILINKBOT_TENANT_ID"] = run.tenant_id
        environment["ILINKBOT_DATABASE_PATH"] = str(self.tenant_registry.database_path)
        if definition.env_allowlist:
            if self.env_resolver is not None:
                # Organization values override global values; reserved names are
                # filtered inside the resolver so the sandbox cannot be hijacked.
                environment.update(
                    self.env_resolver.resolve(run.tenant_id, definition.env_allowlist)
                )
            elif definition.external and self.external_registry is not None:
                environment.update(self.external_registry.environment_for(definition))
        integration_id = self._integration_id(run.script_id)
        if integration_id:
            # Credentials may live on the submitting member's personal tenant;
            # artifacts and ILINKBOT_TENANT_ID intentionally stay on run.tenant_id.
            with self._lock:
                metadata_tenant_id = (
                    self._credential_tenants.get(run.run_id) or run.tenant_id
                )
            metadata = self.integration_store.get(metadata_tenant_id, integration_id)
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
            config_directory = self.project_root / "config"
            if config_directory.is_dir():
                project_config = load_project_config(config_directory)
                profile_id = os.getenv("MODEL_PROFILE") or project_config.app.active_model
                profile = project_config.models[profile_id]
                environment["ILINKBOT_PROJECT_CONFIG"] = str(config_directory)
                environment["AUTOGEN_MODEL_PROFILE"] = profile.id
                if profile.api_key_env:
                    api_key = os.getenv(profile.api_key_env)
                    if api_key:
                        # The isolated child must use the same active model as
                        # the main process; this is the only model credential
                        # it receives and it is never logged or persisted.
                        environment[profile.api_key_env] = api_key
        return environment

    def _execute(self, definition: ScriptDefinition, run: ScriptRun) -> None:
        runs_root, _ = self._roots_for(run)
        child_result = runs_root / ("." + run.run_id + ".child.json")
        secret_values: List[str] = []
        try:
            with self._lock:
                if self._shutting_down:
                    self._cancelled.add(run.run_id)
                cancelled = run.run_id in self._cancelled
            if cancelled:
                run.status = "cancelled"
                run.summary = "脚本任务在启动前已取消。"
                return
            # Revalidate immediately before spawning so queued runs cannot use a
            # file that changed after approval/submission.
            self.verify_definition(definition)
            run.started_at = _utc_now()
            self._persist(run)
            environment = self._environment(run, child_result, definition)
            secret_values = [
                environment[name]
                for name in definition.env_allowlist
                if environment.get(name)
            ]
            process = subprocess.Popen(
                self._argv(definition, run.parameters),
                cwd=definition.working_directory or str(Path(definition.entrypoint).parent),
                env=environment,
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
                run.error = _sanitize(
                    _redact_values(stderr or stdout, secret_values)
                )
            else:
                with self._lock:
                    cancelled = run.run_id in self._cancelled
                if cancelled:
                    run.status = "cancelled"
                    run.summary = "脚本任务已取消。"
                else:
                    self._apply_child_result(
                        run,
                        definition,
                        child_result,
                        process.returncode,
                        _redact_values(stdout, secret_values),
                        _redact_values(stderr, secret_values),
                        secret_values,
                    )
            run.exit_code = process.returncode
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            run.status = "failed"
            run.summary = "无法完成脚本任务。"
            run.error = _sanitize(exc)
        finally:
            try:
                self._persist_log(
                    run,
                    _redact_values(locals().get("stdout", ""), secret_values),
                    _redact_values(locals().get("stderr", ""), secret_values),
                )
            except OSError as exc:
                run.notification_error = _sanitize(
                    "保存脚本日志失败：{}".format(exc), 500
                )
            run.finished_at = _utc_now()
            try:
                child_result.unlink()
            except FileNotFoundError:
                pass
            with self._lock:
                self._processes.pop(run.run_id, None)
                self._credential_tenants.pop(run.run_id, None)
                active_key = self._active_run_keys.pop(run.run_id, "")
                if self._active.get(active_key) == run.run_id:
                    self._active.pop(active_key, None)
            self._persist(run)
            self._emit_completion(run)
            self._notify(run)

    def _apply_child_result(
        self,
        run: ScriptRun,
        definition: ScriptDefinition,
        result_path: Path,
        returncode: int,
        stdout: str,
        stderr: str,
        secret_values: Optional[List[str]] = None,
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
        run.summary = _sanitize(
            _redact_values(
                summary or stdout or stderr or "脚本运行结束。",
                secret_values or [],
            )
        )
        raw_artifacts = payload.get("artifacts", [])
        artifacts: List[str] = []
        if isinstance(raw_artifacts, list):
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
            run.error = _sanitize(
                _redact_values(error or stderr or stdout, secret_values or [])
            )

    def _emit_completion(self, run: ScriptRun) -> None:
        with self._lock:
            listeners = list(self._completion_listeners)
        for listener in listeners:
            try:
                listener(run)
            except Exception:
                continue

    def _persist_log(self, run: ScriptRun, stdout: str, stderr: str) -> None:
        root = self.tenant_registry.tenant_root(run.tenant_id)
        log_root = root / "scripts" / "logs"
        log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(log_root), 0o700)
        content = ""
        if stdout:
            content += "[stdout]\n" + _sanitize(stdout, 512 * 1024)
        if stderr:
            content += ("\n" if content else "") + "[stderr]\n" + _sanitize(
                stderr, 512 * 1024
            )
        if not content:
            return
        path = log_root / (run.run_id + ".log")
        descriptor = os.open(
            str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(str(path), 0o600)

    def cancel_run(self, tenant: TenantContext, run_id: str) -> Dict[str, Any]:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("任务编号格式无效")
        current = self.get_run(tenant, run_id)
        if current["status"] in FINAL_STATUSES:
            raise ValueError("脚本任务已经结束，不能取消")
        with self._lock:
            process = self._processes.get(run_id)
            self._cancelled.add(run_id)
            if process is not None:
                self._stop_process(process)
        return {"run_id": run_id, "status": "cancelling", "summary": "已请求取消脚本任务。"}

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
            enqueue_text = getattr(
                self.notification_service, "enqueue_text_to_tenant", None
            )
            if callable(enqueue_text):
                enqueue_text(
                    run.tenant_id,
                    message,
                    source_type="script",
                    source_key="{}:text".format(run.run_id),
                )
            elif recipient is not None:
                self.notification_service.send_text_to(recipient, message)
        except NotificationError as exc:
            errors.append(_sanitize(exc, 500))
        for index, artifact in enumerate(run.artifacts):
            try:
                enqueue_image = getattr(
                    self.notification_service, "enqueue_image_to_tenant", None
                )
                if callable(enqueue_image):
                    enqueue_image(
                        run.tenant_id,
                        ImageSource.local(Path(artifact)),
                        caption="",
                        source_type="script",
                        source_key="{}:image:{}".format(run.run_id, index),
                    )
                elif recipient is not None:
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
                self._credential_tenants.pop(run_id, None)
        for process in processes:
            self._stop_process(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self._lock:
                if not any(key.startswith(prefix) for key in self._active):
                    return
            time.sleep(0.05)
        raise ValueError("仍有用户脚本正在结束，请稍后重试删除")
