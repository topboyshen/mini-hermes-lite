"""Fetch and parse overnight US index quotes from Sina Finance."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import logging
import threading
import re
from typing import Sequence
from zoneinfo import ZoneInfo

import httpx


LOGGER = logging.getLogger(__name__)

_SINA_URL = "https://hq.sinajs.cn/list=gb_dji,gb_ixic,gb_inx"
_SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 mini-hermes/0.1",
}
_INDICES = {
    "int_dji": ("gb_dji", "道琼斯工业指数"),
    "int_nasdaq": ("gb_ixic", "纳斯达克综合指数"),
    "int_sp500": ("gb_inx", "标普500指数"),
}
_SINA_TIMEZONE = ZoneInfo("Asia/Shanghai")
_MARKET_TIMEZONE = ZoneInfo("America/New_York")
_AS_OF_FORMAT = "%Y-%m-%d %H:%M:%S"



_feed_lock = threading.Lock()
_consecutive_failures = 0
_last_error: str | None = None


def us_market_feed_status() -> dict[str, object]:
    """Expose consecutive Sina fetch/parse failures for /status."""
    with _feed_lock:
        return {
            "consecutive_failures": _consecutive_failures,
            "last_error": _last_error,
            "ok": _consecutive_failures == 0,
        }


def _reset_feed_status_for_tests() -> None:
    global _consecutive_failures, _last_error
    with _feed_lock:
        _consecutive_failures = 0
        _last_error = None


def _record_feed_success() -> None:
    global _consecutive_failures, _last_error
    with _feed_lock:
        _consecutive_failures = 0
        _last_error = None


def _record_feed_failure(error: str) -> None:
    global _consecutive_failures, _last_error
    with _feed_lock:
        _consecutive_failures += 1
        _last_error = error


@dataclass(frozen=True, slots=True)
class UsIndexQuote:
    code: str
    name: str
    price: str
    change: str
    percent: str
    as_of: str
    observed_at: datetime
    open: str | None = None
    high: str | None = None
    low: str | None = None
    week52_high: str | None = None
    week52_low: str | None = None

    @property
    def trade_date(self) -> date:
        return self.observed_at.date()


def _optional_number(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return text


def _optional_range(
    parts: list[str],
    price: str,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Extract open/high/low/52w fields when Sina supplies the longer record."""
    if len(parts) < 10:
        return None, None, None, None, None
    open_price = _optional_number(parts[5])
    high = _optional_number(parts[6])
    low = _optional_number(parts[7])
    week52_high = _optional_number(parts[8]) if len(parts) > 8 else None
    week52_low = _optional_number(parts[9]) if len(parts) > 9 else None
    try:
        last = Decimal(price)
        if high is not None and Decimal(high) < last:
            high = None
        if low is not None and Decimal(low) > last:
            low = None
        if (
            week52_high is not None
            and week52_low is not None
            and Decimal(week52_high) <= Decimal(week52_low)
        ):
            week52_high = None
            week52_low = None
    except InvalidOperation:
        return open_price, high, low, week52_high, week52_low
    return open_price, high, low, week52_high, week52_low


def _market_timestamp(as_of: str) -> datetime:
    observed_at = datetime.strptime(as_of.strip(), _AS_OF_FORMAT).replace(
        tzinfo=_SINA_TIMEZONE
    )
    return observed_at.astimezone(_MARKET_TIMEZONE)


def parse_us_quotes(raw: str) -> tuple[UsIndexQuote, ...]:
    """Parse quotes while preserving the source number strings exactly."""
    quotes: list[UsIndexQuote] = []
    for code, (source_code, name) in _INDICES.items():
        match = re.search(
            rf'var\s+hq_str_{re.escape(source_code)}="([^"]*)"', raw
        )
        if match is None or not match.group(1):
            continue
        parts = match.group(1).split(",")
        if len(parts) < 5:
            continue
        price, percent, as_of, change = parts[1:5]
        try:
            float(price)
            float(change)
            float(percent)
            observed_at = _market_timestamp(as_of)
        except (ValueError, OverflowError):
            continue
        open_price, high, low, week52_high, week52_low = _optional_range(parts, price)
        quotes.append(
            UsIndexQuote(
                code=code,
                name=name,
                price=price,
                change=change,
                percent=percent,
                as_of=as_of,
                observed_at=observed_at,
                open=open_price,
                high=high,
                low=low,
                week52_high=week52_high,
                week52_low=week52_low,
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
        _record_feed_failure(f"fetch:{exc}")
        return ()

    quotes = parse_us_quotes(raw)
    if not quotes:
        LOGGER.warning("us_quotes_parse_empty")
        _record_feed_failure("parse_empty")
        return ()
    _record_feed_success()
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
