"""Validated local registry for administrator-approved external scripts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.core.config.loader import ScriptDefinition, ScriptParameter


_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ENV_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PARAMETER_TYPES = {"string", "date", "integer", "boolean"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


class ExternalScriptRegistry:
    """Persist and validate external script definitions without storing secrets."""

    def __init__(self, path: Path, env_path: Optional[Path] = None) -> None:
        self.path = path
        self.env_path = env_path or path.with_name("scripts.env")
        self._lock = threading.RLock()
        self._allowed_roots: List[Path] = []
        self._definitions: Dict[str, ScriptDefinition] = {}
        self.reload()

    @property
    def allowed_roots(self) -> List[str]:
        with self._lock:
            return [str(path) for path in self._allowed_roots]

    @property
    def definitions(self) -> Dict[str, ScriptDefinition]:
        with self._lock:
            return dict(self._definitions)

    def list_entries(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                self._serialize(item)
                for item in sorted(
                    self._definitions.values(), key=lambda definition: definition.id
                )
            ]

    def reload(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._allowed_roots = []
                self._definitions = {}
                return
            self._require_private_file(self.path, "外部脚本注册表")
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError("无法读取外部脚本注册表：{}".format(exc)) from exc
            if not isinstance(payload, dict):
                raise ValueError("外部脚本注册表必须是 JSON 对象")
            raw_roots = payload.get("allowed_roots", [])
            if not isinstance(raw_roots, list) or any(
                not isinstance(item, str) or not item.strip() for item in raw_roots
            ):
                raise ValueError("allowed_roots 必须是非空路径字符串数组")
            roots: List[Path] = []
            for raw in raw_roots:
                candidate = Path(raw).expanduser()
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError as exc:
                    raise ValueError("脚本允许目录不存在：{}".format(candidate)) from exc
                if not resolved.is_dir():
                    raise ValueError("脚本允许路径不是目录：{}".format(resolved))
                roots.append(resolved)
            items = payload.get("scripts", [])
            if not isinstance(items, list):
                raise ValueError("scripts 必须是数组")
            definitions: Dict[str, ScriptDefinition] = {}
            for raw in items:
                definition = self._validate_definition(raw, roots, trust_current=False)
                if definition.id in definitions:
                    raise ValueError("外部脚本 ID 重复：{}".format(definition.id))
                definitions[definition.id] = definition
            self._allowed_roots = roots
            self._definitions = definitions

    def configure_roots(self, roots: List[str]) -> List[str]:
        payload = self._payload()
        payload["allowed_roots"] = list(roots)
        self._validate_and_save(payload)
        return self.allowed_roots

    def create(self, raw: Dict[str, Any]) -> ScriptDefinition:
        payload = self._payload()
        script_id = str(raw.get("id", ""))
        if any(item.get("id") == script_id for item in payload["scripts"]):
            raise ValueError("脚本 ID 已存在：{}".format(script_id))
        item = dict(raw)
        item.pop("sha256", None)
        definition = self._validate_definition(
            item, self._roots_from_payload(payload), trust_current=True
        )
        payload["scripts"].append(self._serialize(definition))
        self._validate_and_save(payload)
        return self._definitions[definition.id]

    def update(self, script_id: str, changes: Dict[str, Any]) -> ScriptDefinition:
        payload = self._payload()
        for index, item in enumerate(payload["scripts"]):
            if item.get("id") != script_id:
                continue
            merged = dict(item)
            merged.update(changes)
            merged["id"] = script_id
            definition = self._validate_definition(
                merged, self._roots_from_payload(payload), trust_current=False
            )
            payload["scripts"][index] = self._serialize(definition)
            self._validate_and_save(payload)
            return self._definitions[script_id]
        raise ValueError("外部脚本不存在：{}".format(script_id))

    def trust_current(self, script_id: str) -> ScriptDefinition:
        payload = self._payload()
        for index, item in enumerate(payload["scripts"]):
            if item.get("id") != script_id:
                continue
            definition = self._validate_definition(
                item, self._roots_from_payload(payload), trust_current=True
            )
            payload["scripts"][index] = self._serialize(definition)
            self._validate_and_save(payload)
            return self._definitions[script_id]
        raise ValueError("外部脚本不存在：{}".format(script_id))

    def delete(self, script_id: str) -> None:
        payload = self._payload()
        filtered = [item for item in payload["scripts"] if item.get("id") != script_id]
        if len(filtered) == len(payload["scripts"]):
            raise ValueError("外部脚本不存在：{}".format(script_id))
        payload["scripts"] = filtered
        self._validate_and_save(payload)

    def verify(self, definition: ScriptDefinition) -> str:
        if not definition.external:
            return file_sha256(Path(definition.entrypoint))
        path = self._validated_path(
            definition.entrypoint, self._allowed_roots, definition.runtime
        )
        current = file_sha256(path)
        if current != definition.sha256:
            raise ValueError("脚本内容已变化，请管理员重新审核并信任当前版本")
        return current

    def environment_for(self, definition: ScriptDefinition) -> Dict[str, str]:
        if not definition.env_allowlist:
            return {}
        values = self._load_env()
        return {
            name: values[name]
            for name in definition.env_allowlist
            if name in values
        }

    def global_values(self) -> Dict[str, str]:
        """Load the platform-managed global environment values (0600 checked).

        Used as the global layer by :class:`EnvResolver`; organization values
        override these per tenant at runtime.
        """
        return self._load_env()

    def _load_env(self) -> Dict[str, str]:
        if not self.env_path.exists():
            return {}
        self._require_private_file(self.env_path, "脚本环境文件")
        result: Dict[str, str] = {}
        for line_number, line in enumerate(
            self.env_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            key, separator, raw = value.partition("=")
            if not separator or not _ENV_PATTERN.fullmatch(key.strip()):
                raise ValueError("脚本环境文件第 {} 行格式无效".format(line_number))
            result[key.strip()] = raw.strip()
        return result

    def _payload(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "allowed_roots": [str(path) for path in self._allowed_roots],
                "scripts": [
                    self._serialize(item)
                    for item in sorted(
                        self._definitions.values(), key=lambda definition: definition.id
                    )
                ],
            }

    @staticmethod
    def _roots_from_payload(payload: Dict[str, Any]) -> List[Path]:
        roots = []
        for raw in payload.get("allowed_roots", []):
            path = Path(raw).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError("脚本允许路径不是目录：{}".format(path))
            roots.append(path)
        return roots

    def _validate_and_save(self, payload: Dict[str, Any]) -> None:
        roots = self._roots_from_payload(payload)
        definitions: Dict[str, ScriptDefinition] = {}
        for item in payload.get("scripts", []):
            definition = self._validate_definition(item, roots, trust_current=False)
            if definition.id in definitions:
                raise ValueError("外部脚本 ID 重复：{}".format(definition.id))
            definitions[definition.id] = definition
        self._atomic_write(
            {
                "allowed_roots": [str(root) for root in roots],
                "scripts": [
                    self._serialize(item)
                    for item in sorted(definitions.values(), key=lambda value: value.id)
                ],
            }
        )
        with self._lock:
            self._allowed_roots = roots
            self._definitions = definitions

    def _validate_definition(
        self,
        raw: Any,
        roots: List[Path],
        trust_current: bool,
    ) -> ScriptDefinition:
        if not isinstance(raw, dict):
            raise ValueError("外部脚本定义必须是 JSON 对象")
        script_id = raw.get("id")
        if not isinstance(script_id, str) or not _ID_PATTERN.fullmatch(script_id):
            raise ValueError("脚本 ID 格式无效")
        name = raw.get("name")
        description = raw.get("description", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("脚本名称不能为空")
        if not isinstance(description, str):
            raise ValueError("脚本描述必须是字符串")
        runtime = raw.get("runtime", "executable")
        if runtime not in {"python", "executable"}:
            raise ValueError("runtime 仅支持 python 或 executable")
        if not roots:
            raise ValueError("请先配置至少一个脚本允许根目录")
        entrypoint = self._validated_path(raw.get("entrypoint"), roots, runtime)
        working_raw = raw.get("working_directory") or str(entrypoint.parent)
        working_directory = Path(str(working_raw)).expanduser().resolve(strict=True)
        if not working_directory.is_dir() or not _inside(working_directory, roots):
            raise ValueError("脚本工作目录必须位于允许根目录内")
        timeout = raw.get("timeout_seconds", 900)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise ValueError("timeout_seconds 必须是 1 到 3600 的整数")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        scope = raw.get("concurrency_scope", "global")
        if scope not in {"global", "tenant"}:
            raise ValueError("concurrency_scope 仅支持 global 或 tenant")
        concurrency_key = raw.get("concurrency_key", script_id)
        if not isinstance(concurrency_key, str) or not _ID_PATTERN.fullmatch(concurrency_key):
            raise ValueError("concurrency_key 格式无效")
        raw_env = raw.get("env_allowlist", [])
        if not isinstance(raw_env, list) or any(
            not isinstance(item, str) or not _ENV_PATTERN.fullmatch(item)
            for item in raw_env
        ):
            raise ValueError("env_allowlist 只能包含大写环境变量名")
        parameters = self._validate_parameters(raw.get("parameters", {}))
        digest = file_sha256(entrypoint) if trust_current else raw.get("sha256")
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            raise ValueError("sha256 必须是 64 位小写十六进制字符串")
        return ScriptDefinition(
            id=script_id,
            name=name.strip(),
            description=description.strip(),
            entrypoint=str(entrypoint),
            timeout_seconds=timeout,
            requires_approval=True,
            data_directory=script_id,
            parameters=parameters,
            runtime=runtime,
            working_directory=str(working_directory),
            sha256=digest,
            enabled=enabled,
            external=True,
            env_allowlist=list(dict.fromkeys(raw_env)),
            concurrency_scope=scope,
            concurrency_key=concurrency_key,
        )

    @staticmethod
    def _validate_parameters(raw: Any) -> Dict[str, ScriptParameter]:
        if not isinstance(raw, dict):
            raise ValueError("parameters 必须是 JSON 对象")
        result: Dict[str, ScriptParameter] = {}
        for name, spec in raw.items():
            if (
                not isinstance(name, str)
                or not _ID_PATTERN.fullmatch(name)
                or not isinstance(spec, dict)
            ):
                raise ValueError("脚本参数定义格式无效：{}".format(name))
            parameter_type = spec.get("type")
            if parameter_type not in _PARAMETER_TYPES:
                raise ValueError("脚本参数类型无效：{}".format(name))
            flag = spec.get("flag")
            positional = spec.get("positional", False)
            required = spec.get("required", False)
            choices = spec.get("choices", [])
            if not isinstance(required, bool) or not isinstance(positional, bool):
                raise ValueError("参数 required/positional 必须是布尔值")
            if flag is not None and (
                not isinstance(flag, str)
                or not re.fullmatch(r"--[a-zA-Z0-9][a-zA-Z0-9-]*", flag)
            ):
                raise ValueError("参数 flag 格式无效：{}".format(name))
            if positional == (flag is not None):
                raise ValueError("参数必须且只能使用 positional 或 flag")
            if parameter_type == "boolean" and positional:
                raise ValueError("boolean 参数只能使用 flag")
            if not isinstance(choices, list) or any(
                not isinstance(choice, str) or not choice for choice in choices
            ):
                raise ValueError("参数 choices 必须是非空字符串数组")
            result[name] = ScriptParameter(
                type=parameter_type,
                required=required,
                choices=list(choices),
                positional=positional,
                flag=flag,
            )
        return result

    @staticmethod
    def _validated_path(raw: Any, roots: List[Path], runtime: str) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("entrypoint 必须是非空绝对路径")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValueError("外部脚本 entrypoint 必须是绝对路径")
        try:
            if candidate.is_symlink():
                raise ValueError("外部脚本不能是符号链接")
            path = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError("外部脚本不存在：{}".format(candidate)) from exc
        if not path.is_file() or not _inside(path, roots):
            raise ValueError("外部脚本必须是允许根目录内的普通文件")
        if runtime == "executable" and not os.access(str(path), os.X_OK):
            raise ValueError("外部脚本没有执行权限：{}".format(path))
        return path

    @staticmethod
    def _serialize(definition: ScriptDefinition) -> Dict[str, Any]:
        parameters = {}
        for name, spec in definition.parameters.items():
            parameters[name] = {
                "type": spec.type,
                "required": spec.required,
                "choices": list(spec.choices),
                "positional": spec.positional,
                "flag": spec.flag,
            }
        return {
            "id": definition.id,
            "name": definition.name,
            "description": definition.description,
            "runtime": definition.runtime,
            "entrypoint": definition.entrypoint,
            "working_directory": definition.working_directory,
            "sha256": definition.sha256,
            "timeout_seconds": definition.timeout_seconds,
            "enabled": definition.enabled,
            "parameters": parameters,
            "env_allowlist": list(definition.env_allowlist),
            "concurrency_scope": definition.concurrency_scope,
            "concurrency_key": definition.concurrency_key or definition.id,
        }

    def _atomic_write(self, payload: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(self.path.parent), 0o700)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".script-registry-", dir=str(self.path.parent)
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            os.chmod(str(self.path), 0o600)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _require_private_file(path: Path, label: str) -> None:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise ValueError("{}权限必须为 0600".format(label))
