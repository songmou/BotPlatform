"""Startup configuration diagnostics with required and optional results."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Mapping, Optional, TextIO

import httpx

from src.core.config.loader import ConfigError, ProjectConfig, load_project_config


PLACEHOLDERS = {"YOUR_OLLAMA_MODEL", "CHANGE_ME", "YOUR_MODEL"}


@dataclass(frozen=True)
class Diagnostic:
    message: str
    path: Optional[Path] = None


@dataclass
class PreflightReport:
    config: Optional[ProjectConfig] = None
    ready: List[Diagnostic] = field(default_factory=list)
    warnings: List[Diagnostic] = field(default_factory=list)
    errors: List[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _ollama_models(base_url: str, timeout: float = 3.0) -> List[str]:
    response = httpx.get(
        base_url.rstrip("/") + "/api/tags", timeout=timeout, trust_env=False
    )
    response.raise_for_status()
    payload = response.json()
    return [
        str(item.get("name") or item.get("model"))
        for item in payload.get("models", [])
        if isinstance(item, dict) and (item.get("name") or item.get("model"))
    ]


def _browser_runtime() -> Optional[str]:
    """Return the browser runtime selected by the same order as the plugin."""
    try:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        try:
            bundled = Path(str(playwright.chromium.executable_path)).expanduser()
            if bundled.is_file():
                return "Playwright Chromium"
        finally:
            playwright.stop()
    except Exception:
        pass
    for label, candidate in (
        ("Google Chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ("Microsoft Edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ):
        if Path(candidate).is_file():
            return label
    return None


def check_configuration(
    config_dir: Path,
    *,
    environment: Optional[Mapping[str, str]] = None,
) -> PreflightReport:
    report = PreflightReport()
    env = os.environ if environment is None else environment
    try:
        config = load_project_config(config_dir)
    except ConfigError as exc:
        report.errors.append(Diagnostic(str(exc), config_dir))
        return report
    report.config = config
    report.ready.append(Diagnostic("JSON 配置结构和引用关系有效", config_dir))
    from src.core.messaging.credentials import ChannelCredentialStore

    credential_store = ChannelCredentialStore()
    capability_labels = {
        "wechat_ilink": "私聊、文字、图片、输入状态、主动通知",
        "wecom_aibot": "私聊、群聊 @、文字、图片、主动通知",
        "feishu": "私聊、群聊 @、文字、图片、主动通知",
    }
    for channel in config.channels.values():
        target = config_dir / "channels.json"
        if channel.enabled:
            destination = (
                report.ready
                if credential_store.configured(channel.id)
                else report.warnings
            )
            suffix = (
                ""
                if credential_store.configured(channel.id)
                else "；尚未配置平台凭据"
            )
            destination.append(
                Diagnostic(
                    "消息渠道 {} 已启用：{}（{}）{}".format(
                        channel.id,
                        channel.type,
                        capability_labels.get(channel.type, "能力由适配器声明"),
                        suffix,
                    ),
                    target,
                )
            )
        else:
            report.warnings.append(
                Diagnostic("消息渠道 {} 未启用".format(channel.id), target)
            )

    active = config.models[config.app.active_model]
    fallback = config.models[config.app.fallback_model]
    if not fallback.enabled:
        report.errors.append(
            Diagnostic(
                "兜底模型档案 {} 未启用".format(fallback.id),
                config_dir / "models.json",
            )
        )

    checked_key_names = set()
    for profile in config.models.values():
        if not profile.enabled:
            continue
        if profile.model.upper() in PLACEHOLDERS or profile.model.startswith("YOUR_"):
            report.errors.append(
                Diagnostic(
                    "已启用模型档案 {} 仍使用占位模型名".format(profile.id),
                    config_dir / "models.json",
                )
            )
            continue
        if profile.type == "openai_compatible":
            key_name = profile.api_key_env or ""
            if key_name in checked_key_names:
                continue
            checked_key_names.add(key_name)
            if not key_name or not env.get(key_name):
                report.errors.append(
                    Diagnostic(
                        "模型档案 {} 缺少环境变量 {}；请创建 data/system/model.env".format(
                            profile.id, key_name or "API_KEY"
                        ),
                        config_dir / "models.json",
                    )
                )
            else:
                report.ready.append(
                    Diagnostic(
                        "模型档案 {} 已配置密钥环境变量 {}（未发起计费请求）".format(
                            profile.id, key_name
                        ),
                        config_dir / "models.json",
                    )
                )
        elif profile.type == "ollama":
            required_ollama = profile.id in {
                config.app.active_model,
                config.app.fallback_model,
            }
            try:
                models = _ollama_models(profile.base_url)
            except (httpx.HTTPError, ValueError, TypeError):
                target = report.errors if required_ollama else report.warnings
                target.append(
                    Diagnostic(
                        "Ollama 档案 {} 已启用，但服务不可访问；请先启动 Ollama".format(
                            profile.id
                        ),
                        config_dir / "models.json",
                    )
                )
            else:
                if profile.model not in models:
                    target = report.errors if required_ollama else report.warnings
                    target.append(
                        Diagnostic(
                            "Ollama 中未找到模型 {}；请先执行 ollama pull {}".format(
                                profile.model, profile.model
                            ),
                            config_dir / "models.json",
                        )
                    )
                else:
                    report.ready.append(
                        Diagnostic(
                            "Ollama 档案 {} 可用".format(profile.id),
                            config_dir / "models.json",
                        )
                    )

    if not config.models[config.app.local_model].enabled:
        report.warnings.append(
            Diagnostic(
                "本地模型未启用；/model local、默认图片识别和自动长期记忆提取不可用",
                config_dir / "models.json",
            )
        )
    elif config.models[config.app.local_model].type != "ollama":
        report.errors.append(
            Diagnostic("local_model 必须引用 Ollama 档案", config_dir / "app.json")
        )
    if not config.models[config.app.vision_model].enabled:
        report.warnings.append(
            Diagnostic("图片模型未启用；微信图片消息不可用", config_dir / "app.json")
        )
    elif not config.models[config.app.vision_model].capabilities.vision:
        report.errors.append(
            Diagnostic("vision_model 必须启用 vision 能力", config_dir / "models.json")
        )
    for mode_name, profile_id in (
        ("flash", config.app.flash_model),
        ("pro", config.app.pro_model),
    ):
        if not config.models[profile_id].enabled:
            report.warnings.append(
                Diagnostic(
                    "/model {} 对应档案未启用".format(mode_name),
                    config_dir / "models.json",
                )
            )

    embedding_id = config.app.embedding_model
    if not embedding_id:
        report.warnings.append(
            Diagnostic(
                "未绑定向量模型；知识库将使用 FTS5 全文检索",
                config_dir / "app.json",
            )
        )
    else:
        embedding = config.models.get(embedding_id)
        if embedding is None:
            report.errors.append(
                Diagnostic(
                    "embedding_model 引用了不存在的档案 {}".format(embedding_id),
                    config_dir / "app.json",
                )
            )
        elif not embedding.enabled:
            report.warnings.append(
                Diagnostic(
                    "向量模型 {} 未启用；知识库将使用 FTS5 全文检索".format(embedding_id),
                    config_dir / "models.json",
                )
            )
        elif embedding.type == "ollama":
            try:
                models = _ollama_models(embedding.base_url)
            except (httpx.HTTPError, ValueError, TypeError):
                report.warnings.append(
                    Diagnostic(
                        "向量模型已启用但 Ollama 服务不可访问；知识库将降级为全文检索",
                        config_dir / "models.json",
                    )
                )
            else:
                if embedding.model in models:
                    report.ready.append(
                        Diagnostic(
                            "向量模型 {} 可用".format(embedding.model),
                            config_dir / "models.json",
                        )
                    )
                else:
                    report.warnings.append(
                        Diagnostic(
                            "Ollama 中未找到向量模型 {}；知识库将降级为全文检索".format(
                                embedding.model
                            ),
                            config_dir / "models.json",
                        )
                    )
        else:
            report.ready.append(
                Diagnostic(
                    "向量模型 {} 已配置（{}）".format(embedding.model, embedding.type),
                    config_dir / "models.json",
                )
            )

    rerank_id = config.app.rerank_model
    if rerank_id:
        rerank = config.models.get(rerank_id)
        if rerank is None:
            report.errors.append(
                Diagnostic(
                    "rerank_model 引用了不存在的档案 {}".format(rerank_id),
                    config_dir / "app.json",
                )
            )
        elif not rerank.enabled:
            report.warnings.append(
                Diagnostic(
                    "重排模型 {} 未启用；知识库检索将跳过重排".format(rerank_id),
                    config_dir / "models.json",
                )
            )
        else:
            report.ready.append(
                Diagnostic(
                    "重排模型 {} 已配置".format(rerank.model),
                    config_dir / "models.json",
                )
            )

    if config.tools.ocr.enabled:
        from src.core.integrations.paddle_ocr import paddle_ocr_availability

        ocr_available, ocr_reason = paddle_ocr_availability(config.tools.ocr)
        target = config_dir / "tools.json"
        if ocr_available:
            report.ready.append(Diagnostic("本地 OCR 依赖和模型可用", target))
        else:
            report.warnings.append(
                Diagnostic("本地 OCR 已启用但不可用：{}".format(ocr_reason), target)
            )

    for plugin_id, plugin in config.plugins.items():
        if not plugin.enabled:
            report.warnings.append(
                Diagnostic("插件 {} 未启用".format(plugin_id), config_dir / "plugins.json")
            )
            continue
        if plugin_id == "browser_automation":
            explicit = env.get("BROWSER_EXECUTABLE")
            runtime = (
                "显式浏览器"
                if explicit and Path(explicit).expanduser().is_file()
                else _browser_runtime()
                or ("Chromium" if shutil.which("chromium") else None)
                or ("Google Chrome" if shutil.which("google-chrome") else None)
            )
            target = config_dir / "plugins.json"
            if runtime:
                report.ready.append(Diagnostic("浏览器运行环境可用：{}".format(runtime), target))
            else:
                report.warnings.append(
                    Diagnostic(
                        "浏览器插件已启用；请确认 Playwright Chromium 或系统浏览器已安装",
                        target,
                    )
                )

    report.ready.append(
        Diagnostic(
            "默认文字模型：{} / {}".format(active.provider, active.model),
            config_dir / "app.json",
        )
    )
    return report


def print_report(report: PreflightReport, stream: TextIO) -> None:
    groups = (
        ("可用", report.ready),
        ("需配置（不阻止核心功能）", report.warnings),
        ("错误（启动已阻止）", report.errors),
    )
    print("环境与配置检查：", file=stream)
    for title, items in groups:
        print("\n{}：".format(title), file=stream)
        if not items:
            print("- 无", file=stream)
            continue
        for item in items:
            suffix = " [{}]".format(item.path) if item.path else ""
            print("- {}{}".format(item.message, suffix), file=stream)
