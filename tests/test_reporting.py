from decimal import Decimal
import unittest

from mini_hermes.overseas_market import (
    CrossAssetQuote,
    OverseasMarketSnapshot,
    TreasuryCurve,
)
from mini_hermes.reporting import (
    A_SHARE_TERMS,
    analyze_indices,
    compose_global_report,
    infer_report_stance,
)
from mini_hermes.us_market import UsIndexQuote


FULL_UP_QUOTES = (
    UsIndexQuote("int_dji", "道琼斯工业指数", "46247.29", "299.97", "0.65"),
    UsIndexQuote("int_nasdaq", "纳斯达克综合指数", "22484.07", "99.37", "0.44"),
    UsIndexQuote("int_sp500", "标普500指数", "6643.70", "38.98", "0.59"),
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
    us_indices=FULL_UP_QUOTES,
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
        report = compose_global_report("2026-08-04", FULL_SNAPSHOT)

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
        self.assertIn("等权 +0.56%", report)
        self.assertIn("道指 > 标普500 > 纳指", report)
        self.assertIn("分化｜0.21pp", report)
        self.assertIn("前收 45,947.32", report)
        self.assertIn("波动｜VIX 15.86", report)
        self.assertIn("2Y 4.25%（-3bp）", report)
        self.assertIn("10Y 4.70%（-5bp）", report)
        self.assertIn("10Y-2Y +45bp", report)
        self.assertIn("美元｜DXY 100.02", report)
        self.assertIn("黄金｜COMEX 4,110.39", report)
        self.assertIn("原油｜WTI 81.21", report)
        self.assertIn("加密｜BTC $63,816.70", report)
        self.assertIn("跨资产 6/6", report)
        self.assertIn("时间（各源时区）｜VIX 08-03 16:15 · 美债 08-03 · DXY 08-04 12:22", report)
        self.assertNotIn("缺失｜", report)
        self.assertNotIn("#", report)
        self.assertNotIn("_以上", report)
        self.assertLess(len(report), 4000)

    def test_report_preserves_source_index_digits_and_excludes_a_share_terms(self) -> None:
        report = compose_global_report("2026-08-04", FULL_SNAPSHOT)

        self.assertIn("22,484.07", report)
        self.assertNotIn("22,446.07", report)
        for term in A_SHARE_TERMS:
            self.assertNotIn(term, report)

    def test_partial_snapshot_degrades_without_broad_claims(self) -> None:
        snapshot = OverseasMarketSnapshot(
            us_indices=(FULL_UP_QUOTES[1],),
            assets=(),
            treasury=None,
        )
        report = compose_global_report("2026-08-04", snapshot)

        self.assertIn("美股指数数据仅取得 1/3", report)
        self.assertIn("已取得均值（1/3）", report)
        self.assertNotIn("三指数等权平均", report)
        self.assertIn("🌍 跨资产温度计\n暂时不可用", report)
        self.assertIn("跨资产 0/6", report)
        self.assertIn("缺失｜VIX · 美债 · DXY · 黄金 · WTI · BTC", report)
        self.assertNotIn("美股三大指数同涨", report)

    def test_index_display_groups_thousands_without_changing_source_precision(self) -> None:
        precise = OverseasMarketSnapshot(
            us_indices=(
                UsIndexQuote(
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
        report = compose_global_report("2026-08-04", precise)

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
        report = compose_global_report("2026-08-04", snapshot)

        self.assertIn("本次方向一项持平", report)
        self.assertNotIn("本次方向反向", report)

    def test_invalid_duplicate_and_conflicting_indices_are_rejected(self) -> None:
        conflicting = UsIndexQuote("int_dji", "Dow", "101", "1", "-0.99")
        unknown = UsIndexQuote("unknown", "Unknown", "101", "1", "0.99")
        stats = analyze_indices((conflicting, unknown, FULL_UP_QUOTES[1], FULL_UP_QUOTES[1]))

        self.assertEqual(len(stats.quotes), 1)
        self.assertEqual(len(stats.anomalies), 3)
        self.assertFalse(stats.complete)

    def test_rounded_zero_percent_does_not_reject_a_small_point_change(self) -> None:
        rounded = UsIndexQuote("int_dji", "Dow", "50000.01", "0.01", "0.00")
        stats = analyze_indices((rounded,))

        self.assertEqual(stats.quotes, (rounded,))
        self.assertEqual(stats.anomalies, ())
        self.assertEqual(stats.up, 1)
        self.assertEqual(stats.flat, 0)

    def test_headline_handles_ties_without_inventing_one_leader(self) -> None:
        tied = OverseasMarketSnapshot(
            us_indices=(
                UsIndexQuote("int_dji", "Dow", "100.50", "0.50", "0.50"),
                UsIndexQuote("int_sp500", "S&P", "200.00", "1.00", "0.50"),
                UsIndexQuote("int_nasdaq", "Nasdaq", "100.20", "0.20", "0.20"),
            ),
            assets=(),
            treasury=None,
        )
        report = compose_global_report("2026-08-04", tied)

        self.assertIn("道指/标普500并列表现居前", report)

        flat = OverseasMarketSnapshot(
            us_indices=tuple(
                UsIndexQuote(code, name, "100", "0", "0")
                for code, name in (
                    ("int_dji", "Dow"),
                    ("int_sp500", "S&P"),
                    ("int_nasdaq", "Nasdaq"),
                )
            ),
            assets=(),
            treasury=None,
        )
        flat_report = compose_global_report("2026-08-04", flat)
        self.assertIn("美股三大指数持平", flat_report)
        self.assertNotIn("表现居前", flat_report)

    def test_empty_snapshot_still_renders_clear_report(self) -> None:
        empty = OverseasMarketSnapshot(us_indices=(), assets=(), treasury=None)
        report = compose_global_report("2026-08-04", empty)

        self.assertIn("关键市场数据暂不可用", report)
        self.assertIn("📈 美股收盘\n暂时不可用", report)
        self.assertIn("本次无可用来源", report)
        self.assertIn("标题为生成日期", report)

    def test_legacy_quote_sequence_and_stance_remain_supported(self) -> None:
        self.assertEqual(infer_report_stance(FULL_UP_QUOTES), "RISK_ON")
        report = compose_global_report("2026-08-04", FULL_UP_QUOTES)

        self.assertIn("🌐 海外市场晨报", report)
        self.assertIn("跨资产 0/6", report)


if __name__ == "__main__":
    unittest.main()
