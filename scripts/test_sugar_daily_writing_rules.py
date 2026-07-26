"""Regression checks for Sugar Daily report-language rules."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from run_daily import (  # noqa: E402
    MACHINE_DATA_PROCESS_PHRASES,
    _validate_model_content,
    contains_machine_data_process_language,
    remove_machine_data_process_language,
    sanitize_report_body,
)


class SugarDailyWritingRuleTests(unittest.TestCase):
    def test_all_explicit_machine_phrases_are_detected(self) -> None:
        for phrase in MACHINE_DATA_PROCESS_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertTrue(contains_machine_data_process_language(phrase))

    def test_value_is_preserved_while_backend_wording_is_removed(self) -> None:
        source = "巴西糖产量沿用最新一期已确认数据，为2739万吨。"
        cleaned = remove_machine_data_process_language(source)
        self.assertEqual(cleaned, "巴西糖产量为2739万吨。")
        self.assertFalse(contains_machine_data_process_language(cleaned))

    def test_update_process_sentence_is_removed(self) -> None:
        source = "由于最新数据未更新，沿用上一期数据。市场关注后续生产进度。"
        cleaned = sanitize_report_body(source)
        self.assertEqual(cleaned, "市场关注后续生产进度。")
        self.assertFalse(contains_machine_data_process_language(cleaned))

    def test_model_output_with_backend_wording_is_rejected(self) -> None:
        valid, reason = _validate_model_content(
            "国内糖产量采用最新一期已确认数据，为2739万吨。",
            "国内糖产量2739万吨",
            "",
        )
        self.assertFalse(valid)
        self.assertIn("机器化数据处理", reason)

    def test_current_public_report_contains_no_backend_wording(self) -> None:
        report_path = ROOT / "public" / "data" / "reports" / "2026-07-26.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertFalse(contains_machine_data_process_language(serialized))
        self.assertNotIn("数据未更新", serialized)

    def test_skill_does_not_instruct_report_to_publish_data_status(self) -> None:
        skill = (ROOT / ".agents" / "skills" / "sugar-daily-report" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        style = (
            ROOT / ".agents" / "skills" / "sugar-daily-report" / "writing-style.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn('无数据时写"暂无新的已验证信息"', skill)
        self.assertNotIn(
            '无任何有效数据 | "国内25/26榨季暂无新的已验证产销和库存数据',
            style,
        )


if __name__ == "__main__":
    unittest.main()
