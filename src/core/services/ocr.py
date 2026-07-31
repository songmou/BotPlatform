"""Validated OCR orchestration shared by tools and inbound image handling."""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from src.core.config.loader import OcrConfig
from src.core.integrations.paddle_ocr import PaddleOcrError, PaddleOcrProcess


IMAGE_FORMAT_MEDIA_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "GIF": "image/gif",
}
IMAGE_SUFFIX_FORMATS = {
    ".jpg": {"JPEG"},
    ".jpeg": {"JPEG"},
    ".png": {"PNG"},
    ".webp": {"WEBP"},
    ".bmp": {"BMP"},
    ".gif": {"GIF"},
}


class OcrError(RuntimeError):
    """A safe, user-readable OCR validation or execution failure."""


@dataclass(frozen=True)
class OcrResult:
    text: str
    media_type: str
    page_count: int
    processed_pages: int
    char_count: int
    truncated: bool
    language: str = "zh-Hans+en"

    def payload(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "media_type": self.media_type,
            "page_count": self.page_count,
            "processed_pages": self.processed_pages,
            "char_count": self.char_count,
            "truncated": self.truncated,
            "language": self.language,
        }


class OcrService:
    def __init__(
        self,
        config: OcrConfig,
        worker: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.worker = worker or PaddleOcrProcess(config)

    def availability(self) -> Tuple[bool, str]:
        if not self.config.enabled:
            return False, "OCR 未启用"
        checker = getattr(self.worker, "availability", None)
        if not callable(checker):
            return True, ""
        return checker()

    @property
    def available(self) -> bool:
        return self.availability()[0]

    def _check_size(self, size: int) -> None:
        if size <= 0:
            raise OcrError("OCR 输入为空")
        if size > self.config.max_input_bytes:
            raise OcrError(
                "OCR 文件大小 {} 字节，超过 {} 字节上限".format(
                    size, self.config.max_input_bytes
                )
            )

    def _normalize_image(
        self,
        data: bytes,
        expected_suffix: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        self._check_size(len(data))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    image_format = str(image.format or "").upper()
                    if image_format not in IMAGE_FORMAT_MEDIA_TYPES:
                        raise OcrError(
                            "不支持的图片格式，仅支持 JPEG、PNG、WebP、BMP 和 GIF"
                        )
                    if expected_suffix is not None:
                        expected = IMAGE_SUFFIX_FORMATS.get(expected_suffix.lower())
                        if expected is None:
                            raise OcrError("不支持的 OCR 文件扩展名：{}".format(
                                expected_suffix
                            ))
                        if image_format not in expected:
                            raise OcrError("图片扩展名与实际格式不一致")
                    image.seek(0)
                    normalized = ImageOps.exif_transpose(image).convert("RGB")
                    width, height = normalized.size
                    if width * height > self.config.max_image_pixels:
                        raise OcrError(
                            "图片像素数超过 {} 上限".format(
                                self.config.max_image_pixels
                            )
                        )
                    output = io.BytesIO()
                    normalized.save(output, format="PNG")
                    return output.getvalue(), IMAGE_FORMAT_MEDIA_TYPES[image_format]
        except OcrError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise OcrError("图片像素尺寸过大") from exc
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            raise OcrError("图片损坏或不是有效图片") from exc

    def _validate_pdf(self, path: Path) -> int:
        self._check_size(path.stat().st_size)
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                raise OcrError("不支持加密 PDF")
            page_count = len(reader.pages)
        except OcrError:
            raise
        except (PdfReadError, OSError, ValueError) as exc:
            raise OcrError("PDF 损坏或无法读取") from exc
        if page_count <= 0:
            raise OcrError("PDF 不包含页面")
        if page_count > self.config.max_pdf_pages:
            raise OcrError(
                "PDF 共 {} 页，超过单次 {} 页上限，请拆分后重试".format(
                    page_count, self.config.max_pdf_pages
                )
            )
        return page_count

    def _build_result(
        self,
        pages: List[Dict[str, Any]],
        media_type: str,
        page_count: int,
    ) -> OcrResult:
        texts: List[str] = []
        for index, page in enumerate(pages):
            text = str(page.get("text") or "").strip()
            if page_count > 1:
                texts.append("【第 {} 页】\n{}".format(index + 1, text))
            elif text:
                texts.append(text)
        full_text = "\n\n".join(texts).strip()
        original_chars = len(full_text)
        truncated = original_chars > self.config.max_output_chars
        if truncated:
            full_text = full_text[: self.config.max_output_chars]
        return OcrResult(
            text=full_text,
            media_type=media_type,
            page_count=page_count,
            processed_pages=len(pages),
            char_count=original_chars,
            truncated=truncated,
        )

    def recognize_image_bytes(self, data: bytes) -> OcrResult:
        normalized, media_type = self._normalize_image(data)
        try:
            pages = self.worker.recognize({"kind": "image", "data": normalized})
        except PaddleOcrError as exc:
            raise OcrError(str(exc)) from exc
        return self._build_result(pages, media_type, 1)

    def recognize_path(self, path: Path) -> OcrResult:
        if not path.is_file():
            raise OcrError("OCR 目标不是普通文件")
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            with path.open("rb") as source:
                if source.read(5) != b"%PDF-":
                    raise OcrError("PDF 扩展名与实际格式不一致")
            page_count = self._validate_pdf(path)
            try:
                pages = self.worker.recognize(
                    {"kind": "pdf", "path": str(path)}
                )
            except PaddleOcrError as exc:
                raise OcrError(str(exc)) from exc
            return self._build_result(pages, "application/pdf", page_count)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise OcrError("无法读取 OCR 文件") from exc
        normalized, media_type = self._normalize_image(data, suffix)
        try:
            pages = self.worker.recognize({"kind": "image", "data": normalized})
        except PaddleOcrError as exc:
            raise OcrError(str(exc)) from exc
        return self._build_result(pages, media_type, 1)

    def close(self) -> None:
        closer = getattr(self.worker, "close", None)
        if callable(closer):
            closer()
