"""Regression guards for CTSEHR/CTSOA/todo intent-routing configuration.

These tests are deterministic (they never call a model). They lock in the
copy/config governance that keeps the agent from confusing:
  - EHR system attendance (ctsehr_check)
  - OA approval-flow todos (ctsoa_check)
  - private checklist todos (todo_manage)
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
JOBS_ROOT = REPO_ROOT / "src" / "core" / "jobs"
GENERAL_AGENT = REPO_ROOT / "config" / "agents" / "general.json"
CTSEHR_MONITOR = JOBS_ROOT / "ctsehr" / "monitor.py"


def _script_descriptions() -> dict[str, str]:
    descriptions: dict[str, str] = {}
    for manifest in sorted(JOBS_ROOT.glob("*/script.json")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        descriptions[data["id"]] = data["description"]
    return descriptions


class IntentRoutingConfigTests(unittest.TestCase):
    def test_ctsehr_description_is_ehr_scoped_without_oa_contamination(self) -> None:
        desc = _script_descriptions()["ctsehr_check"]
        # Positive scope: clearly EHR attendance.
        self.assertIn("EHR", desc)
        self.assertIn("考勤", desc)
        # No cross-contamination with the old "OA 考勤" wording that misrouted queries.
        self.assertNotIn("OA 考勤", desc)
        self.assertNotIn("OA考勤", desc)
        # Reverse example steers OA/private todos to the right tools.
        self.assertIn("不要用于", desc)
        self.assertIn("ctsoa_check", desc)
        self.assertIn("todo_manage", desc)

    def test_ctsoa_description_is_oa_todo_scoped_with_reverse_examples(self) -> None:
        desc = _script_descriptions()["ctsoa_check"]
        self.assertIn("OA", desc)
        self.assertIn("待办", desc)
        # Trigger words that previously misrouted to ctsehr.
        self.assertIn("OA信息", desc)
        self.assertIn("CTSOA", desc)
        # Reverse example steers attendance/private todos elsewhere.
        self.assertIn("不要用于", desc)
        self.assertIn("ctsehr_check", desc)
        self.assertIn("todo_manage", desc)

    def test_general_prompt_declares_three_way_todo_routing(self) -> None:
        prompt = json.loads(GENERAL_AGENT.read_text(encoding="utf-8"))["system_prompt"]
        # All three routing targets must be named so the model can disambiguate.
        self.assertIn("ctsehr_check", prompt)
        self.assertIn("ctsoa_check", prompt)
        self.assertIn("todo_manage", prompt)
        # Ambiguity fallback: clarify before guessing.
        self.assertIn("澄清", prompt)

    def test_ctsehr_monitor_no_oa_attendance_summary_labels(self) -> None:
        source = CTSEHR_MONITOR.read_text(encoding="utf-8")
        # User-facing summary titles must not label EHR attendance as "OA".
        self.assertNotIn("OA双日考勤汇总", source)
        self.assertNotIn("OA 双日考勤汇总", source)
        self.assertNotIn("企业OA双日考勤汇总助手", source)


if __name__ == "__main__":
    unittest.main()
