from __future__ import annotations

import re


STOCK_CODE = re.compile(r"^[0-9]{6}$")
_STOCK_CODE_IN_TEXT = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_STOCK_ACTION = re.compile(r"(看|分析|研究)")
_STRONG_ASHARE = re.compile(
    r"(A\s*股|沪深|上证|深证|创业板|科创板|北交所|大盘走势|市场概览|今日行情|个股研究)"
)

FULL_REPORT_CALLBACK = re.compile(r"^market_full:([0-9a-f]{32})$")


def classify_plain_text_stock_intent(text: str) -> tuple[str, str | None]:
    """Conservative plain-text stock router for the single Telegram channel.

    Returns:
      ("agent", code) — auto-route to Mini Hermes A-share agent
      ("hint", None) — ambiguous A-share phrase without a code
      ("pi", None) — default coding / general pi path
    """
    stripped = text.strip()
    if not stripped:
        return ("pi", None)
    if STOCK_CODE.fullmatch(stripped):
        return ("agent", stripped)

    code_match = _STOCK_CODE_IN_TEXT.search(stripped)
    if code_match and _STOCK_ACTION.search(stripped):
        return ("agent", code_match.group(1))

    if code_match:
        # Short messages where a 6-digit token is the clear subject (e.g. "600875 怎么样")
        tokens = re.findall(r"\S+", stripped)
        cleaned = [token.strip(".,!?;:，。！？、") for token in tokens]
        code_tokens = [token for token in cleaned if STOCK_CODE.fullmatch(token)]
        if code_tokens and len(cleaned) <= 4:
            return ("agent", code_tokens[0])

    if _STRONG_ASHARE.search(stripped) and code_match is None:
        return ("hint", None)
    return ("pi", None)
