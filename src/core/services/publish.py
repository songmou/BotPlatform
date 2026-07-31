"""Publish bindings between agents and external messaging platforms."""

from __future__ import annotations

import copy
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.core.paths import SYSTEM_DATA_DIR

PLATFORM_WECHAT = "wechat"
PLATFORM_WECOM = "wecom"
SUPPORTED_PLATFORMS = (PLATFORM_WECHAT, PLATFORM_WECOM)
PLACEHOLDER_PLATFORMS = ("dingtalk", "feishu")
ALL_PLATFORMS = SUPPORTED_PLATFORMS + PLACEHOLDER_PLATFORMS

PLATFORM_NAMES = {
    PLATFORM_WECHAT: "微信",
    PLATFORM_WECOM: "企业微信",
    "dingtalk": "钉钉",
    "feishu": "飞书",
}

PUBLISH_PATH = SYSTEM_DATA_DIR / "publish.json"


class PublishError(ValueError):
    """Raised when a publish operation is invalid."""


def ensure_supported(platform: str) -> None:
    if platform in PLACEHOLDER_PLATFORMS:
        raise PublishError("该平台暂未开放")
    if platform not in SUPPORTED_PLATFORMS:
        raise PublishError("未知的发布平台：{}".format(platform))


class PublishStore:
    """File-backed store for agent publish bindings.

    The file lives under data/system so both the bot process and the web
    panel see updates without a restart (reads are mtime-cached).
    """

    def __init__(self, path: Path = PUBLISH_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._cache: Dict[str, Any] = {"platforms": {}}
        self._mtime: Optional[float] = None

    # -- persistence -------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._cache = {"platforms": {}}
            self._mtime = None
            return self._cache
        if self._mtime == mtime:
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        platforms = data.get("platforms")
        self._cache = {
            "platforms": platforms if isinstance(platforms, dict) else {}
        }
        self._mtime = mtime
        return self._cache

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._cache = data
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None

    def _platform_entry(self, data: Dict[str, Any], platform: str) -> Dict[str, Any]:
        entry = data["platforms"].setdefault(platform, {})
        entry.setdefault("agents", [])
        entry.setdefault("config", {})
        return entry

    # -- reads -------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._load())

    def platform_agents(self, platform: str) -> List[Dict[str, Any]]:
        with self._lock:
            data = self._load()
            entry = data["platforms"].get(platform) or {}
            agents = entry.get("agents") or []
            return copy.deepcopy([a for a in agents if isinstance(a, dict)])

    def platform_config(self, platform: str) -> Dict[str, Any]:
        with self._lock:
            data = self._load()
            entry = data["platforms"].get(platform) or {}
            config = entry.get("config") or {}
            return copy.deepcopy(config if isinstance(config, dict) else {})

    def agent_platforms(self, agent_id: str) -> List[Dict[str, Any]]:
        """Return publish states of one agent across all platforms."""
        result: List[Dict[str, Any]] = []
        with self._lock:
            data = self._load()
            for platform, entry in data["platforms"].items():
                for item in entry.get("agents") or []:
                    if isinstance(item, dict) and item.get("agent_id") == agent_id:
                        record = copy.deepcopy(item)
                        record["platform"] = platform
                        result.append(record)
        return result

    # -- writes ------------------------------------------------------

    def publish(self, platform: str, agent_id: str) -> Dict[str, Any]:
        """Bind one agent to a platform, replacing any previous binding.

        A platform connects to exactly one agent; publishing a new agent
        supersedes the old one. Publishing keeps the binding enabled.
        """
        ensure_supported(platform)
        with self._lock:
            data = copy.deepcopy(self._load())
            entry = self._platform_entry(data, platform)
            record = {"agent_id": agent_id, "enabled": True}
            entry["agents"] = [record]
            self._save(data)
            return copy.deepcopy(record)

    def bound_agent(self, platform: str) -> Optional[Dict[str, Any]]:
        """Return the single agent bound to a platform, if any."""
        agents = self.platform_agents(platform)
        return agents[0] if agents else None

    def set_agent_enabled(self, platform: str, agent_id: str, enabled: bool) -> bool:
        ensure_supported(platform)
        with self._lock:
            data = copy.deepcopy(self._load())
            entry = self._platform_entry(data, platform)
            for item in entry["agents"]:
                if item.get("agent_id") == agent_id:
                    item["enabled"] = bool(enabled)
                    self._save(data)
                    return True
            return False

    def remove_agent(self, platform: str, agent_id: str) -> bool:
        ensure_supported(platform)
        with self._lock:
            data = copy.deepcopy(self._load())
            entry = self._platform_entry(data, platform)
            agents = entry["agents"]
            remaining = [a for a in agents if a.get("agent_id") != agent_id]
            if len(remaining) == len(agents):
                return False
            entry["agents"] = remaining
            self._save(data)
            return True

    def set_platform_config(self, platform: str, config: Dict[str, Any]) -> None:
        ensure_supported(platform)
        with self._lock:
            data = copy.deepcopy(self._load())
            entry = self._platform_entry(data, platform)
            entry["config"] = dict(config)
            self._save(data)


@dataclass(frozen=True)
class ResolveResult:
    """Which agent should answer an inbound message on a platform.

    ``agent_id`` is the bound agent (None means no active binding, so the
    platform should not answer). ``reply`` is reserved for future direct
    replies and is currently always None.
    """

    agent_id: Optional[str] = None
    reply: Optional[str] = None


class AgentBindingResolver:
    """Resolve the single agent bound to a platform.

    One platform connects to one agent; there is no keyword routing or
    per-conversation switching. A binding is active only when it is enabled
    and its agent still exists and is enabled.
    """

    def __init__(self, store: PublishStore) -> None:
        self.store = store

    def resolve(
        self,
        platform: str,
        conversation_key: str,
        text: str,
        valid_agents: Mapping[str, Any],
    ) -> ResolveResult:
        item = self.store.bound_agent(platform)
        if item is None or not item.get("enabled", True):
            return ResolveResult()
        agent_id = str(item.get("agent_id") or "")
        preset = valid_agents.get(agent_id)
        if preset is None or not getattr(preset, "enabled", True):
            return ResolveResult()
        return ResolveResult(agent_id=agent_id)

