#!/usr/bin/env python3
"""Read-only CTS Ecology OA pending-workflow job with CAPTCHA handoff."""

from __future__ import annotations

import argparse
import base64
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse


APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.getenv("ILINKBOT_SCRIPT_DATA_ROOT", str(APP_ROOT))).expanduser().resolve()
DEFAULT_CONFIG = APP_ROOT / "config.json"
CHALLENGE_FILE = DATA_ROOT / "login_challenge.json"
SESSION_FILE = DATA_ROOT / "session.json"
CAPTCHA_FILE = DATA_ROOT / "captcha.png"


class MonitorError(RuntimeError):
    """Base error for operational monitoring failures."""


class ConfigurationError(MonitorError):
    """Configuration is missing or invalid."""


class CredentialError(MonitorError):
    """The configured secret could not be loaded."""


class AuthenticationError(MonitorError):
    """OA authentication failed or expired."""


class ChallengeError(MonitorError):
    """The CAPTCHA challenge is missing or expired."""


class ResponseSchemaError(MonitorError):
    """An OA response no longer matches the expected schema."""


def clean_text(value: Any, limit: int = 240) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def load_config(path: Path) -> Dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError("配置文件不存在：{}".format(path)) from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError("配置文件不是有效 JSON：{}".format(exc)) from exc
    if not isinstance(config, dict):
        raise ConfigurationError("配置根节点必须是 JSON 对象")
    schemas = {
        "": {"oa", "pending", "output"},
        "oa": {
            "base_url",
            "account",
            "keychain_service",
            "timeout_seconds",
            "retries",
            "challenge_ttl_seconds",
        },
        "pending": {"max_items"},
        "output": {"results_dir", "logs_dir"},
    }
    unknown = sorted(set(config) - schemas[""])
    if unknown:
        raise ConfigurationError("配置包含未知字段：{}".format("、".join(unknown)))
    for section, allowed in schemas.items():
        if not section:
            continue
        value = config.get(section, {})
        if not isinstance(value, dict):
            raise ConfigurationError("配置 {} 必须是 JSON 对象".format(section))
        extra = sorted(set(value) - allowed)
        if extra:
            raise ConfigurationError(
                "配置 {} 包含未知字段：{}".format(section, "、".join(extra))
            )
    base_url = str(config.get("oa", {}).get("base_url", "")).rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConfigurationError("oa.base_url 必须是有效的 HTTPS 地址")
    config.setdefault("oa", {})["base_url"] = base_url
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
    logger = logging.getLogger("cts_oa")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        log_dir / "monitor.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def read_keychain_password(account: str, service: str) -> str:
    try:
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
    except (OSError, subprocess.SubprocessError) as exc:
        raise CredentialError("未能读取当前用户的 OA 凭据") from exc
    password = completed.stdout.rstrip("\r\n")
    if completed.returncode != 0 or not password:
        raise CredentialError("未能读取当前用户的 OA 凭据")
    return password


def load_password(account: str, config: Dict[str, Any]) -> str:
    keychain_account = os.getenv("ILINKBOT_KEYCHAIN_ACCOUNT") or account
    keychain_service = (
        os.getenv("ILINKBOT_KEYCHAIN_SERVICE")
        or str(config.get("oa", {}).get("keychain_service", ""))
    )
    if not keychain_service:
        raise CredentialError("未配置当前用户的 OA 凭据引用")
    if os.getenv("ILINKBOT_KEYCHAIN_SERVICE"):
        from src.core.integrations.keychain import (
            KeychainError,
            KeychainReference,
            KeychainService,
        )

        try:
            return KeychainService().get_secret(
                KeychainReference(keychain_service, keychain_account)
            )
        except KeychainError as exc:
            raise CredentialError("未能读取当前用户的 OA 凭据") from exc
    return read_keychain_password(keychain_account, keychain_service)


def rsa_encrypt(value: str, rsa_public_key: str, rsa_code: str, rsa_flag: str) -> str:
    """Match the site's JSEncrypt PKCS#1 v1.5 transform."""
    try:
        from Crypto.Cipher import PKCS1_v1_5
        from Crypto.PublicKey import RSA

        key_text = rsa_public_key.strip()
        if "BEGIN" in key_text:
            key = RSA.import_key(key_text)
        else:
            # 站点返回裸 base64 DER（JSEncrypt 可直接使用），需先解码再导入
            key = RSA.import_key(base64.b64decode(key_text))
        encrypted = PKCS1_v1_5.new(key).encrypt((value + rsa_code).encode("utf-8"))
    except (ImportError, ValueError, TypeError) as exc:
        raise AuthenticationError("无法使用 OA 公钥加密登录信息") from exc
    return base64.b64encode(encrypted).decode("ascii") + rsa_flag


def serialize_cookies(cookies: Iterable[Any]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for cookie in cookies:
        output.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
                "expires": cookie.expires,
            }
        )
    return output


def restore_cookies(client: Any, cookies: Iterable[Dict[str, Any]]) -> None:
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        value = str(item.get("value", ""))
        domain = str(item.get("domain", ""))
        if name and domain:
            client.cookies.set(
                name,
                value,
                domain=domain,
                path=str(item.get("path") or "/"),
            )


def _first(row: Dict[str, Any], names: Iterable[str], limit: int = 160) -> str:
    for name in names:
        if row.get(name) not in (None, ""):
            return clean_text(row.get(name), limit)
    return ""


def _integer(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_pending_payload(
    listing: Dict[str, Any],
    count_payload: Optional[Dict[str, Any]] = None,
    max_items: int = 20,
) -> Dict[str, Any]:
    if not isinstance(listing, dict):
        raise ResponseSchemaError("OA 待办接口返回内容不是 JSON 对象")
    rows = listing.get("datas")
    if not isinstance(rows, list):
        message = clean_text(listing.get("msg") or listing.get("message"))
        if message:
            raise AuthenticationError("OA 会话不可用：{}".format(message))
        raise ResponseSchemaError("OA 待办接口缺少 datas")
    items: List[Dict[str, str]] = []
    for row in rows[:max_items]:
        if not isinstance(row, dict):
            continue
        date_text = _first(row, ("receivedate", "createdate", "date"), 40)
        time_text = _first(row, ("receivetime", "createtime", "time"), 20)
        items.append(
            {
                "request_id": _first(row, ("requestid", "requestId", "id"), 80),
                "title": _first(
                    row,
                    ("requestnametitle", "requestname", "title", "subject"),
                    240,
                ),
                "workflow": _first(
                    row,
                    ("workflowname", "workflowName", "typename", "type"),
                    120,
                ),
                "requester": _first(
                    row,
                    ("creatorname", "creatername", "creater", "creator", "username"),
                    80,
                ),
                "received_at": clean_text("{} {}".format(date_text, time_text), 80),
                "status": _first(row, ("statusname", "status", "nodename"), 80),
            }
        )
    counts: Dict[str, int] = {}
    raw_counts = count_payload.get("totalcount", {}) if isinstance(count_payload, dict) else {}
    if isinstance(raw_counts, dict):
        for key, value in raw_counts.items():
            number = _integer(value)
            if number is not None and number >= 0:
                counts[clean_text(key, 80)] = number
    preferred = next(
        (
            value
            for key, value in counts.items()
            if key.lower() in {"全部", "all", "total"} or "全部" in key
        ),
        None,
    )
    total = preferred if preferred is not None else (max(counts.values()) if counts else len(rows))
    return {
        "pending_count": total,
        "returned_count": len(items),
        "items": items,
        "category_counts": counts,
    }


class OAClient:
    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        try:
            import httpx
        except ImportError as exc:
            raise ConfigurationError("缺少 httpx，请先安装项目依赖") from exc
        oa = config["oa"]
        self.httpx = httpx
        self.base_url = str(oa["base_url"]).rstrip("/")
        self.retries = max(1, int(oa.get("retries", 3)))
        self.logger = logger
        self.client = httpx.Client(
            verify=True,
            follow_redirects=True,
            timeout=float(oa.get("timeout_seconds", 30)),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36 CTS-OA-Monitor/1.0"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = path if path.startswith("http") else self.base_url + "/" + path.lstrip("/")
        last_error: Optional[BaseException] = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except (self.httpx.HTTPError, OSError) as exc:
                last_error = exc
                self.logger.warning(
                    "OA 请求失败，准备重试 (%s/%s): %s",
                    attempt,
                    self.retries,
                    type(exc).__name__,
                )
                if attempt < self.retries:
                    time.sleep(2 ** (attempt - 1))
        raise MonitorError("OA 请求连续失败：{}".format(type(last_error).__name__)) from last_error

    def _post_json(self, path: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = self._request(
            "POST",
            path,
            data=data or {},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.base_url + "/wui/index.html",
            },
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AuthenticationError("OA 会话已失效或接口返回了登录页") from exc
        if not isinstance(payload, dict):
            raise ResponseSchemaError("OA 接口返回内容不是 JSON 对象")
        return payload

    def restore_session(self, path: Path, account: str) -> bool:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return False
        if not isinstance(payload, dict) or payload.get("account") != account:
            return False
        restore_cookies(self.client, payload.get("cookies", []))
        return True

    def save_session(self, path: Path, account: str) -> None:
        atomic_write_json(
            path,
            {
                "schema_version": 1,
                "account": account,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "cookies": serialize_cookies(self.client.cookies.jar),
            },
        )

    def create_challenge(self, account: str, challenge_ttl: int) -> Dict[str, Any]:
        self._request("GET", "/wui/index.html")
        form = self._post_json(
            "/api/hrm/login/getLoginForm",
            {"loginid": account, "langid": "7"},
        )
        setting = form.get("loginSetting")
        if not isinstance(setting, dict):
            raise ResponseSchemaError("OA 登录配置缺少 loginSetting")
        if str(setting.get("showDynamicPwd", "0")) == "1":
            raise ChallengeError("该 OA 账号启用了动态密码，当前脚本不支持自动登录")
        key = clean_text(setting.get("validateCodeKey"), 120)
        has_captcha = str(setting.get("hasValidateCode", "0")) == "1"
        if not has_captcha or not key:
            raise ChallengeError("OA 未返回可用的登录验证码配置")
        response = self._request(
            "GET",
            "/weaver/weaver.file.MakeValidateCode",
            params={"seriesnum_": int(time.time() * 1000), "validateCodeKey": key},
            headers={"Referer": self.base_url + "/wui/index.html"},
        )
        content_type = response.headers.get("content-type", "").lower()
        if "image" not in content_type or not response.content:
            raise ResponseSchemaError("OA 验证码接口未返回图片")
        CAPTCHA_FILE.write_bytes(response.content)
        CAPTCHA_FILE.chmod(0o600)
        created_at = datetime.now(timezone.utc)
        atomic_write_json(
            CHALLENGE_FILE,
            {
                "schema_version": 1,
                "account": account,
                "created_at": created_at.isoformat(),
                "expires_at": created_at.timestamp() + challenge_ttl,
                "validate_code_key": key,
                "open_rsa": str(setting.get("openRSA", "0")) == "1",
                "cookies": serialize_cookies(self.client.cookies.jar),
            },
        )
        return {
            "kind": "challenge",
            "captcha": str(CAPTCHA_FILE.resolve()),
            "expires_in_seconds": challenge_ttl,
        }

    def login_with_challenge(
        self,
        account: str,
        password: str,
        validate_code: str,
    ) -> None:
        try:
            challenge = json.loads(CHALLENGE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ChallengeError("登录验证码会话不存在，请先不带验证码运行一次脚本") from exc
        if not isinstance(challenge, dict) or challenge.get("account") != account:
            raise ChallengeError("登录验证码会话与当前账号不匹配，请重新获取验证码")
        if time.time() >= float(challenge.get("expires_at", 0)):
            raise ChallengeError("登录验证码已过期，请重新获取验证码")
        restore_cookies(self.client, challenge.get("cookies", []))
        login_id = account
        login_password = password
        if challenge.get("open_rsa"):
            rsa_info = self._request(
                "GET",
                "/rsa/weaver.rsa.GetRsaInfo",
                params={"ts": int(time.time() * 1000)},
                headers={"Referer": self.base_url + "/wui/index.html"},
            )
            try:
                rsa_payload = rsa_info.json()
                rsa_code = str(rsa_payload["rsa_code"])
                rsa_public = str(rsa_payload["rsa_pub"])
                rsa_flag = str(rsa_payload.get("rsa_flag", ""))
            except (ValueError, KeyError, TypeError) as exc:
                raise AuthenticationError("OA 公钥接口返回结构发生变化") from exc
            login_id = rsa_encrypt(account, rsa_public, rsa_code, rsa_flag)
            login_password = rsa_encrypt(password, rsa_public, rsa_code, rsa_flag)
        payload = self._post_json(
            "/api/hrm/login/checkLogin",
            {
                "islanguid": "7",
                "loginid": login_id,
                "userpassword": login_password,
                "dynamicPassword": "",
                "tokenAuthKey": "",
                "validatecode": validate_code,
                "validateCodeKey": challenge.get("validate_code_key", ""),
                "logintype": "1",
                "messages": "",
                "isie": "false",
                "appid": "",
                "service": "",
                "isRememberPassword": "false",
            },
        )
        login_password = ""
        password = ""
        if str(payload.get("loginstatus", "")).lower() != "true":
            message = clean_text(payload.get("msg") or "账号、密码或验证码不正确")
            raise AuthenticationError("OA 登录失败：{}".format(message))
        if payload.get("authUrl"):
            raise AuthenticationError("OA 登录还需要额外身份校验，请先在浏览器完成登录")
        self.logger.info("OA 登录成功")

    def pending(self, max_items: int) -> Dict[str, Any]:
        listing = self._post_json("/api/workflow/wfcenter/getToDoList")
        counts = self._post_json(
            "/api/workflow/reqlist/doingCountInfo",
            {"source": "wfcenter_todo"},
        )
        return parse_pending_payload(listing, counts, max_items=max_items)


def build_summary(result: Dict[str, Any]) -> str:
    pending = result["pending"]
    lines = [
        "【CTS OA 待办】",
        "当前待办 {} 条，本次展示 {} 条。".format(
            pending["pending_count"], pending["returned_count"]
        ),
    ]
    for index, item in enumerate(pending["items"][:10], 1):
        detail = " · ".join(
            value
            for value in (
                item.get("requester", ""),
                item.get("workflow", ""),
                item.get("received_at", ""),
            )
            if value
        )
        lines.append(
            "{}. {}{}".format(
                index,
                item.get("title") or "未提供标题",
                "（{}）".format(detail) if detail else "",
            )
        )
    if not pending["items"]:
        lines.append("当前没有待办事项。")
    return "\n".join(lines)


def save_result(result: Dict[str, Any], results_root: Path) -> None:
    stamp = datetime.fromisoformat(result["run_at"]).strftime("%Y%m%d_%H%M%S")
    json_path = results_root / "pending_{}.json".format(stamp)
    atomic_write_json(json_path, result)
    atomic_write_json(DATA_ROOT / "latest.json", result)
    markdown = ["# CTS OA 待办", "", "- 查询时间：{}".format(result["run_at"]), ""]
    for item in result["pending"]["items"]:
        detail = " · ".join(
            value
            for value in (
                item.get("requester", ""),
                item.get("workflow", ""),
                item.get("received_at", ""),
            )
            if value
        )
        markdown.append("- {}{}".format(item.get("title") or "未提供标题", "（{}）".format(detail) if detail else ""))
    if not result["pending"]["items"]:
        markdown.append("- 当前没有待办事项。")
    atomic_write_text(DATA_ROOT / "latest.md", "\n".join(markdown) + "\n")


def execute_monitor(
    config: Dict[str, Any],
    logger: logging.Logger,
    validate_code: str = "",
) -> Dict[str, Any]:
    account = os.getenv("ILINKBOT_INTEGRATION_ACCOUNT") or str(
        config.get("oa", {}).get("account", "")
    )
    if not account:
        raise CredentialError("未配置当前用户的 OA 账号")
    client = OAClient(config, logger)
    try:
        if client.restore_session(SESSION_FILE, account):
            try:
                pending = client.pending(int(config.get("pending", {}).get("max_items", 20)))
                return {
                    "kind": "pending",
                    "schema_version": 1,
                    "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "status": "ok",
                    "pending": pending,
                }
            except (AuthenticationError, ResponseSchemaError):
                try:
                    SESSION_FILE.unlink()
                except FileNotFoundError:
                    pass
        if not validate_code:
            return client.create_challenge(
                account,
                max(60, int(config.get("oa", {}).get("challenge_ttl_seconds", 300))),
            )
        password = load_password(account, config)
        try:
            client.login_with_challenge(account, password, validate_code)
        finally:
            password = ""
        client.save_session(SESSION_FILE, account)
        pending = client.pending(int(config.get("pending", {}).get("max_items", 20)))
        for path in (CHALLENGE_FILE, CAPTCHA_FILE):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return {
            "kind": "pending",
            "schema_version": 1,
            "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "ok",
            "pending": pending,
        }
    finally:
        client.close()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读查询 CTS OA 待办")
    parser.add_argument("--validate-code", default="", help="首次运行生成图片中的验证码")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径")
    return parser.parse_args(list(sys.argv[1:] if argv is None else argv))


def write_script_result(
    status: str,
    summary: str,
    artifacts: Optional[List[str]] = None,
    error: str = "",
) -> None:
    result_file = os.getenv("ILINKBOT_SCRIPT_RESULT_FILE")
    if not result_file:
        return
    atomic_write_json(
        Path(result_file),
        {
            "status": status,
            "summary": summary,
            "artifacts": artifacts or [],
            "error": error,
        },
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config.expanduser().resolve())
        results_root, log_dir = prepare_directories(config)
        logger = configure_logging(log_dir)
        result = execute_monitor(config, logger, validate_code=args.validate_code.strip())
        if result["kind"] == "challenge":
            summary = (
                "CTS OA 需要验证码。请查看图片，并在 5 分钟内再次运行 "
                "ctsoa_check，参数 validate_code 填写图片中的字符。"
            )
            write_script_result("success", summary, [result["captcha"]])
            print(summary)
            return 0
        save_result(result, results_root)
        summary = build_summary(result)
        write_script_result("success", summary)
        print(summary)
        return 0
    except MonitorError as exc:
        logger = logging.getLogger("cts_oa")
        logger.error("CTS OA 查询失败：%s", type(exc).__name__)
        summary = "CTS OA 查询失败：{}".format(clean_text(exc, 300))
        write_script_result("failed", summary, error=type(exc).__name__)
        print(summary, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
