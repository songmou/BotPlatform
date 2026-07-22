"""Load and validate image bytes for outbound WeChat notifications."""

from __future__ import annotations

import io
import stat
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, UnidentifiedImageError


MAX_OUTBOUND_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_REDIRECTS = 3
SUPPORTED_IMAGE_FORMATS = {"JPEG", "PNG", "GIF", "WEBP", "BMP"}
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


class ImageSourceError(RuntimeError):
    """Raised when an outbound image cannot be safely loaded or validated."""


@dataclass(frozen=True)
class ImageSource:
    kind: str
    value: str

    @classmethod
    def local(cls, path: Path) -> "ImageSource":
        return cls(kind="path", value=str(path))

    @classmethod
    def remote(cls, url: str) -> "ImageSource":
        return cls(kind="url", value=url)


def validate_image_bytes(data: bytes) -> str:
    if not data:
        raise ImageSourceError("图片文件为空")
    if len(data) > MAX_OUTBOUND_IMAGE_BYTES:
        raise ImageSourceError("图片超过 20MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image_format = str(image.format or "").upper()
                if image_format not in SUPPORTED_IMAGE_FORMATS:
                    raise ImageSourceError(
                        "不支持的图片格式，仅支持 JPEG、PNG、GIF、WebP 和 BMP"
                    )
                image.verify()
    except ImageSourceError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageSourceError("图片文件损坏或不是有效图片") from exc
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageSourceError("图片像素尺寸过大") from exc
    return image_format


class ImageSourceLoader:
    def __init__(
        self,
        client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(
            trust_env=False,
            follow_redirects=False,
        ),
    ) -> None:
        self.client_factory = client_factory

    def load(self, source: ImageSource) -> bytes:
        if source.kind == "path":
            return self.load_path(Path(source.value))
        if source.kind == "url":
            return self.load_url(source.value)
        raise ImageSourceError("未知的图片来源类型")

    def load_path(self, path: Path) -> bytes:
        try:
            resolved = path.expanduser().resolve()
            info = resolved.stat()
        except (OSError, RuntimeError) as exc:
            raise ImageSourceError("读取本地图片失败：文件不存在或不可访问") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ImageSourceError("本地图片必须是普通文件")
        if info.st_size > MAX_OUTBOUND_IMAGE_BYTES:
            raise ImageSourceError("图片超过 20MB")
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            raise ImageSourceError("读取本地图片失败") from exc
        validate_image_bytes(data)
        return data

    def load_url(self, url: str) -> bytes:
        current_url = self._validate_url(url)
        client = self.client_factory()
        try:
            for redirect_count in range(MAX_IMAGE_REDIRECTS + 1):
                try:
                    with client.stream(
                        "GET",
                        current_url,
                        timeout=30.0,
                        follow_redirects=False,
                    ) as response:
                        if response.status_code in REDIRECT_STATUS_CODES:
                            location = response.headers.get("location")
                            if not location:
                                raise ImageSourceError("图片 URL 跳转缺少 Location")
                            if redirect_count >= MAX_IMAGE_REDIRECTS:
                                raise ImageSourceError("图片 URL 跳转次数超过 3 次")
                            current_url = self._validate_url(
                                urljoin(str(response.url), location)
                            )
                            continue
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            raise ImageSourceError(
                                "下载图片失败：HTTP {}".format(response.status_code)
                            ) from exc
                        content_length = response.headers.get("content-length")
                        if content_length:
                            try:
                                declared_size = int(content_length)
                            except ValueError as exc:
                                raise ImageSourceError(
                                    "图片响应的 Content-Length 无效"
                                ) from exc
                            if declared_size < 0:
                                raise ImageSourceError(
                                    "图片响应的 Content-Length 无效"
                                )
                            if declared_size > MAX_OUTBOUND_IMAGE_BYTES:
                                raise ImageSourceError("图片超过 20MB")
                        chunks = []
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > MAX_OUTBOUND_IMAGE_BYTES:
                                raise ImageSourceError("图片超过 20MB")
                            chunks.append(chunk)
                        data = b"".join(chunks)
                        validate_image_bytes(data)
                        return data
                except ImageSourceError:
                    raise
                except httpx.HTTPError as exc:
                    raise ImageSourceError("下载图片失败：网络请求异常") from exc
            raise ImageSourceError("图片 URL 跳转次数超过 3 次")
        finally:
            client.close()

    @staticmethod
    def _validate_url(url: str) -> str:
        if not isinstance(url, str) or not url.strip():
            raise ImageSourceError("图片 URL 不能为空")
        normalized = url.strip()
        parsed = urlparse(normalized)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ImageSourceError("图片 URL 必须使用 HTTP 或 HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ImageSourceError("图片 URL 不能包含用户名或密码")
        return normalized
