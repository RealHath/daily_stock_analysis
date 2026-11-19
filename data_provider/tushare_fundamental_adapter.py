# -*- coding: utf-8 -*-
"""
Tushare fundamental adapter (fail-open, opt-in).

Alternative to :class:`AkshareFundamentalAdapter` for A-share fundamentals.
Selected via ``FUNDAMENTAL_PROVIDER=tushare`` (default stays ``akshare``).

Design contract:
- Same three-method interface and return shape as AkshareFundamentalAdapter,
  so it is a drop-in replacement inside DataManager.get_fundamental_context.
- No internal timeout/budget logic. Callers already wrap each method with
  ``_run_with_retry`` using FUNDAMENTAL_STAGE_TIMEOUT_SECONDS /
  FUNDAMENTAL_FETCH_TIMEOUT_SECONDS. Only the HTTP client socket timeout is
  set here, and it is derived from the shared fetch-timeout config.
- Fail-open: never raises to caller; partial data is allowed.
- Uses the existing lightweight ``_TushareHttpClient`` (no tushare SDK needed).
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s or s.lower() in ("nan", "none", "-"):
        return None
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _normalize_code(raw: Any) -> str:
    """002832.SZ / sz002832 / 002832 -> 002832 for row matching."""
    s = str(raw or "").strip().upper()
    if "." in s:
        s = s.split(".", 1)[0]
    return re.sub(r"^(SH|SZ|BJ)", "", s)


def _to_ts_code(stock_code: str) -> str:
    """002832 -> 002832.SZ ; 600001 -> 600001.SH ; 8xxxxx -> BJ."""
    code = _normalize_code(stock_code)
    if len(code) != 6 or not code.isdigit():
        return str(stock_code or "").strip().upper()
    if code.startswith(("60", "68", "9")):
        suffix = "SH"
    elif code.startswith(("8", "4")):
        suffix = "BJ"
    else:
        suffix = "SZ"
    return f"{code}.{suffix}"


def _row_value(row: Dict[str, Any], key: str) -> Any:
    return row.get(key)


def _recent_report_periods(now: Optional[datetime] = None, count: int = 3) -> List[str]:
    """YYYYMMDD period ends for the most recently completed quarters."""
    now = now or datetime.now()
    quarter_start = datetime(now.year, (now.month - 1) // 3 * 3 + 1, 1)
    periods: List[str] = []
    for _ in range(max(1, count)):
        end = quarter_start - timedelta(days=1)
        periods.append(end.strftime("%Y%m%d"))
        quarter_start = datetime(end.year, (end.month - 1) // 3 * 3 + 1, 1)
    return periods


class TushareFundamentalAdapter:
    """Tushare-backed fundamental/capital-flow/dragon-tiger adapter."""

    def __init__(self) -> None:
        self._client: Optional[Any] = None
        self._init_error: Optional[str] = None
        try:
            from src.config import get_config
            from .tushare_fetcher import _TushareHttpClient, _DEFAULT_TUSHARE_PRO_URL

            config = get_config()
            token = getattr(config, "tushare_token", None)
            if not token:
                self._init_error = "TUSHARE_TOKEN not configured"
                logger.warning("[TushareFundamentalAdapter] TUSHARE_TOKEN 未配置，adapter 不可用")
                return
            # Derive HTTP socket timeout from the shared fundamental fetch
            # timeout so a slow upstream cannot outlive the caller's budget.
            fetch_timeout = float(getattr(config, "fundamental_fetch_timeout_seconds", 8.0) or 8.0)
            socket_timeout = max(5, min(30, int(math.ceil(fetch_timeout))))
            # 项目默认 client 使用 http；实测 https 稳定，这里显式使用 https。
            api_url = _DEFAULT_TUSHARE_PRO_URL.replace("http://", "https://", 1)
            self._client = _TushareHttpClient(token=token, timeout=socket_timeout, api_url=api_url)
        except Exception as exc:  # noqa: BLE001 - fail-open
            self._init_error = f"init:{type(exc).__name__}"
            logger.warning("[TushareFundamentalAdapter] 初始化失败: %s", exc)

    # ------------------------------------------------------------------
    # low-level helpers
    # ------------------------------------------------------------------
    def _query(
        self,
        api_name: str,
        params: Dict[str, Any],
        fields: str = "",
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        if self._client is None:
            return None, self._init_error or "client unavailable"
        try:
            df = self._client.query(api_name, fields=fields, **params)
            return (df if isinstance(df, pd.DataFrame) else None), None
        except Exception as exc:  # noqa: BLE001 - fail-open
            return None, f"{api_name}:{type(exc).__name__}"

    @staticmethod
    def _first_row(df: Optional[pd.DataFrame], stock_code: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        if df is None or df.empty:
            return None, None
        work = df
        code_cols = [c for c in df.columns if str(c).lower() in ("ts_code", "symbol", "code", "股票代码")]
        if code_cols:
            target = _to_ts_code(stock_code)
            col = code_cols[0]
            matched = work[work[col].astype(str).str.upper() == target]
            if matched.empty:
                # tolerate plain 6-digit codes
                plain = _normalize_code(stock_code)
                matched = work[work[col].astype(str).map(_normalize_code) == plain]
            work = matched
        if work.empty:
            return None, None
        # Prefer the latest report date / announcement when present.
        for sort_col in ("end_date", "ann_date", "trade_date", "f_ann_date"):
            if sort_col in work.columns:
                work = work.sort_values(sort_col, ascending=False)
                break
        row = work.iloc[0]
        return {str(k): v for k, v in row.to_dict().items()}, None

    # ------------------------------------------------------------------
    # public interface (mirrors AkshareFundamentalAdapter)
    # ------------------------------------------------------------------
    def get_fundamental_bundle(self, stock_code: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }
        ts_code = _to_ts_code(stock_code)

        # 1) fina_indicator -> growth + ROE + margins + yoy (single call covers growth fully)
        fin_fields = (
            "ts_code,end_date,ann_date,roe,roe_waa,netprofit_yoy,dt_netprofit_yoy,"
            "or_yoy,tr_yoy,grossprofit_margin,netprofit_margin,eps,bps"
        )
        fin_df, fin_err = self._query("fina_indicator", {"ts_code": ts_code}, fin_fields)
        if fin_err:
            result["errors"].append(fin_err)
        fin_row, _ = self._first_row(fin_df, ts_code)
        if fin_row:
            growth = {
                "revenue_yoy": _safe_float(fin_row.get("tr_yoy") or fin_row.get("or_yoy")),
                "net_profit_yoy": _safe_float(fin_row.get("netprofit_yoy")),
                "roe": _safe_float(fin_row.get("roe") if fin_row.get("roe") is not None else fin_row.get("roe_waa")),
                "gross_margin": _safe_float(fin_row.get("grossprofit_margin")),
            }
            growth = {k: v for k, v in growth.items() if v is not None}
            if growth:
                result["growth"] = growth
                result["source_chain"].append("growth:tushare_fina_indicator")

            report_date = str(fin_row.get("end_date") or "")[:8]
            fin_report = {
                "report_date": f"{report_date[:4]}-{report_date[4:6]}-{report_date[6:8]}" if len(report_date) == 8 else None,
                "roe": _safe_float(fin_row.get("roe")),
            }
            fin_report = {k: v for k, v in fin_report.items() if v is not None}
            if fin_report:
                result["earnings"]["financial_report"] = fin_report

        # 2) income -> revenue / net profit attributable to parent
        inc_df, inc_err = self._query(
            "income", {"ts_code": ts_code},
            "ts_code,end_date,total_revenue,revenue,n_income_attr_p,n_income",
        )
        if inc_err:
            result["errors"].append(inc_err)
        inc_row, _ = self._first_row(inc_df, ts_code)
        if inc_row:
            fin_report = result["earnings"].setdefault("financial_report", {})
            revenue = _safe_float(inc_row.get("total_revenue") or inc_row.get("revenue"))
            net_profit = _safe_float(inc_row.get("n_income_attr_p") or inc_row.get("n_income"))
            if revenue is not None:
                fin_report["revenue"] = revenue
            if net_profit is not None:
                fin_report["net_profit_parent"] = net_profit
            if not fin_report.get("report_date") and inc_row.get("end_date"):
                d = str(inc_row["end_date"])[:8]
                if len(d) == 8:
                    fin_report["report_date"] = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            if fin_report:
                result["earnings"]["financial_report"] = fin_report
                result["source_chain"].append("financial_report:tushare_income")

        # 3) cashflow -> operating cash flow
        cf_df, cf_err = self._query(
            "cashflow", {"ts_code": ts_code},
            "ts_code,end_date,n_cashflow_act",
        )
        if cf_err:
            result["errors"].append(cf_err)
        cf_row, _ = self._first_row(cf_df, ts_code)
        if cf_row:
            ocf = _safe_float(cf_row.get("n_cashflow_act"))
            if ocf is not None:
                result["earnings"].setdefault("financial_report", {})["operating_cash_flow"] = ocf
                result["source_chain"].append("cashflow:tushare_cashflow")

        # 4) dividend -> pre-tax cash dividend per share
        div_df, div_err = self._query(
            "dividend", {"ts_code": ts_code},
            "ts_code,end_date,ann_date,div_proc,cash_div_tax,cash_div,ex_date,record_date",
        )
        if div_err:
            result["errors"].append(div_err)
        if div_df is not None and not div_df.empty:
            events: List[Dict[str, Any]] = []
            now_date = datetime.now().date()
            ttm_start = now_date - timedelta(days=365)
            seen = set()
            for _, r in div_df.iterrows():
                ex = str(r.get("ex_date") or "").strip()
                record = str(r.get("record_date") or "").strip()
                ann = str(r.get("ann_date") or "").strip()
                end = str(r.get("end_date") or "").strip()
                # Tushare cash_div_tax = pre-tax dividend PER SHARE (yuan),
                # already matches the per-share semantics expected downstream.
                per_share = _safe_float(r.get("cash_div_tax") if r.get("cash_div_tax") is not None else r.get("cash_div"))
                if per_share is None or per_share <= 0:
                    continue
                per_share = round(per_share, 6)
                event_date = ex or record or ann or end
                if len(event_date) != 8:
                    continue
                try:
                    ed = datetime.strptime(event_date, "%Y%m%d").date()
                except ValueError:
                    continue
                if ed > now_date:
                    continue
                key = (ed.isoformat(), per_share)
                if key in seen:
                    continue
                seen.add(key)
                events.append({
                    "event_date": ed.isoformat(),
                    "ex_dividend_date": f"{ex[:4]}-{ex[4:6]}-{ex[6:8]}" if len(ex) == 8 else None,
                    "record_date": f"{record[:4]}-{record[4:6]}-{record[6:8]}" if len(record) == 8 else None,
                    "announcement_date": f"{ann[:4]}-{ann[4:6]}-{ann[6:8]}" if len(ann) == 8 else None,
                    "cash_dividend_per_share": per_share,
                    "is_pre_tax": True,
                })
            if events:
                events.sort(key=lambda x: x.get("event_date") or "", reverse=True)
                ttm = [e for e in events if ttm_start <= datetime.strptime(e["event_date"], "%Y-%m-%d").date() <= now_date]
                result["earnings"]["dividend"] = {
                    "events": events[:5],
                    "ttm_event_count": len(ttm),
                    "ttm_cash_dividend_per_share": round(
                        sum(float(e.get("cash_dividend_per_share") or 0.0) for e in ttm), 6
                    ) if ttm else None,
                    "coverage": "cash_dividend_pre_tax",
                    "as_of": now_date.isoformat(),
                }
                result["source_chain"].append("dividend:tushare_dividend")

        # 5) forecast -> forecast summary
        fc_df, fc_err = self._query(
            "forecast", {"ts_code": ts_code},
            "ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max,summary,change_reason",
        )
        if fc_err:
            result["errors"].append(fc_err)
        fc_row, _ = self._first_row(fc_df, ts_code)
        if fc_row:
            parts: List[str] = []
            ftype = str(fc_row.get("type") or "").strip()
            if ftype:
                parts.append(ftype)
            lo, hi = _safe_float(fc_row.get("p_change_min")), _safe_float(fc_row.get("p_change_max"))
            if lo is not None or hi is not None:
                parts.append(f"预计同比{lo if lo is not None else '?'}%~{hi if hi is not None else '?'}%")
            summary = str(fc_row.get("summary") or "").strip()
            if summary:
                parts.append(summary[:120])
            if parts:
                result["earnings"]["forecast_summary"] = "；".join(parts)[:200]
                result["source_chain"].append("earnings_forecast:tushare_forecast")

        # 6) express -> quick report summary (only if no forecast to avoid noise)
        if "forecast_summary" not in result["earnings"]:
            ex_df, ex_err = self._query(
                "express", {"ts_code": ts_code},
                "ts_code,ann_date,end_date,revenue,operate_profit,n_income,yoy_net_profit,diluted_roe",
            )
            if ex_err:
                result["errors"].append(ex_err)
            ex_row, _ = self._first_row(ex_df, ts_code)
            if ex_row:
                parts = []
                rev = _safe_float(ex_row.get("revenue"))
                ni = _safe_float(ex_row.get("n_income"))
                yoy = _safe_float(ex_row.get("yoy_net_profit"))
                roe = _safe_float(ex_row.get("diluted_roe"))
                if rev is not None:
                    parts.append(f"营业收入{rev:.12g}元")
                if ni is not None:
                    parts.append(f"净利润{ni:.12g}元")
                if yoy is not None:
                    parts.append(f"净利同比{yoy:.12g}")
                if roe is not None:
                    parts.append(f"ROE{roe:.12g}%")
                if parts:
                    result["earnings"]["quick_report_summary"] = "；".join(parts)[:200]
                    result["source_chain"].append("earnings_quick:tushare_express")

        # 7) top10_holders -> institution holding change
        periods = _recent_report_periods(count=2)
        holders_df = None
        holders_errs: List[str] = []
        for period in periods:
            df, err = self._query(
                "top10_holders", {"ts_code": ts_code, "period": period},
                "ts_code,end_date,holder_name,hold_amount,hold_ratio,hold_change,holder_type",
            )
            if err:
                holders_errs.append(err)
                continue
            if df is not None and not df.empty:
                holders_df = df
                break
        result["errors"].extend(holders_errs)
        if holders_df is not None and not holders_df.empty:
            change_series = holders_df["hold_change"] if "hold_change" in holders_df.columns else None
            vals = [_safe_float(v) for v in change_series.tolist()] if change_series is not None else []
            vals = [v for v in vals if v is not None]
            if vals:
                result["institution"]["institution_holding_change"] = round(sum(vals) / len(vals), 6)
            result["source_chain"].append("institution:tushare_top10_holders")

        # 8) stk_holdernumber -> shareholder count change (fallback institution signal)
        if not result["institution"]:
            hn_df, hn_err = self._query(
                "stk_holdernumber", {"ts_code": ts_code},
                "ts_code,ann_date,end_date,holder_num",
            )
            if hn_err:
                result["errors"].append(hn_err)
            if hn_df is not None and not hn_df.empty:
                hn_df = hn_df.sort_values("end_date", ascending=False) if "end_date" in hn_df.columns else hn_df
                num_series = hn_df["holder_num"] if "holder_num" in hn_df.columns else None
                nums = [_safe_float(v) for v in num_series.tolist()] if num_series is not None else []
                nums = [n for n in nums if n is not None and n > 0]
                if len(nums) >= 2:
                    pct = (nums[0] - nums[1]) / nums[1] * 100.0
                    result["institution"]["holder_count_change_pct"] = round(pct, 4)
                    result["source_chain"].append("institution:tushare_stk_holdernumber")

        has_content = bool(result["growth"] or result["earnings"] or result["institution"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_capital_flow(self, stock_code: str, top_n: int = 5) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }
        ts_code = _to_ts_code(stock_code)
        df, err = self._query(
            "moneyflow", {"ts_code": ts_code},
            "ts_code,trade_date,buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount",
        )
        if err:
            result["errors"].append(err)
        row, _ = self._first_row(df, ts_code)
        if row:
            # net_mf_amount is in 万元; convert to yuan to match AkShare adapter.
            net_wan = _safe_float(row.get("net_mf_amount"))
            if net_wan is not None:
                result["stock_flow"] = {
                    "main_net_inflow": round(net_wan * 10000.0, 2),
                    "inflow_5d": None,
                    "inflow_10d": None,
                }
                result["source_chain"].append("capital_stock:tushare_moneyflow")
        # Sector rankings: Tushare moneyflow_ind_* requires 6000 points; skip
        # gracefully. The existing supplement path can still fill it.
        has_content = bool(result["stock_flow"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_dragon_tiger_flag(self, stock_code: str, lookback_days: int = 20) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "status": "not_supported",
            "is_on_list": False,
            "recent_count": 0,
            "latest_date": None,
            "source_chain": [],
            "errors": [],
        }
        ts_code = _to_ts_code(stock_code)
        # Tushare top_list only accepts trade_date (not code+range). Probe the
        # most recent ~10 sessions; this is a best-effort flag, not exhaustive.
        cal_df, cal_err = self._query("trade_cal", {
            "exchange": "SSE", "start_date": (datetime.now() - timedelta(days=max(lookback_days, 20))).strftime("%Y%m%d"),
            "end_date": datetime.now().strftime("%Y%m%d"), "is_open": "1",
        }, "cal_date,is_open")
        if cal_err:
            result["errors"].append(cal_err)
        dates: List[str] = []
        if cal_df is not None and not cal_df.empty and "cal_date" in cal_df.columns:
            dates = sorted(cal_df["cal_date"].astype(str).tolist(), reverse=True)[:10]
        if not dates:
            # Fallback: last ~14 calendar days (weekends filtered by API empty result).
            base = datetime.now()
            dates = [(base - timedelta(days=i)).strftime("%Y%m%d") for i in range(1, 15)]

        matched_dates: List[str] = []
        for d in dates:
            df, err = self._query("top_list", {"trade_date": d}, "trade_date,ts_code,name,reason")
            if err:
                result["errors"].append(err)
                continue
            if df is None or df.empty:
                continue
            if "ts_code" in df.columns and (df["ts_code"].astype(str).str.upper() == ts_code).any():
                matched_dates.append(d)
        result["source_chain"].append("dragon_tiger:tushare_top_list")
        result["status"] = "ok"
        result["is_on_list"] = bool(matched_dates)
        result["recent_count"] = len(matched_dates)
        result["latest_date"] = max(matched_dates) if matched_dates else None
        return result
