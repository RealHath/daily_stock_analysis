# -*- coding: utf-8 -*-
"""Regression tests for Tushare realtime volume unit normalization.

Background: ``UnifiedRealtimeQuote.volume`` must be in 股 so it can be merged
into the daily-bar frame (also 股). The Pro ``quotation`` payload reports ``vol``
in 手 while the legacy ``get_realtime_quotes`` payload reports ``volume`` in 股.
Blindly trusting either one produced a 100x volume break that flipped
"放量上涨" into "缩量上涨" and changed the technical score.
"""

import importlib.util
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.realtime_types import (  # noqa: E402
    normalize_realtime_volume_to_shares as _normalize_realtime_volume_to_shares,
)
from data_provider.tushare_fetcher import TushareFetcher  # noqa: E402

# Real production values from 002832 on 2026-11-17.
_SHARES = 6831199.0
_LOTS = 68311
_PRICE = 29.12
_AMOUNT = 197799615.63


def _make_fetcher() -> TushareFetcher:
    with patch.object(TushareFetcher, "_init_api", return_value=None):
        fetcher = TushareFetcher()
    fetcher._api = MagicMock()
    fetcher.priority = 2
    return fetcher


class NormalizeHelperTest(unittest.TestCase):
    def test_lot_payload_is_converted_to_shares(self) -> None:
        volume = _normalize_realtime_volume_to_shares(
            _LOTS, amount=_AMOUNT, price=_PRICE, source_unit="lot"
        )
        self.assertEqual(volume, _LOTS * 100)

    def test_share_payload_is_left_unchanged(self) -> None:
        volume = _normalize_realtime_volume_to_shares(
            int(_SHARES), amount=_AMOUNT, price=_PRICE, source_unit="share"
        )
        self.assertEqual(volume, int(_SHARES))

    def test_cross_check_overrides_declared_unit(self) -> None:
        # Upstream silently switching to shares must not double the value.
        volume = _normalize_realtime_volume_to_shares(
            int(_SHARES), amount=_AMOUNT, price=_PRICE, source_unit="lot"
        )
        self.assertEqual(volume, int(_SHARES))

    def test_fallback_to_declared_unit_without_amount(self) -> None:
        self.assertEqual(
            _normalize_realtime_volume_to_shares(_LOTS, source_unit="lot"),
            _LOTS * 100,
        )
        self.assertEqual(
            _normalize_realtime_volume_to_shares(int(_SHARES), source_unit="share"),
            int(_SHARES),
        )

    def test_missing_volume_returns_none(self) -> None:
        self.assertIsNone(_normalize_realtime_volume_to_shares(None))
        self.assertIsNone(_normalize_realtime_volume_to_shares(""))

    def test_zero_volume_is_preserved(self) -> None:
        self.assertEqual(_normalize_realtime_volume_to_shares(0), 0)


class ProQuotationPathTest(unittest.TestCase):
    def _quotation_frame(self, **overrides):
        row = {
            "name": "比音勒芬",
            "price": str(_PRICE),
            "pct_chg": "1.11",
            "change": "0.32",
            "vol": str(_LOTS),
            "amount": str(_AMOUNT),
            "high": "29.47",
            "low": "28.48",
            "open": "28.76",
            "pre_close": "28.80",
        }
        row.update(overrides)
        return pd.DataFrame([row])

    def test_pro_quotation_volume_is_in_shares(self) -> None:
        fetcher = _make_fetcher()
        fetcher._api.quotation.return_value = self._quotation_frame()

        quote = fetcher.get_realtime_quote("002832")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.volume, _LOTS * 100)

    def test_pro_quotation_volume_without_amount_uses_lot_unit(self) -> None:
        fetcher = _make_fetcher()
        frame = self._quotation_frame()
        frame = frame.drop(columns=["amount"])
        fetcher._api.quotation.return_value = frame

        quote = fetcher.get_realtime_quote("002832")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.volume, _LOTS * 100)

    def test_pro_quotation_volume_survives_upstream_share_unit_change(self) -> None:
        fetcher = _make_fetcher()
        fetcher._api.quotation.return_value = self._quotation_frame(vol=str(int(_SHARES)))

        quote = fetcher.get_realtime_quote("002832")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.volume, int(_SHARES))


class LegacyRealtimePathTest(unittest.TestCase):
    def test_legacy_realtime_volume_keeps_share_unit(self) -> None:
        fetcher = _make_fetcher()
        fetcher._api.quotation.side_effect = Exception("no permission")

        tushare_module = MagicMock()
        tushare_module.get_realtime_quotes.return_value = pd.DataFrame(
            [
                {
                    "name": "比音勒芬",
                    "price": str(_PRICE),
                    "pre_close": "28.80",
                    "volume": str(int(_SHARES)),
                    "amount": str(_AMOUNT),
                    "high": "29.47",
                    "low": "28.48",
                    "open": "28.76",
                }
            ]
        )

        with patch.dict(sys.modules, {"tushare": tushare_module}):
            quote = fetcher.get_realtime_quote("002832")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.volume, int(_SHARES))


class AkshareEmRealtimePathTest(unittest.TestCase):
    """AkShare 东财现货的 ``成交量`` 同样是「手」，也必须归一为「股」。"""

    @staticmethod
    def _frame(**overrides):
        row = {
            "代码": "002832",
            "名称": "比音勒芬",
            "最新价": _PRICE,
            "涨跌幅": 1.11,
            "涨跌额": 0.32,
            "成交量": _LOTS,
            "成交额": _AMOUNT,
            "量比": 1.17,
            "换手率": 1.77,
            "振幅": 3.44,
            "今开": 28.76,
            "最高": 29.47,
            "最低": 28.48,
            "市盈率-动态": 24.52,
            "市净率": 2.93,
            "总市值": 16619000000.0,
            "流通市值": 11268000000.0,
            "60日涨跌幅": 5.0,
            "52周最高": 33.0,
            "52周最低": 20.0,
        }
        row.update(overrides)
        return pd.DataFrame([row])

    def _fetch(self, frame):
        from data_provider import akshare_fetcher as af

        fetcher = af.AkshareFetcher()
        breaker = MagicMock()
        with patch.object(fetcher, "_enforce_rate_limit", lambda: None), patch.object(
            fetcher, "_set_random_user_agent", lambda: None
        ), patch.object(
            af,
            "_realtime_cache",
            {"data": frame, "timestamp": time.time(), "ttl": 300},
        ), patch.object(
            af, "get_realtime_circuit_breaker", lambda: breaker
        ):
            return fetcher._get_stock_realtime_quote_em("002832")

    def test_em_spot_volume_is_converted_to_shares(self) -> None:
        quote = self._fetch(self._frame())
        self.assertIsNotNone(quote)
        self.assertEqual(quote.volume, _LOTS * 100)

    def test_em_spot_volume_survives_upstream_share_unit_change(self) -> None:
        quote = self._fetch(self._frame(成交量=int(_SHARES)))
        self.assertIsNotNone(quote)
        self.assertEqual(quote.volume, int(_SHARES))


class DailyBarMergeTest(unittest.TestCase):
    """The regression that mattered: volume ratio must stay in a sane range."""

    def test_volume_ratio_matches_daily_bar_scale(self) -> None:
        fetcher = _make_fetcher()
        fetcher._api.quotation.return_value = pd.DataFrame(
            [
                {
                    "name": "比音勒芬",
                    "price": str(_PRICE),
                    "pct_chg": "1.11",
                    "vol": str(_LOTS),
                    "amount": str(_AMOUNT),
                    "high": "29.47",
                    "low": "28.48",
                    "open": "28.76",
                    "pre_close": "28.80",
                }
            ]
        )

        quote = fetcher.get_realtime_quote("002832")
        self.assertIsNotNone(quote)

        # Prior 5 trading days of daily-bar volume, in shares.
        prior = [6049879.0, 6710019.0, 6385678.0, 4351564.0, 5689094.0]
        avg5 = sum(prior) / len(prior)
        ratio = quote.volume / avg5

        # 0.7 ~ 1.5 is the "量能正常" band in src/stock_analyzer.py; a 100x
        # unit break would land at ~0.011 and be misread as 缩量上涨.
        self.assertGreater(ratio, 0.7)
        self.assertLess(ratio, 1.5)
        self.assertAlmostEqual(ratio, 1.17, places=2)


if __name__ == "__main__":
    unittest.main()
