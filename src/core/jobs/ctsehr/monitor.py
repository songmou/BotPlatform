#!/usr/bin/env python3
"""Read-only CTS EHR attendance and pending-approval background job."""

from __future__ import annotations

import argparse
import ast
import base64
import html
from html.parser import HTMLParser
import json
import logging
from logging.handlers import RotatingFileHandler
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import textwrap
import time
import warnings
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin


APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("ILINKBOT_SCRIPT_DATA_ROOT", str(APP_ROOT))).expanduser().resolve()
DEFAULT_CONFIG = APP_ROOT / "config.json"


class MonitorError(RuntimeError):
    """Base error for operational monitoring failures."""


class ConfigurationError(MonitorError):
    """Configuration is missing or invalid."""


class CredentialError(MonitorError):
    """The password could not be loaded from Keychain."""


class AuthenticationError(MonitorError):
    """CTS EHR authentication failed."""


class ResponseSchemaError(MonitorError):
    """A CTS EHR response no longer matches the expected schema."""


class LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden: Dict[str, str] = {}
        self.action = ""

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "form" and values.get("id") == "_Logon":
            self.action = values.get("action", "")
        if tag.lower() == "input" and values.get("type", "").lower() == "hidden":
            name = values.get("name", "")
            if name:
                self.hidden[name] = values.get("value", "")


def load_config(path: Path) -> Dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"配置文件不是有效 JSON：{exc}") from exc

    if not isinstance(config, dict):
        raise ConfigurationError("配置根节点必须是 JSON 对象")
    schemas = {
        "": {"oa", "attendance", "calendar", "approvals", "ollama", "output"},
        "oa": {"base_url", "account", "keychain_service", "timeout_seconds", "retries"},
        "attendance": {"clock_in_before", "clock_out_at_or_after"},
        "calendar": {"force_workdays", "force_offdays", "off_shift_keywords"},
        "approvals": {"max_items"},
        "ollama": {"base_url", "model", "reasoning", "timeout_seconds", "temperature", "num_predict"},
        "output": {"results_dir", "logs_dir", "font_path"},
    }
    unknown_root = sorted(set(config) - schemas[""])
    if unknown_root:
        raise ConfigurationError("配置包含未知字段：{}".format(", ".join(unknown_root)))
    for section, allowed in schemas.items():
        if not section:
            continue
        value = config.get(section, {})
        if not isinstance(value, dict):
            raise ConfigurationError("配置 {} 必须是 JSON 对象".format(section))
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ConfigurationError(
                "配置 {} 包含未知字段：{}".format(section, ", ".join(unknown))
            )

    required = [("oa", "base_url")]
    for section, key in required:
        if not config.get(section, {}).get(key):
            raise ConfigurationError(f"配置缺少 {section}.{key}")

    calendar = config.setdefault("calendar", {})
    workdays = set(calendar.setdefault("force_workdays", []))
    offdays = set(calendar.setdefault("force_offdays", []))
    overlap = workdays & offdays
    if overlap:
        raise ConfigurationError(f"工作日与休息日覆盖重复：{', '.join(sorted(overlap))}")
    for value in workdays | offdays:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ConfigurationError(f"无效日期覆盖：{value}") from exc

    return config


def resolve_output_path(config: Dict[str, Any], key: str, default: str) -> Path:
    value = Path(config.get("output", {}).get(key, default)).expanduser()
    resolved = (value if value.is_absolute() else DATA_ROOT / value).resolve()
    if resolved != DATA_ROOT and DATA_ROOT not in resolved.parents:
        raise ConfigurationError("output.{} 必须位于脚本数据目录内".format(key))
    return resolved


def prepare_directories(config: Dict[str, Any]) -> Tuple[Path, Path]:
    results = resolve_output_path(config, "results_dir", "results")
    logs = resolve_output_path(config, "logs_dir", "logs")
    for directory in (DATA_ROOT, results, logs):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    return results, logs


def configure_logging(log_dir: Path) -> logging.Logger:
    logger = logging.getLogger("cts_ehr")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        log_dir / "monitor.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def read_keychain_password(account: str, service: str) -> str:
    completed = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            account,
            "-s",
            service,
            "-w",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    password = completed.stdout.rstrip("\r\n")
    if completed.returncode != 0 or not password:
        raise CredentialError(
            "未能从 macOS 钥匙串读取 CTS EHR 密码，请在机器人私聊中使用 /integration setup ctsehr 配置凭据"
        )
    return password


def encrypt_password(password: str, key: str, initial: Optional[int] = None) -> str:
    """Replicate the CTS EHR page's current client-side password transform."""
    if not key:
        raise AuthenticationError("登录页缺少密码加密参数")
    state = secrets.randbelow(256) if initial is None else initial
    if not 0 <= state <= 255:
        raise ValueError("initial must be between 0 and 255")
    output = [f"{state:02x}"]
    key_bytes = key.encode("utf-8")
    for index, value in enumerate(password.encode("utf-8")):
        state = ((value + state) % 255) ^ key_bytes[index % len(key_bytes)]
        output.append(f"{state:02x}")
    return "".join(output)


def validate_login(final_url: str, cookie_names: Iterable[str], page_html: str) -> None:
    names = {name.lower() for name in cookie_names}
    error_match = re.search(
        r'id=["\']ErrorMessage["\'].*?<p[^>]*>(.*?)</p>', page_html, re.I | re.S
    )
    error_text = clean_text(error_match.group(1)) if error_match else ""
    if error_text:
        raise AuthenticationError(f"CTS EHR 登录失败：{error_text}")
    if "/account/logon.aspx" in final_url.lower() or ".formsauthcookie" not in names:
        raise AuthenticationError("CTS EHR 登录失败：未获得有效登录会话")


def clean_text(value: Any, limit: int = 240) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_json_list(raw: str, label: str) -> List[Dict[str, Any]]:
    if not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        try:
            value = ast.literal_eval(raw)
        except (SyntaxError, ValueError) as exc:
            raise ResponseSchemaError(f"{label} 返回内容不是有效列表") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ResponseSchemaError(f"{label} 返回结构发生变化")
    return value


def parse_flow_response(payload: Dict[str, Any], max_items: int = 20) -> Dict[str, Any]:
    if not isinstance(payload, dict) or str(payload.get("errcode")) != "0":
        message = clean_text(payload.get("message")) if isinstance(payload, dict) else "格式错误"
        raise ResponseSchemaError(f"待审批接口失败：{message or '未知错误'}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ResponseSchemaError("待审批接口缺少 data")
    totals = data.get("_t0", [])
    rows = data.get("_t1", [])
    if not isinstance(totals, list) or not isinstance(rows, list):
        raise ResponseSchemaError("待审批列表结构发生变化")
    try:
        total = int(totals[0].get("sum_num", 0)) if totals else 0
    except (AttributeError, TypeError, ValueError) as exc:
        raise ResponseSchemaError("待审批总数字段发生变化") from exc

    items = []
    for row in rows[:max_items]:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "requester": clean_text(row.get("name"), 80),
                "type": clean_text(row.get("type") or row.get("type_id"), 80),
                "type_id": clean_text(row.get("type_id"), 40),
                "submitted_at": clean_text(row.get("date") or row.get("affdate"), 80),
                "summary": clean_text(row.get("content") or row.get("MobileContent"), 240),
            }
        )
    return {"pending_count": total, "items": items}


class OAClient:
    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        try:
            import httpx
        except ImportError as exc:
            raise ConfigurationError("缺少 httpx，请使用项目 run_check.sh 执行") from exc
        oa = config["oa"]
        self.httpx = httpx
        self.base_url = oa["base_url"].rstrip("/")
        self.retries = int(oa.get("retries", 3))
        self.logger = logger
        self.client = httpx.Client(
            verify=True,
            follow_redirects=True,
            timeout=float(oa.get("timeout_seconds", 30)),
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) CTS-EHR-Monitor/1.0",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, url: str, **kwargs: Any):
        full_url = url if url.startswith("http") else self.base_url + "/" + url.lstrip("/")
        last_error: Optional[BaseException] = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.client.request(method, full_url, **kwargs)
                response.raise_for_status()
                return response
            except (self.httpx.HTTPError, OSError) as exc:
                last_error = exc
                self.logger.warning("CTS EHR 请求失败，准备重试 (%s/%s): %s", attempt, self.retries, type(exc).__name__)
                if attempt < self.retries:
                    time.sleep(2 ** (attempt - 1))
        raise MonitorError(f"CTS EHR 请求连续失败：{type(last_error).__name__}") from last_error

    def login(self, account: str, password: str) -> None:
        response = self._request("GET", "/frame2021/default.aspx")
        page = response.text
        key_match = re.search(r'var\s+enc2k\s*=\s*"([^"]+)"', page)
        if not key_match:
            raise AuthenticationError("登录页结构发生变化：缺少 enc2k")
        parser = LoginFormParser()
        parser.feed(page)
        if not parser.action:
            raise AuthenticationError("登录页结构发生变化：缺少登录表单")
        form = dict(parser.hidden)
        form.update(
            {
                "txtLoginID": account,
                "txtLoginPswd": encrypt_password(password, key_match.group(1)),
                "xFingerprint": "",
                "uCulture": "zh-cn",
                "btnLogin.x": "1",
                "btnLogin.y": "1",
            }
        )
        action = urljoin(str(response.url), parser.action)
        logged_in = self._request("POST", action, data=form, headers={"Referer": str(response.url)})
        validate_login(str(logged_in.url), self.client.cookies.keys(), logged_in.text)
        self.logger.info("CTS EHR 登录成功")

    @staticmethod
    def _encoded(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    def _sql_data(self, sql_name: str, uid: str, target_date: date, label: str) -> List[Dict[str, Any]]:
        query = f"uid={uid}&BDate={target_date.isoformat()}&EDate={target_date.isoformat()}"
        response = self._request(
            "GET",
            "/Common/ajaxGet.aspx",
            params={
                "f": "getsqldata",
                "c": "U",
                "p": self._encoded(f"opensql,staff_new/{sql_name}.sql,,,"),
                "q": self._encoded(query),
            },
        )
        return parse_json_list(response.text, label)

    def attendance_and_schedule(self, target_date: date) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        page = self._request("GET", "/staff_new/att_detail.aspx")
        if "/account/logon.aspx" in str(page.url).lower():
            raise AuthenticationError("读取考勤页时登录会话失效")
        uid_match = re.search(r'var\s+uid\s*=\s*"([^"]+)"', page.text)
        if not uid_match:
            raise ResponseSchemaError("考勤页缺少当前人员标识")
        uid = uid_match.group(1)
        attendance = self._sql_data("tab1", uid, target_date, "刷卡记录")
        schedule = self._sql_data("tab6", uid, target_date, "排班记录")
        return attendance, schedule

    def pending_approvals(self, max_items: int = 20) -> Dict[str, Any]:
        request_data = {
            "keyword": "",
            "flowif": "",
            "type_id": "",
            "flow_date_scope": "",
            "flow_sdate_t_scope": "",
            "NO": "",
            "appliycontent": "",
            "status": 0,
            "get_length": max_items,
            "length": 0,
            "order": "desc",
        }
        response = self._request(
            "POST",
            "/m.aspx?&g=huanan/flow/getFlowList",
            data={"jparam": json.dumps(request_data, ensure_ascii=False, separators=(",", ":"))},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ResponseSchemaError("待审批接口返回内容不是 JSON") from exc
        return parse_flow_response(payload, max_items=max_items)


def normalize_punches(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    punches = []
    for row in records:
        value = clean_text(row.get("k0701"), 20)
        try:
            parsed = dt_time.fromisoformat(value)
        except ValueError:
            continue
        valid_text = clean_text(row.get("k0704"), 20)
        is_valid = "无效" not in valid_text
        punches.append(
            {
                "date": clean_text(row.get("k0700"), 20),
                "time": parsed.strftime("%H:%M"),
                "device": clean_text(row.get("k0702"), 80),
                "valid": is_valid,
                "status": valid_text,
                "operator": clean_text(row.get("k0703"), 80),
                "reason": clean_text(row.get("k0705"), 120),
            }
        )
    return sorted(punches, key=lambda item: item["time"])


def determine_workday(
    target_date: date, schedule: Sequence[Dict[str, Any]], calendar: Dict[str, Any]
) -> Dict[str, Any]:
    value = target_date.isoformat()
    if value in set(calendar.get("force_offdays", [])):
        return {"is_workday": False, "source": "force_offdays", "shift": ""}
    if value in set(calendar.get("force_workdays", [])):
        return {"is_workday": True, "source": "force_workdays", "shift": ""}

    shifts = [clean_text(row.get("k1500"), 80) for row in schedule]
    shifts = [shift for shift in shifts if shift]
    if shifts:
        keywords = calendar.get("off_shift_keywords", ["休", "公休", "休息", "无班"])
        working = [shift for shift in shifts if not any(word in shift for word in keywords)]
        return {
            "is_workday": bool(working),
            "source": "oa_schedule",
            "shift": "、".join(shifts),
        }
    return {
        "is_workday": target_date.weekday() < 5,
        "source": "weekday_fallback",
        "shift": "",
    }


def _parse_threshold(value: str) -> dt_time:
    try:
        return dt_time.fromisoformat(value)
    except ValueError as exc:
        raise ConfigurationError(f"无效时间阈值：{value}") from exc


def evaluate_day(
    day: date,
    punches: Sequence[Dict[str, Any]],
    workday: Dict[str, Any],
    settings: Dict[str, Any],
    now: datetime,
    relation: str,
) -> Dict[str, Any]:
    clock_in_threshold_text = settings.get("clock_in_before", "09:00")
    clock_out_threshold_text = settings.get("clock_out_at_or_after", "17:30")
    clock_in_threshold = _parse_threshold(clock_in_threshold_text)
    clock_out_threshold = _parse_threshold(clock_out_threshold_text)
    times = [
        dt_time.fromisoformat(item["time"])
        for item in punches
        if item.get("valid", True) and item.get("time")
    ]
    is_workday = bool(workday.get("is_workday"))
    current_date = now.date()
    clock_in_due = day < current_date or (
        day == current_date and now.time() >= clock_in_threshold
    )
    clock_out_due = day < current_date or (
        day == current_date and now.time() >= clock_out_threshold
    )

    def status(found: bool, due: bool, threshold: str) -> Dict[str, Any]:
        if not is_workday:
            return {"state": "not_required", "ok": None, "threshold": threshold}
        if found:
            return {"state": "normal", "ok": True, "threshold": threshold}
        if not due:
            return {"state": "pending", "ok": None, "threshold": threshold}
        return {"state": "missing", "ok": False, "threshold": threshold}

    return {
        "relation": relation,
        "date": day.isoformat(),
        "workday": workday,
        "punches": list(punches),
        "clock_in": status(
            any(value < clock_in_threshold for value in times),
            clock_in_due,
            f"<{clock_in_threshold_text}",
        ),
        "clock_out": status(
            any(value >= clock_out_threshold for value in times),
            clock_out_due,
            f">={clock_out_threshold_text}",
        ),
    }


def build_alerts(result: Dict[str, Any]) -> List[Dict[str, str]]:
    alerts: List[Dict[str, str]] = []
    for day in result.get("days", []):
        label = "昨天" if day.get("relation") == "previous" else "今天"
        if day.get("clock_in", {}).get("state") == "missing":
            alerts.append(
                {
                    "severity": "warning",
                    "code": "{}_clock_in_missing".format(day.get("relation", "day")),
                    "message": "{}（{}）尚未检测到 9 点前上班打卡".format(
                        label, day.get("date", "-")
                    ),
                }
            )
        if day.get("clock_out", {}).get("state") == "missing":
            alerts.append(
                {
                    "severity": "warning",
                    "code": "{}_clock_out_missing".format(day.get("relation", "day")),
                    "message": "{}（{}）尚未检测到 17:30 后下班打卡".format(
                        label, day.get("date", "-")
                    ),
                }
            )
    pending = int(result.get("approvals", {}).get("pending_count", 0))
    if pending:
        alerts.append(
            {"severity": "warning", "code": "pending_approvals", "message": f"当前有 {pending} 条待审批"}
        )
    return alerts


def template_summary(result: Dict[str, Any]) -> str:
    parts: List[str] = []
    for day in result.get("days", []):
        label = "昨天" if day.get("relation") == "previous" else "今天"
        punches = "、".join(
            item.get("time", "") for item in day.get("punches", [])
        ) or "无"
        parts.append(
            "{}（{}，{}）打卡：{}；上班卡{}，下班卡{}。".format(
                label,
                day.get("date", "-"),
                "工作日" if day.get("workday", {}).get("is_workday") else "休息日",
                punches,
                _attendance_status(day.get("clock_in", {})),
                _attendance_status(day.get("clock_out", {})),
            )
        )
    pending = int(result.get("approvals", {}).get("pending_count", 0))
    parts.append(f"当前待审批 {pending} 条。")
    return "".join(parts)


def generate_ai_summary(
    result: Dict[str, Any], config: Dict[str, Any], llm_factory: Optional[Callable[[], Any]] = None
) -> Dict[str, Any]:
    fallback = template_summary(result)
    try:
        if llm_factory is None:
            warnings.filterwarnings(
                "ignore",
                message="urllib3 v2 only supports OpenSSL 1.1.1+.*",
                category=Warning,
            )
            from langchain_ollama import ChatOllama

            settings = config["ollama"]

            def llm_factory() -> Any:
                return ChatOllama(
                    model=settings.get("model") or os.getenv("OLLAMA_MODEL", "gemma4:e4b"),
                    base_url=settings.get("base_url") or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
                    reasoning=bool(settings.get("reasoning", False)),
                    temperature=float(settings.get("temperature", 0.1)),
                    num_predict=int(settings.get("num_predict", 512)),
                    sync_client_kwargs={
                        "timeout": float(settings.get("timeout_seconds", 120)),
                        "trust_env": False,
                    },
                )

        payload = {
            "target_date": result["target_date"],
            "days": result["days"],
            "approvals": result["approvals"],
            "alerts": result["alerts"],
        }
        prompt = (
            "你是企业 EHR 双日考勤汇总助手。只根据给定JSON写3到5行简洁中文摘要，"
            "分别概括昨天和今天，不得猜测；pending 必须表述为待检查，"
            "不得表述为缺卡或迟到，也不要输出Markdown标题。\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        response = llm_factory().invoke(prompt)
        content = clean_text(getattr(response, "content", response), 1200)
        if not content:
            raise ValueError("模型返回空内容")
        return {"used": True, "model": config["ollama"].get("model", "gemma4:e4b"), "summary": content, "error": ""}
    except Exception as exc:  # Model output is optional; deterministic monitoring still succeeds.
        return {
            "used": False,
            "model": config.get("ollama", {}).get("model", "gemma4:e4b"),
            "summary": fallback,
            "error": f"{type(exc).__name__}: 本地模型不可用，已使用模板摘要",
        }


def build_markdown(result: Dict[str, Any]) -> str:
    lines = [
        "# EHR 双日考勤汇总",
        "",
        f"- 检查时间：{result['run_at']}",
        f"- 目标日期：{result['target_date']}",
        "",
        "## 考勤汇总",
        "",
        "| 日期范围 | 日期 | 工作日 | 所有打卡时间 | 上班卡 | 下班卡 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for day in result.get("days", []):
        relation = "昨天" if day.get("relation") == "previous" else "今天"
        punches = "、".join(
            item.get("time", "") for item in day.get("punches", []) if item.get("time")
        ) or "无"
        lines.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                relation,
                day.get("date", "-"),
                _workday_status(day.get("workday", {})),
                punches,
                _attendance_status(day.get("clock_in", {})),
                _attendance_status(day.get("clock_out", {})),
            )
        )
    lines.extend(["", "## 待审批", "", f"共 {result['approvals']['pending_count']} 条。"])
    for item in result["approvals"]["items"][:10]:
        detail = " · ".join(
            value for value in [item["requester"], item["type"], item["submitted_at"], item["summary"]] if value
        )
        lines.append(f"- {detail or '未提供摘要'}")
    lines.extend(["", "## 摘要", "", result["model"]["summary"]])
    if result.get("alerts"):
        lines.extend(["", "## 提醒", ""])
        lines.extend(f"- {item['message']}" for item in result["alerts"])
    if result.get("errors"):
        lines.extend(["", "## 运行信息", ""])
        lines.extend(f"- {item}" for item in result["errors"])
    lines.append("")
    return "\n".join(lines)


def resolve_font_path(configured: Optional[str] = "auto") -> str:
    """Resolve a usable Unicode font without binding configuration to one OS."""
    raw = str(configured or "auto").strip()
    if raw.lower() != "auto":
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = APP_ROOT / candidate
        if not candidate.is_file():
            raise ConfigurationError("配置的字体文件不存在：{}".format(candidate))
        return str(candidate.resolve())
    candidates = [
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "simsun.ttc",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise ConfigurationError(
        "未找到可用中文字体；请在 output.font_path 中配置字体文件路径"
    )


def _load_font(path: str, size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(path, size=size)
    except OSError as exc:
        raise ConfigurationError("字体文件无法加载：{}".format(path)) from exc


def _wrapped_lines(text: str, width: int) -> List[str]:
    lines: List[str] = []
    for paragraph in str(text).splitlines() or [""]:
        lines.extend(textwrap.wrap(paragraph, width=width, break_long_words=True) or [""])
    return lines


def render_png(result: Dict[str, Any], target: Path, font_path: str) -> None:
    from PIL import Image, ImageDraw

    width, height = 1200, 900
    image = Image.new("RGB", (width, height), "#F4F7FB")
    draw = ImageDraw.Draw(image)
    title_font = _load_font(font_path, 42)
    section_font = _load_font(font_path, 26)
    body_font = _load_font(font_path, 22)
    small_font = _load_font(font_path, 17)
    draw.rounded_rectangle((35, 30, width - 35, height - 30), 24, fill="white", outline="#DCE4EF", width=2)
    draw.text((75, 65), "EHR 双日考勤汇总", font=title_font, fill="#172B4D")
    draw.text((75, 125), f"检查时间 {result['run_at']}", font=small_font, fill="#6B778C")

    y = 180
    columns = [75, 185, 340, 500, 825, 970, 1125]
    headers = ["日期范围", "日期", "工作日", "打卡时间", "上班卡", "下班卡"]
    row_height = 72
    draw.rectangle((columns[0], y, columns[-1], y + row_height), fill="#E9F2FF")
    for index, label in enumerate(headers):
        draw.text((columns[index] + 10, y + 23), label, font=small_font, fill="#172B4D")
    y += row_height
    state_colors = {
        "正常": "#168C4B",
        "未检测到": "#C44536",
        "待检查": "#B76E00",
        "不要求": "#6B778C",
        "未知": "#6B778C",
    }
    for index, day in enumerate(result.get("days", [])[:2]):
        if index % 2:
            draw.rectangle((columns[0], y, columns[-1], y + row_height), fill="#F7F9FC")
        relation = "昨天" if day.get("relation") == "previous" else "今天"
        punches = "、".join(
            item.get("time", "") for item in day.get("punches", []) if item.get("time")
        ) or "无"
        values = [
            relation,
            day.get("date", "-"),
            _workday_status(day.get("workday", {})),
            punches,
            _attendance_status(day.get("clock_in", {})),
            _attendance_status(day.get("clock_out", {})),
        ]
        for column_index, value in enumerate(values):
            color = state_colors.get(value, "#344563")
            draw.text((columns[column_index] + 10, y + 23), value, font=small_font, fill=color)
        y += row_height
    draw.rectangle((columns[0], 180, columns[-1], y), outline="#DCE4EF", width=2)
    for x in columns[1:-1]:
        draw.line((x, 180, x, y), fill="#DCE4EF", width=1)
    draw.line((columns[0], 180 + row_height, columns[-1], 180 + row_height), fill="#DCE4EF", width=1)

    y += 40
    pending = result["approvals"]["pending_count"]
    draw.text((75, y), f"待审批：{pending} 条", font=section_font, fill="#C44536" if pending else "#168C4B")
    y += 50
    for item in result["approvals"]["items"][:4]:
        text = " · ".join(value for value in [item["requester"], item["type"], item["summary"]] if value)
        for line in _wrapped_lines("• " + (text or "未提供摘要"), 47)[:2]:
            draw.text((85, y), line, font=body_font, fill="#344563")
            y += 32
        y += 5

    alert_messages = [
        item.get("message", "") for item in result.get("alerts", []) if item.get("message")
    ]
    if alert_messages:
        y += 10
        draw.text((75, y), "提醒", font=section_font, fill="#C44536")
        y += 42
        for line in _wrapped_lines("；".join(alert_messages), 48)[:3]:
            draw.text((85, y), line, font=small_font, fill="#C44536")
            y += 27

    y = max(y + 20, 650)
    draw.text((75, y), "汇总摘要", font=section_font, fill="#172B4D")
    y += 48
    for line in _wrapped_lines(result["model"]["summary"], 48)[:5]:
        draw.text((85, y), line, font=body_font, fill="#344563")
        y += 32
    image.save(target, format="PNG", optimize=True)
    target.chmod(0o600)


def atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def save_artifacts(result: Dict[str, Any], results_root: Path, config: Dict[str, Any]) -> Dict[str, str]:
    day_dir = results_root / result["target_date"]
    day_dir.mkdir(parents=True, exist_ok=True)
    day_dir.chmod(0o700)
    stamp = datetime.fromisoformat(result["run_at"]).strftime("%Y%m%d_%H%M%S")
    stem = f"summary_{stamp}"
    json_path = day_dir / f"{stem}.json"
    md_path = day_dir / f"{stem}.md"
    png_path = day_dir / f"{stem}.png"
    atomic_write_text(json_path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    atomic_write_text(md_path, build_markdown(result))
    font = resolve_font_path(config.get("output", {}).get("font_path", "auto"))
    render_png(result, png_path, font)
    for source, name in [(json_path, "latest.json"), (md_path, "latest.md"), (png_path, "latest.png")]:
        destination = DATA_ROOT / name
        shutil.copy2(source, destination)
        destination.chmod(0o600)
    return {"json": str(json_path), "markdown": str(md_path), "png": str(png_path)}


def _attendance_status(check: Dict[str, Any]) -> str:
    return {
        "normal": "正常",
        "missing": "未检测到",
        "pending": "待检查",
        "not_required": "不要求",
        "unknown": "未知",
    }.get(check.get("state"), "未知")


def _workday_status(workday: Dict[str, Any]) -> str:
    if workday.get("source") == "unknown":
        return "未知"
    return "工作日" if workday.get("is_workday") else "休息日"


def wechat_notification_text(result: Dict[str, Any]) -> str:
    if result.get("status") != "ok":
        return "\n".join(
            [
                "【EHR考勤双日汇总失败】",
                f"日期：{result['target_date']}",
                "任务执行失败，请查看本地脱敏日志。",
            ]
        )

    lines = ["【EHR考勤双日汇总】"]
    for day in result.get("days", []):
        relation = "昨天" if day.get("relation") == "previous" else "今天"
        punches = "、".join(
            item.get("time", "") for item in day.get("punches", []) if item.get("time")
        ) or "无"
        lines.append(
            "{}（{}）：打卡 {}；上班卡 {}；下班卡 {}".format(
                relation,
                day.get("date", "-"),
                punches,
                _attendance_status(day.get("clock_in", {})),
                _attendance_status(day.get("clock_out", {})),
            )
        )

    pending = int(result.get("approvals", {}).get("pending_count", 0))
    lines.append(f"待审批：{pending} 条")
    alerts = [item.get("message", "") for item in result.get("alerts", []) if item.get("message")]
    if alerts:
        lines.append("提醒：" + "；".join(alerts))
    return "\n".join(lines)


def build_error_result(
    target_date: date, message: str, now: Optional[datetime] = None
) -> Dict[str, Any]:
    run_at = now or datetime.now().astimezone()
    unknown_workday = {"is_workday": False, "source": "unknown", "shift": ""}
    unknown_check = {"state": "unknown", "ok": None, "threshold": ""}

    def empty_day(day: date, relation: str) -> Dict[str, Any]:
        return {
            "relation": relation,
            "date": day.isoformat(),
            "workday": dict(unknown_workday),
            "punches": [],
            "clock_in": dict(unknown_check),
            "clock_out": dict(unknown_check),
        }

    return {
        "schema_version": 3,
        "run_at": run_at.isoformat(timespec="seconds"),
        "target_date": target_date.isoformat(),
        "status": "error",
        "days": [
            empty_day(target_date - timedelta(days=1), "previous"),
            empty_day(target_date, "current"),
        ],
        "approvals": {"pending_count": 0, "items": []},
        "alerts": [{"severity": "error", "code": "run_failed", "message": "CTS EHR 监控运行失败"}],
        "errors": [message],
        "model": {"used": False, "model": "", "summary": "本次监控运行失败，请查看脱敏日志。", "error": ""},
    }


def execute_monitor(
    config: Dict[str, Any],
    target_date: date,
    logger: logging.Logger,
    now_provider: Optional[Callable[[], datetime]] = None,
) -> Dict[str, Any]:
    now = (now_provider or (lambda: datetime.now().astimezone()))()
    account = os.getenv("ILINKBOT_INTEGRATION_ACCOUNT") or config["oa"].get("account", "")
    if not account:
        raise CredentialError("未配置当前用户的 CTS EHR 账号")
    keychain_account = os.getenv("ILINKBOT_KEYCHAIN_ACCOUNT") or account
    keychain_service = (
        os.getenv("ILINKBOT_KEYCHAIN_SERVICE") or config["oa"].get("keychain_service", "")
    )
    if not keychain_service:
        raise CredentialError("未配置当前用户的 CTS EHR 凭据引用")
    if os.getenv("ILINKBOT_KEYCHAIN_SERVICE"):
        from src.core.integrations.keychain import (
            KeychainError,
            KeychainReference,
            KeychainService,
        )

        try:
            password = KeychainService().get_secret(
                KeychainReference(keychain_service, keychain_account)
            )
        except KeychainError as exc:
            raise CredentialError("未能读取当前用户的 CTS EHR 凭据") from exc
    else:
        password = read_keychain_password(keychain_account, keychain_service)
    runtime_config = dict(config)
    runtime_config["oa"] = dict(config["oa"])
    runtime_config["oa"]["account"] = account
    client = OAClient(runtime_config, logger)
    previous_date = target_date - timedelta(days=1)
    try:
        client.login(account, password)
        previous_rows, previous_schedule = client.attendance_and_schedule(previous_date)
        current_rows, current_schedule = client.attendance_and_schedule(target_date)
        approvals = client.pending_approvals(int(config.get("approvals", {}).get("max_items", 20)))
    finally:
        password = ""
        client.close()

    calendar = config.get("calendar", {})
    attendance_settings = config.get("attendance", {})
    previous_workday = determine_workday(previous_date, previous_schedule, calendar)
    current_workday = determine_workday(target_date, current_schedule, calendar)
    days = [
        evaluate_day(
            previous_date,
            normalize_punches(previous_rows),
            previous_workday,
            attendance_settings,
            now,
            "previous",
        ),
        evaluate_day(
            target_date,
            normalize_punches(current_rows),
            current_workday,
            attendance_settings,
            now,
            "current",
        ),
    ]
    result: Dict[str, Any] = {
        "schema_version": 3,
        "run_at": now.isoformat(timespec="seconds"),
        "target_date": target_date.isoformat(),
        "status": "ok",
        "days": days,
        "approvals": approvals,
        "alerts": [],
        "errors": [],
    }
    result["alerts"] = build_alerts(result)
    result["model"] = generate_ai_summary(result, config)
    if not result["model"]["used"]:
        result["errors"].append(result["model"]["error"])
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="只读汇总昨天和今天的 CTS EHR 考勤与待审批")
    parser.add_argument("--date", dest="target_date", help="目标日期，格式 YYYY-MM-DD")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径")
    return parser.parse_args(arguments)


def write_script_result(
    status: str,
    summary: str,
    artifacts: Optional[List[str]] = None,
    error: str = "",
) -> None:
    result_file = os.getenv("ILINKBOT_SCRIPT_RESULT_FILE")
    if not result_file:
        return
    path = Path(result_file)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "status": status,
        "summary": summary,
        "artifacts": artifacts or [],
        "error": error,
    }
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        target_date = date.fromisoformat(args.target_date) if args.target_date else date.today()
    except ValueError:
        write_script_result("failed", "目标日期格式应为 YYYY-MM-DD", error="invalid date")
        print("目标日期格式应为 YYYY-MM-DD", file=sys.stderr)
        return 2
    config: Optional[Dict[str, Any]] = None
    try:
        config = load_config(args.config.expanduser().resolve())
        results_root, log_dir = prepare_directories(config)
        logger = configure_logging(log_dir)
    except ConfigurationError as exc:
        write_script_result(
            "failed",
            "CTS EHR 监控配置无效，请查看本地日志。",
            error=type(exc).__name__,
        )
        print(str(exc), file=sys.stderr)
        return 2
    assert config is not None

    lock_path = log_dir / "monitor.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        lock_path.chmod(0o600)
        try:
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            logger.info("已有监控任务在运行，本次跳过")
            write_script_result("skipped", "已有 CTS EHR 监控任务在运行，本次检查已跳过。")
            return 0

        try:
            result = execute_monitor(config, target_date, logger)
            paths = save_artifacts(result, results_root, config)
            logger.info(
                "双日汇总完成 date=%s previous_punches=%s current_punches=%s pending=%s",
                target_date.isoformat(),
                len(result["days"][0]["punches"]),
                len(result["days"][1]["punches"]),
                result["approvals"]["pending_count"],
            )
            write_script_result(
                "success",
                wechat_notification_text(result),
                artifacts=[paths["png"]],
            )
            print(result["model"]["summary"])
            print(f"JSON: {paths['json']}")
            print(f"报告: {paths['markdown']}")
            print(f"图片: {paths['png']}")
            return 0
        except CredentialError as exc:
            logger.error("凭据错误：%s", exc)
            write_script_result(
                "failed",
                "CTS EHR 凭据不可用，请检查当前租户的凭证配置。",
                error=type(exc).__name__,
            )
            print(str(exc), file=sys.stderr)
            return 2
        except Exception as exc:
            safe_message = f"{type(exc).__name__}: {clean_text(exc, 500)}"
            logger.error("监控失败：%s", safe_message)
            result = build_error_result(target_date, safe_message)
            paths: Dict[str, str] = {}
            try:
                paths = save_artifacts(result, results_root, config)
            except Exception as save_exc:
                logger.error("错误报告保存失败：%s", type(save_exc).__name__)
            write_script_result(
                "failed",
                "CTS EHR 监控运行失败，请查看本地脱敏日志。",
                artifacts=[paths["png"]] if paths.get("png") else [],
                error=type(exc).__name__,
            )
            print("CTS EHR 监控运行失败，请查看本地日志。", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
