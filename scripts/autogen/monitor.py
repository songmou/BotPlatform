#!/usr/bin/env python3
"""Run one end-to-end text-to-image health-monitoring check.

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
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import httpx
from dotenv import load_dotenv
from src.plugins.browser_automation import (
    BrowserAutomation,
    BrowserAutomationConfig,
    BrowserUnavailableError,
)


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("ILINKBOT_SCRIPT_DATA_ROOT", str(ROOT))).expanduser().resolve()
LOGIN_PATH = "/account/login"
IMAGE_PATH = "/general/text-to-image"
EXPECTED_IMAGE_COUNT = 2

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
        "#resultContainer img[src]",
        "[data-result-container] [data-img-url]",
        "[data-result-container] img[src]",
        ".generation-result [data-img-url]",
        ".generation-result img[src]",
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
    ollama_base_url: str
    ollama_model: str
    ollama_timeout_seconds: float
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

    @property
    def elapsed_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return max(0.0, (self.finished_at - self.started_at).total_seconds())


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
        from src.integrations.keychain import (
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
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        ollama_model=os.getenv("OLLAMA_MODEL", "gemma4:e4b"),
        ollama_timeout_seconds=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "30")),
        generation_timeout_seconds=int(os.getenv("GENERATION_TIMEOUT_SECONDS", "600")),
        poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "3")),
        headless=read_bool(os.getenv("HEADLESS"), True),
        browser_executable=os.getenv("BROWSER_EXECUTABLE") or None,
    )


def daily_directory(now: datetime) -> Path:
    path = DATA_ROOT / now.strftime("%Y-%m-%d")
    path.mkdir(parents=True, exist_ok=True)
    return path


def report_value(value: str) -> str:
    return " ".join(str(value).replace("\x00", "").split())


def append_report(day_dir: Path, result: RunResult) -> None:
    finished = result.finished_at or datetime.now()
    image_files = result.image_files or []
    elapsed = "-" if result.elapsed_seconds is None else f"{result.elapsed_seconds:.1f} 秒"
    image_list = "、".join(path.name for path in image_files) if image_files else "无"
    block = textwrap.dedent(
        f"""
        [{result.run_id}]
        开始时间：{result.started_at.strftime('%Y-%m-%d %H:%M:%S')}
        结束时间：{finished.strftime('%Y-%m-%d %H:%M:%S')}
        状态：{result.status}
        耗时：{elapsed}
        图像提示词：{report_value(result.prompt) or '未生成'}
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
    return "\n".join(
        (
            "【文生图监控结果】",
            f"任务编号：{result.run_id}",
            f"状态：{status}（{result.status or 'unknown'}）",
            f"开始时间：{result.started_at.strftime('%Y-%m-%d %H:%M:%S')}",
            f"结束时间：{finished.strftime('%Y-%m-%d %H:%M:%S')}",
            f"耗时：{elapsed}",
            f"图像提示词：{report_value(result.prompt) or '未生成'}",
            f"图片文件：{image_list}",
            f"说明：{report_value(result.detail) or '无'}",
        )
    )


def normalize_prompt(content: object) -> str:
    text = str(content or "").strip().strip('"“”')
    text = re.sub(r"\s+", " ", text)
    if not text:
        raise PreflightError("Gemma 未返回可用图像提示词。")
    return text[:1200]


def check_ollama(config: Config) -> None:
    try:
        with httpx.Client(timeout=config.ollama_timeout_seconds, trust_env=False) as client:
            response = client.get(f"{config.ollama_base_url}/api/tags")
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise PreflightError(f"无法连接本地 Ollama：{exc}") from exc
    models = payload.get("models", []) if isinstance(payload, dict) else []
    names = {str(model.get("name", "")) for model in models if isinstance(model, dict)}
    if config.ollama_model not in names:
        raise PreflightError(f"Ollama 中未找到模型 {config.ollama_model}。")


def generate_prompt(config: Config) -> str:
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_ollama import ChatOllama
    except ImportError as exc:
        raise PreflightError("缺少 LangChain/Ollama 依赖；请使用 gemma4-e4b-langchain 的虚拟环境运行。") from exc

    model = ChatOllama(
        model=config.ollama_model,
        base_url=config.ollama_base_url,
        temperature=0.95,
        sync_client_kwargs={"timeout": config.ollama_timeout_seconds, "trust_env": False},
    )
    instruction = (
        "生成一条可直接用于文生图的随机中文提示词。主题、构图、风格、光影每次都变化；"
        "避免人物肖像、品牌、文字、水印和敏感内容。只输出一条提示词，不要解释、标题或引号。"
    )
    try:
        response = model.invoke([SystemMessage(content="你是图像提示词生成器。"), HumanMessage(content=instruction)])
    except Exception as exc:
        raise PreflightError(f"Gemma 提示词生成失败：{exc}") from exc
    return normalize_prompt(getattr(response, "content", response))


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
    urls = page.locator(RESULT_IMAGE_SELECTOR).evaluate_all(
        """elements => elements.map(element =>
            element.getAttribute('data-img-url') ||
            element.currentSrc || element.getAttribute('src')
        ).filter(Boolean)"""
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


def submit_and_wait(page, config: Config, prompt: str) -> list[str]:
    page.goto(config.base_url + IMAGE_PATH, wait_until="domcontentloaded")
    prompt_input = first_visible_locator(page, PROMPT_SELECTORS)
    if prompt_input is None:
        deadline = time.monotonic() + 25
        while prompt_input is None and time.monotonic() < deadline:
            page.wait_for_timeout(250)
            prompt_input = first_visible_locator(page, PROMPT_SELECTORS)
    if prompt_input is None:
        raise GenerationFailed("文生图页面未找到可见的提示词输入框；网站页面结构可能已更新。")

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
        if len(latest_urls) >= EXPECTED_IMAGE_COUNT:
            return latest_urls[:EXPECTED_IMAGE_COUNT]
        page.wait_for_timeout(int(config.poll_interval_seconds * 1000))
    raise GenerationTimedOut(
        f"等待 {config.generation_timeout_seconds} 秒后仍未得到 {EXPECTED_IMAGE_COUNT} 张图片；已得到 {len(latest_urls)} 张。",
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


def run_browser_check(config: Config, day_dir: Path, result: RunResult) -> None:
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
            try:
                urls = submit_and_wait(page, config, result.prompt)
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


def run_once() -> RunResult:
    started = datetime.now()
    run_id = started.strftime("run-%H%M%S")
    result = RunResult(run_id=run_id, started_at=started, image_files=[])
    day_dir = daily_directory(started)
    try:
        config = load_config()
        assert_site_reachable(config)
        check_ollama(config)
        result.prompt = generate_prompt(config)
        run_browser_check(config, day_dir, result)
        result.status = "success"
        result.detail = f"成功生成并保存 {EXPECTED_IMAGE_COUNT} 张图片。"
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
    result = run_once()
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
