from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from src.core.plugins.base import PluginError
from src.core.plugins.manifest import load_manifest
from src.core.plugins.ocr import OcrPlugin, build_config
from src.core.services.ocr import OcrService


MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "core"
    / "plugins"
    / "bundled"
    / "ocr"
    / "plugin.json"
)


class FakeWorker:
    def __init__(self) -> None:
        self.available = True

    def availability(self):
        return True, ""

    def recognize(self, request):
        return [{"page_index": 0, "text": "你好\nHello"}]

    def close(self):
        pass


class FakeTenantRegistry:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def tenant_root(self, tenant_id: str) -> Path:
        return self._root / tenant_id


def image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (24, 16), "white").save(output, format="PNG")
    return output.getvalue()


class OcrManifestTests(unittest.TestCase):
    def test_manifest_declares_prepare_hook_and_tool(self) -> None:
        manifest = load_manifest(MANIFEST_PATH, "bundled")
        self.assertEqual(manifest.id, "ocr")
        self.assertEqual(
            manifest.prepare, "src.core.plugins.ocr:prepare_components"
        )
        self.assertIn("ocr_extract_text", manifest.tools)
        self.assertEqual(
            sorted(dep.distribution for dep in manifest.dependencies),
            ["paddleocr", "paddlepaddle"],
        )


class BuildConfigTests(unittest.TestCase):
    def test_settings_map_to_config_with_fixed_defaults(self) -> None:
        config = build_config({"model_tier": "medium", "max_pdf_pages": 3})
        self.assertTrue(config.enabled)
        self.assertEqual(config.model_tier, "medium")
        self.assertEqual(config.max_pdf_pages, 3)
        self.assertTrue(config.model_directory.endswith("ocr_models"))

    def test_unknown_settings_are_ignored(self) -> None:
        config = build_config({"model_directory": "/evil", "enabled": False})
        # Fixed fields cannot be overridden by settings.
        self.assertTrue(config.enabled)
        self.assertFalse(config.model_directory == "/evil")


class OcrPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        root = Path(self._tempdir.name)
        self.registry = FakeTenantRegistry(root)
        self.workspace = root / "t1" / "workspace"
        self.workspace.mkdir(parents=True)
        self.tenant = SimpleNamespace(tenant_id="t1")
        context = SimpleNamespace(tenant_registry=self.registry)
        self.plugin = OcrPlugin({}, context)
        # Swap in a deterministic worker instead of the real Paddle process.
        self.plugin._service = OcrService(
            self.plugin._service.config, worker=FakeWorker()
        )

    def tearDown(self) -> None:
        self.plugin.close()
        self._tempdir.cleanup()

    def test_requires_tenant_storage_service(self) -> None:
        with self.assertRaisesRegex(ValueError, "tenant_storage"):
            OcrPlugin({}, SimpleNamespace(tenant_registry=None))

    def test_execute_reads_image_within_workspace(self) -> None:
        target = self.workspace / "scan.png"
        target.write_bytes(image_bytes())
        payload = self.plugin.execute(
            "ocr_extract_text", {"path": "scan.png"}, self.tenant
        )
        self.assertEqual(payload["text"], "你好\nHello")
        self.assertEqual(payload["media_type"], "image/png")

    def test_relative_escape_is_rejected(self) -> None:
        # A real file just outside the workspace still fails the boundary check.
        outside = self.workspace.parent / "scan.png"
        outside.write_bytes(image_bytes())
        with self.assertRaisesRegex(PluginError, "workspace"):
            self.plugin.execute(
                "ocr_extract_text", {"path": "../scan.png"}, self.tenant
            )

    def test_symlink_escape_is_rejected(self) -> None:
        outside = Path(self._tempdir.name) / "secret.png"
        outside.write_bytes(image_bytes())
        link = self.workspace / "link.png"
        os.symlink(outside, link)
        with self.assertRaisesRegex(PluginError, "workspace"):
            self.plugin.execute(
                "ocr_extract_text", {"path": "link.png"}, self.tenant
            )

    def test_missing_file_is_rejected(self) -> None:
        with self.assertRaisesRegex(PluginError, "不存在"):
            self.plugin.execute(
                "ocr_extract_text", {"path": "nope.png"}, self.tenant
            )

    def test_missing_tenant_identity_is_rejected(self) -> None:
        with self.assertRaisesRegex(PluginError, "租户"):
            self.plugin.execute(
                "ocr_extract_text",
                {"path": "scan.png"},
                SimpleNamespace(tenant_id=""),
            )

    def test_blank_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(PluginError, "path"):
            self.plugin.execute(
                "ocr_extract_text", {"path": "  "}, self.tenant
            )

    def test_recognize_chat_image_and_auto_flag(self) -> None:
        self.assertTrue(self.plugin.auto_chat_images)
        result = self.plugin.recognize_chat_image(image_bytes())
        self.assertEqual(result.text, "你好\nHello")

    def test_is_available_reflects_service_state(self) -> None:
        self.assertTrue(self.plugin.is_available("ocr_extract_text"))
        self.assertFalse(self.plugin.is_available("unknown_tool"))


class OcrPluginAvailabilityTests(unittest.TestCase):
    def test_unavailable_when_engine_dependencies_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = FakeTenantRegistry(Path(directory))
            context = SimpleNamespace(tenant_registry=registry)
            plugin = OcrPlugin({}, context)
            try:
                # The real Paddle process reports unavailable without models.
                self.assertFalse(plugin.is_available("ocr_extract_text"))
            finally:
                plugin.close()


if __name__ == "__main__":
    unittest.main()
