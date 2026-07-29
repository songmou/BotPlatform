"""Per-turn model selection, local failover, and cooldown routing."""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional

from .contracts import (
    ModelCapabilities,
    ModelClient,
    ModelError,
    ModelIdentity,
    ModelRequest,
    ModelResponse,
)
from .retry import complete_with_retry


FallbackLogger = Callable[[ModelIdentity, ModelIdentity, str], None]


class ModelSession:
    """Keep one conversation/tool turn on a stable model after failover."""

    def __init__(
        self,
        router: "ModelRouter",
        mode: str,
        *,
        has_image: bool = False,
        start_profile_id: Optional[str] = None,
    ) -> None:
        self._router = router
        self.mode = router.normalize_mode(mode)
        self.has_image = has_image
        self._profile_id = start_profile_id or router.initial_profile(
            self.mode, has_image=has_image
        )

    @property
    def identity(self) -> ModelIdentity:
        return self._router.clients[self._profile_id].identity

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._router.clients[self._profile_id].capabilities

    @property
    def profile_id(self) -> str:
        return self._profile_id

    def ensure_ready(self) -> None:
        self._router.clients[self._profile_id].ensure_ready()

    def complete(self, request: ModelRequest) -> ModelResponse:
        client = self._router.clients[self._profile_id]
        try:
            return complete_with_retry(
                lambda: client.complete(request),
                profile_id=self._profile_id,
                sleep=self._router.retry_sleep,
            )
        except ModelError as exc:
            if not self._router.can_fail_over(
                self.mode, self._profile_id, has_image=self.has_image
            ):
                raise
            source = client.identity
            self._router.record_primary_failure(exc)
            self._profile_id = self._router.fallback_profile_id
            target = self._router.clients[self._profile_id]
            self._router.log_fallback(source, target.identity, exc.safe_message)
            return complete_with_retry(
                lambda: target.complete(request),
                profile_id=self._profile_id,
                sleep=self._router.retry_sleep,
            )

    def close(self) -> None:
        return None


class ModelRouter:
    """Own configured clients and provide isolated per-turn model sessions."""

    MODES = {"auto", "local", "flash", "pro"}

    def __init__(
        self,
        clients: Dict[str, ModelClient],
        *,
        primary_profile_id: str,
        fallback_profile_id: str,
        local_profile_id: Optional[str] = None,
        flash_profile_id: Optional[str] = None,
        pro_profile_id: Optional[str] = None,
        vision_profile_id: Optional[str] = None,
        cooldown_seconds: int = 60,
        fallback_logger: Optional[FallbackLogger] = None,
        monotonic: Callable[[], float] = time.monotonic,
        retry_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if primary_profile_id not in clients:
            raise ValueError("缺少主模型档案 {}".format(primary_profile_id))
        if fallback_profile_id not in clients:
            raise ValueError("缺少兜底模型档案 {}".format(fallback_profile_id))
        self.clients = dict(clients)
        self.primary_profile_id = primary_profile_id
        self.fallback_profile_id = fallback_profile_id
        self.local_profile_id = local_profile_id or (
            "ollama_local" if "ollama_local" in clients else primary_profile_id
        )
        self.flash_profile_id = flash_profile_id or fallback_profile_id
        self.pro_profile_id = pro_profile_id or (
            "deepseek_pro" if "deepseek_pro" in clients else primary_profile_id
        )
        self.vision_profile_id = vision_profile_id or next(
            (
                profile_id
                for profile_id, client in clients.items()
                if client.capabilities.vision
            ),
            None,
        )
        self.cooldown_seconds = cooldown_seconds
        self._fallback_logger = fallback_logger
        self._monotonic = monotonic
        self.retry_sleep = retry_sleep
        self._cooldown_until = 0.0
        self._last_primary_error: Optional[str] = None
        self._lock = threading.RLock()

    @classmethod
    def single(cls, client: ModelClient) -> "ModelRouter":
        profile_id = client.identity.profile_id
        return cls(
            {profile_id: client},
            primary_profile_id=profile_id,
            fallback_profile_id=profile_id,
            local_profile_id=profile_id,
            flash_profile_id=profile_id,
            pro_profile_id=profile_id,
            vision_profile_id=(profile_id if client.capabilities.vision else None),
            cooldown_seconds=1,
        )

    @property
    def identity(self) -> ModelIdentity:
        return self.clients[self.primary_profile_id].identity

    @property
    def capabilities(self) -> ModelCapabilities:
        return self.clients[self.primary_profile_id].capabilities

    @property
    def cooling_down(self) -> bool:
        with self._lock:
            return self._monotonic() < self._cooldown_until

    @property
    def last_primary_error(self) -> Optional[str]:
        with self._lock:
            return self._last_primary_error

    def normalize_mode(self, mode: str) -> str:
        normalized = str(mode or "auto").strip().lower()
        if normalized not in self.MODES:
            raise ModelError("未知模型模式：{}".format(mode), provider="router")
        return normalized

    def _configured_profile(self, profile_id: Optional[str], label: str) -> str:
        if not profile_id or profile_id not in self.clients:
            raise ModelError(
                "{}模型尚未启用，请修改 config/models.json 后重新启动".format(label),
                provider="router",
            )
        return profile_id

    def initial_profile(self, mode: str, *, has_image: bool = False) -> str:
        mode = self.normalize_mode(mode)
        if has_image:
            profile_id = self._configured_profile(self.vision_profile_id, "图片")
            if not self.clients[profile_id].capabilities.vision:
                raise ModelError(
                    "图片模型档案未启用图片能力，请检查 config/models.json",
                    provider="router",
                )
            if mode in {"flash", "pro"}:
                raise ModelError(
                    "当前模型模式不支持图片，请使用 /model auto 或 /model local",
                    provider="router",
                )
            if mode == "local" and profile_id != self.local_profile_id:
                raise ModelError("本地模型未配置图片能力", provider="router")
            return profile_id
        if mode == "local":
            return self._configured_profile(self.local_profile_id, "本地")
        if mode == "flash":
            return self._configured_profile(self.flash_profile_id, "Flash")
        if mode == "pro":
            return self._configured_profile(self.pro_profile_id, "Pro")
        if not has_image and self.cooling_down:
            return self.fallback_profile_id
        return self.primary_profile_id

    def session(
        self,
        mode: str = "auto",
        *,
        has_image: bool = False,
        start_profile_id: Optional[str] = None,
    ) -> ModelSession:
        if start_profile_id is not None and start_profile_id not in self.clients:
            raise ModelError(
                "待处理请求引用了不存在的模型档案", provider="router"
            )
        return ModelSession(
            self,
            mode,
            has_image=has_image,
            start_profile_id=start_profile_id,
        )

    def can_fail_over(self, mode: str, profile_id: str, *, has_image: bool) -> bool:
        return bool(
            mode == "auto"
            and not has_image
            and profile_id == self.primary_profile_id
            and self.fallback_profile_id != self.primary_profile_id
        )

    def record_primary_failure(self, exc: ModelError) -> None:
        with self._lock:
            self._last_primary_error = exc.safe_message
            self._cooldown_until = self._monotonic() + self.cooldown_seconds

    def log_fallback(
        self, source: ModelIdentity, target: ModelIdentity, reason: str
    ) -> None:
        if self._fallback_logger:
            self._fallback_logger(source, target, reason)

    def ensure_ready(self) -> None:
        try:
            self.clients[self.primary_profile_id].ensure_ready()
        except ModelError as exc:
            if self.fallback_profile_id == self.primary_profile_id:
                raise
            self.record_primary_failure(exc)
            self.log_fallback(
                self.clients[self.primary_profile_id].identity,
                self.clients[self.fallback_profile_id].identity,
                exc.safe_message,
            )

    def status_text(self, mode: str) -> str:
        mode = self.normalize_mode(mode)
        def label(profile_id: Optional[str], unavailable: str) -> str:
            if not profile_id or profile_id not in self.clients:
                return unavailable
            identity = self.clients[profile_id].identity
            return "{} / {}".format(identity.provider, identity.configured_model)

        labels = {
            "auto": label(self.primary_profile_id, "默认模型未配置"),
            "local": label(self.local_profile_id, "本地模型未启用"),
            "flash": label(self.flash_profile_id, "Flash 模型未启用"),
            "pro": label(self.pro_profile_id, "Pro 模型未启用"),
        }
        lines = [
            "当前模型模式：{}（{}）".format(mode, labels[mode]),
            "切换命令：/model auto | local | flash | pro",
            "图片仅支持已配置图片模型的 auto/local 模式。",
        ]
        if mode == "auto" and self.cooling_down:
            lines.append(
                "默认模型处于冷却期，当前文字请求将使用 {}。".format(
                    label(self.fallback_profile_id, "兜底模型")
                )
            )
        return "\n".join(lines)

    def close(self) -> None:
        closed = set()
        for client in self.clients.values():
            marker = id(client)
            if marker in closed:
                continue
            closed.add(marker)
            client.close()
