from datetime import UTC, datetime
import unittest

from mini_hermes.market_context import parse_official_rss


FED_RSS = """<?xml version="1.0"?>
<rss><channel>
  <item>
    <title>Minutes of the Federal Open Market Committee, July 28-29, 2026</title>
    <link>https://www.federalreserve.gov/minutes</link>
    <pubDate>Wed, 19 Aug 2026 18:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Minutes of the Board's discount rate meetings</title>
    <link>https://www.federalreserve.gov/discount</link>
    <pubDate>Tue, 18 Aug 2026 18:00:00 GMT</pubDate>
  </item>
</channel></rss>"""

BEA_RSS = """<?xml version="1.0"?>
<rss><channel>
  <item name="GDP">
    <title>GDP (Second Estimate) and Corporate Profits, 2nd Quarter 2026</title>
    <link>https://www.bea.gov/gdp</link>
    <pubDate>Wed, 26 Aug 2026 08:30:00 EDT</pubDate>
    <data><main>
      <current><infoDate>Q2 2026</infoDate><percentChange>+1.5</percentChange></current>
      <previous><infoDate>Q1 2026</infoDate><percentChange>+2.1</percentChange></previous>
    </main></data>
    <changeUnit>PCT</changeUnit>
  </item>
  <item name="Regional GDP">
    <title>Regional GDP</title>
    <pubDate>Wed, 26 Aug 2026 08:30:00 EDT</pubDate>
  </item>
</channel></rss>"""


class MarketContextParserTests(unittest.TestCase):
    def test_fed_parser_keeps_only_high_impact_recent_events(self) -> None:
        events = parse_official_rss(
            FED_RSS,
            source="Fed",
            now=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source, "美联储")
        self.assertEqual(events[0].title, "美联储发布FOMC会议纪要")

    def test_bea_parser_extracts_current_and_previous_values(self) -> None:
        events = parse_official_rss(
            BEA_RSS,
            source="BEA",
            now=datetime(2026, 8, 29, 0, 0, tzinfo=UTC),
        )

        self.assertEqual(len(events), 1)
        self.assertIn("美国GDP更新", events[0].title)
        self.assertEqual(
            events[0].detail,
            "2026年第二季度实际GDP年化 +1.5%；前值 2026年第一季度 +2.1%",
        )
        self.assertEqual(events[0].url, "https://www.bea.gov/gdp")

    def test_old_future_and_malformed_items_fail_closed(self) -> None:
        old = parse_official_rss(
            FED_RSS,
            source="Fed",
            now=datetime(2026, 8, 30, 0, 0, tzinfo=UTC),
        )

        self.assertEqual(old, ())
        self.assertEqual(
            parse_official_rss("not xml", source="Fed", now=datetime.now(UTC)),
            (),
        )


if __name__ == "__main__":
    unittest.main()
