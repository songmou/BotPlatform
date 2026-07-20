from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from src.integrations.images import (
    ImageSource,
    ImageSourceError,
    ImageSourceLoader,
    validate_image_bytes,
)


def image_bytes(image_format: str, animated: bool = False) -> bytes:
    output = io.BytesIO()
    first = Image.new("RGB", (4, 3), "red")
    if animated:
        second = Image.new("RGB", (4, 3), "blue")
        first.save(
            output,
            format=image_format,
            save_all=True,
            append_images=[second],
            duration=100,
            loop=0,
        )
    else:
        first.save(output, format=image_format)
    return output.getvalue()


class ImageSourceTests(unittest.TestCase):
    def test_supported_formats_and_animated_gif_are_preserved(self) -> None:
        for image_format in ("JPEG", "PNG", "GIF", "WEBP", "BMP"):
            with self.subTest(image_format=image_format):
                data = image_bytes(image_format, animated=image_format == "GIF")
                self.assertEqual(validate_image_bytes(data), image_format)
                if image_format == "GIF":
                    with Image.open(io.BytesIO(data)) as image:
                        self.assertEqual(image.n_frames, 2)

    def test_local_file_must_be_valid_regular_image_within_limit(self) -> None:
        loader = ImageSourceLoader()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "actual-image.bin"
            valid.write_bytes(image_bytes("PNG"))
            self.assertEqual(loader.load(ImageSource.local(valid)), valid.read_bytes())

            invalid = root / "fake.png"
            invalid.write_bytes(b"not an image")
            with self.assertRaisesRegex(ImageSourceError, "损坏|有效图片"):
                loader.load(ImageSource.local(invalid))

            with self.assertRaisesRegex(ImageSourceError, "普通文件"):
                loader.load(ImageSource.local(root))

            oversized = root / "large.png"
            oversized.write_bytes(b"x" * 33)
            with patch("src.integrations.images.MAX_OUTBOUND_IMAGE_BYTES", 32):
                with self.assertRaisesRegex(ImageSourceError, "20MB"):
                    loader.load(ImageSource.local(oversized))

    def test_excessive_pixel_count_is_rejected(self) -> None:
        data = image_bytes("PNG")
        with patch.object(Image, "MAX_IMAGE_PIXELS", 1):
            with self.assertRaisesRegex(ImageSourceError, "像素尺寸过大"):
                validate_image_bytes(data)

    def test_remote_http_private_url_redirect_and_query_are_supported(self) -> None:
        data = image_bytes("PNG")
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "/final?secret=1"})
            return httpx.Response(200, content=data)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        loader = ImageSourceLoader(client_factory=lambda: client)
        actual = loader.load_url("http://127.0.0.1/start")
        self.assertEqual(actual, data)
        self.assertEqual(len(requests), 2)
        self.assertIn("secret=1", requests[-1])

    def test_remote_rejects_credentials_redirect_loops_and_streamed_oversize(self) -> None:
        loader = ImageSourceLoader()
        with self.assertRaisesRegex(ImageSourceError, "用户名或密码"):
            loader.load_url("https://user:pass@example.test/image.png")

        def redirect_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "/again"})

        redirect_client = httpx.Client(transport=httpx.MockTransport(redirect_handler))
        loader = ImageSourceLoader(client_factory=lambda: redirect_client)
        with self.assertRaisesRegex(ImageSourceError, "跳转次数"):
            loader.load_url("https://example.test/start")

        def oversized_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 33)

        oversized_client = httpx.Client(transport=httpx.MockTransport(oversized_handler))
        loader = ImageSourceLoader(client_factory=lambda: oversized_client)
        with patch("src.integrations.images.MAX_OUTBOUND_IMAGE_BYTES", 32):
            with self.assertRaisesRegex(ImageSourceError, "20MB"):
                loader.load_url("http://internal.test/image")


if __name__ == "__main__":
    unittest.main()
