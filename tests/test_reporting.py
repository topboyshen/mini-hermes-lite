from datetime import date, datetime
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from mini_hermes.overseas_market import (
    CrossAssetQuote,
    OverseasMarketSnapshot,
    TreasuryCurve,
)
from mini_hermes.reporting import (
    A_SHARE_TERMS,
    analyze_indices,
    compose_global_report,
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
    )


FRESH_QUOTES = (
    us_quote("int_dji", "道琼斯工业指数", "54349.1211", "263.2400", "0.49"),
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
        asset("VIX", "Cboe波动率指数", "15.86", "-0.13", "-0.81", "2026-08-03T16:15:01", "Cboe"),
        asset("DINIW", "美元指数", "100.0195", "0.0658", "0.0658", "2026-08-04 12:22:54", "Sina Finance"),
        asset("hf_GC", "COMEX黄金", "4110.39", "20.01", "0.49", "2026-08-04 12:20:00", "Sina Finance", "USD/oz"),
        asset("hf_CL", "WTI原油", "81.21", "0.87", "1.08", "2026-08-04 12:20:00", "Sina Finance", "USD/barrel"),
        asset("BTC-USD", "比特币", "63816.70", "816.70", "1.30", "2026-08-04T04:20:00Z", "Coinbase", "USD"),
    ),
    treasury=TreasuryCurve(
        date="2026-08-03",
        previous_date="2026-07-31",
        two_year=Decimal("4.25"),
        ten_year=Decimal("4.70"),
        two_year_change_bp=Decimal("-3"),
        ten_year_change_bp=Decimal("-5"),
        spread_bp=Decimal("45"),
    ),
)


class ReportingTests(unittest.TestCase):
    def test_full_report_contains_market_structure_and_cross_assets(self) -> None:
        report = compose_global_report("2026-08-06", FULL_SNAPSHOT)

        for heading in (
            "🧭 核心结论",
            "📈 美股收盘",
            "📊 盘面温度",
            "🌍 跨资产温度计",
            "🔎 结构观察",
            "👀 今日观察",
            "ℹ️ 数据说明",
        ):
            self.assertIn(heading, report)
        self.assertIn("等权 -0.17%", report)
        self.assertIn("3/3 · 交易日校验通过", report)
        self.assertIn("交易日｜2026-08-05（美东）", report)
        self.assertIn("道指 > 标普500 > 纳指", report)
        self.assertIn("分化｜1.32pp", report)
        self.assertIn("前收 54,085.88", report)
        self.assertIn("波动｜VIX 15.86", report)
        self.assertIn("2Y 4.25%（-3bp）", report)
        self.assertIn("10Y 4.70%（-5bp）", report)
        self.assertIn("10Y-2Y +45bp", report)
        self.assertIn("美元｜DXY 100.02", report)
        self.assertIn("黄金｜COMEX 4,110.39", report)
        self.assertIn("原油｜WTI 81.21", report)
        self.assertIn("加密｜BTC $63,816.70", report)
        self.assertIn("跨资产 6/6", report)
        self.assertIn(
            "时间（各源时区）｜美股 2026-08-05 收盘 · 美债 08-03 · VIX 08-03 16:15",
            report,
        )
        self.assertNotIn("缺失｜", report)
        self.assertNotIn("#", report)
        self.assertNotIn("_以上", report)
        self.assertLess(len(report), 4000)

    def test_report_preserves_source_index_digits_and_excludes_a_share_terms(self) -> None:
        report = compose_global_report("2026-08-06", FULL_SNAPSHOT)

        self.assertIn("26,363.4393", report)
        self.assertNotIn("26,336.4393", report)
        for term in A_SHARE_TERMS:
            self.assertNotIn(term, report)

    def test_partial_snapshot_degrades_without_broad_claims(self) -> None:
        snapshot = OverseasMarketSnapshot(
            us_indices=(FRESH_QUOTES[1],),
            assets=(),
            treasury=None,
        )
        report = compose_global_report("2026-08-06", snapshot)

        self.assertIn("美股指数数据仅取得 1/3", report)
        self.assertIn("已验收｜1/3，仅展示明细，不作整体判断", report)
        self.assertNotIn("已取得均值", report)
        self.assertNotIn("三指数等权平均", report)
        self.assertIn("🌍 跨资产温度计\n暂时不可用", report)
        self.assertIn("跨资产 0/6", report)
        self.assertIn(
            "缺失｜美股指数（2 项缺失或未通过） · VIX · 美债 · DXY · 黄金 · WTI · BTC",
            report,
        )
        self.assertNotIn("美股三大指数同涨", report)

    def test_index_display_groups_thousands_without_changing_source_precision(self) -> None:
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

        self.assertIn("12,345.6789｜+123.4567｜+1.01%", report)

    def test_flat_asset_is_not_described_as_opposite_direction(self) -> None:
        snapshot = OverseasMarketSnapshot(
            us_indices=(),
            assets=(
                asset("DINIW", "美元指数", "100", "0", "0", "today", "Sina Finance"),
                asset("hf_GC", "黄金", "2000", "10", "0.5", "today", "Sina Finance"),
            ),
            treasury=None,
        )
        report = compose_global_report("2026-08-06", snapshot)

        self.assertIn("本次方向一项持平", report)
        self.assertNotIn("本次方向反向", report)

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

    def test_headline_handles_ties_without_inventing_one_leader(self) -> None:
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

        self.assertIn("道指/标普500并列表现居前", report)

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
        self.assertIn("美股三大指数持平", flat_report)
        self.assertNotIn("表现居前", flat_report)

    def test_empty_snapshot_still_renders_clear_report(self) -> None:
        empty = OverseasMarketSnapshot(us_indices=(), assets=(), treasury=None)
        report = compose_global_report("2026-08-06", empty)

        self.assertIn("关键市场数据暂不可用", report)
        self.assertIn("📈 美股收盘\n暂时不可用", report)
        self.assertIn("本次无可用来源", report)
        self.assertIn("标题为生成日期", report)

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

        self.assertIn("0/3 · 数据不完整或已过期", report)
        self.assertIn("期望交易日｜2026-08-05（美东）", report)
        self.assertIn("交易日 2026-08-04，预期 2026-08-05", report)
        self.assertNotIn("美股三大指数同涨", report)
        self.assertNotIn("排序｜", report)
        self.assertNotIn("指数分化｜", report)

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
        self.assertNotIn("涨跌｜", report)
        self.assertNotIn("已取得均值", report)
        self.assertNotIn("排序｜", report)
        self.assertNotIn("指数分化｜", report)

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
        self.assertIn("0/3 · 数据不完整或已过期", intraday_report)

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
        self.assertIn("0/3 · 数据不完整或已过期", future_report)

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
        self.assertIn("跨资产 0/6", report)


if __name__ == "__main__":
    unittest.main()
