"""Standalone OCR configuration shared by the service and integration layers.

Kept in a dedicated module so both ``src.core.services.ocr`` and
``src.core.integrations.paddle_ocr`` can import it without forming an import
cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OcrConfig:
    enabled: bool = False
    auto_process_chat_images: bool = True
    engine: str = "paddleocr"
    device: str = "cpu"
    model_tier: str = "small"
    model_directory: str = ""
    max_input_bytes: int = 20 * 1024 * 1024
    max_pdf_pages: int = 10
    max_image_pixels: int = 25_000_000
    max_output_chars: int = 20_000
    startup_timeout_seconds: int = 120
    request_timeout_seconds: int = 60
