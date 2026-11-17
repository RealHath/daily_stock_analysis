# -*- coding: utf-8 -*-
"""
===================================
Report Engine - Review Comparison Service
===================================

Builds forward-outcome review samples ("复盘对照") from persisted analysis
history plus persisted daily bars.

Design notes:
- Signal records come from ``AnalysisHistory`` (same source as
  :mod:`src.services.history_comparison_service`), so the review section and
  the existing history-comparison section never disagree about "what was said".
- Direction/outcome classification is delegated to
  :class:`src.core.backtest_engine.BacktestEngine`, so the review section and
  the backtest module share one definition of "correct".
- Read-only: never writes, never fetches from the network. A missing daily bar
  degrades to ``insufficient_data`` instead of triggering a provider call.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.core.backtest_engine import BacktestEngine, EvaluationConfig
from src.storage import DatabaseManager

logger = logging.getLogger(__name__)

DEFAULT_HORIZON_DAYS = 3
DEFAULT_NEUTRAL_BAND_PCT = 1.0
MIN_NEUTRAL_BAND_PCT = 0.1
MAX_HORIZON_DAYS = 20
# Calendar-day padding around the analysis date. Covers long holiday runs
# without loading an unbounded bar range per history record.
_BAR_LOOKBACK_DAYS = 20
_BAR_LOOKAHEAD_BUFFER_DAYS = 10
_HISTORY_LOOKBACK_DAYS = 90

REVIEW_STATUS_COMPLETED = "completed"
REVIEW_STATUS_INSUFFICIENT_DATA = "insufficient_data"
REVIEW_STATUS_NO_PRICE = "no_price"

OUTCOME_WIN = "win"
OUTCOME_LOSS = "loss"
OUTCOME_NEUTRAL = "neutral"


def _coerce_horizon_days(value: Any, default: int = DEFAULT_HORIZON_DAYS) -> int:
    try:
        horizon = int(value)
    except (TypeError, ValueError):
        return default
    if horizon < 1:
        return default
    return min(horizon, MAX_HORIZON_DAYS)


def _coerce_neutral_band(value: Any, default: float = DEFAULT_NEUTRAL_BAND_PCT) -> float:
    try:
        band = abs(float(value))
    except (TypeError, ValueError):
        return default
    if band < MIN_NEUTRAL_BAND_PCT:
        return default
    return band


def _coerce_count(value: Any, default: int = 0) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return default
    return max(count, 0)


def _bar_date(bar: Any) -> date:
    """Resolve a bar's trading date, tolerating date/datetime/str rows."""
    from src.services.history_loader import _coerce_bar_date

    return _coerce_bar_date(getattr(bar, "date", None))


def _load_bars(
    db: DatabaseManager,
    code: str,
    start: date,
    end: date,
) -> List[Any]:
    """Load daily bars for a code, trying canonical and normalized spellings."""
    try:
        from src.services.history_loader import _history_code_candidates

        candidates, _normalized = _history_code_candidates(code)
    except Exception as exc:  # noqa: BLE001 - provider stack may be unavailable
        # The review section renders from the database only; if code
        # normalization cannot be built, fall back to the raw spelling rather
        # than breaking the report.
        logger.debug("review: code normalization unavailable for %s: %s", code, exc)
        candidates = [code]
    best: List[Any] = []
    for candidate in candidates:
        try:
            bars = list(db.get_data_range(candidate, start, end) or [])
        except Exception as exc:  # noqa: BLE001 - review is best-effort only
            logger.debug("review: bar read failed for %s: %s", candidate, exc)
            continue
        if len(bars) > len(best):
            best = bars
    return best


def _split_bars(
    bars: Sequence[Any],
    analysis_date: date,
    horizon_days: int,
) -> tuple[Optional[Any], List[Any]]:
    """Split bars into the base bar (on/before the date) and forward bars."""
    base_bar: Optional[Any] = None
    forward: List[Any] = []
    for bar in bars:
        bar_day = _bar_date(bar)
        if bar_day == date.min:
            continue
        if bar_day <= analysis_date:
            if base_bar is None or bar_day >= _bar_date(base_bar):
                base_bar = bar
            continue
        forward.append(bar)
    forward.sort(key=_bar_date)
    return base_bar, forward[:horizon_days]


def _build_sample(
    record: Any,
    *,
    bars: Sequence[Any],
    horizon_days: int,
    neutral_band_pct: float,
) -> Optional[Dict[str, Any]]:
    """Build one review sample from an analysis-history record."""
    created_at = getattr(record, "created_at", None)
    if not isinstance(created_at, (date, datetime)):
        return None
    analysis_date = created_at.date() if isinstance(created_at, datetime) else created_at
    advice = getattr(record, "operation_advice", None)

    sample: Dict[str, Any] = {
        "analysis_date": analysis_date.isoformat(),
        "analysis_time": (
            created_at.strftime("%Y-%m-%d %H:%M")
            if isinstance(created_at, datetime)
            else analysis_date.isoformat()
        ),
        "operation_advice": advice,
        "sentiment_score": getattr(record, "sentiment_score", None),
        "trend_prediction": getattr(record, "trend_prediction", None),
        "horizon_days": horizon_days,
        "status": REVIEW_STATUS_INSUFFICIENT_DATA,
        "base_close": None,
        "end_close": None,
        "forward_return_pct": None,
        "direction_expected": None,
        "outcome": None,
        "direction_correct": None,
    }

    base_bar, forward_bars = _split_bars(bars, analysis_date, horizon_days)
    base_close = getattr(base_bar, "close", None) if base_bar is not None else None
    if base_close is None or float(base_close or 0) <= 0:
        sample["status"] = REVIEW_STATUS_NO_PRICE
        return sample

    sample["base_close"] = float(base_close)
    if len(forward_bars) < horizon_days:
        # Not enough trading days have elapsed yet; still report what was said.
        sample["forward_bars_available"] = len(forward_bars)
        return sample

    config = EvaluationConfig(
        eval_window_days=horizon_days,
        neutral_band_pct=neutral_band_pct,
        engine_version="review-comparison-v1",
    )
    try:
        evaluation = BacktestEngine.evaluate_single(
            operation_advice=advice,
            analysis_date=analysis_date,
            start_price=float(base_close),
            forward_bars=forward_bars,
            stop_loss=None,
            take_profit=None,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 - one bad sample must not break the report
        logger.debug("review: evaluate_single failed for %r: %s", advice, exc)
        return sample

    sample.update(
        {
            "status": str(evaluation.get("eval_status") or REVIEW_STATUS_COMPLETED),
            "end_close": evaluation.get("end_close"),
            "forward_return_pct": evaluation.get("stock_return_pct"),
            "direction_expected": evaluation.get("direction_expected"),
            "outcome": evaluation.get("outcome"),
            "direction_correct": evaluation.get("direction_correct"),
        }
    )
    return sample


def summarize_review_samples(samples: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate win/loss counts for one code's (or all) review samples."""
    win = 0
    loss = 0
    neutral = 0
    pending = 0
    for sample in samples:
        if sample.get("status") != REVIEW_STATUS_COMPLETED:
            pending += 1
            continue
        outcome = sample.get("outcome")
        if outcome == OUTCOME_WIN:
            win += 1
        elif outcome == OUTCOME_LOSS:
            loss += 1
        else:
            neutral += 1

    decided = win + loss
    hit_rate_pct: Optional[float] = None
    if decided > 0:
        hit_rate_pct = round(win / decided * 100, 1)

    return {
        "total": win + loss + neutral + pending,
        "evaluated": win + loss + neutral,
        "win": win,
        "loss": loss,
        "neutral": neutral,
        "pending": pending,
        "hit_rate_pct": hit_rate_pct,
    }


def build_review_comparison(
    codes: Sequence[str],
    *,
    limit: int = 5,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    neutral_band_pct: float = DEFAULT_NEUTRAL_BAND_PCT,
    exclude_query_ids: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build review samples for the given codes.

    Returns ``samples_by_code``, ``summary_by_code``, ``summary`` and
    ``horizon_days``. Deliberately fail-open: a per-code error yields an empty
    sample list for that code so the report can still be delivered.
    """
    safe_limit = _coerce_count(limit)
    safe_horizon = _coerce_horizon_days(horizon_days)
    safe_band = _coerce_neutral_band(neutral_band_pct)
    excluded = exclude_query_ids or {}

    samples_by_code: Dict[str, List[Dict[str, Any]]] = {}
    summary_by_code: Dict[str, Dict[str, Any]] = {}

    if safe_limit <= 0:
        return {
            "samples_by_code": samples_by_code,
            "summary_by_code": summary_by_code,
            "summary": summarize_review_samples([]),
            "horizon_days": safe_horizon,
        }

    all_samples: List[Dict[str, Any]] = []
    if codes:
        try:
            db = DatabaseManager.get_instance()
        except Exception as exc:  # noqa: BLE001 - report must still render
            logger.debug("review: database unavailable: %s", exc)
            db = None

        if db is not None:
            for code in codes:
                normalized_code = str(code or "").strip()
                if not normalized_code:
                    continue
                samples_by_code[normalized_code] = []
                try:
                    records = db.get_analysis_history(
                        code=normalized_code,
                        days=_HISTORY_LOOKBACK_DAYS,
                        limit=safe_limit,
                        exclude_query_id=excluded.get(normalized_code),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("review: history read failed for %s: %s", normalized_code, exc)
                    summary_by_code[normalized_code] = summarize_review_samples([])
                    continue

                for record in records or []:
                    created_at = getattr(record, "created_at", None)
                    if not isinstance(created_at, (date, datetime)):
                        continue
                    analysis_date = (
                        created_at.date() if isinstance(created_at, datetime) else created_at
                    )
                    bars = _load_bars(
                        db,
                        normalized_code,
                        analysis_date - timedelta(days=_BAR_LOOKBACK_DAYS),
                        analysis_date
                        + timedelta(days=safe_horizon * 2 + _BAR_LOOKAHEAD_BUFFER_DAYS),
                    )
                    sample = _build_sample(
                        record,
                        bars=bars,
                        horizon_days=safe_horizon,
                        neutral_band_pct=safe_band,
                    )
                    if sample is None:
                        continue
                    samples_by_code[normalized_code].append(sample)
                    all_samples.append(sample)

                summary_by_code[normalized_code] = summarize_review_samples(
                    samples_by_code[normalized_code]
                )

    return {
        "samples_by_code": samples_by_code,
        "summary_by_code": summary_by_code,
        "summary": summarize_review_samples(all_samples),
        "horizon_days": safe_horizon,
    }
