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


# 全部可选策略的分类对照信息，用于多策略推荐邮件首部的说明表格。
# 结构：(分组标题, [(策略值, 中文名, 一句话含义), ...])。
# 分类按风险风格划分：稳健（怕回撤）、均衡（多维度兼顾）、激进（追短线）。
# 仅为展示用途，不影响选股引擎；新增策略时在这里补一行即可。
STRATEGY_CATALOG = [
    (
        "🛡️ 稳健型（怕回撤、求稳）",
        [
            ("dual_low", "双低选股", "低估值+低波动，便宜又不闹腾，最严防追高"),
            ("quality_value", "稳健价值", "估值合理、流动性足、波动不过热，质地全面"),
            ("blue_chip_income", "蓝筹收益质量", "只选大盘蓝筹、高股息，最看公司规模"),
            ("low_volatility_quality", "低波质量", "波动和回撤控制最狠，怕买了就阴跌"),
        ],
    ),
    (
        "⚖️ 均衡型（多维度都不差）",
        [
            ("balanced_alpha", "均衡多因子", "估值、动量、流动性均衡，覆盖广（默认）"),
            ("momentum_quality", "趋势质量", "趋势确认+基本面质量，中线持有"),
            ("shrink_pullback", "缩量回踩", "上升趋势中缩量回踩均线，等支撑再入场"),
            ("oversold_reversal", "超跌反转", "跌幅可控、流动性仍在的超跌修复机会"),
        ],
    ),
    (
        "🚀 激进型（追短线、波动大）",
        [
            ("capital_heat", "资金热度", "资金活跃、量价同步，短线弹性大"),
            ("volume_breakout", "放量突破", "放量突破阻力位、趋势启动，追短线"),
        ],
    ),
]


def _render_strategy_catalog(running: List[str]) -> List[str]:
    """渲染全部策略的分类对照表；本次已出结果的策略用 ✅ 标记。"""
    running_set = set(running or [])
    lines: List[str] = [
        "### 🧭 策略一览",
        "",
        "> 共 10 套可选策略，✅ 为本次运行；策略值可填进 SCREENING_STRATEGY 切换。",
        "",
    ]
    for group_title, entries in STRATEGY_CATALOG:
        lines.extend([
            f"#### {group_title}",
            "",
            "| 策略值 | 中文名 | 挑什么股 | 本次 |",
            "| --- | --- | --- | --- |",
        ])
        for key, cn_name, desc in entries:
            mark = "✅" if key in running_set else ""
            lines.append(f"| {key} | {cn_name} | {desc} | {mark} |")
        lines.append("")
    return lines


def _parse_strategies(raw: str, default: str = "balanced_alpha") -> List[str]:
    """解析 SCREENING_STRATEGY：支持逗号/分号分隔的多策略，去重保序。

    例："dual_low, quality_value, balanced_alpha" → 三个策略。
    空值或全部无效时回退默认值。单策略仍返回长度为 1 的列表。
    """
    if not raw or not str(raw).strip():
        return [default]
    parts = [p.strip() for p in str(raw).replace(";", ",").split(",")]
    seen = set()
    result: List[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            result.append(part)
    return result or [default]


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


def _same_stock_list(left: List[Dict[str, Any]], right: List[Dict[str, Any]]) -> bool:
    """按股票代码与顺序判断两榜是否为同一批候选。

    价格筛选没有改变名单时，邮件里再完整渲染一遍相同明细纯属重复，
    这里识别出来后合并为单榜展示；无代码的条目保守返回 False。
    """
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        code_a = _first_text(a.get("code"))
        code_b = _first_text(b.get("code"))
        if not code_a or code_a != code_b:
            return False
    return True


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


def _render_strategy_section(
    result: Dict[str, Any],
    *,
    top_n: int,
    price_max: Optional[float],
    strategy: str,
    heading_level: int = 3,
    show_strategy_header: bool = False,
) -> List[str]:
    """渲染单套策略的完整内容（市场观点/两榜/组合风险），返回行列表。

    heading_level 控制标题层级：单策略邮件用 3（###），多策略分段时用 4（####）。
    """
    candidates = result.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []

    original = candidates[:top_n]
    filtered = _filter_by_price(candidates, price_max)[:top_n] if price_max is not None else []
    if not original and not filtered:
        return []
    # 两榜同批股票时合并展示，避免把同样的理由/因子/现价渲染两遍。
    merged = price_max is not None and _same_stock_list(original, filtered)

    h = "#" * heading_level
    lines: List[str] = []
    if show_strategy_header:
        # 策略标题比内部小节高一级，作为清晰的分段锚点。
        lines.extend([f"{'#' * (heading_level - 1)} 🧭 策略：{strategy}", ""])

    market_view = _first_text(
        result.get("llm_market_view"),
        result.get("market_view"),
    )
    if market_view:
        lines.extend([f"{h} 📊 市场观点", "", market_view, ""])

    selection_logic = _first_text(
        result.get("llm_selection_logic"),
        result.get("selection_logic"),
    )
    if selection_logic:
        lines.extend([f"{h} 💡 选股思路", "", selection_logic, ""])

    if original:
        if merged:
            lines.extend([f"{h} 📈 推荐列表（原始榜 = 价格筛选榜）", ""])
            lines.append(f"> 本次候选均满足 ≤ {_fmt_score(price_max)} 元，价格筛选未改变名单，两榜合并展示。")
            lines.append("")
        elif price_max is not None:
            lines.extend([f"{h} 📈 推荐列表①：引擎原始榜（不限价）", ""])
        else:
            lines.extend([f"{h} 📈 推荐列表", ""])
        lines.extend(_render_stock_list(original))

    if price_max is not None and not merged:
        lines.extend([f"{h} 💰 推荐列表②：价格筛选榜（≤ {_fmt_score(price_max)} 元）", ""])
        if filtered:
            lines.extend(_render_stock_list(filtered))
        else:
            lines.append("> 该价格区间内暂无候选，可参考上方原始榜。")
            lines.append("")

    portfolio_risk = _first_text(
        result.get("llm_portfolio_risk"),
        result.get("portfolio_risk"),
    )
    if portfolio_risk:
        lines.extend([f"{h} 🛡️ 组合风险", "", portfolio_risk, ""])
    return lines


def render_recommendation_markdown(
    result: Dict[str, Any],
    *,
    top_n: int,
    price_max: Optional[float],
    strategy: str,
) -> str:
    """把单套策略的 screen() 响应渲染成邮件段落（保持原有邮件格式）。"""
    section = _render_strategy_section(
        result, top_n=top_n, price_max=price_max, strategy=strategy, heading_level=3
    )
    if not section:
        return ""

    candidates = result.get("candidates") or []
    original = candidates[:top_n] if isinstance(candidates, list) else []
    filtered = _filter_by_price(candidates, price_max)[:top_n] if price_max is not None and isinstance(candidates, list) else []
    lines: List[str] = ["## 🎯 今日 AI 推荐 Top %d" % max(len(original), len(filtered)), ""]
    lines.extend(section)

    notes: List[str] = []
    if price_max is not None:
        notes.append(f"价格筛选上限 {_fmt_score(price_max)} 元")
    if strategy:
        notes.append(f"策略 {strategy}")
    if notes:
        lines.extend(["---", "", f"> 筛选条件：{' ｜ '.join(notes)}。AI 生成，仅供参考，不构成投资建议。", ""])

    return "\n".join(lines).strip()


def render_multi_strategy_markdown(
    sections: List[Dict[str, Any]],
    *,
    top_n: int,
    price_max: Optional[float],
) -> str:
    """把多套策略结果聚合成一封邮件的 Markdown。

    sections: 每项为 {"strategy": str, "result": dict}，已按策略顺序排列。
    只渲染有候选的策略；全部无候选时返回空串。
    """
    rendered: List[tuple] = []  # (strategy, lines)
    for entry in sections:
        if not isinstance(entry, dict):
            continue
        strategy = _first_text(entry.get("strategy"))
        result = entry.get("result") or {}
        lines = _render_strategy_section(
            result,
            top_n=top_n,
            price_max=price_max,
            strategy=strategy,
            heading_level=4,
            show_strategy_header=True,
        )
        if lines:
            rendered.append((strategy, lines))

    if not rendered:
        return ""

    lines: List[str] = ["## 🎯 今日 AI 推荐 · 多策略", ""]
    lines.append(f"> 本次运行 {len(rendered)} 套策略，各有侧重，可对照参考。")
    lines.append("")
    # 首部附全部策略的分类对照表，方便对照本次运行的是哪几类策略。
    lines.extend(_render_strategy_catalog([s for s, _ in rendered]))
    for idx, (strategy, section_lines) in enumerate(rendered, start=1):
        if idx > 1:
            lines.extend(["---", ""])
        lines.extend(section_lines)
        lines.append("")

    notes: List[str] = []
    if price_max is not None:
        notes.append(f"价格筛选上限 {_fmt_score(price_max)} 元")
    notes.append(f"策略 {'、'.join(s for s, _ in rendered)}")
    lines.extend(["---", "", f"> 筛选条件：{' ｜ '.join(notes)}。AI 生成，仅供参考，不构成投资建议。", ""])

    return "\n".join(lines).strip()


def build_recommendation_markdown(config: Any, db_manager: Any = None) -> Optional[str]:
    """生成每日推荐 Markdown；支持多策略，失败返回 None，不影响主报告。"""
    try:
        if not getattr(config, "screening_enabled", False):
            return None

        strategies = _parse_strategies(os.getenv("SCREENING_STRATEGY", "balanced_alpha"))
        top_n = _env_int("RECOMMEND_TOP_N", 5, minimum=1, maximum=20)
        price_max = _env_float("RECOMMEND_PRICE_MAX", None)

        from src.services.screening_service import ScreeningService

        service = ScreeningService(config=config, db_manager=db_manager)
        # 多取候选，供"原始榜"与"价格筛选榜"两份列表共用；
        # 保证价格过滤后仍有足够数量，上限 60 避免拖慢流程。
        fetch_count = min(max(top_n * 4, top_n), 60)

        # 单策略：保持原有邮件格式不变（向后兼容）。
        if len(strategies) == 1:
            strategy = strategies[0]
            result = service.screen(strategy=strategy, market="cn", max_results=fetch_count)
            if not isinstance(result, dict):
                return None
            markdown = render_recommendation_markdown(
                result, top_n=top_n, price_max=price_max, strategy=strategy
            )
            if not markdown:
                logger.info("AI 推荐：策略 %s 过滤后无候选，跳过推荐邮件", strategy)
            return markdown or None

        # 多策略：逐个运行，单套失败只跳过该套，绝不拖累其他策略。
        sections: List[Dict[str, Any]] = []
        for strategy in strategies:
            try:
                result = service.screen(strategy=strategy, market="cn", max_results=fetch_count)
                if isinstance(result, dict):
                    sections.append({"strategy": strategy, "result": result})
                    logger.info("AI 推荐：策略 %s 选股完成", strategy)
                else:
                    logger.warning("AI 推荐：策略 %s 返回格式异常，已跳过", strategy)
            except Exception as exc:
                logger.warning("AI 推荐：策略 %s 失败，已跳过：%s", strategy, exc)
                continue

        if not sections:
            logger.warning("AI 推荐：所有策略均失败，跳过推荐邮件")
            return None

        markdown = render_multi_strategy_markdown(sections, top_n=top_n, price_max=price_max)
        if not markdown:
            logger.info("AI 推荐：所有策略过滤后均无候选，跳过推荐邮件")
            return None
        return markdown
    except Exception as exc:
        logger.warning("AI 推荐生成失败，跳过推荐邮件：%s", exc, exc_info=True)
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
        logger.warning("AI 推荐邮件发送失败，已跳过：%s", exc, exc_info=True)
        return False
