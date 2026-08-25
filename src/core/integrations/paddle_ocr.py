"""Isolated PaddleOCR inference process and explicit model preparation."""

from __future__ import annotations

import argparse
import importlib.util
import io
import multiprocessing
import queue
import shutil
import tarfile
import tempfile
import threading
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.core.services.ocr_config import OcrConfig
from src.core.paths import CONFIG_DIR


MODEL_BASE_URL = (
    "https://paddle-model-ecology.bj.bcebos.com/"
    "paddlex/official_inference_model/paddle3.0.0/"
)
MODEL_ARCHIVES = {
    "orientation": "PP-LCNet_x1_0_doc_ori_infer.tar",
    "tiny_detection": "PP-OCRv6_tiny_det_infer.tar",
    "tiny_recognition": "PP-OCRv6_tiny_rec_infer.tar",
    "small_detection": "PP-OCRv6_small_det_infer.tar",
    "small_recognition": "PP-OCRv6_small_rec_infer.tar",
    "medium_detection": "PP-OCRv6_medium_det_infer.tar",
    "medium_recognition": "PP-OCRv6_medium_rec_infer.tar",
}
MODEL_DIRECTORIES = {
    "orientation": "PP-LCNet_x1_0_doc_ori",
    "tiny_detection": "PP-OCRv6_tiny_det",
    "tiny_recognition": "PP-OCRv6_tiny_rec",
    "small_detection": "PP-OCRv6_small_det",
    "small_recognition": "PP-OCRv6_small_rec",
    "medium_detection": "PP-OCRv6_medium_det",
    "medium_recognition": "PP-OCRv6_medium_rec",
}


class PaddleOcrError(RuntimeError):
    """A safe, user-readable PaddleOCR integration failure."""


def model_paths(config: OcrConfig) -> Dict[str, Path]:
    root = Path(config.model_directory)
    return {
        "orientation": root / MODEL_DIRECTORIES["orientation"],
        "detection": root / MODEL_DIRECTORIES[config.model_tier + "_detection"],
        "recognition": root / MODEL_DIRECTORIES[config.model_tier + "_recognition"],
    }


def _model_directory_ready(path: Path) -> bool:
    if not path.is_dir():
        return False
    names = {item.name for item in path.iterdir() if item.is_file()}
    return bool(
        {"inference.json", "inference.pdmodel"} & names
        and {"inference.pdiparams", "model.pdiparams"} & names
    )


def paddle_ocr_availability(config: OcrConfig) -> Tuple[bool, str]:
    if not config.enabled:
        return False, "OCR 未启用"
    if importlib.util.find_spec("paddle") is None:
        return False, "未安装 paddlepaddle，请安装 requirements-ocr.txt"
    if importlib.util.find_spec("paddleocr") is None:
        return False, "未安装 paddleocr，请安装 requirements-ocr.txt"
    missing = [
        label for label, path in model_paths(config).items()
        if not _model_directory_ready(path)
    ]
    if missing:
        return False, "OCR 模型尚未准备：{}".format("、".join(missing))
    return True, ""


def _result_payload(value: Any) -> Dict[str, Any]:
    payload = getattr(value, "json", value)
    if callable(payload):
        payload = payload()
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("res")
    return nested if isinstance(nested, dict) else payload


def _normalize_results(values: Any) -> List[Dict[str, Any]]:
    pages: List[Dict[str, Any]] = []
    for index, value in enumerate(values):
        payload = _result_payload(value)
        raw_texts = payload.get("rec_texts", [])
        texts = raw_texts if isinstance(raw_texts, list) else []
        page_index = payload.get("page_index")
        if not isinstance(page_index, int):
            page_index = index
        pages.append(
            {
                "page_index": page_index,
                "text": "\n".join(
                    str(text).strip() for text in texts if str(text).strip()
                ),
            }
        )
    pages.sort(key=lambda item: item["page_index"])
    return pages


def _worker_main(
    requests: Any,
    responses: Any,
    settings: Dict[str, Any],
) -> None:
    try:
        import numpy as np
        from PIL import Image
        from paddleocr import PaddleOCR

        pipeline = PaddleOCR(
            text_detection_model_name=settings["detection_name"],
            text_detection_model_dir=settings["detection_directory"],
            text_recognition_model_name=settings["recognition_name"],
            text_recognition_model_dir=settings["recognition_directory"],
            doc_orientation_classify_model_name="PP-LCNet_x1_0_doc_ori",
            doc_orientation_classify_model_dir=settings["orientation_directory"],
            use_doc_orientation_classify=True,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device=settings["device"],
            engine="paddle",
        )
        responses.put({"type": "ready"})
    except BaseException as exc:  # noqa: BLE001 - cross-process error boundary
        responses.put({"type": "startup_error", "error": str(exc)})
        return

    while True:
        request = requests.get()
        if request is None:
            return
        request_id = str(request.get("id") or "")
        try:
            if request.get("kind") == "image":
                with Image.open(io.BytesIO(request["data"])) as image:
                    input_value = np.asarray(image.convert("RGB"))
            elif request.get("kind") == "pdf":
                input_value = request["path"]
            else:
                raise ValueError("未知 OCR 输入类型")
            results = pipeline.predict(input_value)
            responses.put(
                {
                    "type": "result",
                    "id": request_id,
                    "pages": _normalize_results(results),
                }
            )
        except BaseException as exc:  # noqa: BLE001 - worker stays reusable
            responses.put(
                {"type": "error", "id": request_id, "error": str(exc)}
            )


class PaddleOcrProcess:
    """Own one lazy, serial PaddleOCR process with hard request timeouts."""

    def __init__(self, config: OcrConfig) -> None:
        self.config = config
        self._context = multiprocessing.get_context("spawn")
        self._process: Optional[multiprocessing.context.SpawnProcess] = None
        self._requests: Any = None
        self._responses: Any = None
        self._lock = threading.RLock()

    def availability(self) -> Tuple[bool, str]:
        return paddle_ocr_availability(self.config)

    def _settings(self) -> Dict[str, Any]:
        paths = model_paths(self.config)
        tier = self.config.model_tier
        return {
            "device": self.config.device,
            "detection_name": "PP-OCRv6_{}_det".format(tier),
            "recognition_name": "PP-OCRv6_{}_rec".format(tier),
            "orientation_directory": str(paths["orientation"]),
            "detection_directory": str(paths["detection"]),
            "recognition_directory": str(paths["recognition"]),
        }

    def _start_locked(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        available, reason = self.availability()
        if not available:
            raise PaddleOcrError(reason)
        self._requests = self._context.Queue(maxsize=1)
        self._responses = self._context.Queue(maxsize=1)
        self._process = self._context.Process(
            target=_worker_main,
            args=(self._requests, self._responses, self._settings()),
            daemon=True,
            name="botplatform-ocr",
        )
        self._process.start()
        try:
            response = self._responses.get(
                timeout=self.config.startup_timeout_seconds
            )
        except queue.Empty as exc:
            self._stop_locked()
            raise PaddleOcrError("OCR 引擎启动超时") from exc
        if response.get("type") != "ready":
            error = str(response.get("error") or "未知错误")
            self._stop_locked()
            raise PaddleOcrError("OCR 引擎启动失败：{}".format(error))

    def recognize(self, request: Dict[str, Any]) -> List[Dict[str, Any]]:
        with self._lock:
            self._start_locked()
            assert self._requests is not None
            assert self._responses is not None
            request_id = uuid.uuid4().hex
            payload = dict(request)
            payload["id"] = request_id
            self._requests.put(payload)
            try:
                response = self._responses.get(
                    timeout=self.config.request_timeout_seconds
                )
            except queue.Empty as exc:
                self._stop_locked()
                raise PaddleOcrError("OCR 识别超过 {} 秒，已终止任务".format(
                    self.config.request_timeout_seconds
                )) from exc
            if response.get("id") != request_id:
                self._stop_locked()
                raise PaddleOcrError("OCR worker 返回了不匹配的任务结果")
            if response.get("type") == "error":
                raise PaddleOcrError(
                    "OCR 识别失败：{}".format(response.get("error") or "未知错误")
                )
            pages = response.get("pages")
            if not isinstance(pages, list):
                raise PaddleOcrError("OCR worker 返回格式错误")
            return pages

    def _stop_locked(self) -> None:
        process = self._process
        requests = self._requests
        self._process = None
        self._requests = None
        self._responses = None
        if process is None:
            return
        if process.is_alive() and requests is not None:
            try:
                requests.put_nowait(None)
            except Exception:
                pass
            process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        try:
            process.close()
        except ValueError:
            pass

    def close(self) -> None:
        with self._lock:
            self._stop_locked()


def _safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:*") as bundle:
        destination_root = destination.resolve()
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if target != destination_root and destination_root not in target.parents:
                raise PaddleOcrError("OCR 模型压缩包包含不安全路径")
            if member.issym() or member.islnk():
                raise PaddleOcrError("OCR 模型压缩包不能包含链接")
        bundle.extractall(destination)


def prepare_models(config: OcrConfig) -> None:
    """Download the selected inference models into the configured data directory."""

    root = Path(config.model_directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    keys = [
        "orientation",
        config.model_tier + "_detection",
        config.model_tier + "_recognition",
    ]
    for key in keys:
        destination = root / MODEL_DIRECTORIES[key]
        if _model_directory_ready(destination):
            print("模型已存在：{}".format(destination))
            continue
        url = MODEL_BASE_URL + MODEL_ARCHIVES[key]
        print("下载 OCR 模型：{}".format(url))
        with tempfile.TemporaryDirectory(prefix="botplatform-ocr-") as temporary:
            archive = Path(temporary) / MODEL_ARCHIVES[key]
            urllib.request.urlretrieve(url, archive)
            extracted = Path(temporary) / "extracted"
            extracted.mkdir()
            _safe_extract(archive, extracted)
            candidates = [
                item for item in extracted.iterdir() if item.is_dir()
            ]
            source = candidates[0] if len(candidates) == 1 else extracted
            if destination.exists():
                shutil.rmtree(destination)
            shutil.move(str(source), str(destination))
        if not _model_directory_ready(destination):
            raise PaddleOcrError("OCR 模型文件不完整：{}".format(destination))
    print("OCR 模型准备完成：{}".format(root))


def _load_ocr_config() -> OcrConfig:
    """Load the OCR plugin settings from the project config when enabled."""
    from src.core.config.loader import load_project_config
    from src.core.plugins.ocr import build_config

    plugin = load_project_config(CONFIG_DIR).plugins.get("ocr")
    if plugin is None or not plugin.enabled:
        return OcrConfig(enabled=False)
    return build_config(plugin.settings)


def main() -> None:
    parser = argparse.ArgumentParser(description="准备 BotPlatform 本地 OCR 模型")
    parser.add_argument(
        "action", choices=["prepare", "check"], help="下载模型或检查模型状态"
    )
    args = parser.parse_args()

    config = _load_ocr_config()
    if args.action == "prepare":
        prepare_models(config)
        return
    available, reason = paddle_ocr_availability(config)
    if not available:
        raise SystemExit("OCR 不可用：{}".format(reason))
    print("OCR 依赖和模型均可用")


if __name__ == "__main__":
    main()
