"""Deterministic routing for explicit CTS EHR and CTS OA queries."""

from __future__ import annotations

import re
from typing import Tuple


CTSEHR_SCRIPT_ID = "ctsehr_check"
CTSOA_SCRIPT_ID = "ctsoa_check"

_QUERY_ACTIONS = (
    "查看",
    "查询",
    "查一下",
    "查下",
    "帮我查",
    "帮忙查",
    "显示",
    "列出",
)
_EXPLANATION_MARKERS = (
    "是什么",
    "什么意思",
    "为什么",
    "有何区别",
    "有什么区别",
    "怎么配置",
    "如何配置",
    "配置方法",
    "介绍一下",
    "说明一下",
)
_EHR_MARKERS = ("打卡", "考勤", "缺卡", "上班卡", "下班卡", "ctsehr", "ehr")
_OA_QUERY_MARKERS = ("待办", "审批", "流程", "信息")


def _normalize(text: str) -> str:
    return re.sub(r"[\s，。！？?!：:；;、]+", "", str(text or "")).lower()


def classify_integration_script_query(text: str) -> Tuple[str, ...]:
    """Return script ids for an explicit live EHR/OA query.

    The classifier is intentionally narrow. Explanations, configuration
    questions, and a bare private "待办" remain in the normal model route.
    """

    raw = str(text or "").lower()
    normalized = _normalize(raw)
    if not normalized or any(
        marker in normalized for marker in _EXPLANATION_MARKERS
    ):
        return ()
    if not any(action in normalized for action in _QUERY_ACTIONS):
        return ()

    scripts = []
    if any(marker in normalized for marker in _EHR_MARKERS):
        scripts.append(CTSEHR_SCRIPT_ID)
    names_oa = "ctsoa" in normalized or bool(
        re.search(r"(^|[^a-z0-9])oa([^a-z0-9]|$)", raw)
    )
    if names_oa and any(marker in normalized for marker in _OA_QUERY_MARKERS):
        scripts.append(CTSOA_SCRIPT_ID)
    return tuple(scripts)
