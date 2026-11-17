# -*- coding: utf-8 -*-
"""Tests for the forward-outcome review comparison service."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from src.services import review_comparison_service as svc


def _bar(day: date, close: float) -> SimpleNamespace:
    return SimpleNamespace(date=day, open=close, high=close, low=close, close=close)


def _record(day: datetime, advice: str, score: int = 60) -> SimpleNamespace:
    return SimpleNamespace(
        created_at=day,
        operation_advice=advice,
        sentiment_score=score,
        trend_prediction="看多",
        query_id=f"q-{day:%Y%m%d}",
    )


class SummarizeReviewSamplesTest(unittest.TestCase):
    def test_counts_outcomes_and_hit_rate(self):
        samples = [
            {"status": "completed", "outcome": "win"},
            {"status": "completed", "outcome": "win"},
            {"status": "completed", "outcome": "loss"},
            {"status": "completed", "outcome": "neutral"},
            {"status": "insufficient_data", "outcome": None},
        ]
        summary = svc.summarize_review_samples(samples)

        self.assertEqual(summary["win"], 2)
        self.assertEqual(summary["loss"], 1)
        self.assertEqual(summary["neutral"], 1)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["evaluated"], 4)
        self.assertAlmostEqual(summary["hit_rate_pct"], 66.7)

    def test_hit_rate_none_without_decided_samples(self):
        summary = svc.summarize_review_samples([{"status": "completed", "outcome": "neutral"}])
        self.assertIsNone(summary["hit_rate_pct"])


class BuildReviewComparisonTest(unittest.TestCase):
    def test_limit_zero_disables_section(self):
        payload = svc.build_review_comparison(["600519"], limit=0)
        self.assertEqual(payload["samples_by_code"], {})
        self.assertEqual(payload["summary"]["total"], 0)

    def test_empty_codes(self):
        payload = svc.build_review_comparison([], limit=5)
        self.assertEqual(payload["samples_by_code"], {})

    def test_forward_return_and_direction(self):
        analysis_day = date(2026, 11, 2)
        record = _record(datetime(2026, 11, 2, 18, 0), "买入", 70)
        bars = [_bar(analysis_day, 100.0)]
        for offset in range(1, 4):
            bars.append(_bar(analysis_day + timedelta(days=offset), 100.0 + offset))

        fake_db = mock.MagicMock()
        fake_db.get_analysis_history.return_value = [record]
        fake_db.get_data_range.return_value = bars

        with mock.patch.object(
            svc.DatabaseManager, "get_instance", return_value=fake_db
        ):
            payload = svc.build_review_comparison(["600519"], limit=5, horizon_days=3)

        samples = payload["samples_by_code"]["600519"]
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample["status"], "completed")
        self.assertAlmostEqual(sample["forward_return_pct"], 3.0)
        self.assertEqual(sample["direction_expected"], "up")
        self.assertEqual(sample["outcome"], "win")
        self.assertTrue(sample["direction_correct"])
        self.assertAlmostEqual(payload["summary"]["hit_rate_pct"], 100.0)

    def test_insufficient_forward_bars_is_pending(self):
        analysis_day = date(2026, 11, 2)
        record = _record(datetime(2026, 11, 2, 18, 0), "买入", 70)
        bars = [_bar(analysis_day, 100.0), _bar(analysis_day + timedelta(days=1), 101.0)]

        fake_db = mock.MagicMock()
        fake_db.get_analysis_history.return_value = [record]
        fake_db.get_data_range.return_value = bars

        with mock.patch.object(
            svc.DatabaseManager, "get_instance", return_value=fake_db
        ):
            payload = svc.build_review_comparison(["600519"], limit=5, horizon_days=3)

        sample = payload["samples_by_code"]["600519"][0]
        self.assertEqual(sample["status"], "insufficient_data")
        self.assertIsNone(sample["forward_return_pct"])
        self.assertEqual(payload["summary"]["pending"], 1)
        self.assertIsNone(payload["summary"]["hit_rate_pct"])

    def test_missing_bars_marks_no_price(self):
        record = _record(datetime(2026, 11, 2, 18, 0), "买入", 70)
        fake_db = mock.MagicMock()
        fake_db.get_analysis_history.return_value = [record]
        fake_db.get_data_range.return_value = []

        with mock.patch.object(
            svc.DatabaseManager, "get_instance", return_value=fake_db
        ):
            payload = svc.build_review_comparison(["600519"], limit=5)

        sample = payload["samples_by_code"]["600519"][0]
        self.assertEqual(sample["status"], "no_price")
        self.assertEqual(payload["summary"]["pending"], 1)

    def test_history_read_failure_is_fail_open(self):
        fake_db = mock.MagicMock()
        fake_db.get_analysis_history.side_effect = RuntimeError("db down")

        with mock.patch.object(
            svc.DatabaseManager, "get_instance", return_value=fake_db
        ):
            payload = svc.build_review_comparison(["600519"], limit=5)

        self.assertEqual(payload["samples_by_code"]["600519"], [])
        self.assertEqual(payload["summary"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
