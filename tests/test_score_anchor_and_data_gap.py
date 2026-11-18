# -*- coding: utf-8 -*-
"""Tests for the score-anchor clamp and the data-gap prompt section."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from unittest.mock import MagicMock

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from src.analyzer import (  # noqa: E402
    SCORE_ANCHOR_MAX_DEVIATION,
    GeminiAnalyzer,
    AnalysisResult,
    _format_data_gap_section,
)


def _result(score: int) -> AnalysisResult:
    return AnalysisResult(
        code="002832",
        name="比音勒芬",
        sentiment_score=score,
        trend_prediction="看多",
        operation_advice="买入",
        dashboard={"core_conclusion": {"one_sentence": "test"}},
    )


def _context(system_score) -> dict:
    return {"trend_analysis": {"signal_score": system_score}}


class ScoreAnchorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = object.__new__(GeminiAnalyzer)

    def test_within_band_keeps_model_score(self) -> None:
        result = _result(70)
        self.analyzer._apply_score_anchor(result, _context(66))

        self.assertEqual(result.sentiment_score, 70)
        anchor = result.dashboard["score_anchor"]
        self.assertFalse(anchor["clamped"])
        self.assertEqual(anchor["system_score"], 66)
        self.assertEqual(anchor["model_score"], 70)
        self.assertEqual(anchor["adjustment"], 4)

    def test_above_band_is_clamped_to_upper_boundary(self) -> None:
        result = _result(92)
        self.analyzer._apply_score_anchor(result, _context(66))

        self.assertEqual(result.sentiment_score, 66 + SCORE_ANCHOR_MAX_DEVIATION)
        anchor = result.dashboard["score_anchor"]
        self.assertTrue(anchor["clamped"])
        self.assertEqual(anchor["model_score"], 92)
        self.assertEqual(anchor["final_score"], 76)
        self.assertTrue(anchor["reason_required"])
        # dashboard 内的分数也要同步，避免前后不一致
        self.assertEqual(result.dashboard["sentiment_score"], 76)

    def test_below_band_is_clamped_to_lower_boundary(self) -> None:
        result = _result(20)
        self.analyzer._apply_score_anchor(result, _context(66))

        self.assertEqual(result.sentiment_score, 66 - SCORE_ANCHOR_MAX_DEVIATION)
        self.assertTrue(result.dashboard["score_anchor"]["clamped"])

    def test_clamp_respects_zero_hundred_bounds(self) -> None:
        low = _result(0)
        self.analyzer._apply_score_anchor(low, _context(3))
        self.assertGreaterEqual(low.sentiment_score, 0)

        high = _result(100)
        self.analyzer._apply_score_anchor(high, _context(98))
        self.assertLessEqual(high.sentiment_score, 100)

    def test_missing_system_score_is_noop(self) -> None:
        result = _result(80)
        self.analyzer._apply_score_anchor(result, {"trend_analysis": {}})
        self.assertEqual(result.sentiment_score, 80)
        self.assertNotIn("score_anchor", result.dashboard)

    def test_invalid_system_score_is_noop(self) -> None:
        for bad in (None, "", "abc", -5, 150):
            result = _result(80)
            self.analyzer._apply_score_anchor(result, _context(bad))
            self.assertEqual(result.sentiment_score, 80)
            self.assertNotIn("score_anchor", result.dashboard)

    def test_empty_dashboard_is_noop(self) -> None:
        result = _result(95)
        result.dashboard = {}
        self.analyzer._apply_score_anchor(result, _context(66))
        self.assertEqual(result.sentiment_score, 95)


class DataGapSectionTest(unittest.TestCase):
    def test_complete_data_returns_empty(self) -> None:
        ctx = {
            "fundamental_context": {"coverage": {"valuation": "ok", "growth": "ok"}},
            "chip": {"profit_ratio": 0.5},
            "today": {"is_estimated": False},
            "market_structure_context": {"status": "ok", "missing_fields": []},
        }
        self.assertEqual(_format_data_gap_section(ctx, english=False), "")

    def test_failed_blocks_are_listed(self) -> None:
        ctx = {
            "fundamental_context": {
                "coverage": {"valuation": "ok", "growth": "failed", "capital_flow": "not_supported"}
            }
        }
        rendered = _format_data_gap_section(ctx, english=False)
        self.assertIn("本轮数据缺口", rendered)
        self.assertIn("成长性", rendered)
        self.assertIn("主力资金流", rendered)
        self.assertNotIn("估值", rendered)
        self.assertIn("数据不足，无法判断", rendered)

    def test_partial_status_reported(self) -> None:
        ctx = {"fundamental_context": {"coverage": {"boards": "partial"}}}
        rendered = _format_data_gap_section(ctx, english=False)
        self.assertIn("板块归属：仅部分字段可用", rendered)

    def test_missing_chip_and_news_reported(self) -> None:
        rendered = _format_data_gap_section(
            {"chip": {}, "today": {}}, english=False, news_available=False
        )
        self.assertIn("筹码分布", rendered)
        self.assertIn("新闻与公告", rendered)

    def test_estimated_quote_fields_listed(self) -> None:
        ctx = {"today": {"is_estimated": True, "estimated_fields": ["close", "volume"]}}
        rendered = _format_data_gap_section(ctx, english=False)
        self.assertIn("盘中估算值", rendered)
        self.assertIn("close, volume", rendered)

    def test_english_variant(self) -> None:
        ctx = {"fundamental_context": {"coverage": {"growth": "failed"}}}
        rendered = _format_data_gap_section(ctx, english=True)
        self.assertIn("Data Gaps", rendered)
        self.assertIn("Growth", rendered)
        self.assertIn("insufficient data, cannot judge", rendered)


class PromptWiringTest(unittest.TestCase):
    def test_all_four_prompt_variants_carry_the_anchor(self) -> None:
        zh_variants = ("LEGACY_DEFAULT_SYSTEM_PROMPT", "SYSTEM_PROMPT")
        en_variants = ("LEGACY_DEFAULT_SYSTEM_PROMPT_EN", "SYSTEM_PROMPT_EN")
        for name in zh_variants:
            text = getattr(GeminiAnalyzer, name)
            self.assertIn("评分锚定", text, f"{name} missing anchor block")
            self.assertIn("系统评分 ±10", text, f"{name} missing band rule")
        for name in en_variants:
            text = getattr(GeminiAnalyzer, name)
            self.assertIn("Score Anchoring", text, f"{name} missing anchor block")
            self.assertIn("System Score ±10", text, f"{name} missing band rule")


if __name__ == "__main__":
    unittest.main()
