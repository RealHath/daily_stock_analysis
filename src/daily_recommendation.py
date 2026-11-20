# -*- coding: utf-8 -*-
"""每日 AI 推荐段落生成。

把 DSA 已有的选股引擎（ScreeningService）结果渲染成与每日邮件报告
同风格的 Markdown 段落，作为一封独立邮件发送。邮件同时展示"引擎原始
推荐"与"按用户价格上限筛选后推荐"两份列表，方便对照。

设计原则：
- 完全 fail-open：任何异常都返回 None，绝不影响原有个股/大盘报告。
- 零侵入：不修改筛选引擎任何代码，只在结果层做价格过滤与渲染。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _env_float(name: str, default: Optional[float] = None) -> Optional[float]:
    """读取非负浮点环境变量，非法值回退默认值。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        return value if value >= 0 else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 100) -> int:
    """读取有界整数环境变量，非法值回退默认值。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        return min(max(value, minimum), maximum)
    except (TypeError, ValueError):
        return default


def _as_number(value: Any) -> Optional[float]:
    """把可能是字符串/None 的分数稳妥转成 float。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_score(value: Any) -> str:
    number = _as_number(value)
    if number is None:
        return "--"
    if number == int(number):
        return str(int(number))
    return f"{number:.1f}"


def _first_text(*values: Any) -> str:
    """返回第一个非空文本，去掉首尾空白。"""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _filter_by_price(candidates: List[Dict[str, Any]], price_max: Optional[float]) -> List[Dict[str, Any]]:
    """按价格上限过滤候选；无价格或无法解析的候选予以保留（避免误删）。"""
    if price_max is None:
        return candidates
    kept: List[Dict[str, Any]] = []
    for item in candidates:
        price = _as_number(item.get("price"))
        if price is None or price <= price_max:
            kept.append(item)
    return kept


def _factor_summary(factor_scores: Any) -> str:
    """把因子字典压成一行中文摘要，只保留有值的项。"""
    if not isinstance(factor_scores, dict) or not factor_scores:
        return ""
    label_map = {
        "value": "估值",
        "liquidity": "流动性",
        "momentum": "动量",
        "reversal": "反转",
        "activity": "活跃度",
        "stability": "稳定性",
        "size": "市值",
        "theme_heat": "题材热度",
        "topic_alignment": "题材契合",
    }
    parts: List[str] = []
    for key, value in factor_scores.items():
        score = _as_number(value)
        if score is None:
            continue
        label = label_map.get(str(key), str(key))
        parts.append(f"{label} {_fmt_score(score)}")
    return " / ".join(parts)


def _render_stock_list(items: List[Dict[str, Any]], *, show_price: bool = True) -> List[str]:
    """把一个候选列表渲染成带编号的条目行（两份列表共用的渲染逻辑）。"""
    lines: List[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        code = _first_text(item.get("code"))
        name = _first_text(item.get("name"))
        score = _fmt_score(item.get("score") if item.get("score") is not None else item.get("final_score"))
        title = name or "未知股票"
        label = f"{title}({code})" if code else title
        lines.append(f"{index}. 🟢 **{label}** · {score}分")

        reason = _first_text(
            item.get("ranking_reason"),
            item.get("reason"),
            item.get("llm_thesis"),
        )
        if reason:
            lines.append(f"   - 💡 入选理由：{reason}")

        factor_text = _factor_summary(item.get("factor_scores"))
        if factor_text:
            lines.append(f"   - 📊 因子：{factor_text}")

        if show_price:
            price = _as_number(item.get("price"))
            if price is not None:
                lines.append(f"   - 💰 现价：{price:.2f} 元")
        lines.append("")
    return lines


def render_recommendation_markdown(
    result: Dict[str, Any],
    *,
    top_n: int,
    price_max: Optional[float],
    strategy: str,
) -> str:
    """把 ScreeningService.screen() 的响应渲染成邮件段落。

    邮件同时给出两份列表：
    1. 原始推荐榜——引擎按策略默认价格上限（如 220 元）筛出的前 top_n 只；
    2. 价格筛选榜——在上面结果里再按用户 price_max 过滤后的前 top_n 只。
    两份列表都为空时才返回空串（跳过整封邮件）。
    """
    candidates = result.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []

    # 原始榜：引擎返回顺序即最终排序，直接取前 top_n 只（不含用户价格上限）。
    original = candidates[:top_n]
    # 价格筛选榜：在候选池里按用户价格上限过滤后再取前 top_n 只。
    filtered = _filter_by_price(candidates, price_max)[:top_n] if price_max is not None else []

    if not original and not filtered:
        return ""

    lines: List[str] = ["## 🎯 今日 AI 推荐 Top %d" % max(len(original), len(filtered)), ""]

    market_view = _first_text(
        result.get("llm_market_view"),
        result.get("market_view"),
    )
    if market_view:
        lines.extend(["### 📊 市场观点", "", market_view, ""])

    selection_logic = _first_text(
        result.get("llm_selection_logic"),
        result.get("selection_logic"),
    )
    if selection_logic:
        lines.extend(["### 💡 选股思路", "", selection_logic, ""])

    # 列表一：引擎原始推荐（不按用户的价格上限过滤）
    if original:
        if price_max is not None:
            lines.extend(["### 📈 推荐列表①：引擎原始榜（不限价）", ""])
        else:
            lines.extend(["### 📈 推荐列表", ""])
        lines.extend(_render_stock_list(original))

    # 列表二：用户价格筛选后推荐（仅当设置了价格上限时才展示）
    if price_max is not None:
        lines.extend(["### 💰 推荐列表②：价格筛选榜（≤ %s 元）" % _fmt_score(price_max), ""])
        if filtered:
            lines.extend(_render_stock_list(filtered))
        else:
            lines.append(f"> 该价格区间内暂无候选，可参考上方原始榜。")
            lines.append("")

    portfolio_risk = _first_text(
        result.get("llm_portfolio_risk"),
        result.get("portfolio_risk"),
    )
    if portfolio_risk:
        lines.extend(["### 🛡️ 组合风险", "", portfolio_risk, ""])

    notes: List[str] = []
    if price_max is not None:
        notes.append(f"价格筛选上限 {_fmt_score(price_max)} 元")
    if strategy:
        notes.append(f"策略 {strategy}")
    if notes:
        lines.extend(["---", "", f"> 筛选条件：{' ｜ '.join(notes)}。AI 生成，仅供参考，不构成投资建议。", ""])

    return "\n".join(lines).strip()


def build_recommendation_markdown(config: Any, db_manager: Any = None) -> Optional[str]:
    """生成每日推荐 Markdown 段落；失败返回 None，不影响主报告。"""
    try:
        if not getattr(config, "screening_enabled", False):
            return None

        strategy = os.getenv("SCREENING_STRATEGY", "balanced_alpha").strip() or "balanced_alpha"
        top_n = _env_int("RECOMMEND_TOP_N", 5, minimum=1, maximum=20)
        price_max = _env_float("RECOMMEND_PRICE_MAX", None)

        from src.services.screening_service import ScreeningService

        service = ScreeningService(config=config, db_manager=db_manager)
        # 多取候选，供"原始榜"与"价格筛选榜"两份列表共用；
        # 保证价格过滤后仍有足够数量，上限 60 避免拖慢流程。
        fetch_count = min(max(top_n * 4, top_n), 60)
        result = service.screen(
            strategy=strategy,
            market="cn",
            max_results=fetch_count,
        )
        if not isinstance(result, dict):
            return None

        markdown = render_recommendation_markdown(
            result,
            top_n=top_n,
            price_max=price_max,
            strategy=strategy,
        )
        if not markdown:
            logger.info("AI 推荐：价格过滤后无候选，跳过推荐段")
            return None
        return markdown
    except Exception as exc:
        logger.warning("AI 推荐生成失败，跳过推荐段：%s", exc)
        return None


def send_recommendation_email(
    notifier: Any,
    config: Any,
    db_manager: Any = None,
) -> bool:
    """生成并单独发送一封 AI 推荐邮件；失败返回 False，绝不影响主报告。

    复用 notifier 已有的 SMTP 配置与收件人，只在发送时指定独立标题，
    使推荐邮件与个股/大盘主报告分开。
    """
    try:
        markdown = build_recommendation_markdown(config, db_manager=db_manager)
        if not markdown:
            return False

        # 与主报告一致的收件人：全员邮件组，未配置分组时走默认收件人。
        receivers = None
        if getattr(notifier, "_stock_email_groups", None):
            get_all = getattr(notifier, "get_all_email_receivers", None)
            if callable(get_all):
                receivers = get_all() or None

        date_str = datetime.now().strftime("%Y-%m-%d")
        subject = f"🤖 AI 智能推荐 - {date_str}"
        sent = notifier.send_to_email(markdown, subject=subject, receivers=receivers)
        if sent:
            logger.info("已单独推送 AI 推荐邮件：%s", subject)
        else:
            logger.warning("AI 推荐邮件发送失败（SMTP 未配置或发送异常）")
        return bool(sent)
    except Exception as exc:
        logger.warning("AI 推荐邮件发送失败，已跳过：%s", exc)
        return False
