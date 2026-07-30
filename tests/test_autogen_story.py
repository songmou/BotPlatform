from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.jobs.autogen import monitor


def scene(number: int = 1, reference: str | None = None) -> monitor.StoryState:
    return monitor.StoryState(
        story_id="story-test",
        scene_number=number,
        story_bible="雾港探险故事",
        anchors="主角红色雨衣，青蓝色电影光，低机位广角",
        scene_summary="主角走向灯塔",
        prompt="红色雨衣主角走向雾中的灯塔，青蓝色电影光，低机位广角",
        reference_image=reference,
    )


class _Locator:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.uploads: list[str] = []
        self.checked = False

    def is_enabled(self) -> bool:
        return self.enabled

    def set_input_files(self, value: str) -> None:
        self.uploads.append(value)

    def is_visible(self) -> bool:
        return True

    def check(self) -> None:
        self.checked = True

    def evaluate(self, _expression: str):
        return self.checked


class _Matches:
    def __init__(self, candidates) -> None:
        self.candidates = candidates

    def count(self) -> int:
        return len(self.candidates)

    def nth(self, index: int):
        return self.candidates[index]


class _Page:
    def __init__(self, candidates) -> None:
        self.candidates = candidates

    def locator(self, _selector: str):
        return _Matches(self.candidates)


class _ModelResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _Model:
    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return _ModelResponse(self.contents.pop(0))


class _StringModel(_Model):
    def invoke(self, messages):
        self.calls.append(messages)
        return self.contents.pop(0)


class _Response:
    def __init__(self, url: str, payload: dict) -> None:
        self.url = url
        self.payload = payload

    def json(self):
        return self.payload


class _ResponsePage:
    def __init__(self) -> None:
        self.callback = None

    def on(self, event: str, callback) -> None:
        self.event = event
        self.callback = callback


class _ResultLocator:
    def __init__(self, urls: list[str]) -> None:
        self.urls = urls
        self.expression = ""

    def evaluate_all(self, expression: str) -> list[str]:
        self.expression = expression
        return self.urls


class _ResultPage:
    def __init__(self, urls: list[str]) -> None:
        self.url = "https://site/general/text-to-image"
        self.result_locator = _ResultLocator(urls)
        self.selector = ""

    def locator(self, selector: str) -> _ResultLocator:
        self.selector = selector
        return self.result_locator


class AutoGenStoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "autogen"

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits (0o600) are asserted")
    def test_first_story_state_is_saved_and_loaded(self) -> None:
        with patch.object(monitor, "DATA_ROOT", self.data_root):
            monitor.save_story_state(scene())
            loaded = monitor.load_story_state()
        self.assertEqual(loaded, scene())
        self.assertEqual(
            (self.data_root / monitor.STORY_STATE_FILE).stat().st_mode & 0o777,
            0o600,
        )

    def test_successful_next_frame_uses_first_image_as_next_reference(self) -> None:
        image_one = self.data_root / "2026-07-23" / "run-image-01.jpg"
        image_two = self.data_root / "2026-07-23" / "run-image-02.jpg"
        image_one.parent.mkdir(parents=True)
        image_one.write_bytes(b"first")
        image_two.write_bytes(b"second")
        with patch.object(monitor, "DATA_ROOT", self.data_root):
            committed = monitor.committed_story_frame(scene(2), [image_one, image_two])
            monitor.save_story_state(committed)
            reference = monitor.reference_image_path(monitor.load_story_state())
        self.assertEqual(committed.reference_image, "2026-07-23/run-image-01.jpg")
        self.assertEqual(reference, image_one.resolve())

    def test_reset_can_create_new_story_without_overwriting_prior_state_until_success(self) -> None:
        prior = scene(reference="2026-07-22/run-image-01.jpg")
        replacement = scene(reference="2026-07-23/run-image-01.jpg")
        replacement = monitor.StoryState(**{**replacement.to_dict(), "story_id": "story-new"})
        with patch.object(monitor, "DATA_ROOT", self.data_root):
            monitor.save_story_state(prior)
            # A reset run that fails before committing must leave the previous
            # story intact; only a successful run commits the replacement.
            self.assertEqual(monitor.load_story_state(), prior)
            monitor.save_story_state(replacement)
            self.assertEqual(monitor.load_story_state(), replacement)

    def test_missing_reference_upload_control_stops_generation(self) -> None:
        with self.assertRaisesRegex(monitor.PreflightError, "参考图"):
            monitor.require_reference_upload(_Page([]))

    def test_reference_upload_control_can_be_hidden_but_enabled(self) -> None:
        upload = _Locator()
        self.assertIs(monitor.require_reference_upload(_Page([upload])), upload)

    def test_first_scene_uses_text_to_image_then_continuations_use_image_to_image(self) -> None:
        self.assertEqual(monitor.generation_image_path(None), monitor.TEXT_TO_IMAGE_PATH)
        self.assertEqual(
            monitor.generation_image_path(Path("previous-frame.jpg")),
            monitor.IMAGE_TO_IMAGE_PATH,
        )

    def test_uploaded_reference_is_confirmed_and_set_to_passthrough(self) -> None:
        image = self.root / "previous-frame.jpg"
        image.write_bytes(b"image")
        upload = _Locator()
        monitor.upload_reference_image(_Page([upload]), image)
        self.assertEqual(upload.uploads, [str(image)])
        self.assertTrue(upload.checked)

    def test_story_response_requires_json_fields(self) -> None:
        payload = monitor._story_json('{"scene_summary":"海浪拍向码头","prompt":"雾港码头"}')
        self.assertEqual(monitor._required_story_text(payload, "scene_summary"), "海浪拍向码头")
        with self.assertRaises(monitor.PreflightError):
            monitor._story_json("不是 JSON")

    def test_chinese_story_payload_is_accepted_without_retry(self) -> None:
        model = _Model(['{"scene_summary":"海浪拍向码头","prompt":"雾港码头的海浪拍向旧木船"}'])
        payload = monitor._generate_chinese_story_payload(
            model, "生成故事下一场", ("scene_summary", "prompt")
        )
        self.assertEqual(payload["prompt"], "雾港码头的海浪拍向旧木船")
        self.assertEqual(len(model.calls), 1)

    def test_default_model_string_response_is_parsed(self) -> None:
        model = _StringModel(['{"scene_summary":"海浪拍向码头","prompt":"雾港码头的海浪拍向旧木船"}'])
        payload = monitor._generate_chinese_story_payload(
            model, "生成故事下一场", ("scene_summary", "prompt")
        )
        self.assertEqual(payload["scene_summary"], "海浪拍向码头")
        self.assertEqual(len(model.calls), 1)

    def test_chinese_dominant_prompt_allows_short_style_terms(self) -> None:
        self.assertTrue(monitor._is_chinese_story_text("雾港码头的电影感画面，8K 细节"))
        self.assertFalse(monitor._is_chinese_story_text("Cinematic dock with 林月"))

    def test_english_story_payload_is_corrected_once(self) -> None:
        model = _Model([
            '{"scene_summary":"Waves strike the dock","prompt":"Cinematic waves at dock"}',
            '{"scene_summary":"海浪拍向码头","prompt":"电影感海浪拍向雾港码头"}',
        ])
        payload = monitor._generate_chinese_story_payload(
            model, "生成故事下一场", ("scene_summary", "prompt")
        )
        self.assertEqual(payload["scene_summary"], "海浪拍向码头")
        self.assertEqual(len(model.calls), 2)
        self.assertIn("Waves strike the dock", model.calls[1][-1].content)

    def test_non_chinese_story_payload_is_rejected_before_generation(self) -> None:
        model = _Model([
            '{"scene_summary":"Waves strike the dock","prompt":"Cinematic waves at dock"}',
            '{"scene_summary":"Waves return","prompt":"Noir waterfront"}',
        ])
        with self.assertRaisesRegex(monitor.PreflightError, "两次"):
            monitor._generate_chinese_story_payload(
                model, "生成故事下一场", ("scene_summary", "prompt")
            )

    def test_website_task_tracking_records_submission_and_progress(self) -> None:
        result = monitor.RunResult("run-test", monitor.datetime(2026, 7, 23, 14, 0))
        page = _ResponsePage()
        monitor.attach_website_task_tracker(page, result)
        self.assertEqual(page.event, "response")
        page.callback(
            _Response(
                "https://site/api/general-ai/generate-image",
                {"taskId": "task-123", "content": "任务已创建"},
            )
        )
        page.callback(
            _Response(
                "https://site/api/general-ai/task/task-123",
                {"status": "processing", "content": "正在排队"},
            )
        )
        self.assertEqual(result.website_task_id, "task-123")
        self.assertEqual(result.website_task_status, "processing")
        self.assertEqual(result.website_task_detail, "正在排队")

    def test_result_image_urls_exclude_nested_thumbnail_sources(self) -> None:
        page = _ResultPage(["/images/full-01.jpg", "/images/full-02.jpg"])

        urls = monitor.image_urls_from_result(page)

        self.assertEqual(
            urls,
            ["https://site/images/full-01.jpg", "https://site/images/full-02.jpg"],
        )
        self.assertNotIn("img[src]", page.selector)
        self.assertIn("data-img-url", page.selector)
        self.assertNotIn("currentSrc", page.result_locator.expression)
        self.assertNotIn("getAttribute('src')", page.result_locator.expression)

    def test_timeout_detail_distinguishes_missing_and_pending_website_tasks(self) -> None:
        missing = monitor.website_timeout_detail(monitor.WebsiteTaskTracker(), 600, 0)
        pending = monitor.website_timeout_detail(
            monitor.WebsiteTaskTracker("task-123", "processing", "正在排队"), 600, 1
        )
        self.assertIn("未返回任务编号", missing)
        self.assertIn("task-123", pending)
        self.assertIn("processing", pending)
