from __future__ import annotations

import io
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from pypdf import PdfWriter

from src.core.config.loader import OcrConfig, load_project_config
from src.core.integrations.paddle_ocr import (
    PaddleOcrError,
    PaddleOcrProcess,
    paddle_ocr_availability,
)
from src.core.services.ocr import OcrError, OcrService
from src.core.tooling import ToolRuntime


SOURCE_CONFIG = Path(__file__).resolve().parents[1] / "config"


class FakeWorker:
    def __init__(self) -> None:
        self.requests = []
        self.closed = False
        self.pages = [{"page_index": 0, "text": "你好\nHello"}]

    def availability(self):
        return True, ""

    def recognize(self, request):
        self.requests.append(request)
        return list(self.pages)

    def close(self):
        self.closed = True


def image_bytes(image_format: str = "PNG", size=(24, 16)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, format=image_format)
    return output.getvalue()


class OcrServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = FakeWorker()
        self.config = OcrConfig(
            enabled=True,
            max_input_bytes=1024 * 1024,
            max_pdf_pages=2,
            max_image_pixels=10_000,
            max_output_chars=12,
        )
        self.service = OcrService(self.config, worker=self.worker)

    def test_image_bytes_are_normalized_and_result_is_truncated(self) -> None:
        result = self.service.recognize_image_bytes(image_bytes("JPEG"))
        self.assertEqual(result.media_type, "image/jpeg")
        self.assertEqual(result.text, "你好\nHello")
        self.assertFalse(result.truncated)
        request = self.worker.requests[0]
        self.assertEqual(request["kind"], "image")
        self.assertTrue(request["data"].startswith(b"\x89PNG"))

        self.worker.pages = [{"page_index": 0, "text": "x" * 20}]
        truncated = self.service.recognize_image_bytes(image_bytes())
        self.assertEqual(truncated.text, "x" * 12)
        self.assertEqual(truncated.char_count, 20)
        self.assertTrue(truncated.truncated)

    def test_image_limits_damage_and_extension_spoofing_are_rejected(self) -> None:
        with self.assertRaisesRegex(OcrError, "像素数"):
            self.service.recognize_image_bytes(image_bytes(size=(101, 101)))
        with self.assertRaisesRegex(OcrError, "损坏"):
            self.service.recognize_image_bytes(b"not-image")
        with tempfile.TemporaryDirectory() as directory:
            spoofed = Path(directory) / "photo.jpg"
            spoofed.write_bytes(image_bytes("PNG"))
            with self.assertRaisesRegex(OcrError, "扩展名"):
                self.service.recognize_path(spoofed)

    def test_pdf_is_preflighted_and_page_text_is_joined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "two-pages.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.add_blank_page(width=100, height=100)
            with path.open("wb") as output:
                writer.write(output)
            self.worker.pages = [
                {"page_index": 0, "text": "第一页"},
                {"page_index": 1, "text": "第二页"},
            ]
            result = self.service.recognize_path(path)
        self.assertEqual(result.media_type, "application/pdf")
        self.assertEqual(result.page_count, 2)
        self.assertIn("【第 1 页】", result.text)
        self.assertEqual(self.worker.requests[-1]["kind"], "pdf")

    def test_pdf_page_limit_and_fake_pdf_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            too_many = root / "large.pdf"
            writer = PdfWriter()
            for _index in range(3):
                writer.add_blank_page(width=100, height=100)
            with too_many.open("wb") as output:
                writer.write(output)
            with self.assertRaisesRegex(OcrError, "超过单次"):
                self.service.recognize_path(too_many)
            fake = root / "fake.pdf"
            fake.write_bytes(b"not a pdf")
            with self.assertRaisesRegex(OcrError, "扩展名"):
                self.service.recognize_path(fake)

    def test_close_delegates_to_worker(self) -> None:
        self.service.close()
        self.assertTrue(self.worker.closed)


class OcrToolTests(unittest.TestCase):
    def test_tool_uses_runtime_path_isolation_and_returns_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            path = root / "scan.png"
            path.write_bytes(image_bytes())
            tool_config = load_project_config(SOURCE_CONFIG).tools
            from dataclasses import replace

            tool_config = replace(
                tool_config,
                default_working_directory=str(root),
                allowed_roots=[str(root)],
            )
            worker = FakeWorker()
            service = OcrService(tool_config.ocr, worker=worker)
            runtime = ToolRuntime(
                tool_config,
                "Asia/Shanghai",
                ocr_service=service,
            )
            self.assertTrue(runtime.is_available("ocr_extract_text"))
            result = runtime.execute("ocr_extract_text", {"path": "scan.png"})
            self.assertTrue(result.ok)
            self.assertEqual(result.data["text"], "你好\nHello")
            escaped = runtime.execute(
                "ocr_extract_text", {"path": "../outside.png"}
            )
            self.assertFalse(escaped.ok)


class PaddleOcrProcessTests(unittest.TestCase):
    class FakeQueue:
        def __init__(self, responses=None) -> None:
            self.responses = list(responses or [])
            self.items = []

        def get(self, timeout=None):
            if not self.responses:
                raise queue.Empty
            return self.responses.pop(0)

        def put(self, value):
            self.items.append(value)

        def put_nowait(self, value):
            self.put(value)

    class FakeProcess:
        def __init__(self) -> None:
            self.alive = False
            self.terminated = False
            self.start_count = 0

        def start(self):
            self.alive = True
            self.start_count += 1

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            return None

        def terminate(self):
            self.alive = False
            self.terminated = True

        def close(self):
            return None

    class FakeContext:
        def __init__(self, response_queue, process) -> None:
            self.request_queue = PaddleOcrProcessTests.FakeQueue()
            self.response_queue = response_queue
            self.process = process
            self.queue_count = 0

        def Queue(self, maxsize=0):
            self.queue_count += 1
            return (
                self.request_queue
                if self.queue_count == 1
                else self.response_queue
            )

        def Process(self, **_kwargs):
            return self.process

    def process_with(self, responses):
        config = OcrConfig(
            enabled=True,
            model_directory="/tmp/unused-ocr-models",
            startup_timeout_seconds=1,
            request_timeout_seconds=1,
        )
        process = PaddleOcrProcess(config)
        fake_process = self.FakeProcess()
        response_queue = self.FakeQueue(responses)
        process._context = self.FakeContext(response_queue, fake_process)
        process.availability = lambda: (True, "")
        return process, fake_process

    def test_worker_is_started_once_and_reused(self) -> None:
        process, fake_process = self.process_with([
            {"type": "ready"},
            {"type": "result", "id": "one", "pages": [{"text": "一"}]},
            {"type": "result", "id": "two", "pages": [{"text": "二"}]},
        ])
        ids = [SimpleNamespace(hex="one"), SimpleNamespace(hex="two")]
        with patch(
            "src.core.integrations.paddle_ocr.uuid.uuid4",
            side_effect=ids,
        ):
            self.assertEqual(process.recognize({"kind": "image"})[0]["text"], "一")
            self.assertEqual(process.recognize({"kind": "image"})[0]["text"], "二")
        self.assertEqual(fake_process.start_count, 1)
        process.close()
        self.assertTrue(fake_process.terminated)

    def test_request_timeout_terminates_worker(self) -> None:
        process, fake_process = self.process_with([{"type": "ready"}])
        with self.assertRaisesRegex(PaddleOcrError, "超过 1 秒"):
            process.recognize({"kind": "image"})
        self.assertTrue(fake_process.terminated)
        self.assertIsNone(process._process)

    def test_availability_reports_missing_models_without_importing_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = OcrConfig(
                enabled=True,
                model_directory=directory,
            )
            with patch(
                "src.core.integrations.paddle_ocr.importlib.util.find_spec",
                return_value=object(),
            ):
                available, reason = paddle_ocr_availability(config)
        self.assertFalse(available)
        self.assertIn("模型尚未准备", reason)


if __name__ == "__main__":
    unittest.main()
