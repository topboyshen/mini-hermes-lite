from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx

from mini_hermes import overseas_market
from mini_hermes.market_context import MarketContext
from mini_hermes.overseas_market import (
    CrossAssetQuote,
    RegionalIndexQuote,
    TreasuryCurve,
    fetch_overseas_snapshot,
    parse_cboe_vix,
    parse_coinbase_btc,
    parse_sina_dollar_quote,
    parse_sina_hf_quotes,
    parse_sina_regional_quotes,
    parse_treasury_curve,
)
from mini_hermes.us_market import UsIndexQuote


SINA_HF_RAW = (
    'var hq_str_hf_GC="2450.50,x,x,x,2470.00,2420.00,05:15:00,'
    '2400.00,2410.00,x,x,x,2026-08-04,COMEX黄金";\n'
    'var hq_str_hf_CL="75.25,x,x,x,76.50,73.80,05:14:00,'
    '74.00,74.50,x,x,x,2026-08-04,WTI原油";\n'
    'var hq_str_hf_SI="48.50,x,x,x,49.10,47.80,05:16:00,'
    '48.00,48.10,x,x,x,2026-08-04,COMEX白银";'
)

SINA_REGIONAL_RAW = (
    'var hq_str_int_nikkei="日经指数,44946.64,-408.35,-0.90";\n'
    'var hq_str_int_hangseng="恒生指数,25120.11,180.22,0.72";\n'
    'var hq_str_int_ftse="伦敦指数,9284.83,70.85,0.77";'
)

SINA_DOLLAR_RAW = (
    'var hq_str_DINIW="12:22:54,103.25,x,102.50,x,102.80,103.40,102.20,'
    'x,美元指数,2026-08-04";'
)

TREASURY_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
  <entry><content><m:properties>
    <d:NEW_DATE>2026-08-01T00:00:00</d:NEW_DATE>
    <d:BC_3MONTH>3.90</d:BC_3MONTH><d:BC_2YEAR>3.99</d:BC_2YEAR>
    <d:BC_10YEAR>4.23</d:BC_10YEAR><d:BC_30YEAR>4.50</d:BC_30YEAR>
  </m:properties></content></entry>
  <entry><content><m:properties>
    <d:NEW_DATE>2026-08-03T00:00:00</d:NEW_DATE>
    <d:BC_3MONTH>3.91</d:BC_3MONTH><d:BC_2YEAR>4.00</d:BC_2YEAR>
    <d:BC_10YEAR>4.24</d:BC_10YEAR><d:BC_30YEAR>4.52</d:BC_30YEAR>
  </m:properties></content></entry>
  <entry><content><m:properties>
    <d:NEW_DATE>2026-08-04T00:00:00</d:NEW_DATE>
    <d:BC_3MONTH>3.93</d:BC_3MONTH><d:BC_2YEAR>4.02</d:BC_2YEAR>
    <d:BC_10YEAR>4.25</d:BC_10YEAR><d:BC_30YEAR>4.55</d:BC_30YEAR>
  </m:properties></content></entry>
</feed>"""


class OverseasParserTests(unittest.TestCase):
    def test_sina_futures_positions_and_decimal_calculations(self) -> None:
        gold, crude, silver = parse_sina_hf_quotes(SINA_HF_RAW)

        self.assertEqual(gold.code, "hf_GC")
        self.assertEqual(gold.price, Decimal("2450.50"))
        self.assertEqual(str(gold.price), "2450.50")
        self.assertEqual(gold.change, Decimal("50.50"))
        self.assertEqual(
            gold.percent,
            Decimal("50.50") / Decimal("2400.00") * Decimal("100"),
        )
        self.assertEqual(gold.open, Decimal("2410.00"))
        self.assertEqual(gold.high, Decimal("2470.00"))
        self.assertEqual(gold.low, Decimal("2420.00"))
        self.assertEqual(gold.as_of, "2026-08-04 05:15:00")
        self.assertEqual(crude.change, Decimal("1.25"))
        self.assertEqual(silver.code, "hf_SI")
        self.assertEqual(silver.price, Decimal("48.50"))
        self.assertEqual(silver.change, Decimal("0.50"))

    def test_sina_dollar_positions_and_decimal_calculations(self) -> None:
        quote = parse_sina_dollar_quote(SINA_DOLLAR_RAW)

        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.price, Decimal("103.25"))
        self.assertEqual(quote.change, Decimal("0.75"))
        self.assertEqual(
            quote.percent,
            Decimal("0.75") / Decimal("102.50") * Decimal("100"),
        )
        self.assertEqual(quote.open, Decimal("102.80"))
        self.assertEqual(quote.high, Decimal("103.40"))
        self.assertEqual(quote.low, Decimal("102.20"))
        self.assertEqual(quote.as_of, "2026-08-04 12:22:54")

    def test_cboe_vix_accepts_nested_data_and_percent_text(self) -> None:
        quote = parse_cboe_vix(
            {
                "data": {
                    "current_price": "17.55",
                    "price_change": "-0.45",
                    "price_change_percent": "-2.50%",
                    "high": "18.20",
                    "low": "17.10",
                    "last_trade_time": "2026-08-03T20:15:00",
                }
            }
        )

        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.price, Decimal("17.55"))
        self.assertEqual(quote.change, Decimal("-0.45"))
        self.assertEqual(quote.percent, Decimal("-2.50"))
        self.assertEqual(quote.high, Decimal("18.20"))
        self.assertEqual(quote.low, Decimal("17.10"))
        self.assertEqual(quote.as_of, "2026-08-03T20:15:00")

    def test_cboe_vix_rolled_previous_close_drops_inconsistent_percent(self) -> None:
        # Real Cboe payload fetched 2026-10-17 19:35 (UTC+8), after the 10/16 session.
        rolled = {
            "data": {
                "close": 19.99, "current_price": 19.99, "prev_day_close": 19.99,
                "price_change": 3.53, "price_change_percent": 17.6588,
                "open": 15.81, "high": 22.31, "low": 15.72,
                "last_trade_time": "2026-10-16T16:15:01",
            }
        }
        quote = parse_cboe_vix(rolled)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertIsNone(quote.percent)
        self.assertEqual(quote.price, Decimal("19.99"))
        self.assertEqual(quote.change, Decimal("3.53"))
        self.assertEqual(quote.high, Decimal("22.31"))
        self.assertEqual(quote.low, Decimal("15.72"))
        self.assertEqual(quote.as_of, "2026-10-16T16:15:01")

    def test_cboe_vix_consistent_previous_close_keeps_percent(self) -> None:
        quote = parse_cboe_vix(
            {
                "data": {
                    "current_price": 19.99, "prev_day_close": 16.46,
                    "price_change": 3.53, "price_change_percent": 21.45,
                    "high": 22.31, "low": 15.72,
                    "last_trade_time": "2026-10-16T16:15:01",
                }
            }
        )
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.percent, Decimal("21.45"))

    def test_cboe_vix_without_previous_close_inconsistent_percent_still_rejected(self) -> None:
        from datetime import timezone
        from mini_hermes.reporting import _asset_quality_issue
        generated_at = datetime(2026, 10, 17, 0, 30, tzinfo=timezone.utc)
        payload = {
            "data": {
                "current_price": 19.99, "price_change": 3.53, "price_change_percent": 17.6588,
                "high": 22.31, "low": 15.72, "last_trade_time": "2026-10-16T16:15:01",
            }
        }
        quote = parse_cboe_vix(payload)
        assert quote is not None
        self.assertEqual(quote.percent, Decimal("17.6588"))
        self.assertEqual(_asset_quality_issue(quote, generated_at=generated_at), "涨跌幅与价格变动不一致")
        rolled = parse_cboe_vix({"data": {**payload["data"], "prev_day_close": 19.99}})
        assert rolled is not None
        self.assertIsNone(_asset_quality_issue(rolled, generated_at=generated_at))

    def test_treasury_uses_latest_two_complete_rows(self) -> None:
        curve = parse_treasury_curve(TREASURY_XML)

        self.assertIsNotNone(curve)
        assert curve is not None
        self.assertEqual(curve.date, "2026-08-04")
        self.assertEqual(curve.previous_date, "2026-08-03")
        self.assertEqual(curve.two_year, Decimal("4.02"))
        self.assertEqual(curve.ten_year, Decimal("4.25"))
        self.assertEqual(curve.two_year_change_bp, Decimal("2.00"))
        self.assertEqual(curve.ten_year_change_bp, Decimal("1.00"))
        self.assertEqual(curve.spread_bp, Decimal("23.00"))
        self.assertEqual(curve.three_month, Decimal("3.93"))
        self.assertEqual(curve.thirty_year, Decimal("4.55"))
        self.assertEqual(curve.three_month_change_bp, Decimal("2.00"))
        self.assertEqual(curve.thirty_year_change_bp, Decimal("3.00"))

    def test_sina_regional_compact_records(self) -> None:
        nikkei, hang_seng, ftse = parse_sina_regional_quotes(SINA_REGIONAL_RAW)

        self.assertEqual(nikkei.code, "int_nikkei")
        self.assertEqual(nikkei.price, Decimal("44946.64"))
        self.assertEqual(nikkei.change, Decimal("-408.35"))
        self.assertEqual(nikkei.percent, Decimal("-0.90"))
        self.assertEqual(hang_seng.percent, Decimal("0.72"))
        self.assertEqual(ftse.name, "伦敦指数")

    def test_coinbase_combines_ticker_and_stats(self) -> None:
        quote = parse_coinbase_btc(
            {"price": "105.00", "time": "2026-08-04T00:00:00Z"},
            {"open": "100.00", "high": "110.00", "low": "95.00"},
        )

        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.price, Decimal("105.00"))
        self.assertEqual(quote.change, Decimal("5.00"))
        self.assertEqual(quote.percent, Decimal("5.00"))
        self.assertEqual(quote.open, Decimal("100.00"))
        self.assertEqual(quote.high, Decimal("110.00"))
        self.assertEqual(quote.low, Decimal("95.00"))

    def test_invalid_or_incomplete_payloads_degrade_to_missing(self) -> None:
        self.assertEqual(parse_sina_hf_quotes('var hq_str_hf_GC="";'), ())
        self.assertIsNone(parse_sina_dollar_quote('var hq_str_DINIW="bad";'))
        self.assertIsNone(parse_cboe_vix({"data": {"current_price": "NaN"}}))
        self.assertIsNone(parse_treasury_curve("<feed />"))
        self.assertIsNone(parse_treasury_curve("not xml"))
        zero_open = parse_coinbase_btc(
            {"price": "105", "time": "now"},
            {"open": "0", "high": "105", "low": "0"},
        )
        self.assertIsNotNone(zero_open)
        assert zero_open is not None
        self.assertIsNone(zero_open.percent)


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class OverseasFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_cboe_vix_uses_canonical_api_without_redirect(self) -> None:
        seen = []
        canonical = "https://cdn-api.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
        payload = {
            "timestamp": "2026-10-08 07:12:10",
            "data": {"current_price": "15.08", "price_change": "0.07",
                     "price_change_percent": "0.4642", "high": "15.93", "low": "14.97",
                     "last_trade_time": "2026-10-07T16:15:01"},
        }

        def respond(request):
            seen.append(str(request.url))
            if request.url.host == "cdn.cboe.com":
                return httpx.Response(307, headers={"Location": canonical})
            self.assertEqual(str(request.url), canonical)
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            quote = await overseas_market._fetch_cboe_vix(client)
        self.assertEqual(seen, [canonical])
        self.assertEqual(quote.price, Decimal("15.08"))
        self.assertEqual(quote.as_of, "2026-10-07T16:15:01")

    async def test_cboe_unexpected_redirect_remains_missing_with_warning(self) -> None:
        seen = []

        def respond(request):
            seen.append(str(request.url))
            return httpx.Response(307, headers={"Location": "https://unexpected.invalid/vix"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with self.assertLogs(overseas_market.LOGGER, level="WARNING") as logs:
                quote = await overseas_market._safe_fetch(
                    "cboe_vix", overseas_market._fetch_cboe_vix(client), None,
                )
        self.assertIsNone(quote)
        self.assertEqual(len(seen), 1)
        self.assertIn("source=cboe_vix", "\n".join(logs.output))

    async def test_sources_share_client_and_one_failure_does_not_abort_snapshot(self) -> None:
        fake_client = _FakeClient()
        seen_clients: list[object] = []
        us_quote = UsIndexQuote(
            "int_dji",
            "Dow",
            "1",
            "1",
            "1",
            "2026-08-06 05:15:59",
            datetime(
                2026,
                8,
                5,
                17,
                15,
                tzinfo=ZoneInfo("America/New_York"),
            ),
        )
        gold = CrossAssetQuote(
            code="hf_GC",
            name="Gold",
            price=Decimal("1"),
            change=Decimal("0"),
            percent=Decimal("0"),
            high=Decimal("1"),
            low=Decimal("1"),
            as_of="now",
            unit="USD/oz",
            source="Sina Finance",
        )
        dollar = CrossAssetQuote(
            code="DINIW",
            name="Dollar",
            price=Decimal("1"),
            change=Decimal("0"),
            percent=Decimal("0"),
            high=Decimal("1"),
            low=Decimal("1"),
            as_of="today",
            unit="index",
            source="Sina Finance",
        )
        btc = CrossAssetQuote(
            code="BTC-USD",
            name="Bitcoin",
            price=Decimal("1"),
            change=Decimal("0"),
            percent=Decimal("0"),
            high=Decimal("1"),
            low=Decimal("1"),
            as_of="now",
            unit="USD",
            source="Coinbase",
        )
        curve = TreasuryCurve(
            date="2026-08-04",
            previous_date="2026-08-03",
            two_year=Decimal("4"),
            ten_year=Decimal("4.2"),
            two_year_change_bp=Decimal("0"),
            ten_year_change_bp=Decimal("0"),
            spread_bp=Decimal("20"),
        )
        nikkei = RegionalIndexQuote(
            code="int_nikkei",
            name="Nikkei",
            price=Decimal("1"),
            change=Decimal("0"),
            percent=Decimal("0"),
        )

        async def futures(client):
            seen_clients.append(client)
            return (gold,)

        async def dollar_index(client):
            seen_clients.append(client)
            return dollar

        async def failed_vix(client):
            seen_clients.append(client)
            raise RuntimeError("Cboe unavailable")

        async def treasury(client, year):
            seen_clients.append(client)
            self.assertEqual(year, 2026)
            return curve

        async def bitcoin(client):
            seen_clients.append(client)
            return btc

        async def regional(client):
            seen_clients.append(client)
            return (nikkei,)

        async def market_context(client, now):
            seen_clients.append(client)
            self.assertIsNotNone(now.tzinfo)
            return MarketContext(available_sources=("Fed", "BEA"))

        with (
            patch.object(overseas_market.httpx, "AsyncClient", return_value=fake_client),
            patch.object(
                overseas_market,
                "fetch_us_quotes",
                new=AsyncMock(return_value=(us_quote,)),
            ),
            patch.object(overseas_market, "_fetch_sina_futures", side_effect=futures),
            patch.object(overseas_market, "_fetch_sina_dollar", side_effect=dollar_index),
            patch.object(overseas_market, "_collect_cboe_vix", side_effect=failed_vix),
            patch.object(overseas_market, "_fetch_treasury", side_effect=treasury),
            patch.object(overseas_market, "_fetch_coinbase_btc", side_effect=bitcoin),
            patch.object(overseas_market, "_fetch_sina_regional", side_effect=regional),
            patch.object(overseas_market, "fetch_market_context", side_effect=market_context),
            self.assertLogs(overseas_market.LOGGER, level="WARNING") as logs,
        ):
            snapshot = await fetch_overseas_snapshot(year=2026)

        self.assertEqual(snapshot.us_indices, (us_quote,))
        self.assertEqual(snapshot.assets, (gold, dollar, btc))
        self.assertEqual(snapshot.treasury, curve)
        self.assertEqual(snapshot.regional_indices, (nikkei,))
        self.assertEqual(snapshot.market_context, MarketContext(available_sources=("Fed", "BEA")))
        self.assertEqual(seen_clients, [fake_client] * 7)
        self.assertIn("source=cboe_vix", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
