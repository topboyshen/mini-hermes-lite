"""Verified macro-event context from official U.S. sources.

This module deliberately reports publication facts, not causal claims.  A
nearby release may be relevant to market pricing, but its presence alone does
not prove that it caused a move.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
import logging
import re
from xml.etree import ElementTree

import httpx


LOGGER = logging.getLogger(__name__)

_FED_MONETARY_RSS = "https://www.federalreserve.gov/feeds/press_monetary.xml"
_BEA_RELEASE_RSS = "https://apps.bea.gov/rss/rss.xml"
_HEADERS = {
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9",
    "User-Agent": "Mozilla/5.0 (compatible; mini-hermes/0.1; official-market-context)",
}
_LOOKBACK = timedelta(days=5)


@dataclass(frozen=True, slots=True)
class MarketEvent:
    source: str
    title: str
    detail: str
    published_at: datetime
    url: str


@dataclass(frozen=True, slots=True)
class MarketContext:
    events: tuple[MarketEvent, ...] = ()
    available_sources: tuple[str, ...] = ()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node: ElementTree.Element, name: str) -> str:
    for child in node:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _descendant_text(node: ElementTree.Element, path: tuple[str, ...]) -> str:
    current = node
    for name in path:
        match = next(
            (child for child in current if _local_name(child.tag) == name),
            None,
        )
        if match is None:
            return ""
        current = match
    return (current.text or "").strip()


def _publication_time(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _clean_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", unescape(value))
    return " ".join(without_tags.split())


def _fed_event(item: ElementTree.Element, published: datetime) -> MarketEvent | None:
    raw_title = _child_text(item, "title")
    normalized = raw_title.casefold()
    if "federal open market committee" in normalized and "minutes" in normalized:
        title = "美联储发布FOMC会议纪要"
    elif "fomc statement" in normalized:
        title = "美联储发布FOMC声明"
    elif "economic projections" in normalized:
        title = "美联储发布经济预测"
    else:
        return None
    return MarketEvent(
        source="美联储",
        title=title,
        detail="",
        published_at=published,
        url=_child_text(item, "link"),
    )


def _bea_title(name: str, raw_title: str) -> str | None:
    translations = {
        "GDP": "美国GDP更新",
        "Personal Income and Outlays": "美国个人收入与支出",
        "U.S. International Trade in Goods and Services": "美国国际贸易",
        "International Transactions": "美国国际收支",
    }
    base = translations.get(name)
    if base is None:
        return None
    period_match = re.search(
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+\d{4}|(?:1st|2nd|3rd|4th) Quarter \d{4}",
        raw_title,
        flags=re.IGNORECASE,
    )
    period = _translate_period(period_match.group(0)) if period_match else ""
    return f"{base}（{period}）" if period else base


def _translate_period(value: str) -> str:
    text = value.strip()
    quarter_match = re.fullmatch(r"([1-4])(?:st|nd|rd|th) Quarter (\d{4})", text, re.IGNORECASE)
    if quarter_match:
        quarter_names = {"1": "第一季度", "2": "第二季度", "3": "第三季度", "4": "第四季度"}
        return f"{quarter_match.group(2)}年{quarter_names[quarter_match.group(1)]}"
    compact_quarter = re.fullmatch(r"Q([1-4])\s+(\d{4})", text, re.IGNORECASE)
    if compact_quarter:
        quarter_names = {"1": "第一季度", "2": "第二季度", "3": "第三季度", "4": "第四季度"}
        return f"{compact_quarter.group(2)}年{quarter_names[compact_quarter.group(1)]}"
    month_match = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", text)
    if month_match:
        months = {
            "january": 1,
            "february": 2,
            "march": 3,
            "april": 4,
            "may": 5,
            "june": 6,
            "july": 7,
            "august": 8,
            "september": 9,
            "october": 10,
            "november": 11,
            "december": 12,
        }
        month = months.get(month_match.group(1).casefold())
        if month is not None:
            return f"{month_match.group(2)}年{month}月"
    return text


def _bea_value(item: ElementTree.Element, value: str) -> str:
    if (
        _child_text(item, "changeUnit").casefold() == "pct"
        and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value)
    ):
        return f"{value}%"
    return value


def _bea_event(item: ElementTree.Element, published: datetime) -> MarketEvent | None:
    name = item.attrib.get("name", "").strip()
    raw_title = _child_text(item, "title")
    title = _bea_title(name, raw_title)
    if title is None:
        return None
    current_period = _descendant_text(item, ("data", "main", "current", "infoDate"))
    current_value = _descendant_text(item, ("data", "main", "current", "percentChange"))
    previous_period = _descendant_text(item, ("data", "main", "previous", "infoDate"))
    previous_value = _descendant_text(item, ("data", "main", "previous", "percentChange"))
    details: list[str] = []
    if current_value:
        period = _translate_period(current_period) if current_period else "本期"
        metric = "实际GDP年化" if name == "GDP" else "个人收入环比" if name == "Personal Income and Outlays" else "本期值"
        details.append(f"{period}{metric} {_bea_value(item, current_value)}")
    if previous_value:
        period = _translate_period(previous_period) if previous_period else "前期"
        details.append(f"前值 {period} {_bea_value(item, previous_value)}")
    if not details:
        description = _clean_text(_child_text(item, "description"))
        if description:
            details.append(description[:120])
    return MarketEvent(
        source="BEA",
        title=title,
        detail="；".join(details),
        published_at=published,
        url=_child_text(item, "link"),
    )


def parse_official_rss(
    raw: str | bytes,
    *,
    source: str,
    now: datetime,
) -> tuple[MarketEvent, ...]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return ()
    now_utc = now.astimezone(UTC)
    events: list[MarketEvent] = []
    for item in root.iter():
        if _local_name(item.tag) != "item":
            continue
        published = _publication_time(_child_text(item, "pubDate"))
        if published is None:
            continue
        age = now_utc - published
        if age < -timedelta(hours=1) or age > _LOOKBACK:
            continue
        event = (
            _fed_event(item, published)
            if source == "Fed"
            else _bea_event(item, published)
            if source == "BEA"
            else None
        )
        if event is not None:
            events.append(event)
    events.sort(key=lambda item: item.published_at, reverse=True)
    return tuple(events)


async def _fetch_feed(
    client: httpx.AsyncClient,
    source: str,
    url: str,
    now: datetime,
) -> tuple[str, tuple[MarketEvent, ...]]:
    response = await client.get(url, headers=_HEADERS)
    response.raise_for_status()
    return source, parse_official_rss(response.content, source=source, now=now)


async def fetch_market_context(
    client: httpx.AsyncClient,
    *,
    now: datetime | None = None,
) -> MarketContext:
    observed_at = now or datetime.now(UTC)
    results = await asyncio.gather(
        _fetch_feed(client, "Fed", _FED_MONETARY_RSS, observed_at),
        _fetch_feed(client, "BEA", _BEA_RELEASE_RSS, observed_at),
        return_exceptions=True,
    )
    events: list[MarketEvent] = []
    available: list[str] = []
    for source, result in zip(("Fed", "BEA"), results, strict=True):
        if isinstance(result, BaseException):
            LOGGER.warning("official_event_feed_failed source=%s error=%s", source, result)
            continue
        returned_source, source_events = result
        available.append(returned_source)
        events.extend(source_events)
    events.sort(key=lambda item: item.published_at, reverse=True)
    return MarketContext(events=tuple(events[:3]), available_sources=tuple(available))
