import unittest
from datetime import date

from unittest.mock import AsyncMock, patch

from mini_hermes.us_market import (
    _reset_feed_status_for_tests,
    fetch_us_quotes,
    parse_us_quotes,
    render_us_index_block,
    us_market_feed_status,
)


RAW_QUOTES = (
    'var hq_str_gb_dji="道琼斯,54349.1211,0.49,2026-08-06 05:22:24,263.2400,'
    '54266.1211,54744.3281,54266.1211,54272.6016,43799.1992,524200884,'
    '550235368,0,0.00,--,0.00,0.00,0.00,0.00,0,0,0.0000,0.00,0.0000,,'
    'Aug 5 05:22PM EDT,54085.8789,0,1,2026";\n'
    'var hq_str_gb_ixic="纳斯达克,26363.4393,-0.83,2026-08-06 05:29:30,'
    '-221.5537,26698.0181,26738.9959,26348.5085,27190.2070,20690.2500,'
    '8245186464,7244523585,0,0.00,--,0.00,0.00,0.00,0.00,0,0,0.0000,'
    '0.00,0.00,,Aug 05 05:16PM EDT,26584.9930,0,1,2026,0.0000";\n'
    'var hq_str_gb_inx="标普500指数,7723.5498,-0.17,2026-08-06 04:42:03,'
    '-12.9700,7771.6201,7793.6802,7720.1699,7758.2100,6310.3198,'
    '3159366216,3369068540,0,0.00,--,0.00,0.00,0.00,0.00,0,0,0.0000,'
    '0.00,0.0000,,Aug 5 04:41PM EDT,7736.5200,0,1,2026";'
)


class UsMarketTests(unittest.TestCase):
    def test_parse_preserves_exact_source_numbers(self) -> None:
        quotes = parse_us_quotes(RAW_QUOTES)
        self.assertEqual(len(quotes), 3)
        self.assertEqual(quotes[1].code, "int_nasdaq")
        self.assertEqual(quotes[1].price, "26363.4393")
        self.assertEqual(quotes[1].change, "-221.5537")
        self.assertEqual(quotes[1].percent, "-0.83")
        self.assertEqual(quotes[1].as_of, "2026-08-06 05:29:30")

    def test_beijing_as_of_converts_to_new_york_trade_date(self) -> None:
        quotes = parse_us_quotes(RAW_QUOTES)
        self.assertEqual(quotes[0].trade_date, date(2026, 8, 5))
        self.assertEqual(quotes[1].trade_date, date(2026, 8, 5))
        self.assertEqual(quotes[1].observed_at.hour, 17)
        self.assertEqual(quotes[1].observed_at.utcoffset().total_seconds(), -4 * 3600)

    def test_winter_timestamp_uses_new_york_standard_time(self) -> None:
        raw = (
            'var hq_str_gb_dji="道琼斯,50000.00,0.20,'
            '2026-01-06 05:15:00,100.00";'
        )
        quote = parse_us_quotes(raw)[0]

        self.assertEqual(quote.trade_date, date(2026, 1, 5))
        self.assertEqual(quote.observed_at.hour, 16)
        self.assertEqual(quote.observed_at.utcoffset().total_seconds(), -5 * 3600)

    def test_missing_or_invalid_as_of_discards_quote(self) -> None:
        raw = (
            'var hq_str_gb_dji="道琼斯,54349.12,0.49,,263.24";\n'
            'var hq_str_gb_ixic="纳斯达克,26363.44,-0.83,not-a-time,-221.55";'
        )
        self.assertEqual(parse_us_quotes(raw), ())

    def test_render_keeps_source_numbers(self) -> None:
        rendered = render_us_index_block(parse_us_quotes(RAW_QUOTES))
        self.assertIn("26363.4393", rendered)
        self.assertNotIn("26336.4393", rendered)
        self.assertIn("-221.5537", rendered)
        self.assertIn("-0.83%", rendered)



class UsMarketFeedStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _reset_feed_status_for_tests()

    async def test_fetch_failure_increments_and_success_resets(self) -> None:
        with patch("mini_hermes.us_market.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            client.get.side_effect = RuntimeError("sina down")
            client_cls.return_value = client

            quotes = await fetch_us_quotes()

        self.assertEqual(quotes, ())
        status = us_market_feed_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["consecutive_failures"], 1)
        self.assertIn("sina down", str(status["last_error"]))

        with patch("mini_hermes.us_market.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            response = AsyncMock()
            response.raise_for_status = lambda: None
            response.content = RAW_QUOTES.encode("gbk", errors="replace")
            client.get.return_value = response
            client_cls.return_value = client

            quotes = await fetch_us_quotes()

        self.assertEqual(len(quotes), 3)
        status = us_market_feed_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["consecutive_failures"], 0)
        self.assertIsNone(status["last_error"])

    async def test_parse_empty_counts_as_failure(self) -> None:
        with patch("mini_hermes.us_market.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.__aexit__.return_value = None
            response = AsyncMock()
            response.raise_for_status = lambda: None
            response.content = b'var hq_str_gb_dji="";'
            client.get.return_value = response
            client_cls.return_value = client

            quotes = await fetch_us_quotes()

        self.assertEqual(quotes, ())
        status = us_market_feed_status()
        self.assertEqual(status["consecutive_failures"], 1)
        self.assertEqual(status["last_error"], "parse_empty")



if __name__ == "__main__":
    unittest.main()
