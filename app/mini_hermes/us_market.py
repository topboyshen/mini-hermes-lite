"""Fetch and parse overnight US index quotes from Sina Finance."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Sequence

import httpx


LOGGER = logging.getLogger(__name__)

_SINA_URL = "https://hq.sinajs.cn/list=int_dji,int_nasdaq,int_sp500"
_SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 mini-hermes/0.1",
}
_INDICES = {
    "int_dji": "道琼斯工业指数",
    "int_nasdaq": "纳斯达克综合指数",
    "int_sp500": "标普500指数",
}


@dataclass(frozen=True, slots=True)
class UsIndexQuote:
    code: str
    name: str
    price: str
    change: str
    percent: str


def parse_us_quotes(raw: str) -> tuple[UsIndexQuote, ...]:
    """Parse quotes while preserving the source number strings exactly."""
    quotes: list[UsIndexQuote] = []
    for code, name in _INDICES.items():
        match = re.search(rf'var\s+hq_str_{re.escape(code)}="([^"]*)"', raw)
        if match is None or not match.group(1):
            continue
        parts = match.group(1).split(",")
        if len(parts) < 4:
            continue
        price, change, percent = parts[1:4]
        try:
            float(price)
            float(change)
            float(percent)
        except ValueError:
            continue
        quotes.append(
            UsIndexQuote(
                code=code,
                name=name,
                price=price,
                change=change,
                percent=percent,
            )
        )
    return tuple(quotes)


async def fetch_us_quotes() -> tuple[UsIndexQuote, ...]:
    """Return parsed US index quotes; return an empty tuple on fetch failure."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(_SINA_URL, headers=_SINA_HEADERS)
            response.raise_for_status()
            raw = response.content.decode("gbk", errors="replace")
    except Exception as exc:  # noqa: BLE001 - degrade to a clear no-data state
        LOGGER.warning("us_quotes_fetch_failed error=%s", exc)
        return ()

    quotes = parse_us_quotes(raw)
    if not quotes:
        LOGGER.warning("us_quotes_parse_empty")
    return quotes


def _signed(value: str) -> str:
    return value if value.startswith(("+", "-")) or float(value) < 0 else f"+{value}"


def render_us_index_block(quotes: Sequence[UsIndexQuote]) -> str:
    """Render exact source strings without passing them through an LLM."""
    if not quotes:
        return "- 美股主要指数数据暂时不可用。"
    return "\n".join(
        f"- {quote.name}: {quote.price} 点, 涨跌 {_signed(quote.change)}, "
        f"{_signed(quote.percent)}%"
        for quote in quotes
    )
