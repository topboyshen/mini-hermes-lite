from __future__ import annotations

from typing import Sequence

from mini_hermes.us_market import UsIndexQuote, render_us_index_block


A_SHARE_TERMS = (
    "A股",
    "A 股",
    "A-share",
    "A share",
    "沪指",
    "沪市",
    "上证",
    "上交所",
    "深证",
    "深市",
    "深交所",
    "深成指",
    "创业板",
    "科创板",
    "北证",
    "北交所",
    "沪深300",
    "中证",
    "沪深",
    "两市",
    "北向资金",
    "陆股通",
    "中国股市",
    "内地股市",
    "内地股票",
    "境内股票",
    "境内股市",
    "中国股票",
    "中国内地市场",
    "China A-share",
    "Chinese A-share",
    "mainland equities",
    "mainland stocks",
    "onshore China",
    "Shanghai Composite",
    "Shenzhen Component",
    "ChiNext",
    "STAR Market",
    "自选股",
    "东方电气",
    "600875",
)
_EXPECTED_US_CODES = frozenset({"int_dji", "int_nasdaq", "int_sp500"})


def infer_report_stance(quotes: Sequence[UsIndexQuote]) -> str:
    """Classify the complete source quote set without model interpretation."""
    if not quotes:
        return "UNAVAILABLE"
    if {quote.code for quote in quotes} != _EXPECTED_US_CODES:
        return "PARTIAL"
    changes = [float(quote.change) for quote in quotes]
    if all(change > 0 for change in changes):
        return "RISK_ON"
    if all(change < 0 for change in changes):
        return "RISK_OFF"
    if all(change == 0 for change in changes):
        return "FLAT"
    return "MIXED"


def render_report_analysis(quotes: Sequence[UsIndexQuote]) -> str:
    """Render fixed prose derived only from source quote directions."""
    stance = infer_report_stance(quotes)
    lines_by_stance = {
        "RISK_ON": (
            "- 美股主要指数隔夜普遍上涨，市场风险偏好偏强。",
            "- 上涨覆盖主要风格指数，市场情绪较为一致。",
            "- 后续关注风险偏好能否延续。",
        ),
        "RISK_OFF": (
            "- 美股主要指数隔夜普遍下跌，市场风险偏好偏弱。",
            "- 下跌覆盖主要风格指数，市场情绪较为谨慎。",
            "- 后续关注波动是否继续扩大。",
        ),
        "MIXED": (
            "- 美股主要指数隔夜表现分化，市场风险偏好方向不一。",
            "- 不同风格指数之间缺少一致趋势，市场情绪偏中性。",
            "- 后续关注主要指数能否形成更明确的共同方向。",
        ),
        "FLAT": (
            "- 美股主要指数隔夜整体持平，市场方向暂不明确。",
            "- 风险偏好变化有限，市场情绪偏中性。",
            "- 后续等待更清晰的方向信号。",
        ),
        "PARTIAL": (
            "- 当前仅取得部分美股主要指数行情，数据覆盖不完整。",
            "- 暂不据此判断整体风险偏好，请以已显示的原始行情为准。",
        ),
        "UNAVAILABLE": (
            "- 美股主要指数行情暂不可用，无法判断隔夜风险偏好。",
            "- 当前不作方向推断，请以恢复后的原始行情为准。",
        ),
    }
    return "\n".join(lines_by_stance[stance])


def compose_global_report(
    report_date: str,
    quotes: Sequence[UsIndexQuote],
) -> str:
    quote_block = render_us_index_block(quotes)
    analysis = render_report_analysis(quotes)
    return (
        f"# 海外市场晨报｜{report_date}\n\n"
        "## 美股隔夜数据\n\n"
        f"{quote_block}\n\n"
        "## 市场解读\n\n"
        f"{analysis}\n\n"
        "_行情数据源：新浪财经；仅供研究参考，不构成投资建议。_"
    )
