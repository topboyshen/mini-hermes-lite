from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from itertools import groupby
from typing import Sequence
from zoneinfo import ZoneInfo

from mini_hermes.overseas_market import (
    CrossAssetQuote,
    OverseasMarketSnapshot,
)
from mini_hermes.us_market import UsIndexQuote


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
_DISPLAY_ORDER = {"int_dji": 0, "int_sp500": 1, "int_nasdaq": 2}
_SHORT_NAMES = {
    "int_dji": "道指",
    "int_nasdaq": "纳指",
    "int_sp500": "标普500",
}
_TWO_PLACES = Decimal("0.01")
_EXPECTED_ASSET_CODES = frozenset({"VIX", "DINIW", "hf_GC", "hf_CL", "BTC-USD"})
_DIVIDER = "────────────"


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


def render_index_snapshot(stats: IndexStats) -> str:
    if not stats.quotes:
        return "暂时不可用"
    return "\n".join(
        f"{_SHORT_NAMES[quote.code]}｜"
        f"{_group_source_number(quote.price)}｜"
        f"{_group_source_number(quote.change, signed=True)}｜"
        f"{_group_source_number(quote.percent, signed=True)}%"
        f"（前收 {_format_market_number(_previous_close(quote), grouped=True)}）"
        for quote in stats.quotes
    )


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
        lines.append(
            f"黄金｜COMEX {_format_market_number(gold.price, grouped=True)} 美元/盎司｜"
            f"{_asset_percent(gold)}"
        )

    crude = assets.get("hf_CL")
    if crude is not None:
        lines.append(
            f"原油｜WTI {_format_market_number(crude.price)} 美元/桶｜{_asset_percent(crude)}"
        )

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
    bitcoin = assets.get("BTC-USD")
    movement_parts: list[str] = []
    if crude is not None and crude.percent is not None:
        movement_parts.append(
            f"WTI {_movement(crude.percent)} {_format_market_number(abs(crude.percent))}%"
        )
    if bitcoin is not None and bitcoin.percent is not None:
        movement_parts.append(
            f"BTC 24h {_movement(bitcoin.percent)} {_format_market_number(abs(bitcoin.percent))}%"
        )
    if movement_parts:
        lines.append(f"• {'；'.join(movement_parts)}。")

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

    return "\n".join(lines) if lines else "关键数据不足，系统观察条件暂不判定。"


def _data_notes(stats: IndexStats, snapshot: OverseasMarketSnapshot) -> str:
    assets = _asset_map(snapshot)
    available = len(assets) + (1 if snapshot.treasury is not None else 0)
    sources: list[str] = []
    sina_items: list[str] = []
    if stats.quotes:
        sina_items.append("指数")
    if "DINIW" in assets:
        sina_items.append("DXY")
    if "hf_GC" in assets:
        sina_items.append("黄金")
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
        ("hf_CL", "WTI"),
        ("BTC-USD", "BTC"),
    ):
        quote = assets.get(code)
        if quote is not None:
            times.append(f"{label} {_compact_as_of(quote.as_of)}")
    if snapshot.treasury is not None:
        times.insert(1, f"美债 {_compact_as_of(snapshot.treasury.date)}")

    lines = [
        f"完整度｜美股 {len(stats.quotes)}/3 · 跨资产 {available}/6",
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
        ("hf_CL", "WTI"),
        ("BTC-USD", "BTC"),
    ):
        if code not in assets:
            missing.append(label)
    if missing:
        lines.append(f"缺失｜{' · '.join(missing)}（本次未取得）")
    lines.append(f"来源｜{source_text}")
    for index in range(0, len(times), 3):
        lines.append(f"时间（各源时区）｜{' · '.join(times[index:index + 3])}")
    lines.extend(
        (
            "口径｜黄金/WTI较前结算价；BTC较24h开盘价",
            "说明｜标题为生成日期；美股仅接受预期最近交易日的收盘值",
        )
    )
    return "\n".join(lines)


def _as_snapshot(
    data: OverseasMarketSnapshot | Sequence[UsIndexQuote],
) -> OverseasMarketSnapshot:
    if isinstance(data, OverseasMarketSnapshot):
        return data
    return OverseasMarketSnapshot(us_indices=tuple(data), assets=(), treasury=None)


def compose_global_report(
    report_date: str,
    data: OverseasMarketSnapshot | Sequence[UsIndexQuote],
    *,
    generated_at: datetime | None = None,
) -> str:
    snapshot = _as_snapshot(data)
    expected_trade_day = expected_us_trade_date(
        report_date,
        generated_at=generated_at,
    )
    stats = analyze_indices(
        snapshot.us_indices,
        expected_trade_date=expected_trade_day,
        generated_at=generated_at,
    )
    quote_block = render_index_snapshot(stats)
    temperature = render_index_temperature(stats)
    index_analysis = render_index_observations(stats)
    cross_assets = render_cross_asset_snapshot(snapshot)
    cross_analysis = render_cross_asset_observations(snapshot)
    observation_rules = render_observation_rules(stats, snapshot)
    data_notes = _data_notes(stats, snapshot)
    return (
        f"🌐 海外市场晨报｜{report_date}\n"
        "美股收盘 · 跨资产快照\n"
        f"{_DIVIDER}\n\n"
        "🧭 核心结论\n"
        f"{_headline(stats, snapshot)}\n\n"
        "📈 美股收盘\n"
        f"{quote_block}\n\n"
        "📊 盘面温度\n"
        f"{temperature}\n\n"
        f"{_DIVIDER}\n\n"
        "🌍 跨资产温度计\n"
        f"{cross_assets}\n\n"
        f"{_DIVIDER}\n\n"
        "🔎 结构观察\n"
        f"{index_analysis}\n{cross_analysis}\n\n"
        "👀 今日观察\n"
        f"{observation_rules}\n\n"
        f"{_DIVIDER}\n\n"
        "ℹ️ 数据说明\n"
        f"{data_notes}\n\n"
        "监测阈值不是预测或交易信号。\n"
        "仅供研究参考，不构成投资建议。"
    )
