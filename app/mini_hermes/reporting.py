from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from itertools import groupby
import re
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from mini_hermes.overseas_market import (
    CrossAssetQuote,
    OverseasMarketSnapshot,
    RegionalIndexQuote,
)
from mini_hermes.us_market import UsIndexQuote
from mini_hermes.vix_evidence import build_report_evidence


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


@dataclass(frozen=True)
class GlobalReportBundle:
    report_date: str
    brief: str
    full: str
    vix_evidence: Mapping[str, object] | None = None


_EXPECTED_US_CODES = frozenset({"int_dji", "int_nasdaq", "int_sp500"})
_DISPLAY_ORDER = {"int_dji": 0, "int_sp500": 1, "int_nasdaq": 2}
_SHORT_NAMES = {
    "int_dji": "道指",
    "int_nasdaq": "纳指",
    "int_sp500": "标普500",
}
_TWO_PLACES = Decimal("0.01")
_EXPECTED_ASSET_CODES = frozenset({"VIX", "DINIW", "hf_GC", "hf_CL", "hf_SI", "BTC-USD"})
_ASSET_LIMITS = {
    "VIX": (Decimal("5"), Decimal("100"), Decimal("50")),
    "DINIW": (Decimal("50"), Decimal("200"), Decimal("10")),
    "hf_GC": (Decimal("100"), Decimal("10000"), Decimal("20")),
    "hf_CL": (Decimal("1"), Decimal("500"), Decimal("30")),
    "hf_SI": (Decimal("1"), Decimal("500"), Decimal("30")),
    "BTC-USD": (Decimal("1000"), Decimal("10000000"), Decimal("50")),
}
_ASSET_MAX_AGE = {
    "VIX": timedelta(hours=24),
    "DINIW": timedelta(hours=18),
    "hf_GC": timedelta(hours=18),
    "hf_CL": timedelta(hours=18),
    "hf_SI": timedelta(hours=18),
    "BTC-USD": timedelta(hours=2),
}
_EXPECTED_REGIONAL_CODES = ("int_nikkei", "int_hangseng", "int_ftse")
_REGIONAL_SHORT_NAMES = {
    "int_nikkei": "日经",
    "int_hangseng": "恒生",
    "int_ftse": "富时100",
}
_DIVIDER = "────────────"
_OFFICIAL_SOURCE_LABELS = {
    "Fed": "美联储（Fed）",
    "BEA": "美国经济分析局（BEA）",
}


@dataclass(frozen=True, slots=True)
class IndexStats:
    quotes: tuple[UsIndexQuote, ...]
    complete: bool
    anomalies: tuple[str, ...]
    expected_trade_date: date | None
    trade_date: date | None
    up: int
    down: int
    flat: int
    average_percent: Decimal | None
    spread_percent: Decimal | None
    synchronization: str
    temperature: str


def _decimal(value: str) -> Decimal:
    number = Decimal(value)
    if not number.is_finite():
        raise InvalidOperation
    return number


def _direction(value: Decimal) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        first_next_month = date(year + 1, 1, 1)
    else:
        first_next_month = date(year, month + 1, 1)
    candidate = first_next_month - timedelta(days=1)
    return candidate - timedelta(days=(candidate.weekday() - weekday) % 7)


def _observed_fixed_holiday(value: date) -> date:
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def _easter_sunday(year: int) -> date:
    """Return Gregorian Easter Sunday using the Meeus/Jones/Butcher algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _us_market_holidays(year: int) -> frozenset[date]:
    holidays = {
        _observed_fixed_holiday(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed_fixed_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed_fixed_holiday(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed_fixed_holiday(date(year, 6, 19)))
    return frozenset(holidays)


def expected_us_trade_date(
    report_date: str | date,
    *,
    generated_at: datetime | None = None,
) -> date:
    """Return the last expected regular US market session before a report date.

    This intentionally errs toward a degraded report on extraordinary closures:
    an unexpected session-date mismatch must never be presented as fresh data.
    """
    generated = date.fromisoformat(report_date) if isinstance(report_date, str) else report_date
    if generated_at is None:
        candidate = generated - timedelta(days=1)
    else:
        if generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        new_york_now = generated_at.astimezone(ZoneInfo("America/New_York"))
        # Sina's final index records can settle well after the 16:00 close.
        # Before 18:00 ET, fail closed to the prior completed session.
        candidate = new_york_now.date()
        if new_york_now.timetz().replace(tzinfo=None) < time(18, 0):
            candidate -= timedelta(days=1)
    while candidate.weekday() >= 5 or candidate in _us_market_holidays(candidate.year):
        candidate -= timedelta(days=1)
    return candidate


def analyze_indices(
    quotes: Sequence[UsIndexQuote],
    *,
    expected_trade_date: date | None = None,
    generated_at: datetime | None = None,
) -> IndexStats:
    valid: list[UsIndexQuote] = []
    anomalies: list[str] = []
    seen: set[str] = set()
    generated_market_time: datetime | None = None
    if generated_at is not None:
        if generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        generated_market_time = generated_at.astimezone(ZoneInfo("America/New_York"))
    for quote in quotes:
        if quote.code not in _EXPECTED_US_CODES:
            anomalies.append(f"未知指数代码 {quote.code}")
            continue
        if quote.code in seen:
            anomalies.append(f"指数 {quote.code} 重复")
            continue
        seen.add(quote.code)
        if quote.observed_at.tzinfo is None or quote.observed_at.utcoffset() is None:
            anomalies.append(f"指数 {quote.code} 时间戳无时区")
            continue
        observed_market_time = quote.observed_at.astimezone(
            ZoneInfo("America/New_York")
        )
        if observed_market_time.timetz().replace(tzinfo=None) < time(16, 0):
            anomalies.append(f"指数 {quote.code} 尚未到常规收盘")
            continue
        if (
            generated_market_time is not None
            and observed_market_time > generated_market_time
        ):
            anomalies.append(f"指数 {quote.code} 时间戳晚于报告生成时间")
            continue
        if expected_trade_date is not None and quote.trade_date != expected_trade_date:
            anomalies.append(
                f"指数 {quote.code} 交易日 {quote.trade_date.isoformat()}，"
                f"预期 {expected_trade_date.isoformat()}"
            )
            continue
        try:
            price = _decimal(quote.price)
            change = _decimal(quote.change)
            percent = _decimal(quote.percent)
            previous_close = price - change
            implied_percent = change / previous_close * Decimal("100")
        except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
            anomalies.append(f"指数 {quote.code} 数值无效")
            continue
        if price <= 0 or previous_close <= 0:
            anomalies.append(f"指数 {quote.code} 价格无效")
            continue
        # A source may round a very small percentage to 0.00 while still
        # reporting a non-zero point change.  Only reject genuinely opposite
        # signs; rounded zero is not a contradiction.
        if change != 0 and percent != 0 and _direction(change) != _direction(percent):
            anomalies.append(f"指数 {quote.code} 涨跌方向冲突")
            continue
        if abs(implied_percent - percent) > Decimal("0.05"):
            anomalies.append(f"指数 {quote.code} 涨跌幅校验失败")
            continue
        valid.append(quote)

    ordered = tuple(sorted(valid, key=lambda quote: _DISPLAY_ORDER[quote.code]))
    percents = [_decimal(quote.percent) for quote in ordered]
    changes = [_decimal(quote.change) for quote in ordered]
    trade_dates = {quote.trade_date for quote in ordered}
    trade_date = next(iter(trade_dates)) if len(trade_dates) == 1 else None
    if len(trade_dates) > 1:
        anomalies.append("指数交易日不一致")
    # Direction comes from the unrounded point change.  Percentage strings may
    # legitimately round a very small move to 0.00.
    up = sum(value > 0 for value in changes)
    down = sum(value < 0 for value in changes)
    flat = sum(value == 0 for value in changes)
    average = sum(percents, Decimal("0")) / len(percents) if percents else None
    spread = max(percents) - min(percents) if percents else None
    complete = (
        len(ordered) == 3
        and {quote.code for quote in ordered} == _EXPECTED_US_CODES
        and trade_date is not None
        and (expected_trade_date is None or trade_date == expected_trade_date)
    )

    if not complete or spread is None:
        synchronization = "不判定"
        temperature = "数据不完整"
    elif sum(count > 0 for count in (up, down, flat)) > 1:
        synchronization = "分化"
        temperature = _temperature(average)
    else:
        synchronization = "高" if spread <= Decimal("0.25") else "中" if spread <= Decimal("0.75") else "低"
        temperature = _temperature(average)

    return IndexStats(
        quotes=ordered,
        complete=complete,
        anomalies=tuple(anomalies),
        expected_trade_date=expected_trade_date,
        trade_date=trade_date,
        up=up,
        down=down,
        flat=flat,
        average_percent=average,
        spread_percent=spread,
        synchronization=synchronization,
        temperature=temperature,
    )


def _temperature(average: Decimal | None) -> str:
    if average is None:
        return "数据不完整"
    if average >= Decimal("1"):
        return "明显偏强"
    if average >= Decimal("0.25"):
        return "温和偏强"
    if average <= Decimal("-1"):
        return "明显偏弱"
    if average <= Decimal("-0.25"):
        return "温和偏弱"
    return "中性"


def _format_decimal(value: Decimal, *, signed: bool = False) -> str:
    rounded = value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    rendered = f"{rounded:f}"
    return f"+{rendered}" if signed and value > 0 else rendered


def _previous_close(quote: UsIndexQuote) -> Decimal:
    return _decimal(quote.price) - _decimal(quote.change)


def _group_source_number(value: str, *, signed: bool = False) -> str:
    """Add thousands separators without changing source precision or digits."""
    number = _decimal(value)
    text = value.strip()
    if "e" in text.casefold():
        rendered = text
    else:
        sign = text[:1] if text.startswith(("+", "-")) else ""
        unsigned = text[1:] if sign else text
        integer, dot, fraction = unsigned.partition(".")
        chunks: list[str] = []
        while integer:
            chunks.append(integer[-3:])
            integer = integer[:-3]
        grouped = ",".join(reversed(chunks)) or "0"
        rendered = f"{sign}{grouped}{dot}{fraction}"
    if signed and number > 0 and not rendered.startswith("+"):
        return f"+{rendered}"
    return rendered


def _rank_indices(stats: IndexStats) -> str:
    ranked = sorted(stats.quotes, key=lambda quote: _decimal(quote.percent), reverse=True)
    groups: list[str] = []
    for _, items in groupby(ranked, key=lambda quote: _decimal(quote.percent)):
        groups.append(" = ".join(_SHORT_NAMES[item.code] for item in items))
    return " > ".join(groups)


def _week52_position(quote: UsIndexQuote) -> str | None:
    if quote.week52_high is None or quote.week52_low is None:
        return None
    try:
        high = _decimal(quote.week52_high)
        low = _decimal(quote.week52_low)
        price = _decimal(quote.price)
    except (InvalidOperation, TypeError, ValueError):
        return None
    span = high - low
    if span <= 0:
        return None
    position = (price - low) / span * Decimal("100")
    return f"52周 {_format_decimal(position)}%"


def _session_range(quote: UsIndexQuote) -> str | None:
    if quote.high is None or quote.low is None:
        return None
    try:
        high = _decimal(quote.high)
        low = _decimal(quote.low)
        if high <= 0 or low <= 0 or high < low:
            return None
    except (InvalidOperation, TypeError, ValueError):
        return None
    parts = [
        f"高 {_group_source_number(quote.high)}",
        f"低 {_group_source_number(quote.low)}",
    ]
    if quote.open is not None:
        parts.append(f"开 {_group_source_number(quote.open)}")
    return " · ".join(parts)


def render_index_snapshot(stats: IndexStats) -> str:
    if not stats.quotes:
        return "暂时不可用"
    lines: list[str] = []
    for quote in stats.quotes:
        line = (
            f"{_SHORT_NAMES[quote.code]}｜"
            f"{_group_source_number(quote.price)}｜"
            f"{_group_source_number(quote.change, signed=True)}｜"
            f"{_group_source_number(quote.percent, signed=True)}%"
            f"（前收 {_format_market_number(_previous_close(quote), grouped=True)}）"
        )
        extras = [item for item in (_session_range(quote), _week52_position(quote)) if item]
        if extras:
            line += f"\n　{' · '.join(extras)}"
        lines.append(line)
    return "\n".join(lines)


def render_index_temperature(stats: IndexStats) -> str:
    status = (
        "3/3 · 交易日校验通过"
        if stats.complete
        else f"{len(stats.quotes)}/3 · 数据不完整或已过期"
    )
    lines = [f"数据｜{status}"]
    if stats.trade_date is not None:
        lines.append(f"交易日｜{stats.trade_date.isoformat()}（美东）")
    elif stats.expected_trade_date is not None:
        lines.append(f"期望交易日｜{stats.expected_trade_date.isoformat()}（美东）")
    if stats.anomalies:
        lines.append(f"校验｜{'；'.join(stats.anomalies)}")
    if not stats.complete:
        if stats.quotes:
            lines.append(f"已验收｜{len(stats.quotes)}/3，仅展示明细，不作整体判断")
        return "\n".join(lines)
    lines.append(f"涨跌｜{stats.up} 涨 · {stats.down} 跌 · {stats.flat} 平")
    if stats.average_percent is not None:
        average_label = (
            "等权"
            if stats.complete
            else f"已取得均值（{len(stats.quotes)}/3）"
        )
        lines.append(
            f"{average_label}｜{_format_decimal(stats.average_percent, signed=True)}%"
            "（仅作概览）"
        )
    if stats.complete and stats.spread_percent is not None:
        lines.append(f"排序｜{_rank_indices(stats)}")
        lines.append(
            f"分化｜{_format_decimal(stats.spread_percent)}pp · "
            f"同步：{stats.synchronization} · 温度：{stats.temperature}"
        )
    return "\n".join(lines)


def render_index_observations(stats: IndexStats) -> str:
    if not stats.complete or stats.spread_percent is None:
        return "• 指数数据不完整，暂不作整体结构判断。"
    if stats.up == 3:
        direction = "三项指数同向上涨，指数层面方向一致。"
    elif stats.down == 3:
        direction = "三项指数同向下跌，指数层面方向一致。"
    elif stats.flat == 3:
        direction = "三项指数整体持平，方向暂不明确。"
    else:
        direction = f"指数表现分化：{stats.up} 项上涨、{stats.down} 项下跌、{stats.flat} 项持平。"

    ranked = sorted(stats.quotes, key=lambda quote: _decimal(quote.percent), reverse=True)
    strongest = ranked[0]
    weakest = ranked[-1]
    lines: list[str] = []
    if stats.spread_percent == 0:
        lines.append(f"• {direction}三项指数涨跌幅相同。")
    else:
        lines.append(
            f"• {direction.rstrip('。')}；"
            f"{_SHORT_NAMES[strongest.code]}较{_SHORT_NAMES[weakest.code]}领先"
            f" {_format_decimal(stats.spread_percent)}pp。"
        )
    lines.append(f"• 指数温度：{stats.temperature}（仅描述涨跌幅）。")
    return "\n".join(lines)


def infer_report_stance(quotes: Sequence[UsIndexQuote]) -> str:
    """Retain the compact legacy classification for API compatibility."""
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


def _asset_map(snapshot: OverseasMarketSnapshot) -> dict[str, CrossAssetQuote]:
    """Return one valid quote per supported code in display-independent form."""
    result: dict[str, CrossAssetQuote] = {}
    for quote in snapshot.assets:
        if quote.code not in _EXPECTED_ASSET_CODES or quote.code in result:
            continue
        if not quote.price.is_finite() or quote.price <= 0:
            continue
        numeric_values = (quote.change, quote.percent, quote.high, quote.low, quote.open)
        if any(value is not None and not value.is_finite() for value in numeric_values):
            continue
        result[quote.code] = quote
    return result


def _asset_observed_at(quote: CrossAssetQuote) -> datetime | None:
    text = quote.as_of.strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            return datetime.fromisoformat(text.removesuffix("Z") + "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed
    source_zone = (
        ZoneInfo("America/New_York")
        if quote.code == "VIX"
        else ZoneInfo("Asia/Shanghai")
    )
    return parsed.replace(tzinfo=source_zone)


def _asset_quality_issue(
    quote: CrossAssetQuote,
    *,
    generated_at: datetime,
) -> str | None:
    limits = _ASSET_LIMITS.get(quote.code)
    if limits is None:
        return "代码不受支持"
    minimum, maximum, max_percent = limits
    if quote.price < minimum or quote.price > maximum:
        return "价格超出合理范围"
    if quote.percent is not None and abs(quote.percent) > max_percent:
        return "涨跌幅超出合理范围"
    if quote.change is not None and quote.percent is not None:
        if (
            quote.change != 0
            and quote.percent != 0
            and _direction(quote.change) != _direction(quote.percent)
        ):
            return "涨跌方向冲突"
        previous = quote.price - quote.change
        if previous > 0:
            implied = quote.change / previous * Decimal("100")
            if abs(implied - quote.percent) > Decimal("0.15"):
                return "涨跌幅与价格变动不一致"
    if quote.high is not None and quote.low is not None:
        if quote.high < quote.low or quote.low <= 0:
            return "日内高低值无效"
        tolerance = max((quote.high - quote.low) * Decimal("0.05"), quote.price * Decimal("0.002"))
        if quote.price < quote.low - tolerance or quote.price > quote.high + tolerance:
            return "当前价明显超出日内高低区间"
    observed_at = _asset_observed_at(quote)
    if observed_at is None:
        return "时间戳无法解析"
    age = generated_at.astimezone(UTC) - observed_at.astimezone(UTC)
    if age < -timedelta(hours=1):
        return "时间戳晚于报告生成时间"
    if age > _ASSET_MAX_AGE[quote.code]:
        return "数据已过期"
    return None


def sanitize_snapshot(
    snapshot: OverseasMarketSnapshot,
    *,
    generated_at: datetime,
) -> tuple[OverseasMarketSnapshot, tuple[str, ...]]:
    """Fail closed on stale or implausible cross-asset observations."""
    if generated_at.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    valid_assets: list[CrossAssetQuote] = []
    issues: list[str] = []
    seen: set[str] = set()
    for quote in snapshot.assets:
        if quote.code in seen:
            issues.append(f"{quote.name} 重复，已剔除")
            continue
        seen.add(quote.code)
        issue = _asset_quality_issue(quote, generated_at=generated_at)
        if issue is not None:
            issues.append(f"{quote.name} {issue}，已剔除")
            continue
        valid_assets.append(quote)

    curve = snapshot.treasury
    if curve is not None:
        try:
            curve_date = date.fromisoformat(curve.date)
            market_date = generated_at.astimezone(ZoneInfo("America/New_York")).date()
            curve_age = market_date - curve_date
        except ValueError:
            curve = None
            issues.append("美债日期无法解析，已剔除")
        else:
            if curve_age < timedelta(0):
                curve = None
                issues.append("美债日期晚于报告生成时间，已剔除")
            elif curve_age > timedelta(days=4):
                curve = None
                issues.append("美债数据已过期，已剔除")

    return (
        OverseasMarketSnapshot(
            us_indices=snapshot.us_indices,
            assets=tuple(valid_assets),
            treasury=curve,
            regional_indices=snapshot.regional_indices,
            market_context=snapshot.market_context,
            vix_observation_json=snapshot.vix_observation_json,
        ),
        tuple(issues),
    )


def _format_market_number(
    value: Decimal,
    *,
    signed: bool = False,
    grouped: bool = False,
) -> str:
    rounded = value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    rendered = f"{rounded:,.2f}" if grouped else f"{rounded:.2f}"
    return f"+{rendered}" if signed and value > 0 else rendered


def _format_bp(value: Decimal, *, signed: bool = False) -> str:
    rounded = value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    rendered = f"{rounded:f}".rstrip("0").rstrip(".")
    return f"+{rendered}" if signed and value > 0 else rendered


def _asset_percent(quote: CrossAssetQuote, *, prefix: str = "") -> str:
    if quote.percent is None:
        return f"{prefix}涨跌幅不可用"
    return f"{prefix}{_format_market_number(quote.percent, signed=True)}%"


def _intraday_position(quote: CrossAssetQuote) -> str | None:
    if quote.high is None or quote.low is None:
        return None
    span = quote.high - quote.low
    if span <= 0:
        return None
    position = (quote.price - quote.low) / span * Decimal("100")
    return f"日内 {_format_decimal(position)}%"


def _compact_as_of(value: str) -> str:
    text = value.strip()
    if not text:
        return "时间未知"
    is_utc = text.endswith("Z")
    text = text.removesuffix("Z").replace("T", " ")
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        text = text[5:]
    if " " in text:
        date_part, time_part = text.split(" ", 1)
        time_part = time_part.split(".", 1)[0]
        time_part = ":".join(time_part.split(":")[:2])
        text = f"{date_part} {time_part}".rstrip()
    return f"{text} UTC" if is_utc else text


def _regional_map(snapshot: OverseasMarketSnapshot) -> dict[str, RegionalIndexQuote]:
    result: dict[str, RegionalIndexQuote] = {}
    for quote in snapshot.regional_indices:
        if quote.code not in _REGIONAL_SHORT_NAMES or quote.code in result:
            continue
        if not quote.price.is_finite() or quote.price <= 0:
            continue
        if not quote.change.is_finite() or not quote.percent.is_finite():
            continue
        result[quote.code] = quote
    return result


def render_regional_snapshot(snapshot: OverseasMarketSnapshot) -> str:
    quotes = _regional_map(snapshot)
    if not quotes:
        return "暂时不可用"
    lines: list[str] = []
    for code in _EXPECTED_REGIONAL_CODES:
        quote = quotes.get(code)
        if quote is None:
            continue
        lines.append(
            f"{_REGIONAL_SHORT_NAMES[code]}｜"
            f"{_format_market_number(quote.price, grouped=True)}｜"
            f"{_format_market_number(quote.change, signed=True)}｜"
            f"{_format_market_number(quote.percent, signed=True)}%"
        )
    return "\n".join(lines) if lines else "暂时不可用"


def render_cross_asset_snapshot(snapshot: OverseasMarketSnapshot) -> str:
    assets = _asset_map(snapshot)
    lines: list[str] = []
    vix = assets.get("VIX")
    if vix is not None:
        lines.append(
            f"波动｜VIX {_format_market_number(vix.price)}｜{_asset_percent(vix)}｜"
            f"{_vix_band(vix.price)}"
        )

    curve = snapshot.treasury
    if curve is not None:
        lines.append(
            "利率｜"
            f"2Y {_format_market_number(curve.two_year)}%"
            f"（{_format_bp(curve.two_year_change_bp, signed=True)}bp）｜"
            f"10Y {_format_market_number(curve.ten_year)}%"
            f"（{_format_bp(curve.ten_year_change_bp, signed=True)}bp）"
        )
        extra_tenors: list[str] = []
        if curve.three_month is not None:
            tenor = f"3M {_format_market_number(curve.three_month)}%"
            if curve.three_month_change_bp is not None:
                tenor += f"（{_format_bp(curve.three_month_change_bp, signed=True)}bp）"
            extra_tenors.append(tenor)
        if curve.thirty_year is not None:
            tenor = f"30Y {_format_market_number(curve.thirty_year)}%"
            if curve.thirty_year_change_bp is not None:
                tenor += f"（{_format_bp(curve.thirty_year_change_bp, signed=True)}bp）"
            extra_tenors.append(tenor)
        if extra_tenors:
            lines.append("短长端｜" + "｜".join(extra_tenors))
        slope = "正斜率" if curve.spread_bp > 0 else "倒挂" if curve.spread_bp < 0 else "平坦"
        lines.append(
            f"曲线｜10Y-2Y {_format_bp(curve.spread_bp, signed=True)}bp｜{slope}"
        )

    dollar = assets.get("DINIW")
    if dollar is not None:
        lines.append(
            f"美元｜DXY {_format_market_number(dollar.price)}｜{_asset_percent(dollar)}"
        )

    gold = assets.get("hf_GC")
    if gold is not None:
        gold_line = (
            f"黄金｜COMEX {_format_market_number(gold.price, grouped=True)} 美元/盎司｜"
            f"{_asset_percent(gold)}"
        )
        gold_pos = _intraday_position(gold)
        if gold_pos is not None:
            gold_line += f"｜{gold_pos}"
        lines.append(gold_line)

    silver = assets.get("hf_SI")
    if silver is not None:
        silver_line = (
            f"白银｜COMEX {_format_market_number(silver.price)} 美元/盎司｜"
            f"{_asset_percent(silver)}"
        )
        silver_pos = _intraday_position(silver)
        if silver_pos is not None:
            silver_line += f"｜{silver_pos}"
        lines.append(silver_line)

    if gold is not None and silver is not None and silver.price > 0:
        ratio = gold.price / silver.price
        lines.append(f"金银比｜{_format_market_number(ratio, grouped=True)}")

    crude = assets.get("hf_CL")
    if crude is not None:
        crude_line = (
            f"原油｜WTI {_format_market_number(crude.price)} 美元/桶｜{_asset_percent(crude)}"
        )
        crude_pos = _intraday_position(crude)
        if crude_pos is not None:
            crude_line += f"｜{crude_pos}"
        lines.append(crude_line)

    bitcoin = assets.get("BTC-USD")
    if bitcoin is not None:
        lines.append(
            f"加密｜BTC ${_format_market_number(bitcoin.price, grouped=True)}｜"
            f"{_asset_percent(bitcoin, prefix='24h ')}"
        )

    if not lines:
        return "暂时不可用"
    return "\n".join(lines)


def _movement(value: Decimal, *, up: str = "上涨", down: str = "下跌") -> str:
    if value > 0:
        return up
    if value < 0:
        return down
    return "持平"


def _headline(stats: IndexStats, snapshot: OverseasMarketSnapshot) -> str:
    parts: list[str] = []
    if stats.complete and stats.average_percent is not None:
        if stats.up == 3:
            direction = "同涨"
        elif stats.down == 3:
            direction = "同跌"
        elif stats.flat == 3:
            direction = "持平"
        else:
            direction = "涨跌分化"
        summary = (
            f"美股三大指数{direction}，"
            f"等权 {_format_decimal(stats.average_percent, signed=True)}%"
        )
        if stats.spread_percent != 0:
            best_percent = max(_decimal(quote.percent) for quote in stats.quotes)
            leaders = [
                _SHORT_NAMES[quote.code]
                for quote in stats.quotes
                if _decimal(quote.percent) == best_percent
            ]
            leader_text = "/".join(leaders)
            summary += (
                f"，{leader_text}并列表现居前"
                if len(leaders) > 1
                else f"，{leader_text}表现居前"
            )
        parts.append(summary)
    elif stats.quotes:
        parts.append(f"美股指数数据仅取得 {len(stats.quotes)}/3")

    assets = _asset_map(snapshot)
    vix = assets.get("VIX")
    if vix is not None:
        if vix.percent is None:
            parts.append(f"VIX报 {_format_market_number(vix.price)}")
        else:
            parts.append(
                f"VIX{_movement(vix.percent, up='上行', down='回落')}至"
                f" {_format_market_number(vix.price)}"
            )

    if snapshot.treasury is not None:
        curve = snapshot.treasury
        parts.append(
            f"10Y美债收益率{_movement(curve.ten_year_change_bp, up='上行', down='下行')}"
            f" {_format_bp(abs(curve.ten_year_change_bp))}bp"
        )

    regional = _regional_map(snapshot)
    if regional:
        strongest = max(regional.values(), key=lambda quote: quote.percent)
        weakest = min(regional.values(), key=lambda quote: quote.percent)
        if strongest.percent == weakest.percent:
            parts.append(
                f"亚太欧{len(regional)}项涨跌幅相同"
                f" {_format_market_number(strongest.percent, signed=True)}%"
            )
        else:
            parts.append(
                f"亚太欧{len(regional)}项中"
                f"{_REGIONAL_SHORT_NAMES[strongest.code]}"
                f"{_format_market_number(strongest.percent, signed=True)}%居前、"
                f"{_REGIONAL_SHORT_NAMES[weakest.code]}"
                f"{_format_market_number(weakest.percent, signed=True)}%居后"
            )

    if not parts:
        return "关键市场数据暂不可用，以下仅展示已成功取得的项目。"
    return f"{'；'.join(parts)}。"


def _vix_band(price: Decimal) -> str:
    if price < Decimal("15"):
        return "偏低区间（<15）"
    if price < Decimal("20"):
        return "常态区间（15–20）"
    if price < Decimal("30"):
        return "偏高区间（20–30）"
    return "高位区间（≥30）"


def render_cross_asset_observations(snapshot: OverseasMarketSnapshot) -> str:
    assets = _asset_map(snapshot)
    lines: list[str] = []
    vix = assets.get("VIX")
    if vix is not None:
        movement = ""
        if vix.percent is not None:
            movement = (
                f"，本次{_movement(vix.percent, up='上行', down='回落')}"
                f" {_format_market_number(abs(vix.percent))}%"
            )
        lines.append(
            f"• VIX位于{_vix_band(vix.price)}{movement}。"
        )

    curve = snapshot.treasury
    if curve is not None:
        slope = "正斜率" if curve.spread_bp > 0 else "倒挂" if curve.spread_bp < 0 else "平坦"
        lines.append(
            f"• 2Y收益率{_movement(curve.two_year_change_bp, up='上行', down='下行')}"
            f" {_format_bp(abs(curve.two_year_change_bp))}bp，"
            f"10Y收益率{_movement(curve.ten_year_change_bp, up='上行', down='下行')}"
            f" {_format_bp(abs(curve.ten_year_change_bp))}bp；"
            f"期限曲线为{slope}，利差 {_format_bp(curve.spread_bp, signed=True)}bp。"
        )

    dollar = assets.get("DINIW")
    gold = assets.get("hf_GC")
    if dollar is not None and dollar.percent is not None and gold is not None and gold.percent is not None:
        dollar_direction = _direction(dollar.percent)
        gold_direction = _direction(gold.percent)
        if dollar_direction == gold_direction:
            relation = "同向"
        elif 0 in {dollar_direction, gold_direction}:
            relation = "一项持平"
        else:
            relation = "反向"
        lines.append(
            f"• 美元指数{_movement(dollar.percent)} {_format_market_number(abs(dollar.percent))}%，"
            f"黄金期货{_movement(gold.percent)} {_format_market_number(abs(gold.percent))}%；"
            f"本次方向{relation}，仅作价格关系描述。"
        )

    crude = assets.get("hf_CL")
    silver = assets.get("hf_SI")
    bitcoin = assets.get("BTC-USD")
    movement_parts: list[str] = []
    if crude is not None and crude.percent is not None:
        movement_parts.append(
            f"WTI {_movement(crude.percent)} {_format_market_number(abs(crude.percent))}%"
        )
    if silver is not None and silver.percent is not None:
        movement_parts.append(
            f"白银 {_movement(silver.percent)} {_format_market_number(abs(silver.percent))}%"
        )
    if bitcoin is not None and bitcoin.percent is not None:
        movement_parts.append(
            f"BTC 24h {_movement(bitcoin.percent)} {_format_market_number(abs(bitcoin.percent))}%"
        )
    if movement_parts:
        lines.append(f"• {'；'.join(movement_parts)}。")

    if gold is not None and silver is not None and silver.price > 0:
        ratio = gold.price / silver.price
        lines.append(f"• 金银比 {_format_market_number(ratio, grouped=True)}（金价/银价）。")

    regional = _regional_map(snapshot)
    if regional:
        ranked = sorted(regional.values(), key=lambda quote: quote.percent, reverse=True)
        strongest = ranked[0]
        weakest = ranked[-1]
        spread = strongest.percent - weakest.percent
        names = "、".join(
            f"{_REGIONAL_SHORT_NAMES[quote.code]} {_format_market_number(quote.percent, signed=True)}%"
            for quote in ranked
        )
        if spread == 0:
            lines.append(f"• 亚太欧指数：{names}。")
        else:
            lines.append(
                f"• 亚太欧指数：{names}；"
                f"{_REGIONAL_SHORT_NAMES[strongest.code]}较"
                f"{_REGIONAL_SHORT_NAMES[weakest.code]}领先"
                f" {_format_decimal(spread)}pp。"
            )

    return "\n".join(lines) if lines else "• 跨资产数据不足，暂不作结构描述。"


def render_observation_rules(stats: IndexStats, snapshot: OverseasMarketSnapshot) -> str:
    lines: list[str] = []
    if stats.complete and stats.spread_percent is not None:
        triggered = stats.spread_percent > Decimal("0.50")
        marker = "●" if triggered else "○"
        state = "已触发" if triggered else "未触发"
        lines.append(
            f"{marker} 指数分化｜{_format_decimal(stats.spread_percent)} / 0.50pp｜{state}"
        )

    vix = _asset_map(snapshot).get("VIX")
    if vix is not None:
        triggered = vix.price >= Decimal("20")
        marker = "●" if triggered else "○"
        state = "已触发" if triggered else "未触发"
        lines.append(
            f"{marker} VIX｜{_format_market_number(vix.price)} / 20｜{state}"
        )

    curve = snapshot.treasury
    if curve is not None:
        triggered = abs(curve.ten_year_change_bp) >= Decimal("5")
        marker = "●" if triggered else "○"
        state = "已触发" if triggered else "未触发"
        lines.append(
            f"{marker} 10Y日变｜{_format_bp(curve.ten_year_change_bp, signed=True)}bp / ±5bp｜{state}"
        )

    assets = _asset_map(snapshot)
    gold = assets.get("hf_GC")
    silver = assets.get("hf_SI")
    if gold is not None and silver is not None and silver.price > 0:
        ratio = gold.price / silver.price
        triggered = ratio >= Decimal("80") or ratio <= Decimal("60")
        marker = "●" if triggered else "○"
        state = "已触发" if triggered else "未触发"
        lines.append(
            f"{marker} 金银比｜{_format_market_number(ratio, grouped=True)} / 60–80｜{state}"
        )

    regional = _regional_map(snapshot)
    if len(regional) >= 2:
        percents = [quote.percent for quote in regional.values()]
        spread = max(percents) - min(percents)
        triggered = spread > Decimal("1.00")
        marker = "●" if triggered else "○"
        state = "已触发" if triggered else "未触发"
        lines.append(
            f"{marker} 亚太欧分化｜{_format_decimal(spread)} / 1.00pp｜{state}"
        )

    return "\n".join(lines) if lines else "关键数据不足，系统观察条件暂不判定。"


def _data_notes(stats: IndexStats, snapshot: OverseasMarketSnapshot) -> str:
    assets = _asset_map(snapshot)
    regional = _regional_map(snapshot)
    available = len(assets) + (1 if snapshot.treasury is not None else 0)
    sources: list[str] = []
    sina_items: list[str] = []
    if stats.quotes:
        sina_items.append("指数")
    if regional:
        sina_items.append("亚太欧")
    if "DINIW" in assets:
        sina_items.append("DXY")
    if "hf_GC" in assets:
        sina_items.append("黄金")
    if "hf_SI" in assets:
        sina_items.append("白银")
    if "hf_CL" in assets:
        sina_items.append("WTI")
    if sina_items:
        sources.append(f"新浪财经（{'/'.join(sina_items)}）")
    if snapshot.us_indices and not stats.quotes:
        sources.append("新浪财经（指数未通过校验）")
    if "VIX" in assets:
        sources.append("Cboe（VIX）")
    if snapshot.treasury is not None:
        sources.append("美国财政部（美债）")
    if "BTC-USD" in assets:
        sources.append("Coinbase（BTC）")
    source_text = "｜".join(sources) if sources else "本次无可用来源"

    times: list[str] = []
    if stats.trade_date is not None:
        times.append(f"美股 {stats.trade_date.isoformat()} 收盘")
    for code, label in (
        ("VIX", "VIX"),
        ("DINIW", "DXY"),
        ("hf_GC", "黄金"),
        ("hf_SI", "白银"),
        ("hf_CL", "WTI"),
        ("BTC-USD", "BTC"),
    ):
        quote = assets.get(code)
        if quote is not None:
            times.append(f"{label} {_compact_as_of(quote.as_of)}")
    if snapshot.treasury is not None:
        times.insert(1, f"美债 {_compact_as_of(snapshot.treasury.date)}")

    lines = [
        f"完整度｜美股 {len(stats.quotes)}/3 · 跨资产 {available}/7 · 亚太欧 {len(regional)}/3",
    ]
    missing: list[str] = []
    if not stats.complete:
        missing.append(f"美股指数（{3 - len(stats.quotes)} 项缺失或未通过）")
    if "VIX" not in assets:
        missing.append("VIX")
    if snapshot.treasury is None:
        missing.append("美债")
    for code, label in (
        ("DINIW", "DXY"),
        ("hf_GC", "黄金"),
        ("hf_SI", "白银"),
        ("hf_CL", "WTI"),
        ("BTC-USD", "BTC"),
    ):
        if code not in assets:
            missing.append(label)
    absent_regional = [
        _REGIONAL_SHORT_NAMES[code]
        for code in _EXPECTED_REGIONAL_CODES
        if code not in regional
    ]
    if absent_regional:
        missing.append(f"亚太欧（{'/'.join(absent_regional)}）")
    if missing:
        lines.append(f"缺失｜{' · '.join(missing)}（本次未取得）")
    lines.append(f"来源｜{source_text}")
    for index in range(0, len(times), 3):
        lines.append(f"时间（各源时区）｜{' · '.join(times[index:index + 3])}")
    lines.extend(
        (
            "口径｜黄金/白银/WTI较前结算价；BTC较24h开盘价；52周为源区间位置",
            "说明｜标题为生成日期；美股仅接受预期最近交易日的收盘值",
        )
    )
    return "\n".join(lines)


def _as_snapshot(
    data: OverseasMarketSnapshot | Sequence[UsIndexQuote],
) -> OverseasMarketSnapshot:
    if isinstance(data, OverseasMarketSnapshot):
        return data
    return OverseasMarketSnapshot(
        us_indices=tuple(data),
        assets=(),
        treasury=None,
        regional_indices=(),
    )


def _index_percent(stats: IndexStats, code: str) -> Decimal | None:
    for quote in stats.quotes:
        if quote.code == code:
            return _decimal(quote.percent)
    return None


def _rate_pressure(snapshot: OverseasMarketSnapshot) -> bool:
    curve = snapshot.treasury
    return curve is not None and (
        curve.two_year_change_bp >= Decimal("5")
        or curve.ten_year_change_bp >= Decimal("5")
    )


def _dollar_pressure(snapshot: OverseasMarketSnapshot) -> bool:
    dollar = _asset_map(snapshot).get("DINIW")
    return (
        dollar is not None
        and dollar.percent is not None
        and dollar.percent >= Decimal("0.30")
    )


def _vix_move_label(vix: CrossAssetQuote | None) -> str:
    """Use neutral wording when VIX is below the previous close."""
    if vix is not None and vix.percent is not None and vix.percent < 0:
        return "VIX报"
    return "VIX升至"


def render_risk_level(stats: IndexStats, snapshot: OverseasMarketSnapshot) -> str:
    """Return a low-noise severity banner; only exceptional moves turn red."""
    assets = _asset_map(snapshot)
    vix = assets.get("VIX")
    critical: list[str] = []
    watch: list[str] = []
    if vix is not None:
        vix_label = _vix_move_label(vix)
        if vix.price >= Decimal("30"):
            critical.append(f"{vix_label} {_format_market_number(vix.price)}")
        elif vix.price >= Decimal("20"):
            watch.append(f"{vix_label} {_format_market_number(vix.price)}")
    if stats.complete and stats.average_percent is not None:
        if stats.average_percent <= Decimal("-2"):
            critical.append(f"三大指数均值 {_format_decimal(stats.average_percent, signed=True)}%")
        elif stats.average_percent <= Decimal("-1"):
            watch.append(f"三大指数均值 {_format_decimal(stats.average_percent, signed=True)}%")
    curve = snapshot.treasury
    if curve is not None:
        largest_rate_move = max(
            abs(curve.two_year_change_bp),
            abs(curve.ten_year_change_bp),
        )
        if largest_rate_move >= Decimal("20"):
            critical.append(f"美债收益率单日最大变动 {_format_bp(largest_rate_move)}bp")
        elif largest_rate_move >= Decimal("10"):
            watch.append(f"美债收益率单日最大变动 {_format_bp(largest_rate_move)}bp")
    dollar = assets.get("DINIW")
    if dollar is not None and dollar.percent is not None and abs(dollar.percent) >= Decimal("1"):
        watch.append(f"美元单日 {_format_market_number(dollar.percent, signed=True)}%")

    if critical:
        return f"🔴 重要风险｜{'；'.join(critical[:2])}。优先控制风险，不做冲动交易。"
    if watch:
        suffix = ""
        if vix is not None and vix.price < Decimal("20"):
            suffix = "；VIX仍低于20，暂未见恐慌"
        return f"🟡 留意｜{'；'.join(watch[:2])}{suffix}。"
    return "🟢 常态｜未触发重要风险条件，普通波动不单独提醒。"


def render_verified_events(snapshot: OverseasMarketSnapshot) -> str:
    context = snapshot.market_context
    if context is None or not context.available_sources:
        return "• 官方事件源暂不可用；本次只保留价格关系推断，不编造原因。"
    if not context.events:
        sources = "、".join(
            _OFFICIAL_SOURCE_LABELS.get(source, source)
            for source in context.available_sources
        )
        return f"• 近5日未从 {sources} 检出高影响发布；本次只保留价格关系推断。"
    lines: list[str] = []
    market_zone = ZoneInfo("America/New_York")
    for event in context.events[:2]:
        published = event.published_at.astimezone(market_zone).strftime("%m-%d")
        detail = f"｜{event.detail}" if event.detail else ""
        source = _OFFICIAL_SOURCE_LABELS.get(event.source, event.source)
        lines.append(f"• {source} · {published}｜{event.title}{detail}")
    lines.append("• 以上是同期官方发布，不等于已证明其导致当日涨跌。")
    return "\n".join(lines)


def render_plain_language_summary(
    stats: IndexStats,
    snapshot: OverseasMarketSnapshot,
) -> str:
    """Explain the price pattern before presenting raw market numbers."""
    assets = _asset_map(snapshot)
    vix = assets.get("VIX")
    sentences: list[str] = []

    if stats.complete and stats.average_percent is not None:
        if stats.average_percent <= Decimal("-1"):
            tone = "市场明显降温"
        elif stats.average_percent <= Decimal("-0.25"):
            tone = "市场温和降温"
        elif stats.average_percent >= Decimal("1"):
            tone = "市场明显转强"
        elif stats.average_percent >= Decimal("0.25"):
            tone = "市场温和转强"
        else:
            tone = "市场总体平稳"
        if vix is not None and vix.price < Decimal("20"):
            sentences.append(
                f"{tone}，但VIX只有 {_format_market_number(vix.price)}，"
                "暂未出现明显恐慌。"
            )
        elif vix is not None:
            sentences.append(
                f"{tone}，{_vix_move_label(vix)} {_format_market_number(vix.price)}，"
                "风险偏好需要谨慎。"
            )
        else:
            sentences.append(f"{tone}。")
    elif stats.quotes:
        sentences.append("美股数据不完整，今天只看已取得价格，不作整体判断。")
    else:
        sentences.append("美股数据暂不可用，今天不作整体判断。")

    rate_pressure = _rate_pressure(snapshot)
    dollar_pressure = _dollar_pressure(snapshot)
    if rate_pressure and dollar_pressure:
        sentences.append(
            "价格组合更像在交易“利率更高、美元更强”，通常对高估值科技、"
            "贵金属和加密资产偏不利；这是价格关系推断，不是对具体新闻原因的确认。"
        )
    elif rate_pressure:
        sentences.append(
            "美债收益率明显上行，利率是今天最值得关注的压力项。"
        )
    elif dollar_pressure:
        sentences.append("美元明显走强，美元计价资产短期更容易承压。")

    nasdaq = _index_percent(stats, "int_nasdaq")
    dow = _index_percent(stats, "int_dji")
    if nasdaq is not None and dow is not None:
        lag = dow - nasdaq
        if lag >= Decimal("0.30"):
            sentences.append(
                f"纳指弱于道指 {_format_decimal(lag)}pp，科技和成长风格相对吃亏。"
            )
        elif lag <= Decimal("-0.30"):
            sentences.append(
                f"纳指强于道指 {_format_decimal(abs(lag))}pp，科技和成长风格相对占优。"
            )
    return "".join(sentences)


def render_portfolio_implications(
    stats: IndexStats,
    snapshot: OverseasMarketSnapshot,
) -> str:
    """Map observable prices to common portfolio exposures without overclaiming."""
    assets = _asset_map(snapshot)
    lines: list[str] = []
    nasdaq = _index_percent(stats, "int_nasdaq")
    dow = _index_percent(stats, "int_dji")
    rate_pressure = _rate_pressure(snapshot)

    if nasdaq is not None:
        if not stats.complete:
            lines.append(
                f"• 科技/成长｜纳指 {_format_market_number(nasdaq, signed=True)}%；"
                "指数数据不完整，不作相对强弱判断。"
            )
        else:
            relative = ""
            if dow is not None:
                gap = dow - nasdaq
                if gap >= Decimal("0.30"):
                    relative = f"，落后道指 {_format_decimal(gap)}pp"
                elif gap <= Decimal("-0.30"):
                    relative = f"，领先道指 {_format_decimal(abs(gap))}pp"
            stance = (
                "短期偏逆风"
                if rate_pressure or (dow is not None and nasdaq < dow)
                else "暂未显示明显逆风"
            )
            lines.append(
                f"• 科技/成长｜{stance}；纳指 {_format_market_number(nasdaq, signed=True)}%"
                f"{relative}。"
            )

    gold = assets.get("hf_GC")
    silver = assets.get("hf_SI")
    precious: list[str] = []
    if gold is not None and gold.percent is not None:
        precious.append(f"黄金 {_format_market_number(gold.percent, signed=True)}%")
    if silver is not None and silver.percent is not None:
        precious.append(f"白银 {_format_market_number(silver.percent, signed=True)}%")
    if precious:
        suffix = (
            "，与美元走强同时出现，短期压力尚未解除。"
            if _dollar_pressure(snapshot)
            else "；先观察美元和利率方向。"
        )
        lines.append(f"• 贵金属｜{' · '.join(precious)}{suffix}")

    bitcoin = assets.get("BTC-USD")
    if bitcoin is not None and bitcoin.percent is not None:
        if bitcoin.percent <= Decimal("-2"):
            comment = "风险偏好降温，短线波动可能仍大"
        elif bitcoin.percent >= Decimal("2"):
            comment = "风险偏好回升，但仍属于高波动资产"
        else:
            comment = "波动有限，暂不单独构成方向信号"
        lines.append(
            f"• BTC｜24h {_format_market_number(bitcoin.percent, signed=True)}%；{comment}。"
        )

    curve = snapshot.treasury
    if curve is not None:
        if curve.two_year_change_bp > 0 or curve.ten_year_change_bp > 0:
            comment = "收益率上涨意味着存量债券价格承压"
        elif curve.two_year_change_bp < 0 or curve.ten_year_change_bp < 0:
            comment = "收益率回落通常有利于存量债券价格"
        else:
            comment = "收益率变化不大"
        lines.append(
            f"• 美债｜2Y {_format_bp(curve.two_year_change_bp, signed=True)}bp · "
            f"10Y {_format_bp(curve.ten_year_change_bp, signed=True)}bp；{comment}。"
        )
    return "\n".join(lines) if lines else "• 关键资产数据不足，暂不映射持仓影响。"


def render_action_plan(stats: IndexStats, snapshot: OverseasMarketSnapshot) -> str:
    """Give conditional, risk-aware actions instead of pretending to predict."""
    assets = _asset_map(snapshot)
    vix = assets.get("VIX")
    macro_pressure = _rate_pressure(snapshot) and _dollar_pressure(snapshot)
    lines: list[str] = []
    if macro_pressure:
        lines.extend(
            (
                "• 已持有高波动资产｜先检查总仓位和止损规则，不因单日下跌盲目补仓。",
                "• 准备买入｜先等美债收益率或美元至少一项回落，再判断是否分批进入。",
            )
        )
    else:
        lines.append("• 仓位处理｜没有强宏观共振信号时，不因一天涨跌改变长期计划。")

    if (
        stats.complete
        and stats.average_percent is not None
        and stats.average_percent > Decimal("-1")
        and vix is not None
        and vix.price < Decimal("20")
    ):
        lines.append("• 风险判断｜指数跌幅与VIX都未显示系统性恐慌，不把普通回调当成危机。")
    elif vix is not None and vix.price >= Decimal("20"):
        lines.append("• 风险判断｜VIX已进入偏高区间，优先控制仓位，再考虑收益。")
    return "\n".join(lines) if lines else "• 暂无足够数据形成行动条件，保持原计划。"


def render_watch_conditions(stats: IndexStats, snapshot: OverseasMarketSnapshot) -> str:
    lines: list[str] = []
    curve = snapshot.treasury
    dollar = _asset_map(snapshot).get("DINIW")
    if curve is not None:
        lines.append(
            f"• 利率｜2Y {_format_market_number(curve.two_year)}%"
            f"（{_format_bp(curve.two_year_change_bp, signed=True)}bp） · "
            f"10Y {_format_market_number(curve.ten_year)}%"
            f"（{_format_bp(curve.ten_year_change_bp, signed=True)}bp）"
        )
    if dollar is not None and dollar.percent is not None:
        lines.append(
            f"• 美元｜DXY {_format_market_number(dollar.price)}"
            f"（{_format_market_number(dollar.percent, signed=True)}%）"
        )
    nasdaq = _index_percent(stats, "int_nasdaq")
    dow = _index_percent(stats, "int_dji")
    if nasdaq is not None and dow is not None:
        gap = nasdaq - dow
        lines.append(
            f"• 风格｜纳指相对道指 {_format_decimal(gap, signed=True)}pp"
        )
    if lines:
        lines.extend(
            (
                "• 转弱信号｜若收益率与美元继续上行，且纳指转为跑输。",
                "• 缓解信号｜若收益率或美元回落，同时纳指相对表现企稳。",
            )
        )
    return "\n".join(lines) if lines else "• 关键数据不足，今天不设方向判断。"


def render_key_numbers(stats: IndexStats, snapshot: OverseasMarketSnapshot) -> str:
    assets = _asset_map(snapshot)
    lines: list[str] = []
    if stats.quotes:
        indices = " · ".join(
            f"{_SHORT_NAMES[quote.code]} {_format_market_number(_decimal(quote.percent), signed=True)}%"
            for quote in stats.quotes
        )
        lines.append(f"美股｜{indices}")

    vix = assets.get("VIX")
    if vix is not None:
        lines.append(
            f"波动｜VIX {_format_market_number(vix.price)} · {_asset_percent(vix)}"
        )

    compact_assets: list[str] = []
    for code, label, price_prefix in (
        ("hf_GC", "黄金", "$"),
        ("hf_SI", "白银", "$"),
        ("hf_CL", "WTI", "$"),
        ("BTC-USD", "BTC", "$"),
    ):
        quote = assets.get(code)
        if quote is None:
            continue
        percent = f" {_asset_percent(quote)}" if quote.percent is not None else ""
        compact_assets.append(
            f"{label} {price_prefix}{_format_market_number(quote.price, grouped=True)}{percent}"
        )
    if compact_assets:
        lines.append("其他｜" + " · ".join(compact_assets))

    regional = _regional_map(snapshot)
    if regional:
        parts = [
            f"{_REGIONAL_SHORT_NAMES[code]} {_format_market_number(regional[code].percent, signed=True)}%"
            for code in _EXPECTED_REGIONAL_CODES
            if code in regional
        ]
        lines.append("亚太欧｜" + " · ".join(parts))
    return "\n".join(lines) if lines else "关键数据暂不可用。"


def render_compact_data_notes(
    stats: IndexStats,
    snapshot: OverseasMarketSnapshot,
    *,
    quality_issues: Sequence[str] = (),
) -> str:
    lines = [
        line
        for line in _data_notes(stats, snapshot).splitlines()
        if line.startswith(("完整度｜", "缺失｜", "来源｜"))
    ]
    if stats.trade_date is not None:
        lines.insert(1, f"美股交易日｜{stats.trade_date.isoformat()}（美东）")
    elif stats.expected_trade_date is not None:
        lines.insert(1, f"期望交易日｜{stats.expected_trade_date.isoformat()}（美东）")
    if stats.anomalies:
        lines.insert(2, f"校验｜{'；'.join(stats.anomalies)}")
    if quality_issues:
        lines.insert(2, f"资产校验｜{'；'.join(quality_issues)}")
    if stats.quotes and not stats.complete:
        lines.insert(3, f"已验收｜{len(stats.quotes)}/3，仅展示明细，不作整体判断")
    if snapshot.regional_indices:
        lines.append("时效说明｜亚太欧简表源未提供时间戳；其余跨资产已校验")
    return "\n".join(lines)


def render_a_share_impact(
    stats: IndexStats,
    snapshot: OverseasMarketSnapshot,
) -> str:
    """Translate overseas signals into cautious A-share pre-open implications."""
    assets = _asset_map(snapshot)
    vix = assets.get("VIX")
    lines: list[str] = []

    if stats.complete and stats.average_percent is not None:
        if stats.average_percent <= Decimal("-1") or (
            vix is not None and vix.price >= Decimal("20")
        ):
            lines.append("• 整体情绪｜外部风险偏好偏弱，A股开盘情绪可能承压。")
        elif stats.average_percent >= Decimal("1") and (
            vix is None or vix.price < Decimal("20")
        ):
            lines.append("• 整体情绪｜外部风险偏好偏暖，对A股开盘情绪略有支持。")
        else:
            lines.append("• 整体情绪｜海外信号偏中性，暂不足以单独改变A股判断。")
    else:
        lines.append("• 整体情绪｜美股数据不完整，不据此判断A股方向。")

    nasdaq = _index_percent(stats, "int_nasdaq")
    dow = _index_percent(stats, "int_dji")
    growth_pressure = _rate_pressure(snapshot) or _dollar_pressure(snapshot)
    if nasdaq is not None and dow is not None:
        relative = nasdaq - dow
        if relative <= Decimal("-0.30") or growth_pressure:
            lines.append(
                "• 风格线索｜高估值科技和成长开盘可能相对吃亏，先看利率、"
                "美元与纳指能否企稳。"
            )
        elif relative >= Decimal("0.30") and not growth_pressure:
            lines.append("• 风格线索｜科技和成长情绪略占优，但不把隔夜表现当成全天预测。")
    elif growth_pressure:
        lines.append("• 风格线索｜利率或美元偏强，高估值成长方向短期更容易受压。")

    lines.append("• 使用边界｜这只是开盘前参考；A股全天仍以国内政策、资金和成交为主。")
    return "\n".join(lines)


def render_brief_watch(
    stats: IndexStats,
    snapshot: OverseasMarketSnapshot,
) -> str:
    lines: list[str] = []
    curve = snapshot.treasury
    if curve is not None:
        if curve.ten_year_change_bp > 0:
            focus = "是否继续上行"
        elif curve.ten_year_change_bp < 0:
            focus = "能否延续回落"
        else:
            focus = "接下来向上还是向下"
        lines.append(
            f"• 10Y美债｜{_format_market_number(curve.ten_year)}%，看{focus}。"
        )
    dollar = _asset_map(snapshot).get("DINIW")
    if dollar is not None and dollar.percent is not None:
        focus = "是否继续走强" if dollar.percent > 0 else "会不会重新转强"
        lines.append(
            f"• 美元｜DXY {_format_market_number(dollar.price)}，看{focus}。"
        )
    nasdaq = _index_percent(stats, "int_nasdaq")
    dow = _index_percent(stats, "int_dji")
    if nasdaq is not None and dow is not None:
        relative = nasdaq - dow
        if relative < Decimal("-0.10"):
            focus = "能否企稳，还是继续落后"
        elif relative > Decimal("0.10"):
            focus = "能否保持领先"
        else:
            focus = "会不会出现明显分化"
        lines.append(f"• 风格｜看纳指相对道指{focus}。")
    return "\n".join(lines[:3]) if lines else "• 数据不足，今天不额外设置海外观察条件。"


def render_action_decision(
    stats: IndexStats,
    snapshot: OverseasMarketSnapshot,
) -> str:
    assets = _asset_map(snapshot)
    vix = assets.get("VIX")
    curve = snapshot.treasury
    average = stats.average_percent if stats.complete else None
    largest_rate_move = (
        max(abs(curve.two_year_change_bp), abs(curve.ten_year_change_bp))
        if curve is not None
        else Decimal("0")
    )
    critical = (
        (vix is not None and vix.price >= Decimal("30"))
        or (average is not None and average <= Decimal("-2"))
        or largest_rate_move >= Decimal("20")
    )
    watch = (
        (vix is not None and vix.price >= Decimal("20"))
        or (average is not None and average <= Decimal("-1"))
        or largest_rate_move >= Decimal("10")
        or (_rate_pressure(snapshot) and _dollar_pressure(snapshot))
    )
    if critical:
        return (
            "需要，但只做风险管理｜开盘不追涨杀跌，先检查高波动方向的仓位上限，"
            "等待风险指标缓和。"
        )
    if watch:
        return "暂不急着交易｜先观察开盘后30–60分钟，至少等利率、美元或成长风格一项企稳。"
    return "不需要额外行动｜维持原计划，不因一晚涨跌临时改变长期安排。"


def compose_global_report_bundle(
    report_date: str,
    data: OverseasMarketSnapshot | Sequence[UsIndexQuote],
    *,
    generated_at: datetime | None = None,
) -> GlobalReportBundle:
    raw_snapshot = _as_snapshot(data)
    quality_time = generated_at
    if quality_time is None:
        quality_time = datetime.combine(
            date.fromisoformat(report_date),
            time(8, 30),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
    snapshot, quality_issues = sanitize_snapshot(
        raw_snapshot,
        generated_at=quality_time,
    )
    expected_trade_day = expected_us_trade_date(
        report_date,
        generated_at=quality_time,
    )
    stats = analyze_indices(
        snapshot.us_indices,
        expected_trade_date=expected_trade_day,
        generated_at=quality_time,
    )
    risk_level = render_risk_level(stats, snapshot)
    summary = render_plain_language_summary(stats, snapshot)
    verified_events = render_verified_events(snapshot)
    implications = render_portfolio_implications(stats, snapshot)
    actions = render_action_plan(stats, snapshot)
    watch_conditions = render_watch_conditions(stats, snapshot)
    key_numbers = render_key_numbers(stats, snapshot)
    data_notes = render_compact_data_notes(
        stats,
        snapshot,
        quality_issues=quality_issues,
    )
    a_share_impact = render_a_share_impact(stats, snapshot)
    brief_watch = render_brief_watch(stats, snapshot)
    action_decision = render_action_decision(stats, snapshot)
    brief = (
        f"🌐 海外市场晨报｜{report_date}\n\n"
        "🌙 昨晚发生了什么\n"
        f"{risk_level}\n{summary}\n\n"
        "🇨🇳 对今天 A 股可能有什么影响\n"
        f"{a_share_impact}\n\n"
        "👀 今天建议观察什么\n"
        f"{brief_watch}\n\n"
        "✅ 是否需要行动\n"
        f"{action_decision}\n\n"
        "这是开盘前参考，不构成投资建议。"
    )
    full = (
        f"🌐 海外市场晨报｜{report_date}\n"
        "风险级别 · 原因核实 · 今日动作\n"
        f"{_DIVIDER}\n\n"
        f"{risk_level}\n\n"
        "🧭 一句话看懂\n"
        f"{summary}\n\n"
        "📰 已核实事件\n"
        f"{verified_events}\n\n"
        "🎯 如果你关注这些资产\n"
        f"{implications}\n\n"
        "✅ 今天怎么做\n"
        f"{actions}\n\n"
        "👀 今天盯住\n"
        f"{watch_conditions}\n\n"
        f"{_DIVIDER}\n\n"
        "📌 关键数据\n"
        f"{key_numbers}\n\n"
        "ℹ️ 数据质量\n"
        f"{data_notes}\n\n"
        "价格关系用于辅助判断，不构成投资建议。"
    )
    evidence = build_report_evidence(
        report_date=report_date, full=full, brief=brief, generated_at=generated_at,
        expected_trade_date=expected_trade_day.isoformat(),
        observation=raw_snapshot.vix_observation_json,
        rendered_quote=next((quote for quote in snapshot.assets if quote.code == "VIX"), None),
    )
    return GlobalReportBundle(report_date=report_date, brief=brief, full=full, vix_evidence=evidence)


def compose_global_report(
    report_date: str,
    data: OverseasMarketSnapshot | Sequence[UsIndexQuote],
    *,
    generated_at: datetime | None = None,
) -> str:
    return compose_global_report_bundle(
        report_date,
        data,
        generated_at=generated_at,
    ).full


_WEEKLY_PERCENT_PATTERN = re.compile(
    r"(道指|标普500|纳指) ([+-]?\d+(?:\.\d+)?)%"
)
_WEEKLY_ASSET_PATTERN = re.compile(
    r"(黄金|白银|WTI|BTC) \$[\d,]+(?:\.\d+)? ([+-]?\d+(?:\.\d+)?)%"
)
_WEEKLY_DOLLAR_PATTERN = re.compile(
    r"^• 美元｜DXY [\d,]+(?:\.\d+)?（([+-]?\d+(?:\.\d+)?)%）$"
)
_WEEKLY_RATE_PATTERN = re.compile(
    r"^• 利率｜2Y [\d.]+%（([+-]?\d+)bp） · 10Y [\d.]+%（([+-]?\d+)bp）$"
)


def _compound_percent(values: Sequence[Decimal]) -> Decimal:
    product = Decimal("1")
    for value in values:
        product *= Decimal("1") + value / Decimal("100")
    return (product - Decimal("1")) * Decimal("100")


def _extract_weekly_observations(
    report: str,
) -> tuple[str | None, dict[str, Decimal], dict[str, Decimal], tuple[Decimal, Decimal] | None]:
    risk: str | None = None
    moves: dict[str, Decimal] = {}
    assets: dict[str, Decimal] = {}
    rates: tuple[Decimal, Decimal] | None = None
    for line in report.splitlines():
        if line.startswith("🟢"):
            risk = "green"
        elif line.startswith("🟡"):
            risk = "yellow"
        elif line.startswith("🔴"):
            risk = "red"
        elif line.startswith("美股｜"):
            for match in _WEEKLY_PERCENT_PATTERN.finditer(line):
                moves[match.group(1)] = Decimal(match.group(2))
        elif line.startswith("其他｜"):
            for match in _WEEKLY_ASSET_PATTERN.finditer(line):
                assets[match.group(1)] = Decimal(match.group(2))
        else:
            dollar = _WEEKLY_DOLLAR_PATTERN.fullmatch(line)
            if dollar is not None:
                assets["美元"] = Decimal(dollar.group(1))
                continue
            rate = _WEEKLY_RATE_PATTERN.fullmatch(line)
            if rate is not None:
                rates = (Decimal(rate.group(1)), Decimal(rate.group(2)))
    return risk, moves, assets, rates


def compose_weekly_report(
    report_date: str,
    reports: Sequence[tuple[str, str]],
) -> str:
    """Summarize recent daily reports without requiring portfolio disclosure."""
    dated_reports = sorted(reports, key=lambda item: item[0])
    move_series: dict[str, list[Decimal]] = {}
    asset_series: dict[str, list[Decimal]] = {}
    risk_counts = {"green": 0, "yellow": 0, "red": 0}
    two_year_moves: list[Decimal] = []
    ten_year_moves: list[Decimal] = []

    for _, report in dated_reports:
        risk, moves, assets, rates = _extract_weekly_observations(report)
        if risk is not None:
            risk_counts[risk] += 1
        for label, value in moves.items():
            move_series.setdefault(label, []).append(value)
        for label, value in assets.items():
            asset_series.setdefault(label, []).append(value)
        if rates is not None:
            two_year_moves.append(rates[0])
            ten_year_moves.append(rates[1])

    observations = len(dated_reports)
    compounded = {
        label: _compound_percent(values)
        for label, values in move_series.items()
    }
    compounded_assets = {
        label: _compound_percent(values)
        for label, values in asset_series.items()
    }
    rate_2y = sum(two_year_moves, Decimal("0"))
    rate_10y = sum(ten_year_moves, Decimal("0"))

    if observations < 3:
        conclusion = (
            f"目前只积累了 {observations} 个交易日，至少需要 3 个交易日才判断周趋势。"
            "本期只列数据，不把短样本包装成结论。"
        )
    else:
        parts: list[str] = []
        sp500 = compounded.get("标普500")
        if sp500 is not None:
            direction = "上涨" if sp500 > 0 else "下跌" if sp500 < 0 else "持平"
            parts.append(
                f"标普500本周{direction} {_format_decimal(abs(sp500))}%"
            )
        if risk_counts["red"]:
            parts.append(f"出现 {risk_counts['red']} 个红色风险日")
        elif risk_counts["yellow"]:
            parts.append(f"有 {risk_counts['yellow']} 个需要留意的交易日")
        elif risk_counts["green"]:
            parts.append("风险灯均未升级")
        nasdaq = compounded.get("纳指")
        dow = compounded.get("道指")
        if nasdaq is not None and dow is not None and abs(nasdaq - dow) >= Decimal("1"):
            leader = "纳指" if nasdaq > dow else "道指"
            parts.append(f"{leader}相对占优")
        conclusion = "；".join(parts) + "。" if parts else "本周有效数据有限，维持中性判断。"

    actions: list[str] = []
    if observations < 3:
        actions.append("• 暂不根据本期周报改变长期计划，等样本积累到至少3天。")
    else:
        if risk_counts["red"]:
            actions.append("• 高波动资产｜先检查总仓位和退出规则，再考虑新增风险。")
        elif risk_counts["yellow"]:
            actions.append("• 仓位处理｜有风险灯升级但未到红色，不追涨杀跌，新增仓位分批进行。")
        else:
            actions.append("• 仓位处理｜没有周度风险升级，不因普通波动改变长期计划。")
        nasdaq = compounded.get("纳指")
        dow = compounded.get("道指")
        if nasdaq is not None and dow is not None and nasdaq - dow <= Decimal("-1"):
            actions.append("• 科技/成长｜本周明显跑输价值风格，等相对表现止跌后再加速买入。")
        if max(rate_2y, rate_10y) >= Decimal("10"):
            actions.append("• 利率敏感资产｜收益率周内累计上行较多，先等利率压力缓解。")

    watch: list[str] = []
    if two_year_moves or ten_year_moves:
        watch.append("• 美债收益率是否停止上行。")
    if "美元" in compounded_assets:
        watch.append("• 美元是否继续走强并压制黄金、科技和加密资产。")
    if "纳指" in compounded and "道指" in compounded:
        watch.append("• 纳指相对道指是否企稳，判断成长风格压力是否缓解。")
    if not watch:
        watch.append("• 下周风险灯是否从绿色升级为黄色或红色。")

    data_lines: list[str] = []
    index_parts = [
        f"{label} {_format_decimal(compounded[label], signed=True)}%"
        for label in ("道指", "标普500", "纳指")
        if label in compounded
    ]
    if index_parts:
        data_lines.append("美股累计｜" + " · ".join(index_parts))
    asset_parts = [
        f"{label} {_format_decimal(compounded_assets[label], signed=True)}%"
        for label in ("美元", "黄金", "白银", "WTI", "BTC")
        if label in compounded_assets
    ]
    if asset_parts:
        data_lines.append("其他累计｜" + " · ".join(asset_parts))
    if two_year_moves or ten_year_moves:
        data_lines.append(
            f"利率日变累计｜2Y {_format_decimal(rate_2y, signed=True)}bp · "
            f"10Y {_format_decimal(rate_10y, signed=True)}bp"
        )
    data_lines.append(
        f"风险灯｜🟢 {risk_counts['green']} · 🟡 {risk_counts['yellow']} · 🔴 {risk_counts['red']}"
    )
    if not index_parts and not asset_parts:
        data_lines.insert(0, "市场累计｜暂无足够可解析数据")

    if dated_reports:
        range_text = f"{dated_reports[0][0]} 至 {dated_reports[-1][0]}"
    else:
        range_text = "本周暂无晨报记录"
    action_text = "\n".join(actions)
    watch_text = "\n".join(watch)
    data_text = "\n".join(data_lines)
    return (
        f"🌐 海外市场周报｜{report_date}\n"
        "一周变化 · 下周动作 · 不需要真实持仓\n"
        f"{_DIVIDER}\n\n"
        "🧭 一周结论\n"
        f"{conclusion}\n\n"
        "✅ 下周怎么做\n"
        f"{action_text}\n\n"
        "👀 下周盯住\n"
        f"{watch_text}\n\n"
        f"{_DIVIDER}\n\n"
        "📊 一周数据\n"
        f"{data_text}\n\n"
        "ℹ️ 统计说明\n"
        f"范围｜{range_text} · {observations} 份有效晨报\n"
        "算法｜日涨跌幅按复利累计；利率为各日报告日变动之和\n"
        "隐私｜只使用公开市场数据，不记录真实持仓、账户或资金规模\n\n"
        "价格关系用于辅助判断，不构成投资建议。"
    )
