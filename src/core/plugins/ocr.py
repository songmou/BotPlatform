"""OCR document recognition exposed as an in-process platform plugin."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from src.core.paths import SYSTEM_DATA_DIR
from src.core.services.ocr import OcrError, OcrResult, OcrService
from src.core.services.ocr_config import OcrConfig

from .base import PluginContext, PluginError, PluginToolDefinition


_OCR_SETTING_KEYS = (
    "auto_process_chat_images",
    "engine",
    "device",
    "model_tier",
    "max_input_bytes",
    "max_pdf_pages",
    "max_image_pixels",
    "max_output_chars",
    "startup_timeout_seconds",
    "request_timeout_seconds",
)


def build_config(settings: Mapping[str, Any]) -> OcrConfig:
    """Build an enabled OcrConfig from plugin settings and fixed defaults."""
    overrides = {key: settings[key] for key in _OCR_SETTING_KEYS if key in settings}
    return OcrConfig(
        enabled=True,
        model_directory=str(SYSTEM_DATA_DIR / "ocr_models"),
        **overrides,
    )


class OcrPlugin:
    """Recognize text from workspace files and inbound chat images."""

    id = "ocr"
    TOOL_DEFINITIONS: Dict[str, PluginToolDefinition] = {
        "ocr_extract_text": PluginToolDefinition(
            description=(
                "识别当前用户 workspace 内图片或 PDF 中的文字。"
                "支持 JPEG、PNG、WebP、BMP、GIF 首帧和最多 10 页的 PDF。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "workspace 内的文件路径"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
    }

    def __init__(
        self,
        settings: Mapping[str, Any],
        context: Optional[PluginContext] = None,
    ) -> None:
        if context is None or context.tenant_registry is None:
            raise ValueError("ocr 缺少 tenant_storage 平台服务")
        self._config = build_config(settings)
        self._service = OcrService(self._config)
        self._tenant_registry = context.tenant_registry

    @property
    def tool_definitions(self) -> Mapping[str, PluginToolDefinition]:
        return self.TOOL_DEFINITIONS

    def is_available(self, tool_name: str) -> bool:
        return tool_name in self.TOOL_DEFINITIONS and self._service.available

    @property
    def auto_chat_images(self) -> bool:
        return self._config.auto_process_chat_images

    def availability(self) -> tuple[bool, str]:
        return self._service.availability()

    def recognize_chat_image(self, data: bytes) -> OcrResult:
        return self._service.recognize_image_bytes(data)

    def _resolve_workspace_path(self, raw_path: str, tenant: Any) -> Path:
        tenant_id = str(getattr(tenant, "tenant_id", "") or "")
        if not tenant_id:
            raise PluginError("OCR 工具需要租户身份")
        workspace = (
            self._tenant_registry.tenant_root(tenant_id) / "workspace"
        ).resolve()
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            candidate = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PluginError("OCR 目标文件不存在") from exc
        if workspace not in candidate.parents:
            raise PluginError("OCR 文件必须位于当前租户 workspace 内")
        return candidate

    def execute(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> Any:
        if tool_name != "ocr_extract_text":
            raise PluginError("未知 OCR 工具：{}".format(tool_name))
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise PluginError("path 必须是非空字符串")
        path = self._resolve_workspace_path(raw_path.strip(), tenant)
        try:
            return self._service.recognize_path(path).payload()
        except OcrError as exc:
            raise PluginError(str(exc)) from exc

    def preview(self, tool_name: str, arguments: Dict[str, Any], tenant: Any) -> str:
        return "识别文件 {} 中的文字".format(arguments.get("path", ""))

    def close_tenant(self, tenant_id: str) -> None:
        pass

    def close(self) -> None:
        self._service.close()


def prepare_components(
    settings: Mapping[str, Any], log: Callable[[str], None]
) -> None:
    """Download the OCR models; invoked by the plugin setup service."""
    from src.core.integrations.paddle_ocr import prepare_models

    config = build_config(settings)
    log("准备 OCR 模型目录：{}".format(config.model_directory))
    prepare_models(config)
    log("OCR 模型准备完成")
