import unittest

from mini_hermes.reporting import compose_global_report, infer_report_stance
from mini_hermes.us_market import UsIndexQuote


FULL_UP_QUOTES = (
    UsIndexQuote("int_dji", "道琼斯工业指数", "44563.12", "10.50", "0.02"),
    UsIndexQuote("int_nasdaq", "纳斯达克综合指数", "22484.07", "99.37", "0.44"),
    UsIndexQuote("int_sp500", "标普500指数", "6329.94", "7.18", "0.11"),
)


class ReportingTests(unittest.TestCase):
    def test_complete_quotes_determine_risk_stance(self) -> None:
        self.assertEqual(infer_report_stance(FULL_UP_QUOTES), "RISK_ON")
        report = compose_global_report("2026-08-04", FULL_UP_QUOTES)
        self.assertIn("市场风险偏好偏强", report)
        self.assertNotIn("市场风险偏好偏弱", report)

    def test_partial_quotes_do_not_claim_broad_market_direction(self) -> None:
        quotes = (FULL_UP_QUOTES[1],)
        self.assertEqual(infer_report_stance(quotes), "PARTIAL")
        report = compose_global_report("2026-08-04", quotes)
        self.assertIn("数据覆盖不完整", report)
        self.assertNotIn("市场风险偏好偏强", report)

    def test_composer_preserves_source_quotes_without_model_text(self) -> None:
        report = compose_global_report("2026-08-04", FULL_UP_QUOTES)
        self.assertIn("22484.07", report)
        self.assertNotIn("22446.07", report)
        self.assertIn("# 海外市场晨报", report)


if __name__ == "__main__":
    unittest.main()
