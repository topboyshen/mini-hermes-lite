"""Deterministic, best-effort overseas market data collection.

The parsers in this module intentionally keep market numbers out of model
prompts.  Source values are converted directly from their text form to
``Decimal`` and calculated values use Decimal arithmetic throughout.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import logging
import re
from typing import Any, Awaitable, Mapping, TypeVar
from xml.etree import ElementTree

import httpx

from mini_hermes.market_context import MarketContext, fetch_market_context
from mini_hermes.us_market import UsIndexQuote, fetch_us_quotes


LOGGER = logging.getLogger(__name__)

_SINA_FUTURES_URL = "https://hq.sinajs.cn/list=hf_GC,hf_CL,hf_SI"
_SINA_DOLLAR_URL = "https://hq.sinajs.cn/list=DINIW"
_SINA_REGIONAL_URL = "https://hq.sinajs.cn/list=int_nikkei,int_hangseng,int_ftse"
_CBOE_VIX_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
_TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value={year}"
)
_COINBASE_TICKER_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
_COINBASE_STATS_URL = "https://api.exchange.coinbase.com/products/BTC-USD/stats"

_SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 mini-hermes/0.1",
}
_JSON_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "mini-hermes/0.1",
}

_FUTURES = {
    "hf_GC": ("COMEX黄金", "USD/oz"),
    "hf_CL": ("WTI原油", "USD/barrel"),
    "hf_SI": ("COMEX白银", "USD/oz"),
}
_REGIONAL_INDICES = {
    "int_nikkei": "日经225",
    "int_hangseng": "恒生指数",
    "int_ftse": "富时100",
}


@dataclass(frozen=True, slots=True)
class CrossAssetQuote:
    """One non-equity market quote, with all numeric fields as Decimals."""

    code: str
    name: str
    price: Decimal
    change: Decimal | None
    percent: Decimal | None
    high: Decimal | None
    low: Decimal | None
    as_of: str
    unit: str
    source: str
    open: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RegionalIndexQuote:
    """One overseas equity index quote from Sina's compact int_* feed."""

    code: str
    name: str
    price: Decimal
    change: Decimal
    percent: Decimal
    source: str = "Sina Finance"


@dataclass(frozen=True, slots=True)
class TreasuryCurve:
    """Latest two Treasury curve observations and deterministic deltas."""

    date: str
    previous_date: str
    two_year: Decimal
    ten_year: Decimal
    two_year_change_bp: Decimal
    ten_year_change_bp: Decimal
    spread_bp: Decimal
    three_month: Decimal | None = None
    thirty_year: Decimal | None = None
    three_month_change_bp: Decimal | None = None
    thirty_year_change_bp: Decimal | None = None
    source: str = "U.S. Department of the Treasury"


@dataclass(frozen=True, slots=True)
class OverseasMarketSnapshot:
    """Best-effort aggregate; missing sources are represented by omission/None."""

    us_indices: tuple[UsIndexQuote, ...]
    assets: tuple[CrossAssetQuote, ...]
    treasury: TreasuryCurve | None
    regional_indices: tuple[RegionalIndexQuote, ...] = ()
    market_context: MarketContext | None = None


def _decimal(value: Any) -> Decimal:
    """Convert a source value without a float round-trip."""
    if value is None or isinstance(value, bool):
        raise ValueError("numeric value is missing")
    text = str(value).strip().replace("%", "")
    if not text:
        raise ValueError("numeric value is empty")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"invalid numeric value: {text!r}") from exc
    if not number.is_finite():
        raise ValueError(f"non-finite numeric value: {text!r}")
    return number


def _change(current: Decimal, previous: Decimal) -> tuple[Decimal, Decimal | None]:
    change = current - previous
    percent = None if previous == 0 else change / previous * Decimal("100")
    return change, percent


def _sina_record(raw: str, code: str) -> list[str] | None:
    match = re.search(
        rf'var\s+hq_str_{re.escape(code)}\s*=\s*"([^"]*)"',
        raw,
    )
    if match is None or not match.group(1).strip():
        return None
    return [part.strip() for part in match.group(1).split(",")]


def _as_of(date: str, time: str = "") -> str:
    return " ".join(part for part in (date.strip(), time.strip()) if part)


def parse_sina_hf_quotes(raw: str) -> tuple[CrossAssetQuote, ...]:
    """Parse Sina hf_GC, hf_CL and hf_SI records using fixed positions."""
    quotes: list[CrossAssetQuote] = []
    for code, (fallback_name, unit) in _FUTURES.items():
        parts = _sina_record(raw, code)
        if parts is None or len(parts) <= 13:
            continue
        try:
            price = _decimal(parts[0])
            high = _decimal(parts[4])
            low = _decimal(parts[5])
            previous = _decimal(parts[7])
            opening = _decimal(parts[8])
        except ValueError:
            continue
        change, percent = _change(price, previous)
        quotes.append(
            CrossAssetQuote(
                code=code,
                name=parts[13] or fallback_name,
                price=price,
                change=change,
                percent=percent,
                open=opening,
                high=high,
                low=low,
                as_of=_as_of(parts[12], parts[6]),
                unit=unit,
                source="Sina Finance",
            )
        )
    return tuple(quotes)


def parse_sina_regional_quotes(raw: str) -> tuple[RegionalIndexQuote, ...]:
    """Parse compact Sina ``int_*`` records: name,price,change,percent."""
    quotes: list[RegionalIndexQuote] = []
    for code, fallback_name in _REGIONAL_INDICES.items():
        parts = _sina_record(raw, code)
        if parts is None or len(parts) < 4:
            continue
        try:
            price = _decimal(parts[1])
            change = _decimal(parts[2])
            percent = _decimal(parts[3])
        except ValueError:
            continue
        if price <= 0:
            continue
        if change != 0 and percent != 0 and (change > 0) != (percent > 0):
            continue
        quotes.append(
            RegionalIndexQuote(
                code=code,
                name=parts[0] or fallback_name,
                price=price,
                change=change,
                percent=percent,
            )
        )
    return tuple(quotes)


def parse_sina_dollar_quote(raw: str) -> CrossAssetQuote | None:
    """Parse Sina's ``DINIW`` dollar-index record."""
    parts = _sina_record(raw, "DINIW")
    if parts is None or len(parts) <= 10:
        return None
    try:
        price = _decimal(parts[1])
        previous = _decimal(parts[3])
        opening = _decimal(parts[5])
        high = _decimal(parts[6])
        low = _decimal(parts[7])
    except ValueError:
        return None
    change, percent = _change(price, previous)
    return CrossAssetQuote(
        code="DINIW",
        name=parts[9] or "美元指数",
        price=price,
        change=change,
        percent=percent,
        open=opening,
        high=high,
        low=low,
        as_of=_as_of(parts[10], parts[0]),
        unit="index",
        source="Sina Finance",
    )


def _payload_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, Mapping) else payload


def parse_cboe_vix(payload: Mapping[str, Any]) -> CrossAssetQuote | None:
    """Parse the Cboe delayed VIX JSON payload."""
    data = _payload_data(payload)
    try:
        price = _decimal(data.get("current_price"))
        change = _decimal(data.get("price_change"))
        percent = _decimal(data.get("price_change_percent"))
        high = _decimal(data.get("high"))
        low = _decimal(data.get("low"))
    except ValueError:
        return None
    return CrossAssetQuote(
        code="VIX",
        name=str(data.get("name") or "Cboe波动率指数"),
        price=price,
        change=change,
        percent=percent,
        open=None,
        high=high,
        low=low,
        as_of=str(data.get("last_trade_time") or "").strip(),
        unit="index",
        source="Cboe",
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _optional_decimal(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        return _decimal(value)
    except ValueError:
        return None


def _treasury_rows(raw: str | bytes) -> list[dict[str, Decimal | str]]:
    root = ElementTree.fromstring(raw)
    rows: list[dict[str, Decimal | str]] = []
    for node in root.iter():
        if _local_name(node.tag) != "properties":
            continue
        values = {
            _local_name(child.tag): (child.text or "").strip()
            for child in node
        }
        date = values.get("NEW_DATE", "").split("T", 1)[0]
        try:
            two_year = _decimal(values.get("BC_2YEAR"))
            ten_year = _decimal(values.get("BC_10YEAR"))
        except ValueError:
            continue
        if not date:
            continue
        rows.append(
            {
                "date": date,
                "two_year": two_year,
                "ten_year": ten_year,
                "three_month": _optional_decimal(values.get("BC_3MONTH")),
                "thirty_year": _optional_decimal(values.get("BC_30YEAR")),
            }
        )
    return rows


def _tenor_change_bp(
    current: Decimal | None,
    previous: Decimal | None,
    basis_points: Decimal,
) -> Decimal | None:
    if current is None or previous is None:
        return None
    return (current - previous) * basis_points


def parse_treasury_curve(raw: str | bytes) -> TreasuryCurve | None:
    """Parse the latest two complete 2Y/10Y observations from Treasury XML."""
    try:
        rows = _treasury_rows(raw)
    except ElementTree.ParseError:
        return None
    if len(rows) < 2:
        return None
    rows.sort(key=lambda item: str(item["date"]))
    previous = rows[-2]
    latest = rows[-1]
    basis_points = Decimal("100")
    two_year = Decimal(str(latest["two_year"]))
    ten_year = Decimal(str(latest["ten_year"]))
    previous_two = Decimal(str(previous["two_year"]))
    previous_ten = Decimal(str(previous["ten_year"]))
    three_month = latest["three_month"] if isinstance(latest["three_month"], Decimal) else None
    thirty_year = latest["thirty_year"] if isinstance(latest["thirty_year"], Decimal) else None
    previous_three = (
        previous["three_month"] if isinstance(previous["three_month"], Decimal) else None
    )
    previous_thirty = (
        previous["thirty_year"] if isinstance(previous["thirty_year"], Decimal) else None
    )
    return TreasuryCurve(
        date=str(latest["date"]),
        previous_date=str(previous["date"]),
        two_year=two_year,
        ten_year=ten_year,
        two_year_change_bp=(two_year - previous_two) * basis_points,
        ten_year_change_bp=(ten_year - previous_ten) * basis_points,
        spread_bp=(ten_year - two_year) * basis_points,
        three_month=three_month,
        thirty_year=thirty_year,
        three_month_change_bp=_tenor_change_bp(three_month, previous_three, basis_points),
        thirty_year_change_bp=_tenor_change_bp(thirty_year, previous_thirty, basis_points),
    )


def parse_coinbase_btc(
    ticker: Mapping[str, Any],
    stats: Mapping[str, Any],
) -> CrossAssetQuote | None:
    """Combine Coinbase ticker and 24-hour stats for BTC-USD."""
    try:
        price = _decimal(ticker.get("price"))
        opening = _decimal(stats.get("open"))
        high = _decimal(stats.get("high"))
        low = _decimal(stats.get("low"))
    except ValueError:
        return None
    change, percent = _change(price, opening)
    return CrossAssetQuote(
        code="BTC-USD",
        name="比特币",
        price=price,
        change=change,
        percent=percent,
        open=opening,
        high=high,
        low=low,
        as_of=str(ticker.get("time") or "").strip(),
        unit="USD",
        source="Coinbase",
    )


async def _fetch_sina_futures(client: httpx.AsyncClient) -> tuple[CrossAssetQuote, ...]:
    response = await client.get(_SINA_FUTURES_URL, headers=_SINA_HEADERS)
    response.raise_for_status()
    quotes = parse_sina_hf_quotes(response.content.decode("gbk", errors="replace"))
    if not quotes:
        raise ValueError("no valid futures records")
    returned = {quote.code for quote in quotes}
    for code in _FUTURES.keys() - returned:
        LOGGER.warning("overseas_asset_parse_missing source=sina code=%s", code)
    return quotes


async def _fetch_sina_dollar(client: httpx.AsyncClient) -> CrossAssetQuote:
    response = await client.get(_SINA_DOLLAR_URL, headers=_SINA_HEADERS)
    response.raise_for_status()
    quote = parse_sina_dollar_quote(response.content.decode("gbk", errors="replace"))
    if quote is None:
        raise ValueError("no valid dollar-index record")
    return quote


async def _fetch_sina_regional(client: httpx.AsyncClient) -> tuple[RegionalIndexQuote, ...]:
    response = await client.get(_SINA_REGIONAL_URL, headers=_SINA_HEADERS)
    response.raise_for_status()
    quotes = parse_sina_regional_quotes(response.content.decode("gbk", errors="replace"))
    if not quotes:
        raise ValueError("no valid regional index records")
    returned = {quote.code for quote in quotes}
    for code in _REGIONAL_INDICES.keys() - returned:
        LOGGER.warning("overseas_regional_parse_missing source=sina code=%s", code)
    return quotes


async def _fetch_cboe_vix(client: httpx.AsyncClient) -> CrossAssetQuote:
    response = await client.get(_CBOE_VIX_URL, headers=_JSON_HEADERS)
    response.raise_for_status()
    quote = parse_cboe_vix(response.json())
    if quote is None:
        raise ValueError("no valid VIX record")
    return quote


async def _fetch_treasury(client: httpx.AsyncClient, year: int) -> TreasuryCurve:
    response = await client.get(_TREASURY_URL.format(year=year), headers=_JSON_HEADERS)
    response.raise_for_status()
    curve = parse_treasury_curve(response.content)
    if curve is None:
        raise ValueError("fewer than two complete Treasury observations")
    return curve


async def _fetch_coinbase_btc(client: httpx.AsyncClient) -> CrossAssetQuote:
    ticker_response, stats_response = await asyncio.gather(
        client.get(_COINBASE_TICKER_URL, headers=_JSON_HEADERS),
        client.get(_COINBASE_STATS_URL, headers=_JSON_HEADERS),
    )
    ticker_response.raise_for_status()
    stats_response.raise_for_status()
    quote = parse_coinbase_btc(ticker_response.json(), stats_response.json())
    if quote is None:
        raise ValueError("no valid BTC-USD record")
    return quote


_T = TypeVar("_T")


async def _safe_fetch(
    source: str,
    operation: Awaitable[_T],
    fallback: _T,
) -> _T:
    try:
        return await operation
    except Exception as exc:  # noqa: BLE001 - one failed source must not fail the report
        LOGGER.warning("overseas_source_fetch_failed source=%s error=%s", source, exc)
        return fallback


async def fetch_overseas_snapshot(
    *,
    year: int | None = None,
    now: datetime | None = None,
) -> OverseasMarketSnapshot:
    """Fetch all sources concurrently, degrading each source independently."""
    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    treasury_year = year if year is not None else observed_at.year
    us_task = asyncio.create_task(_safe_fetch("sina_us", fetch_us_quotes(), ()))

    async with httpx.AsyncClient(timeout=10.0) as client:
        futures, dollar, vix, treasury, btc, regional, market_context = await asyncio.gather(
            _safe_fetch("sina_futures", _fetch_sina_futures(client), ()),
            _safe_fetch("sina_dollar", _fetch_sina_dollar(client), None),
            _safe_fetch("cboe_vix", _fetch_cboe_vix(client), None),
            _safe_fetch("us_treasury", _fetch_treasury(client, treasury_year), None),
            _safe_fetch("coinbase_btc", _fetch_coinbase_btc(client), None),
            _safe_fetch("sina_regional", _fetch_sina_regional(client), ()),
            _safe_fetch(
                "official_market_context",
                fetch_market_context(client, now=observed_at),
                MarketContext(),
            ),
        )
    us_indices = await us_task

    assets = list(futures)
    assets.extend(quote for quote in (dollar, vix, btc) if quote is not None)
    return OverseasMarketSnapshot(
        us_indices=tuple(us_indices),
        assets=tuple(assets),
        treasury=treasury,
        regional_indices=tuple(regional),
        market_context=market_context,
    )
