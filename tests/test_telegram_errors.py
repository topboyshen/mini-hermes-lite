from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock

from httpx import ConnectError, ReadError
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from mini_hermes.gateway.telegram.errors import (
    is_transient_network_error,
    log_handler_error,
    reset_error_log_cooldowns,
    should_emit_stacktrace,
)
from mini_hermes.gateway.telegram.routing import classify_plain_text_stock_intent


class TransientClassificationTests(unittest.TestCase):
    def test_network_family_is_transient(self) -> None:
        self.assertTrue(is_transient_network_error(NetworkError("boom")))
        self.assertTrue(is_transient_network_error(TimedOut("slow")))
        self.assertTrue(is_transient_network_error(RetryAfter(1)))
        self.assertTrue(is_transient_network_error(ReadError("")))
        self.assertTrue(is_transient_network_error(ConnectError("")))

    def test_persistent_errors_are_not_transient(self) -> None:
        self.assertFalse(is_transient_network_error(BadRequest("bad")))
        self.assertFalse(is_transient_network_error(ValueError("nope")))
        self.assertFalse(is_transient_network_error(None))

    def test_wrapped_cause_is_detected(self) -> None:
        err = RuntimeError("wrap")
        err.__cause__ = TimedOut("inner")
        self.assertTrue(is_transient_network_error(err))


class StacktraceCooldownTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_error_log_cooldowns()

    def test_emits_once_per_cooldown_window(self) -> None:
        state: dict[str, float] = {}
        err = NetworkError("x")
        self.assertTrue(
            should_emit_stacktrace(err, now=100.0, cooldown_seconds=60.0, state=state)
        )
        self.assertFalse(
            should_emit_stacktrace(err, now=130.0, cooldown_seconds=60.0, state=state)
        )
        self.assertTrue(
            should_emit_stacktrace(err, now=161.0, cooldown_seconds=60.0, state=state)
        )

    def test_different_types_are_independent(self) -> None:
        state: dict[str, float] = {}
        self.assertTrue(
            should_emit_stacktrace(
                NetworkError("a"), now=1.0, cooldown_seconds=60.0, state=state
            )
        )
        self.assertTrue(
            should_emit_stacktrace(
                TimedOut("b"), now=1.0, cooldown_seconds=60.0, state=state
            )
        )


class LogHandlerErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_error_log_cooldowns()

    def test_transient_warning_after_first_stacktrace(self) -> None:
        logger = MagicMock(spec=logging.Logger)
        state: dict[str, float] = {}
        err = NetworkError("flaky")
        first = log_handler_error(
            logger, err, cooldown_seconds=60.0, state=state
        )
        second = log_handler_error(
            logger, err, cooldown_seconds=60.0, state=state
        )
        self.assertEqual(first, "exception_rate_limited")
        self.assertEqual(second, "warning")
        self.assertEqual(logger.exception.call_count, 1)
        self.assertEqual(logger.warning.call_count, 1)

    def test_persistent_always_exception(self) -> None:
        logger = MagicMock(spec=logging.Logger)
        mode = log_handler_error(logger, BadRequest("nope"), state={})
        self.assertEqual(mode, "exception")
        logger.exception.assert_called_once()
        logger.warning.assert_not_called()


class RoutingExportTests(unittest.TestCase):
    def test_classify_importable_and_routes(self) -> None:
        self.assertEqual(classify_plain_text_stock_intent("600875"), ("agent", "600875"))
        self.assertEqual(classify_plain_text_stock_intent("hello"), ("pi", None))
        self.assertEqual(classify_plain_text_stock_intent("今日行情"), ("hint", None))


if __name__ == "__main__":
    unittest.main()
