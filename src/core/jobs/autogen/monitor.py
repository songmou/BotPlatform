#!/usr/bin/env python3
"""Run one end-to-end text-to-image health-monitoring job.

All run records and downloaded images are written beneath the directory that
contains this script.  The script deliberately uses the site's visible UI for
submission, then downloads only image URLs that the completed result panel
reports for this run.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import textwrap
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import httpx
from dotenv import load_dotenv
from src.core.plugins.browser_automation import (
    BrowserAutomation,
    BrowserAutomationConfig,
    BrowserUnavailableError,
)


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("ILINKBOT_SCRIPT_DATA_ROOT", str(ROOT))).expanduser().resolve()
LOGIN_PATH = "/account/login"
TEXT_TO_IMAGE_PATH = "/general/text-to-image"
IMAGE_TO_IMAGE_PATH = "/general/image-to-image"
EXPECTED_IMAGE_COUNT = 2
STORY_STATE_FILE = "story-state.json"

LOGIN_PASSWORD_SELECTORS = (
    "#loginPassword",
    'input[name="password"]',
    'input[autocomplete="current-password"]',
    'input[type="password"]',
)
LOGIN_ACCOUNT_SELECTORS = (
    "#loginAccount",
    'input[name="account"]',
    'input[name="username"]',
    'input[name="login"]',
    'input[autocomplete="username"]',
    'input[type="email"]',
    'input[type="tel"]',
    'input[type="text"]',
)
LOGIN_MODE_SELECTORS = (
    '[data-login-tab="password"]',
    '[data-login-mode="password"]',
    '[data-tab="password"]',
    '[data-target*="password"]',
    '[role="tab"]:has-text("密码登录")',
    'button:has-text("密码登录")',
    '[role="tab"]:has-text("账号登录")',
    'button:has-text("账号登录")',
)
LOGIN_SUBMIT_SELECTORS = (
    "#loginBtn",
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("登录")',
)
PROMPT_SELECTORS = (
    "#imagePrompt",
    'textarea[name="prompt"]',
    'textarea[placeholder*="提示"]',
    '[contenteditable="true"][data-placeholder*="提示"]',
)
REFERENCE_IMAGE_SELECTORS = ("#sourceImagesInput",)
REFERENCE_PREVIEW_SELECTOR = "#sourcePreview .generic-source-preview-image"
REFERENCE_PASSTHROUGH_SELECTOR = 'input[name="refMode"][value="passthrough"]'
IMAGE_COUNT_SELECTORS = (
    "#imageCount",
    'select[name="imageCount"]',
    'select[name="count"]',
    'input[type="number"][name*="count"]',
)
SUBMIT_SELECTORS = (
    "#submitBtn",
    '[data-action="generate"]',
    'button[type="submit"]',
    'button:has-text("生成图片")',
    'button:has-text("立即生成")',
    'button:has-text("开始生成")',
)
RESULT_IMAGE_SELECTOR = ", ".join(
    (
        ".generic-result-img-card[data-img-url]",
        "#resultContainer [data-img-url]",
        "[data-result-container] [data-img-url]",
        ".generation-result [data-img-url]",
    )
)
GENERATION_ERROR_SELECTORS = (
    "#resultContainer .generic-result-error",
    "#resultContainer [role=alert]",
    "[data-result-container] [role=alert]",
    ".generation-result .error",
    ".generation-error",
)
MODAL_OVERLAY_SELECTORS = (
    '#kModalOverlay[aria-hidden="false"]',
    '#kModalOverlay[role="dialog"]',
    '[role="dialog"][aria-modal="true"]',
)
SAFE_MODAL_CLOSE_SELECTORS = (
    'button[aria-label="关闭"]',
    'button[aria-label="Close"]',
    '[data-dismiss="modal"]',
    '[data-action="close"]',
    ".k-modal-close",
    ".btn-close",
    'button:has-text("我知道了")',
    'button:has-text("知道了")',
    'button:has-text("稍后再说")',
    'button:has-text("关闭")',
    'button:has-text("取消")',
    'button:has-text("好的")',
    'button:has-text("确定")',
    'button:has-text("确认")',
)
MODAL_MANUAL_CONFIRM_WORDS = (
    "验证码",
    "人机验证",
    "滑动验证",
    "支付",
    "付款",
    "购买",
    "充值",
    "扣款",
    "授权",
    "协议",
    "条款",
    "隐私",
    "删除",
    "清空",
    "订单",
)


class MonitorError(RuntimeError):
    """A known monitor failure that should become a readable report entry."""


class PreflightError(MonitorError):
    pass


class AuthenticationBlocked(MonitorError):
    pass


class PageStructureChanged(MonitorError):
    pass


class GenerationFailed(MonitorError):
    def __init__(self, message: str, image_urls: Iterable[str] | None = None):
        super().__init__(message)
        self.image_urls = list(image_urls or [])


class GenerationTimedOut(GenerationFailed):
    pass


@dataclass(frozen=True)
class Config:
    base_url: str
    username: str
    password: str
    generation_timeout_seconds: int
    poll_interval_seconds: float
    headless: bool
    browser_executable: str | None


@dataclass
class RunResult:
    run_id: str
    started_at: datetime
    prompt: str = ""
    status: str = "preflight_failed"
    detail: str = ""
    finished_at: datetime | None = None
    image_files: list[Path] | None = None
    story: "StoryState | None" = None
    reset_story: bool = False
    website_task_id: str = ""
    website_task_status: str = ""
    website_task_detail: str = ""

    @property
    def elapsed_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return max(0.0, (self.finished_at - self.started_at).total_seconds())


@dataclass
class WebsiteTaskTracker:
    """Small, non-sensitive trace of the website task created by this run."""

    task_id: str = ""
    status: str = ""
    detail: str = ""

    def observe_submission(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        task_id = str(payload.get("taskId") or "").strip()
        if task_id:
            self.task_id = task_id
        self._observe_payload(payload)

    def observe_task(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        self._observe_payload(payload)

    def _observe_payload(self, payload: dict) -> None:
        status = str(payload.get("status") or "").strip()
        if status:
            self.status = status
        detail = str(
            payload.get("errorMessage") or payload.get("content") or payload.get("message") or ""
        ).strip()
        if detail:
            self.detail = detail

    def apply_to(self, result: RunResult) -> None:
        result.website_task_id = self.task_id
        result.website_task_status = self.status
        result.website_task_detail = self.detail


@dataclass(frozen=True)
class StoryState:
    """The committed frame of one tenant's ongoing image story."""

    story_id: str
    scene_number: int
    story_bible: str
    anchors: str
    scene_summary: str
    prompt: str
    reference_image: str | None = None

    @classmethod
    def from_dict(cls, raw: object) -> "StoryState":
        if not isinstance(raw, dict):
            raise ValueError("故事状态不是 JSON 对象")
        required = ("story_id", "scene_number", "story_bible", "anchors", "scene_summary", "prompt")
        if any(not raw.get(name) for name in required):
            raise ValueError("故事状态字段不完整")
        scene_number = raw["scene_number"]
        if not isinstance(scene_number, int) or scene_number < 1:
            raise ValueError("故事场景序号无效")
        reference = raw.get("reference_image")
        if reference is not None and not isinstance(reference, str):
            raise ValueError("故事参考帧无效")
        return cls(
            story_id=str(raw["story_id"]),
            scene_number=scene_number,
            story_bible=str(raw["story_bible"]),
            anchors=str(raw["anchors"]),
            scene_summary=str(raw["scene_summary"]),
            prompt=str(raw["prompt"]),
            reference_image=reference,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "story_id": self.story_id,
            "scene_number": self.scene_number,
            "story_bible": self.story_bible,
            "anchors": self.anchors,
            "scene_summary": self.scene_summary,
            "prompt": self.prompt,
            "reference_image": self.reference_image,
        }


def read_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    env_file = Path(os.getenv("AUTOGEN_ENV_FILE", str(ROOT / ".env"))).expanduser()
    load_dotenv(env_file)
    username = os.getenv("ILINKBOT_INTEGRATION_ACCOUNT") or os.getenv("SITE_USERNAME", "")
    password = os.getenv("SITE_PASSWORD", "")
    keychain_service = os.getenv("ILINKBOT_KEYCHAIN_SERVICE", "")
    if keychain_service:
        from src.core.integrations.keychain import (
            KeychainError,
            KeychainReference,
            KeychainService,
        )

        try:
            password = KeychainService().get_secret(
                KeychainReference(
                    keychain_service,
                    os.getenv("ILINKBOT_KEYCHAIN_ACCOUNT", "credential"),
                )
            )
        except KeychainError as exc:
            raise PreflightError("当前用户的悟空 AI 凭据不可用。") from exc
    required = {"SITE_USERNAME": username, "SITE_PASSWORD": password}
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise PreflightError("缺少运行配置：" + "、".join(missing) + "。请配置 AutoGen 凭据文件。")
    return Config(
        base_url=os.getenv("SITE_BASE_URL", "https://sjmaoyi.com").rstrip("/"),
        username=required["SITE_USERNAME"].strip(),
        password=required["SITE_PASSWORD"],
        generation_timeout_seconds=int(os.getenv("GENERATION_TIMEOUT_SECONDS", "600")),
        poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "3")),
        headless=read_bool(os.getenv("HEADLESS"), True),
        browser_executable=os.getenv("BROWSER_EXECUTABLE") or None,
    )


def daily_directory(now: datetime) -> Path:
    path = DATA_ROOT / now.strftime("%Y-%m-%d")
    path.mkdir(parents=True, exist_ok=True)
    return path


def story_state_path() -> Path:
    return DATA_ROOT / STORY_STATE_FILE


def load_story_state() -> StoryState | None:
    path = story_state_path()
    if not path.exists():
        return None
    try:
        return StoryState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise PreflightError(f"故事状态文件不可用：{exc}") from exc


def save_story_state(state: StoryState) -> None:
    """Atomically commit a frame only after its images have been saved."""
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    path = story_state_path()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def reference_image_path(state: StoryState | None) -> Path | None:
    if state is None or not state.reference_image:
        return None
    candidate = Path(state.reference_image)
    root = DATA_ROOT.resolve()
    path = candidate if candidate.is_absolute() else root / candidate
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PreflightError("故事参考帧不在当前 AutoGen 数据目录中。") from exc
    if not resolved.is_file():
        raise PreflightError("上一场景的参考帧文件不存在，无法安全续写故事。")
    return resolved


def report_value(value: str) -> str:
    return " ".join(str(value).replace("\x00", "").split())


def append_report(day_dir: Path, result: RunResult) -> None:
    finished = result.finished_at or datetime.now()
    image_files = result.image_files or []
    elapsed = "-" if result.elapsed_seconds is None else f"{result.elapsed_seconds:.1f} 秒"
    image_list = "、".join(path.name for path in image_files) if image_files else "无"
    story = result.story
    story_lines = ""
    if story is not None:
        story_lines = (
            f"故事编号：{story.story_id}\n"
            f"场景序号：{story.scene_number}\n"
            f"参考帧：{story.reference_image or '首场景（无）'}\n"
            f"5 秒事件：{report_value(story.scene_summary) or '未生成'}\n"
        )
    website_lines = ""
    if result.website_task_id or result.website_task_status or result.website_task_detail:
        website_lines = (
            f"网站任务编号：{report_value(result.website_task_id) or '未返回'}\n"
            f"网站最后状态：{report_value(result.website_task_status) or '未返回'}\n"
            f"网站进度/错误：{report_value(result.website_task_detail) or '未返回'}\n"
        )
    block = textwrap.dedent(
        f"""
        [{result.run_id}]
        开始时间：{result.started_at.strftime('%Y-%m-%d %H:%M:%S')}
        结束时间：{finished.strftime('%Y-%m-%d %H:%M:%S')}
        状态：{result.status}
        耗时：{elapsed}
        图像提示词：{report_value(result.prompt) or '未生成'}
        {story_lines.rstrip()}
        {website_lines.rstrip()}
        图片文件：{image_list}
        说明：{report_value(result.detail) or '无'}

        """
    ).lstrip()
    with (day_dir / "report.txt").open("a", encoding="utf-8") as handle:
        handle.write(block)


def format_wechat_notification(result: RunResult) -> str:
    status_labels = {
        "success": "成功",
        "failed": "失败",
        "timeout": "超时",
        "authentication_blocked": "登录受阻",
        "preflight_failed": "预检失败",
    }
    finished = result.finished_at or datetime.now()
    elapsed = "-" if result.elapsed_seconds is None else f"{result.elapsed_seconds:.1f} 秒"
    image_files = result.image_files or []
    image_list = "、".join(path.name for path in image_files) if image_files else "无"
    status = status_labels.get(result.status, result.status or "未知")
    story = result.story
    story_details = []
    if story is not None:
        story_details = [
            f"故事编号：{story.story_id}",
            f"场景序号：{story.scene_number}",
            f"参考帧：{story.reference_image or '首场景（无）'}",
            f"5 秒事件：{report_value(story.scene_summary) or '未生成'}",
        ]
    website_details = []
    if result.website_task_id or result.website_task_status or result.website_task_detail:
        website_details = [
            f"网站任务编号：{report_value(result.website_task_id) or '未返回'}",
            f"网站最后状态：{report_value(result.website_task_status) or '未返回'}",
            f"网站进度/错误：{report_value(result.website_task_detail) or '未返回'}",
        ]
    return "\n".join(
        (
            "【文生图监控结果】",
            f"任务编号：{result.run_id}",
            f"状态：{status}（{result.status or 'unknown'}）",
            f"开始时间：{result.started_at.strftime('%Y-%m-%d %H:%M:%S')}",
            f"结束时间：{finished.strftime('%Y-%m-%d %H:%M:%S')}",
            f"耗时：{elapsed}",
            f"图像提示词：{report_value(result.prompt) or '未生成'}",
            *story_details,
            *website_details,
            f"图片文件：{image_list}",
            f"说明：{report_value(result.detail) or '无'}",
        )
    )


def normalize_prompt(content: object) -> str:
    text = str(content or "").strip().strip('"“”')
    text = re.sub(r"\s+", " ", text)
    if not text:
        raise PreflightError("默认语言模型未返回可用图像提示词。")
    return text[:1200]


class DefaultLanguageModel:
    """Adapter that gives the script the main process' model-client contract."""

    def __init__(self, client) -> None:
        self.client = client

    def invoke(self, messages) -> str:
        from src.core.modeling.contracts import (
            CanonicalMessage,
            GenerationOptions,
            ModelRequest,
        )

        request = ModelRequest(
            messages=[
                CanonicalMessage(
                    "system" if item.type == "system" else "user",
                    str(item.content),
                )
                for item in messages
            ],
            generation=GenerationOptions(temperature=0.3),
        )
        return self.client.complete(request).message.content

    def close(self) -> None:
        self.client.close()


def create_default_language_model() -> DefaultLanguageModel:
    """Load the active application profile instead of using a local model."""
    try:
        from src.core.config.loader import load_project_config
        from src.core.modeling.factory import create_model_client
    except ImportError as exc:
        raise PreflightError("缺少主进程模型客户端依赖。") from exc
    config_directory = Path(
        os.getenv("ILINKBOT_PROJECT_CONFIG", str(ROOT.parents[3] / "config"))
    )
    try:
        project_config = load_project_config(config_directory)
        profile_id = os.getenv("AUTOGEN_MODEL_PROFILE") or project_config.app.active_model
        profile = project_config.models[profile_id]
        if not profile.enabled:
            raise ValueError("默认模型档案未启用")
        client = create_model_client(profile)
        client.ensure_ready()
    except Exception as exc:
        raise PreflightError(f"无法使用主进程默认语言模型：{exc}") from exc
    return DefaultLanguageModel(client)


def _story_json(content: object) -> dict[str, object]:
    text = str(content or "").strip()
    if "```" in text:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
        if match:
            text = match.group(1)
    if not text.startswith("{"):
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            text = match.group(0)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PreflightError("默认语言模型未按要求返回故事场景 JSON。") from exc
    if not isinstance(payload, dict):
        raise PreflightError("默认语言模型返回的故事场景不是 JSON 对象。")
    return payload


def _required_story_text(payload: dict[str, object], name: str, limit: int = 1200) -> str:
    value = normalize_prompt(payload.get(name, ""))
    return value[:limit]


def _is_chinese_story_text(value: str) -> bool:
    """Accept Chinese-dominant prompts while rejecting English prose with a Chinese name."""
    chinese = len(re.findall(r"[\u4e00-\u9fff]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    return chinese >= 4 and chinese >= latin


def _require_chinese_story_payload(payload: dict[str, object], fields: tuple[str, ...]) -> None:
    invalid = [
        field for field in fields
        if not _is_chinese_story_text(_required_story_text(payload, field))
    ]
    if invalid:
        raise PreflightError("默认语言模型未使用简体中文输出字段：" + "、".join(invalid))


def _generate_chinese_story_payload(model, instruction: str, fields: tuple[str, ...]) -> dict[str, object]:
    """Generate once, then make one bounded correction attempt if language drifts."""
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content="你是中文图像提示词生成器。所有输出必须使用简体中文。"),
        HumanMessage(content=instruction),
    ]
    first_content = ""
    try:
        first_response = model.invoke(messages)
        first_content = getattr(first_response, "content", first_response)
        payload = _story_json(first_content)
        _require_chinese_story_payload(payload, fields)
        return payload
    except PreflightError:
        correction = (
            "你刚才的 JSON 未完全使用简体中文。请重新输出同一内容的严格 JSON，"
            "不要保留英文单词、英文姓名、英文风格名或拼音；所有值都必须为简体中文。"
            f"必须包含字段：{'、'.join(fields)}。只输出 JSON。\n"
            f"原始要求：{instruction}\n上一次输出：{first_content}"
        )
        try:
            corrected_response = model.invoke([
                SystemMessage(content="你是中文图像提示词生成器。"),
                HumanMessage(content=correction),
            ])
            payload = _story_json(getattr(corrected_response, "content", corrected_response))
            _require_chinese_story_payload(payload, fields)
            return payload
        except Exception as exc:
            raise PreflightError("默认语言模型两次均未返回合格的简体中文故事提示词。") from exc


def generate_story_scene(config: Config, previous: StoryState | None, run_id: str) -> StoryState:
    model = create_default_language_model()
    if previous is None:
        instruction = (
            "创建一个可长期连载的简体中文图像故事的第 1 场。随机选择原创题材，避免品牌、文字、水印和敏感内容。"
            "所有字段必须只用简体中文表达，不得出现英文、拼音或英文风格名。"
            "输出严格 JSON，不要 Markdown："
            '{"story_bible":"世界观与长期主线","anchors":"固定角色外观、服装、场景、主色调、电影风格与镜头语言",'
            '"scene_summary":"本场可见的关键画面","prompt":"可直接用于文生图的中文提示词"}。'
            "prompt 必须包含 anchors，并描述一个稳定、可作为续帧起点的电影画面。"
        )
    else:
        instruction = (
            "为以下持续连载图像故事写下一场。它必须是上一场约 5 秒后的一个关键画面，而不是新主题。"
            "保持角色外观、服装、世界观、主色调、电影风格和相近机位；只推进一个清晰可视事件，"
            "让首尾帧视频可自然衔接。避免品牌、文字、水印和敏感内容。所有字段必须只用简体中文表达，"
            "不得出现英文、拼音或英文风格名。输出严格 JSON，不要 Markdown："
            '{"scene_summary":"5 秒后发生的单一可见变化","prompt":"可直接用于文生图的中文提示词"}。\n'
            f"故事设定：{previous.story_bible}\n固定锚点：{previous.anchors}\n"
            f"上一场景（第 {previous.scene_number} 场）：{previous.scene_summary}\n"
            f"上一场提示词：{previous.prompt}"
        )
    fields = ("story_bible", "anchors", "scene_summary", "prompt") if previous is None else ("scene_summary", "prompt")
    try:
        payload = _generate_chinese_story_payload(model, instruction, fields)
    except PreflightError:
        raise
    except Exception as exc:
        raise PreflightError(f"默认语言模型提示词生成失败：{exc}") from exc
    finally:
        model.close()
    if previous is None:
        return StoryState(
            story_id="story-" + uuid.uuid4().hex[:12],
            scene_number=1,
            story_bible=_required_story_text(payload, "story_bible"),
            anchors=_required_story_text(payload, "anchors"),
            scene_summary=_required_story_text(payload, "scene_summary"),
            prompt=_required_story_text(payload, "prompt"),
        )
    return StoryState(
        story_id=previous.story_id,
        scene_number=previous.scene_number + 1,
        story_bible=previous.story_bible,
        anchors=previous.anchors,
        scene_summary=_required_story_text(payload, "scene_summary"),
        prompt=_required_story_text(payload, "prompt"),
        reference_image=previous.reference_image,
    )


def assert_site_reachable(config: Config) -> None:
    try:
        with httpx.Client(timeout=20, follow_redirects=True, trust_env=False) as client:
            response = client.get(config.base_url + LOGIN_PATH)
            response.raise_for_status()
    except Exception as exc:
        raise PreflightError(f"无法访问目标网站：{exc}") from exc


def first_visible_locator(scope, selectors: Iterable[str]):
    """Return the first visible match without trusting hidden legacy nodes."""
    for selector in selectors:
        matches = scope.locator(selector)
        for index in range(matches.count()):
            candidate = matches.nth(index)
            if candidate.is_visible():
                return candidate
    return None


def visible_message(page, selectors: Iterable[str]) -> str | None:
    message = first_visible_locator(page, selectors)
    if message is None:
        return None
    text = message.inner_text().strip()
    return text or None


def login_challenge_message(page) -> str | None:
    selectors = (
        'iframe[src*="captcha"]',
        '[class*="captcha"]',
        '[id*="captcha"]',
        ':text("人机验证")',
        ':text("滑动验证")',
        ':text("短信验证码")',
    )
    challenge = first_visible_locator(page, selectors)
    if challenge is None:
        return None
    return "登录需要验证码或人机验证，自动化已停止。"


def ensure_logged_in(page, config: Config) -> None:
    page.goto(config.base_url + LOGIN_PATH, wait_until="domcontentloaded")
    if LOGIN_PATH not in page.url:
        return

    password_input = first_visible_locator(page, LOGIN_PASSWORD_SELECTORS)
    if password_input is None:
        # The refreshed login page defaults to an SMS panel and leaves the old
        # #loginForm in the DOM but hidden. Switch to a password/account panel.
        for selector in LOGIN_MODE_SELECTORS:
            tab = first_visible_locator(page, (selector,))
            if tab is None:
                continue
            tab.click()
            page.wait_for_timeout(300)
            password_input = first_visible_locator(page, LOGIN_PASSWORD_SELECTORS)
            if password_input is not None:
                break

    if password_input is None:
        challenge = login_challenge_message(page)
        if challenge:
            raise AuthenticationBlocked(challenge)
        raise PageStructureChanged("登录页未找到可见的密码登录表单；网站登录结构可能已更新。")

    forms = password_input.locator("xpath=ancestor::form[1]")
    form = forms.nth(0) if forms.count() else page
    account_input = first_visible_locator(form, LOGIN_ACCOUNT_SELECTORS)
    submit = first_visible_locator(form, LOGIN_SUBMIT_SELECTORS)
    if account_input is None or submit is None:
        raise PageStructureChanged("密码登录表单缺少账号输入框或登录按钮；网站登录结构可能已更新。")

    account_input.fill(config.username)
    password_input.fill(config.password)
    submit.click()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        error = visible_message(
            page,
            ("#loginError", ".login-error", "form:visible [role=alert]", ".alert-danger"),
        )
        if error:
            raise AuthenticationBlocked("登录失败：" + error)
        if LOGIN_PATH not in page.url:
            return
        challenge = login_challenge_message(page)
        if challenge:
            raise AuthenticationBlocked(challenge)
        page.wait_for_timeout(500)
    raise AuthenticationBlocked("登录未在 20 秒内完成。")


def image_urls_from_result(page) -> list[str]:
    """Return only the full-size URLs explicitly published by result cards.

    Result cards render a nested ``img`` for their preview.  Its ``src`` can
    point to a thumbnail of the first candidate, so it must not be treated as
    another generated image.
    """
    urls = page.locator(RESULT_IMAGE_SELECTOR).evaluate_all(
        "elements => elements.map(element => element.getAttribute('data-img-url')).filter(Boolean)"
    )
    unique: list[str] = []
    for url in urls:
        absolute = urljoin(page.url, str(url))
        if absolute not in unique:
            unique.append(absolute)
    return unique


def check_generation_error(page) -> str | None:
    return visible_message(page, GENERATION_ERROR_SELECTORS)


def visible_blocking_modal(page):
    return first_visible_locator(page, MODAL_OVERLAY_SELECTORS)


def concise_modal_text(modal) -> str:
    try:
        text = report_value(modal.inner_text(timeout=2_000))
    except Exception:
        text = ""
    return text[:240] or "未提供文字说明"


def wait_until_modal_hidden(page, modal, timeout_ms: int = 3_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if not modal.is_visible():
            return True
        page.wait_for_timeout(100)
    return not modal.is_visible()


def dismiss_safe_blocking_modals(page) -> None:
    """Dismiss non-sensitive page announcements that cover generation controls."""
    for _ in range(3):
        modal = visible_blocking_modal(page)
        if modal is None:
            return
        text = concise_modal_text(modal)
        if any(word in text for word in MODAL_MANUAL_CONFIRM_WORDS):
            raise GenerationFailed("页面出现需要人工确认的弹窗，自动化未操作：" + text)

        close = first_visible_locator(modal, SAFE_MODAL_CLOSE_SELECTORS)
        if close is not None:
            close.click()
        else:
            # Escape is a non-destructive fallback for announcement/tutorial
            # dialogs that omit an accessible close button.
            page.keyboard.press("Escape")
        if not wait_until_modal_hidden(page, modal):
            raise GenerationFailed("页面弹窗遮挡生成按钮且无法安全关闭：" + text)
    if visible_blocking_modal(page) is not None:
        raise GenerationFailed("页面连续出现多个遮挡弹窗，自动化已停止。")


def set_image_count(page) -> None:
    control = first_visible_locator(page, IMAGE_COUNT_SELECTORS)
    if control is not None:
        tag_name = control.evaluate("element => element.tagName.toLowerCase()")
        input_type = control.get_attribute("type") or ""
        if tag_name == "select":
            control.select_option(str(EXPECTED_IMAGE_COUNT))
        elif input_type.lower() == "number":
            control.fill(str(EXPECTED_IMAGE_COUNT))
        if control.input_value() == str(EXPECTED_IMAGE_COUNT):
            return

    radio = page.locator(
        f'input[type="radio"][value="{EXPECTED_IMAGE_COUNT}"], '
        f'input[type="radio"][data-value="{EXPECTED_IMAGE_COUNT}"]'
    )
    for index in range(radio.count()):
        candidate = radio.nth(index)
        if candidate.is_visible():
            candidate.check()
            return

    button = first_visible_locator(
        page,
        (
            f'[data-count="{EXPECTED_IMAGE_COUNT}"]',
            f'[data-value="{EXPECTED_IMAGE_COUNT}"]',
            f'button:has-text("{EXPECTED_IMAGE_COUNT} 张")',
        ),
    )
    if button is not None:
        button.click()
        return
    raise GenerationFailed(f"未找到可设置为 {EXPECTED_IMAGE_COUNT} 张的生成数量控件。")


def reference_upload_input(page):
    """Find the target site's dedicated image-to-image source input."""
    for selector in REFERENCE_IMAGE_SELECTORS:
        matches = page.locator(selector)
        for index in range(matches.count()):
            candidate = matches.nth(index)
            try:
                if candidate.is_enabled():
                    return candidate
            except Exception:
                continue
    return None


def require_reference_upload(page):
    upload = reference_upload_input(page)
    if upload is None:
        raise PreflightError("文生图页面未检测到可用的参考图/图生图上传控件，已停止以避免生成非连续场景。")
    return upload


def select_passthrough_reference(page) -> None:
    control = first_visible_locator(page, (REFERENCE_PASSTHROUGH_SELECTOR,))
    if control is None:
        # The native radio can be visually hidden by a custom widget, while
        # still being the actual form control Playwright must select.
        controls = page.locator(REFERENCE_PASSTHROUGH_SELECTOR)
        if controls.count() == 1:
            control = controls.nth(0)
    if control is None:
        raise PreflightError("图生图页面未找到“透传原图”选项，无法确认参考帧会参与生成。")
    try:
        control.check()
        enabled = control.evaluate("element => element.checked && element.value === 'passthrough'")
    except Exception as exc:
        raise GenerationFailed("无法启用图生图的“透传原图”模式。") from exc
    if not enabled:
        raise GenerationFailed("图生图未确认使用“透传原图”模式。")


def upload_reference_image(page, reference_image: Path) -> None:
    upload = require_reference_upload(page)
    try:
        upload.set_input_files(str(reference_image))
    except Exception as exc:
        raise GenerationFailed("上传上一场景参考帧失败，故事状态未推进。") from exc
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        preview = first_visible_locator(page, (REFERENCE_PREVIEW_SELECTOR,))
        if preview is not None:
            select_passthrough_reference(page)
            return
        page.wait_for_timeout(250)
    raise GenerationFailed("参考帧上传后未出现图生图预览，故事状态未推进。")


def assert_image_to_image_available(page, config: Config) -> None:
    """Verify the actual image-to-image route before creating a new story."""
    page.goto(config.base_url + IMAGE_TO_IMAGE_PATH, wait_until="domcontentloaded")
    require_reference_upload(page)


def generation_image_path(reference_image: Path | None) -> str:
    return IMAGE_TO_IMAGE_PATH if reference_image is not None else TEXT_TO_IMAGE_PATH


def attach_website_task_tracker(page, result: RunResult) -> WebsiteTaskTracker:
    tracker = WebsiteTaskTracker()

    def on_response(response) -> None:
        try:
            url = str(response.url)
            if "/api/general-ai/generate-image" in url:
                tracker.observe_submission(response.json())
            elif "/api/general-ai/task/" in url:
                tracker.observe_task(response.json())
            else:
                return
            tracker.apply_to(result)
        except Exception:
            # Page response tracing must never break the generation workflow.
            return

    page.on("response", on_response)
    return tracker


def website_timeout_detail(tracker: WebsiteTaskTracker, timeout_seconds: int, image_count: int) -> str:
    if not tracker.task_id:
        return (
            f"等待 {timeout_seconds} 秒后仍未得到 {EXPECTED_IMAGE_COUNT} 张图片；"
            f"已得到 {image_count} 张。网站未返回任务编号，无法在后台追踪。"
        )
    status = tracker.status or "未返回状态"
    progress = report_value(tracker.detail) or "未返回进度"
    return (
        f"等待 {timeout_seconds} 秒后仍未得到 {EXPECTED_IMAGE_COUNT} 张图片；"
        f"已得到 {image_count} 张。网站任务 {tracker.task_id} 最后状态：{status}；"
        f"进度/错误：{progress}。"
    )


def submit_and_wait(
    page, config: Config, prompt: str, reference_image: Path | None, result: RunResult
) -> list[str]:
    page.goto(config.base_url + generation_image_path(reference_image), wait_until="domcontentloaded")
    prompt_input = first_visible_locator(page, PROMPT_SELECTORS)
    if prompt_input is None:
        deadline = time.monotonic() + 25
        while prompt_input is None and time.monotonic() < deadline:
            page.wait_for_timeout(250)
            prompt_input = first_visible_locator(page, PROMPT_SELECTORS)
    if prompt_input is None:
        raise GenerationFailed("文生图页面未找到可见的提示词输入框；网站页面结构可能已更新。")

    if reference_image is not None:
        upload_reference_image(page, reference_image)

    # The old page required #imageModelChoice to be populated. The refreshed
    # page may not expose that select at all, so it is no longer a hard gate.
    model_choice = first_visible_locator(page, ("#imageModelChoice",))
    if model_choice is not None:
        try:
            page.wait_for_function(
                "() => document.querySelector('#imageModelChoice')?.options.length > 0",
                timeout=10_000,
            )
        except Exception as exc:
            raise GenerationFailed("图像模型列表未能加载。") from exc

    set_image_count(page)
    prompt_input.fill(prompt)
    form = prompt_input.locator("xpath=ancestor::form[1]")
    submit_scope = form.nth(0) if form.count() else page
    submit = first_visible_locator(submit_scope, SUBMIT_SELECTORS)
    if submit is None and submit_scope is not page:
        submit = first_visible_locator(page, SUBMIT_SELECTORS)
    if submit is None:
        raise GenerationFailed("文生图页面未找到可见的生成按钮；网站页面结构可能已更新。")
    dismiss_safe_blocking_modals(page)
    tracker = attach_website_task_tracker(page, result)
    try:
        submit.click()
    except Exception:
        # A site announcement can be mounted between the pre-check and click.
        # Retry once only when a visible modal explains the interception.
        if visible_blocking_modal(page) is None:
            raise
        dismiss_safe_blocking_modals(page)
        submit.click()

    deadline = time.monotonic() + config.generation_timeout_seconds
    latest_urls: list[str] = []
    while time.monotonic() < deadline:
        latest_urls = image_urls_from_result(page)
        error = check_generation_error(page)
        if error:
            raise GenerationFailed(error, latest_urls)
        if tracker.status.lower() == "failed":
            detail = report_value(tracker.detail) or "网站未提供失败原因"
            raise GenerationFailed(f"网站任务 {tracker.task_id or '未返回编号'} 失败：{detail}", latest_urls)
        if len(latest_urls) >= EXPECTED_IMAGE_COUNT:
            return latest_urls[:EXPECTED_IMAGE_COUNT]
        page.wait_for_timeout(int(config.poll_interval_seconds * 1000))
    raise GenerationTimedOut(
        website_timeout_detail(tracker, config.generation_timeout_seconds, len(latest_urls)),
        latest_urls,
    )


def image_extension(url: str, content_type: str | None) -> str:
    if content_type:
        extension = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if extension:
            return ".jpg" if extension == ".jpe" else extension
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


def download_images(urls: Iterable[str], day_dir: Path, run_id: str) -> list[Path]:
    files: list[Path] = []
    with httpx.Client(timeout=60, follow_redirects=True, trust_env=False) as client:
        for index, url in enumerate(urls, start=1):
            response = client.get(url)
            response.raise_for_status()
            content_type = response.headers.get("content-type")
            if content_type and not content_type.lower().startswith("image/"):
                raise GenerationFailed(f"第 {index} 张返回的不是图片：{content_type}")
            data = response.content
            if len(data) < 100:
                raise GenerationFailed(f"第 {index} 张图片内容异常。")
            target = day_dir / f"{run_id}-image-{index:02d}{image_extension(url, content_type)}"
            target.write_bytes(data)
            files.append(target)
    return files


def run_browser_check(
    config: Config, day_dir: Path, result: RunResult, reference_image: Path | None
) -> None:
    browser_config = BrowserAutomationConfig(
        headless=config.headless,
        executable_path=config.browser_executable,
        navigation_timeout_seconds=30,
    )
    try:
        with BrowserAutomation(browser_config).session() as session:
            page = session.page
            if page is None:
                raise PreflightError("浏览器页面创建失败。")
            page.set_viewport_size({"width": 1440, "height": 1100})
            ensure_logged_in(page, config)
            if reference_image is None:
                # A new story starts with text-to-image, but it is only useful
                # when the next scene can use the site's real image-to-image
                # mode. Verify that contract before spending generation quota.
                assert_image_to_image_available(page, config)
            try:
                urls = submit_and_wait(page, config, result.prompt, reference_image, result)
            except GenerationFailed as exc:
                # A partial result is still useful evidence and must be retained.
                if exc.image_urls:
                    result.image_files = download_images(exc.image_urls, day_dir, result.run_id)
                raise
            result.image_files = download_images(urls, day_dir, result.run_id)
            if len(result.image_files) != EXPECTED_IMAGE_COUNT:
                raise GenerationFailed(f"仅成功保存 {len(result.image_files)} 张图片。")
    except BrowserUnavailableError as exc:
        raise PreflightError(str(exc)) from exc


def committed_story_frame(scene: StoryState, image_files: list[Path]) -> StoryState:
    if not image_files:
        raise GenerationFailed("未保存第 1 张候选图，无法提交故事状态。")
    try:
        reference = image_files[0].resolve().relative_to(DATA_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise GenerationFailed("候选图不在当前 AutoGen 数据目录中，无法作为后续参考帧。") from exc
    return StoryState(
        story_id=scene.story_id,
        scene_number=scene.scene_number,
        story_bible=scene.story_bible,
        anchors=scene.anchors,
        scene_summary=scene.scene_summary,
        prompt=scene.prompt,
        reference_image=reference,
    )


def run_once(reset_story: bool = False) -> RunResult:
    started = datetime.now()
    run_id = started.strftime("run-%H%M%S")
    result = RunResult(
        run_id=run_id, started_at=started, image_files=[], reset_story=reset_story
    )
    day_dir = daily_directory(started)
    try:
        config = load_config()
        assert_site_reachable(config)
        previous = None if reset_story else load_story_state()
        reference = reference_image_path(previous)
        scene = generate_story_scene(config, previous, run_id)
        result.prompt = scene.prompt
        result.story = scene
        run_browser_check(config, day_dir, result, reference)
        result.story = committed_story_frame(scene, result.image_files or [])
        save_story_state(result.story)
        result.status = "success"
        result.detail = (
            f"成功生成并保存 {EXPECTED_IMAGE_COUNT} 张图片，"
            f"已提交故事第 {result.story.scene_number} 场。"
        )
    except AuthenticationBlocked as exc:
        result.status = "authentication_blocked"
        result.detail = str(exc)
    except PageStructureChanged as exc:
        result.status = "failed"
        result.detail = str(exc)
    except GenerationTimedOut as exc:
        result.status = "timeout"
        result.detail = str(exc)
    except GenerationFailed as exc:
        result.status = "failed"
        result.detail = str(exc)
    except PreflightError as exc:
        result.status = "preflight_failed"
        result.detail = str(exc)
    except Exception as exc:  # Unexpected faults must still be recorded locally.
        result.status = "failed"
        result.detail = f"未预期异常：{type(exc).__name__}: {exc}"
    finally:
        result.finished_at = datetime.now()
        append_report(day_dir, result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行一次悟空 AI 文生图监控。")
    parser.add_argument("--self-test", action="store_true", help="运行内置文件输出测试，不访问模型或网站。")
    parser.add_argument(
        "--reset-story",
        nargs="?",
        const="true",
        choices=("true",),
        help="本次成功后以新的随机故事替换当前连载状态。",
    )
    return parser.parse_args()


def self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        result = RunResult(
            run_id="run-080000",
            started_at=datetime(2026, 7, 10, 8, 0, 0),
            prompt="测试提示词\n第二行",
            status="success",
            detail="成功",
            finished_at=datetime(2026, 7, 10, 8, 0, 5),
            image_files=[directory / "run-080000-image-01.jpg", directory / "run-080000-image-02.jpg"],
        )
        append_report(directory, result)
        report = (directory / "report.txt").read_text(encoding="utf-8")
        assert "状态：success" in report
        assert "耗时：5.0 秒" in report
        assert "测试提示词 第二行" in report
        assert "image-02.jpg" in report
    print("自检通过：报告写入和文本清理正常。")
    return 0


def main() -> int:
    args = parse_args()
    if args.self_test:
        return self_test()
    result = run_once(reset_story=args.reset_story == "true")
    elapsed = "-" if result.elapsed_seconds is None else f"{result.elapsed_seconds:.1f} 秒"
    print(f"[{result.run_id}] {result.status}，耗时 {elapsed}。{result.detail}")
    result_file = os.getenv("ILINKBOT_SCRIPT_RESULT_FILE")
    if result_file:
        path = Path(result_file)
        temporary = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "status": "success" if result.status == "success" else "failed",
            "summary": format_wechat_notification(result),
            "artifacts": [str(item.resolve()) for item in result.image_files or []],
            "error": "" if result.status == "success" else report_value(result.detail),
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
