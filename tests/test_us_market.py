import unittest
from datetime import date

from mini_hermes.us_market import (
    parse_us_quotes,
    render_us_index_block,
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
        self.assertEqual(quotes[0].open, "54266.1211")
        self.assertEqual(quotes[0].high, "54744.3281")
        self.assertEqual(quotes[0].low, "54266.1211")
        self.assertEqual(quotes[0].week52_high, "54272.6016")
        self.assertEqual(quotes[0].week52_low, "43799.1992")

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


if __name__ == "__main__":
    unittest.main()
