from datetime import UTC, date, datetime
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from mini_hermes.market_context import MarketContext, MarketEvent
from mini_hermes.overseas_market import (
    CrossAssetQuote,
    OverseasMarketSnapshot,
    RegionalIndexQuote,
    TreasuryCurve,
)
from mini_hermes.reporting import (
    A_SHARE_TERMS,
    analyze_indices,
    compose_global_report,
    compose_global_report_bundle,
    compose_weekly_report,
    expected_us_trade_date,
    infer_report_stance,
)
from mini_hermes.us_market import UsIndexQuote


TRADE_DATE = date(2026, 8, 5)


def us_quote(
    code: str,
    name: str,
    price: str,
    change: str,
    percent: str,
    trade_date: date = TRADE_DATE,
    observed_at: datetime | None = None,
    *,
    open_price: str | None = None,
    high: str | None = None,
    low: str | None = None,
    week52_high: str | None = None,
    week52_low: str | None = None,
) -> UsIndexQuote:
    market_timestamp = observed_at or datetime(
        trade_date.year,
        trade_date.month,
        trade_date.day,
        17,
        15,
        tzinfo=ZoneInfo("America/New_York"),
    )
    return UsIndexQuote(
        code=code,
        name=name,
        price=price,
        change=change,
        percent=percent,
        as_of="2026-08-06 05:15:59",
        observed_at=market_timestamp,
        open=open_price,
        high=high,
        low=low,
        week52_high=week52_high,
        week52_low=week52_low,
    )


FRESH_QUOTES = (
    us_quote(
        "int_dji",
        "道琼斯工业指数",
        "54349.1211",
        "263.2400",
        "0.49",
        open_price="54266.1211",
        high="54744.3281",
        low="54000.0000",
        week52_high="54744.3281",
        week52_low="43799.1992",
    ),
    us_quote("int_nasdaq", "纳斯达克综合指数", "26363.4393", "-221.5537", "-0.83"),
    us_quote("int_sp500", "标普500指数", "7723.5498", "-12.9700", "-0.17"),
)


def asset(
    code: str,
    name: str,
    price: str,
    change: str,
    percent: str,
    as_of: str,
    source: str,
    unit: str = "index",
) -> CrossAssetQuote:
    return CrossAssetQuote(
        code=code,
        name=name,
        price=Decimal(price),
        change=Decimal(change),
        percent=Decimal(percent),
        high=Decimal(price),
        low=Decimal(price),
        as_of=as_of,
        unit=unit,
        source=source,
    )


FULL_SNAPSHOT = OverseasMarketSnapshot(
    us_indices=FRESH_QUOTES,
    assets=(
        asset("VIX", "Cboe波动率指数", "15.86", "-0.13", "-0.81", "2026-08-05T16:15:01", "Cboe"),
        asset("DINIW", "美元指数", "100.0195", "0.0658", "0.0658", "2026-08-06 05:22:54", "Sina Finance"),
        asset("hf_GC", "COMEX黄金", "4110.39", "20.01", "0.49", "2026-08-06 05:20:00", "Sina Finance", "USD/oz"),
        asset("hf_SI", "COMEX白银", "48.50", "0.40", "0.83", "2026-08-06 05:20:00", "Sina Finance", "USD/oz"),
        asset("hf_CL", "WTI原油", "81.21", "0.87", "1.08", "2026-08-06 05:20:00", "Sina Finance", "USD/barrel"),
        asset("BTC-USD", "比特币", "63816.70", "816.70", "1.30", "2026-08-06T00:20:00Z", "Coinbase", "USD"),
    ),
    treasury=TreasuryCurve(
        date="2026-08-03",
        previous_date="2026-07-31",
        two_year=Decimal("4.25"),
        ten_year=Decimal("4.70"),
        two_year_change_bp=Decimal("-3"),
        ten_year_change_bp=Decimal("-5"),
        spread_bp=Decimal("45"),
        three_month=Decimal("4.10"),
        thirty_year=Decimal("4.90"),
        three_month_change_bp=Decimal("-1"),
        thirty_year_change_bp=Decimal("-4"),
    ),
    regional_indices=(
        RegionalIndexQuote("int_nikkei", "日经指数", Decimal("44946.64"), Decimal("-408.35"), Decimal("-0.90")),
        RegionalIndexQuote("int_hangseng", "恒生指数", Decimal("25120.11"), Decimal("180.22"), Decimal("0.72")),
        RegionalIndexQuote("int_ftse", "伦敦指数", Decimal("9284.83"), Decimal("70.85"), Decimal("0.77")),
    ),
)


class ReportingTests(unittest.TestCase):
    def test_brief_report_has_four_plain_language_sections(self) -> None:
        bundle = compose_global_report_bundle("2026-08-06", FULL_SNAPSHOT)

        for heading in (
            "🌙 昨晚发生了什么",
            "🇨🇳 对今天 A 股可能有什么影响",
            "👀 今天建议观察什么",
            "✅ 是否需要行动",
        ):
            self.assertEqual(bundle.brief.count(heading), 1)
        self.assertIn("A股全天仍以国内政策、资金和成交为主", bundle.brief)
        self.assertIn("不需要额外行动", bundle.brief)
        self.assertNotIn("📌 关键数据", bundle.brief)
        self.assertNotIn("ℹ️ 数据质量", bundle.brief)
        self.assertIn("📌 关键数据", bundle.full)
        self.assertIn("ℹ️ 数据质量", bundle.full)
        self.assertEqual(bundle.report_date, "2026-08-06")
        self.assertLess(len(bundle.brief), 2000)

    def test_brief_report_turns_macro_pressure_into_cautious_a_share_context(self) -> None:
        pressured = OverseasMarketSnapshot(
            us_indices=FRESH_QUOTES,
            assets=(
                asset("VIX", "Cboe波动率指数", "22", "3", "15", "2026-08-05T16:15:01", "Cboe"),
                asset("DINIW", "美元指数", "101", "0.5", "0.5", "2026-08-06 05:22:54", "Sina Finance"),
            ),
            treasury=TreasuryCurve(
                date="2026-08-05",
                previous_date="2026-08-04",
                two_year=Decimal("4.40"),
                ten_year=Decimal("4.80"),
                two_year_change_bp=Decimal("12"),
                ten_year_change_bp=Decimal("10"),
                spread_bp=Decimal("40"),
            ),
        )

        brief = compose_global_report_bundle("2026-08-06", pressured).brief

        self.assertIn("高估值科技和成长开盘可能相对吃亏", brief)
        self.assertIn("暂不急着交易", brief)
        self.assertNotIn("真实持仓", brief)

    def test_brief_watch_handles_flat_rates_and_nasdaq_lead(self) -> None:
        snapshot = OverseasMarketSnapshot(
            us_indices=(
                us_quote("int_dji", "Dow", "100.10", "0.10", "0.10"),
                us_quote("int_nasdaq", "Nasdaq", "100.80", "0.80", "0.80"),
                us_quote("int_sp500", "S&P 500", "100.20", "0.20", "0.20"),
            ),
            assets=(),
            treasury=TreasuryCurve(
                date="2026-08-05",
                previous_date="2026-08-04",
                two_year=Decimal("4.20"),
                ten_year=Decimal("4.60"),
                two_year_change_bp=Decimal("0"),
                ten_year_change_bp=Decimal("0"),
                spread_bp=Decimal("40"),
            ),
        )

        brief = compose_global_report_bundle("2026-08-06", snapshot).brief

        self.assertIn("接下来向上还是向下", brief)
        self.assertIn("纳指相对道指能否保持领先", brief)
        self.assertNotIn("纳指相对道指是企稳，还是继续走弱", brief)

    def test_weekly_report_compounds_daily_moves_and_keeps_privacy(self) -> None:
        reports = (
            (
                "2026-08-25",
                "🟢 常态｜未触发重要风险条件。\n"
                "• 利率｜2Y 4.20%（+5bp） · 10Y 4.60%（+4bp）\n"
                "• 美元｜DXY 100.00（+0.40%）\n"
                "美股｜道指 +1.00% · 标普500 +0.50% · 纳指 -0.50%\n"
                "其他｜黄金 $4,100.00 -0.50% · BTC $70,000.00 +1.00%",
            ),
            (
                "2026-08-26",
                "🟡 留意｜美债收益率单日最大变动 10bp。\n"
                "• 利率｜2Y 4.24%（+4bp） · 10Y 4.63%（+3bp）\n"
                "• 美元｜DXY 100.30（+0.30%）\n"
                "美股｜道指 -0.50% · 标普500 +0.50% · 纳指 -1.00%\n"
                "其他｜黄金 $4,080.00 -0.49% · BTC $69,000.00 -1.43%",
            ),
            (
                "2026-08-27",
                "🟢 常态｜未触发重要风险条件。\n"
                "• 利率｜2Y 4.27%（+3bp） · 10Y 4.65%（+2bp）\n"
                "• 美元｜DXY 100.60（+0.30%）\n"
                "美股｜道指 +0.20% · 标普500 +0.20% · 纳指 -0.20%\n"
                "其他｜黄金 $4,090.00 +0.25% · BTC $69,500.00 +0.72%",
            ),
        )

        report = compose_weekly_report("2026-08-29", reports)

        self.assertIn("🌐 海外市场周报｜2026-08-29", report)
        self.assertIn("标普500本周上涨 1.20%", report)
        self.assertIn("有 1 个需要留意的交易日", report)
        self.assertIn("美股累计｜道指 +0.70% · 标普500 +1.20% · 纳指 -1.69%", report)
        self.assertIn("利率日变累计｜2Y +12.00bp · 10Y +9.00bp", report)
        self.assertIn("科技/成长｜本周明显跑输价值风格", report)
        self.assertIn("不记录真实持仓、账户或资金规模", report)
        self.assertLess(len(report), 4000)

    def test_weekly_report_does_not_infer_trend_from_too_few_days(self) -> None:
        report = compose_weekly_report(
            "2026-08-29",
            (("2026-08-28", "🟢 常态\n美股｜标普500 +2.00%"),),
        )

        self.assertIn("只积累了 1 个交易日", report)
        self.assertIn("至少需要 3 个交易日", report)
        self.assertIn("暂不根据本期周报改变长期计划", report)

    def test_full_report_prioritizes_interpretation_actions_and_compact_data(self) -> None:
        report = compose_global_report("2026-08-06", FULL_SNAPSHOT)

        for heading in (
            "🧭 一句话看懂",
            "📰 已核实事件",
            "🎯 如果你关注这些资产",
            "✅ 今天怎么做",
            "👀 今天盯住",
            "📌 关键数据",
            "ℹ️ 数据质量",
        ):
            self.assertIn(heading, report)
        self.assertNotIn("对你的持仓意味着什么", report)
        self.assertIn("🟢 常态", report)
        self.assertIn("官方事件源暂不可用", report)
        self.assertIn("市场总体平稳", report)
        self.assertIn("纳指弱于道指 1.32pp", report)
        self.assertIn("科技/成长｜短期偏逆风", report)
        self.assertIn("风险判断｜指数跌幅与VIX都未显示系统性恐慌", report)
        self.assertIn("美股交易日｜2026-08-05（美东）", report)
        self.assertIn("美股｜道指 +0.49% · 标普500 -0.17% · 纳指 -0.83%", report)
        self.assertIn("波动｜VIX 15.86", report)
        self.assertIn("2Y 4.25%（-3bp）", report)
        self.assertIn("10Y 4.70%（-5bp）", report)
        self.assertIn("美元｜DXY 100.02（+0.07%）", report)
        self.assertIn("黄金 $4,110.39 +0.49%", report)
        self.assertIn("白银 $48.50 +0.83%", report)
        self.assertIn("WTI $81.21 +1.08%", report)
        self.assertIn("BTC $63,816.70 +1.30%", report)
        self.assertIn("亚太欧｜日经 -0.90% · 恒生 +0.72% · 富时100 +0.77%", report)
        self.assertIn("跨资产 7/7", report)
        self.assertIn("亚太欧 3/3", report)
        self.assertNotIn("缺失｜", report)
        self.assertNotIn("日内 ", report)
        self.assertNotIn("52周", report)
        self.assertNotIn("前收", report)
        self.assertNotIn("#", report)
        self.assertNotIn("_以上", report)
        self.assertLess(len(report), 4000)

    def test_report_rounds_noisy_index_digits_and_excludes_a_share_terms(self) -> None:
        report = compose_global_report("2026-08-06", FULL_SNAPSHOT)

        self.assertIn("纳指 -0.83%", report)
        self.assertNotIn("26,363.4393", report)
        for term in A_SHARE_TERMS:
            self.assertNotIn(term, report)

    def test_partial_snapshot_degrades_without_broad_claims(self) -> None:
        snapshot = OverseasMarketSnapshot(
            us_indices=(FRESH_QUOTES[1],),
            assets=(),
            treasury=None,
        )
        report = compose_global_report("2026-08-06", snapshot)

        self.assertIn("美股数据不完整，今天只看已取得价格，不作整体判断", report)
        self.assertIn("已验收｜1/3，仅展示明细，不作整体判断", report)
        self.assertNotIn("已取得均值", report)
        self.assertNotIn("三指数等权平均", report)
        self.assertIn("指数数据不完整，不作相对强弱判断", report)
        self.assertIn("跨资产 0/7", report)
        self.assertIn("亚太欧 0/3", report)
        self.assertIn(
            "缺失｜美股指数（2 项缺失或未通过） · VIX · 美债 · DXY · 黄金 · 白银 · WTI · BTC · 亚太欧（日经/恒生/富时100）",
            report,
        )
        self.assertNotIn("美股三大指数同涨", report)

    def test_index_display_omits_noisy_price_precision(self) -> None:
        precise = OverseasMarketSnapshot(
            us_indices=(
                us_quote(
                    "int_dji",
                    "Dow",
                    "12345.6789",
                    "123.4567",
                    "1.01",
                ),
            ),
            assets=(),
            treasury=None,
        )
        report = compose_global_report("2026-08-06", precise)

        self.assertIn("美股｜道指 +1.01%", report)
        self.assertNotIn("12,345.6789", report)

    def test_flat_asset_is_not_described_as_opposite_direction(self) -> None:
        snapshot = OverseasMarketSnapshot(
            us_indices=(),
            assets=(
                asset("DINIW", "美元指数", "100", "0", "0", "2026-08-06 05:00:00", "Sina Finance"),
                asset("hf_GC", "黄金", "2000", "10", "0.5", "2026-08-06 05:00:00", "Sina Finance"),
            ),
            treasury=None,
        )
        report = compose_global_report("2026-08-06", snapshot)

        self.assertIn("贵金属｜黄金 +0.50%", report)
        self.assertNotIn("美元明显走强", report)

    def test_rate_and_dollar_pressure_produces_conditional_action_plan(self) -> None:
        snapshot = OverseasMarketSnapshot(
            us_indices=FRESH_QUOTES,
            assets=(
                asset("VIX", "波动率", "14.43", "-0.20", "-1.37", "2026-08-05 16:15:00", "Cboe"),
                asset("DINIW", "美元指数", "99.68", "0.55", "0.55", "2026-08-06 05:00:00", "Sina Finance"),
                asset("hf_GC", "黄金", "4503.37", "-160.00", "-3.44", "2026-08-06 05:00:00", "Sina Finance"),
                asset("BTC-USD", "比特币", "77895.79", "-2700", "-3.35", "2026-08-06T00:20:00Z", "Coinbase"),
            ),
            treasury=TreasuryCurve(
                date="2026-08-05",
                previous_date="2026-08-04",
                two_year=Decimal("4.34"),
                ten_year=Decimal("4.73"),
                two_year_change_bp=Decimal("14"),
                ten_year_change_bp=Decimal("6"),
                spread_bp=Decimal("39"),
            ),
        )

        report = compose_global_report("2026-08-06", snapshot)

        self.assertIn("利率更高、美元更强", report)
        self.assertIn("🟡 留意｜美债收益率单日最大变动 14bp", report)
        self.assertIn("不是对具体新闻原因的确认", report)
        self.assertIn("先检查总仓位和止损规则", report)
        self.assertIn("先等美债收益率或美元至少一项回落", report)
        self.assertIn("转弱信号｜若收益率与美元继续上行", report)
        self.assertIn("缓解信号｜若收益率或美元回落", report)
        self.assertNotIn("日内 ", report)

    def test_stale_or_implausible_assets_are_removed_with_clear_reasons(self) -> None:
        snapshot = OverseasMarketSnapshot(
            us_indices=FRESH_QUOTES,
            assets=(
                asset(
                    "BTC-USD",
                    "比特币",
                    "70000",
                    "-1000",
                    "-1.41",
                    "2026-08-05T20:00:00Z",
                    "Coinbase",
                ),
                asset(
                    "DINIW",
                    "美元指数",
                    "100",
                    "1",
                    "-1",
                    "2026-08-06 05:00:00",
                    "Sina Finance",
                ),
                asset(
                    "hf_CL",
                    "WTI原油",
                    "999",
                    "1",
                    "0.10",
                    "2026-08-06 05:00:00",
                    "Sina Finance",
                ),
            ),
            treasury=None,
        )

        report = compose_global_report("2026-08-06", snapshot)

        self.assertIn("比特币 数据已过期，已剔除", report)
        self.assertIn("美元指数 涨跌方向冲突，已剔除", report)
        self.assertIn("WTI原油 价格超出合理范围，已剔除", report)
        self.assertNotIn("BTC $", report)
        self.assertNotIn("WTI $", report)

    def test_verified_event_is_labeled_as_context_not_proven_cause(self) -> None:
        snapshot = OverseasMarketSnapshot(
            us_indices=FULL_SNAPSHOT.us_indices,
            assets=FULL_SNAPSHOT.assets,
            treasury=FULL_SNAPSHOT.treasury,
            regional_indices=FULL_SNAPSHOT.regional_indices,
            market_context=MarketContext(
                events=(
                    MarketEvent(
                        source="BEA",
                        title="美国GDP更新（2nd Quarter 2026）",
                        detail="Q2 2026 +1.5；前值 Q1 2026 +2.1",
                        published_at=datetime(2026, 8, 5, 12, 30, tzinfo=UTC),
                        url="https://www.bea.gov/example",
                    ),
                ),
                available_sources=("Fed", "BEA"),
            ),
        )

        report = compose_global_report("2026-08-06", snapshot)

        self.assertIn("美国经济分析局（BEA） · 08-05｜美国GDP更新", report)
        self.assertIn("同期官方发布，不等于已证明其导致当日涨跌", report)

    def test_available_official_sources_use_plain_chinese_labels(self) -> None:
        snapshot = OverseasMarketSnapshot(
            us_indices=FULL_SNAPSHOT.us_indices,
            assets=FULL_SNAPSHOT.assets,
            treasury=FULL_SNAPSHOT.treasury,
            regional_indices=FULL_SNAPSHOT.regional_indices,
            market_context=MarketContext(available_sources=("Fed", "BEA")),
        )

        report = compose_global_report("2026-08-06", snapshot)

        self.assertIn("美联储（Fed）、美国经济分析局（BEA）", report)

    def test_invalid_duplicate_and_conflicting_indices_are_rejected(self) -> None:
        conflicting = us_quote("int_dji", "Dow", "101", "1", "-0.99")
        unknown = us_quote("unknown", "Unknown", "101", "1", "0.99")
        stats = analyze_indices((conflicting, unknown, FRESH_QUOTES[1], FRESH_QUOTES[1]))

        self.assertEqual(len(stats.quotes), 1)
        self.assertEqual(len(stats.anomalies), 3)
        self.assertFalse(stats.complete)

    def test_rounded_zero_percent_does_not_reject_a_small_point_change(self) -> None:
        rounded = us_quote("int_dji", "Dow", "50000.01", "0.01", "0.00")
        stats = analyze_indices((rounded,))

        self.assertEqual(stats.quotes, (rounded,))
        self.assertEqual(stats.anomalies, ())
        self.assertEqual(stats.up, 1)
        self.assertEqual(stats.flat, 0)

    def test_summary_compares_growth_with_value_without_inventing_a_leader(self) -> None:
        tied = OverseasMarketSnapshot(
            us_indices=(
                us_quote("int_dji", "Dow", "100.50", "0.50", "0.50"),
                us_quote("int_sp500", "S&P", "200.00", "1.00", "0.50"),
                us_quote("int_nasdaq", "Nasdaq", "100.20", "0.20", "0.20"),
            ),
            assets=(),
            treasury=None,
        )
        report = compose_global_report("2026-08-06", tied)

        self.assertIn("纳指弱于道指 0.30pp", report)

        flat = OverseasMarketSnapshot(
            us_indices=tuple(
                us_quote(code, name, "100", "0", "0")
                for code, name in (
                    ("int_dji", "Dow"),
                    ("int_sp500", "S&P"),
                    ("int_nasdaq", "Nasdaq"),
                )
            ),
            assets=(),
            treasury=None,
        )
        flat_report = compose_global_report("2026-08-06", flat)
        self.assertIn("市场总体平稳", flat_report)
        self.assertNotIn("相对吃亏", flat_report)

    def test_empty_snapshot_still_renders_clear_report(self) -> None:
        empty = OverseasMarketSnapshot(us_indices=(), assets=(), treasury=None)
        report = compose_global_report("2026-08-06", empty)

        self.assertIn("美股数据暂不可用", report)
        self.assertIn("📌 关键数据\n关键数据暂不可用", report)
        self.assertIn("本次无可用来源", report)

    def test_stale_complete_indices_fail_closed_without_market_claims(self) -> None:
        stale = tuple(
            us_quote(
                quote.code,
                quote.name,
                quote.price,
                quote.change,
                quote.percent,
                trade_date=date(2026, 8, 4),
            )
            for quote in FRESH_QUOTES
        )
        report = compose_global_report(
            "2026-08-06",
            OverseasMarketSnapshot(us_indices=stale, assets=(), treasury=None),
        )

        self.assertIn("完整度｜美股 0/3", report)
        self.assertIn("期望交易日｜2026-08-05（美东）", report)
        self.assertIn("交易日 2026-08-04，预期 2026-08-05", report)
        self.assertIn("美股数据暂不可用", report)

    def test_partial_freshness_never_renders_aggregate_index_conclusions(self) -> None:
        mixed = (
            FRESH_QUOTES[0],
            us_quote(
                FRESH_QUOTES[1].code,
                FRESH_QUOTES[1].name,
                FRESH_QUOTES[1].price,
                FRESH_QUOTES[1].change,
                FRESH_QUOTES[1].percent,
                trade_date=date(2026, 8, 4),
            ),
            us_quote(
                FRESH_QUOTES[2].code,
                FRESH_QUOTES[2].name,
                FRESH_QUOTES[2].price,
                FRESH_QUOTES[2].change,
                FRESH_QUOTES[2].percent,
                trade_date=date(2026, 8, 4),
            ),
        )
        report = compose_global_report(
            "2026-08-06",
            OverseasMarketSnapshot(us_indices=mixed, assets=(), treasury=None),
        )

        self.assertIn("已验收｜1/3，仅展示明细，不作整体判断", report)
        self.assertIn("美股数据不完整", report)
        self.assertNotIn("市场温和", report)

    def test_same_session_intraday_or_future_timestamp_fails_closed(self) -> None:
        new_york = ZoneInfo("America/New_York")
        intraday = tuple(
            us_quote(
                quote.code,
                quote.name,
                quote.price,
                quote.change,
                quote.percent,
                observed_at=datetime(2026, 8, 5, 15, 30, tzinfo=new_york),
            )
            for quote in FRESH_QUOTES
        )
        generated_at = datetime(
            2026,
            8,
            6,
            8,
            30,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        intraday_report = compose_global_report(
            "2026-08-06",
            OverseasMarketSnapshot(us_indices=intraday, assets=(), treasury=None),
            generated_at=generated_at,
        )
        self.assertIn("尚未到常规收盘", intraday_report)
        self.assertIn("完整度｜美股 0/3", intraday_report)

        future = tuple(
            us_quote(
                quote.code,
                quote.name,
                quote.price,
                quote.change,
                quote.percent,
                observed_at=datetime(2026, 8, 5, 21, 0, tzinfo=new_york),
            )
            for quote in FRESH_QUOTES
        )
        future_report = compose_global_report(
            "2026-08-06",
            OverseasMarketSnapshot(us_indices=future, assets=(), treasury=None),
            generated_at=generated_at,
        )
        self.assertIn("时间戳晚于报告生成时间", future_report)
        self.assertIn("完整度｜美股 0/3", future_report)

    def test_expected_trade_date_handles_weekends_holidays_and_new_year_rule(self) -> None:
        self.assertEqual(expected_us_trade_date("2026-08-06"), date(2026, 8, 5))
        self.assertEqual(expected_us_trade_date("2026-12-26"), date(2026, 12, 24))
        self.assertEqual(expected_us_trade_date("2026-04-04"), date(2026, 4, 2))
        self.assertEqual(expected_us_trade_date("2028-01-01"), date(2027, 12, 31))

    def test_expected_trade_date_rejects_an_in_progress_us_session(self) -> None:
        shanghai = ZoneInfo("Asia/Shanghai")
        during_us_session = datetime(2026, 8, 6, 0, 30, tzinfo=shanghai)
        after_source_settlement = datetime(2026, 8, 6, 8, 30, tzinfo=shanghai)

        self.assertEqual(
            expected_us_trade_date("2026-08-06", generated_at=during_us_session),
            date(2026, 8, 4),
        )
        self.assertEqual(
            expected_us_trade_date("2026-08-06", generated_at=after_source_settlement),
            date(2026, 8, 5),
        )

    def test_legacy_quote_sequence_and_stance_remain_supported(self) -> None:
        self.assertEqual(infer_report_stance(FRESH_QUOTES), "MIXED")
        report = compose_global_report("2026-08-06", FRESH_QUOTES)

        self.assertIn("🌐 海外市场晨报", report)
        self.assertIn("跨资产 0/7", report)


if __name__ == "__main__":
    unittest.main()
