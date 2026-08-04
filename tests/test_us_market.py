import unittest

from mini_hermes.us_market import (
    parse_us_quotes,
    render_us_index_block,
)


RAW_QUOTES = (
    'var hq_str_int_dji="道琼斯,44563.12,-10.50,-0.02";\n'
    'var hq_str_int_nasdaq="纳斯达克,22484.07,99.37,0.44";\n'
    'var hq_str_int_sp500="标普500,6329.94,7.18,0.11";'
)


class UsMarketTests(unittest.TestCase):
    def test_parse_preserves_exact_source_numbers(self) -> None:
        quotes = parse_us_quotes(RAW_QUOTES)
        self.assertEqual(len(quotes), 3)
        self.assertEqual(quotes[1].price, "22484.07")
        self.assertEqual(quotes[1].change, "99.37")
        self.assertEqual(quotes[1].percent, "0.44")

    def test_render_keeps_source_numbers(self) -> None:
        rendered = render_us_index_block(parse_us_quotes(RAW_QUOTES))
        self.assertIn("22484.07", rendered)
        self.assertNotIn("22446.07", rendered)
        self.assertIn("+99.37", rendered)


if __name__ == "__main__":
    unittest.main()
