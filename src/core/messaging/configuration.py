"""Atomic management of the non-secret channel configuration file."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from src.core.config.loader import AgentPreset, ChannelConfig, _load_channels


class ChannelConfigurationError(ValueError):
    """Raised when a channel configuration update is invalid."""


class ChannelConfigurationStore:
    def __init__(
        self,
        path: Path,
        agents: Mapping[str, AgentPreset],
        default_agent: str,
    ) -> None:
        self.path = path
        self.agents = agents
        self.default_agent = default_agent

    def load(self) -> Dict[str, ChannelConfig]:
        try:
            channels = _load_channels(self.path)
        except Exception as exc:
            raise ChannelConfigurationError(str(exc)) from exc
        self._validate_agents(channels.values())
        return channels

    def _validate_agents(self, channels: Iterable[ChannelConfig]) -> None:
        for channel in channels:
            agent_id = channel.agent_id or self.default_agent
            agent = self.agents.get(agent_id)
            if agent is None:
                raise ChannelConfigurationError(
                    "渠道 {} 引用了未知 Agent：{}".format(
                        channel.id,
                        agent_id,
                    )
                )
            if not agent.enabled:
                raise ChannelConfigurationError(
                    "渠道 {} 引用了已停用 Agent：{}".format(
                        channel.id,
                        agent_id,
                    )
                )

    @staticmethod
    def _entry(config: ChannelConfig) -> Dict[str, Any]:
        value = asdict(config)
        if not value["agent_id"]:
            value.pop("agent_id")
        return value

    def _read_entries(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return [
                {
                    "id": "wechat-main",
                    "type": "wechat_ilink",
                    "enabled": True,
                    "settings": {"group_policy": "private_only"},
                }
            ]
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ChannelConfigurationError("渠道配置文件无效") from exc
        entries = payload.get("channels") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise ChannelConfigurationError("channels 必须是数组")
        return [dict(item) for item in entries if isinstance(item, dict)]

    def _validated_payload(
        self,
        entries: List[Dict[str, Any]],
    ) -> Dict[str, ChannelConfig]:
        payload = {"channels": entries}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".channels-validate-",
            suffix=".json",
            dir=str(self.path.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            channels = _load_channels(temporary)
            self._validate_agents(channels.values())
            return channels
        except Exception as exc:
            if isinstance(exc, ChannelConfigurationError):
                raise
            raise ChannelConfigurationError(str(exc)) from exc
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _write(
        self,
        entries: List[Dict[str, Any]],
    ) -> Dict[str, ChannelConfig]:
        channels = self._validated_payload(entries)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".channels-",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {"channels": entries},
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.write("\n")
            os.replace(str(temporary), str(self.path))
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        return channels

    def upsert(self, raw: Mapping[str, Any]) -> ChannelConfig:
        channel_id = str(raw.get("id") or "").strip()
        if not channel_id:
            raise ChannelConfigurationError("渠道实例编号不能为空")
        entries = self._read_entries()
        replacement = dict(raw)
        replaced = False
        for index, entry in enumerate(entries):
            if str(entry.get("id") or "") == channel_id:
                entries[index] = replacement
                replaced = True
                break
        if not replaced:
            entries.append(replacement)
        channels = self._write(entries)
        return channels[channel_id]

    def set_enabled(self, channel_id: str, enabled: bool) -> ChannelConfig:
        entries = self._read_entries()
        for entry in entries:
            if str(entry.get("id") or "") == channel_id:
                entry["enabled"] = enabled
                channels = self._write(entries)
                return channels[channel_id]
        raise ChannelConfigurationError("未知消息渠道：{}".format(channel_id))

    def remove(self, channel_id: str) -> None:
        entries = self._read_entries()
        remaining = [
            entry
            for entry in entries
            if str(entry.get("id") or "") != channel_id
        ]
        if len(remaining) == len(entries):
            raise ChannelConfigurationError(
                "未知消息渠道：{}".format(channel_id)
            )
        self._write(remaining)
