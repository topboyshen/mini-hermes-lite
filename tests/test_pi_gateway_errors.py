import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mini_hermes.gateway.telegram import MiniHermesBot
from mini_hermes.pi_channel.client import PiChannelError
from mini_hermes.pi_channel.errors import error_message


class PiGatewayFailureTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, error=None, answer="safe answer"):
        bot = object.__new__(MiniHermesBot)
        bot._authorized = AsyncMock(return_value=True)
        bot._finish_progress = AsyncMock()
        bot.memory = SimpleNamespace(append_turn=AsyncMock())
        bot.pi_channel = SimpleNamespace(prompt=AsyncMock(return_value=answer, side_effect=error))
        bot.application = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        progress = SimpleNamespace(edit_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=42, type="private"),
            effective_message=SimpleNamespace(reply_text=AsyncMock(return_value=progress)),
        )
        return bot, update

    async def run_and_logs(self, bot, update):
        with self.assertLogs("mini_hermes.gateway.telegram.bot", level="INFO") as capture:
            await bot._run_pi(update, "SYNTHETIC_PRIVATE_PROMPT")
        lines = [record.getMessage() for record in capture.records]
        self.assertEqual(sum("pi_request_started" in line for line in lines), 1)
        self.assertEqual(sum("pi_request_finished" in line for line in lines), 1)
        self.assertTrue(all("SYNTHETIC" not in line for line in lines))
        start = next(line for line in lines if "pi_request_started" in line)
        finish = next(line for line in lines if "pi_request_finished" in line)
        self.assertEqual(start.split("request_id=")[1].split()[0],
                         finish.split("request_id=")[1].split()[0])
        return lines, finish

    async def test_all_error_categories_have_safe_message_and_matching_metrics(self):
        for code in ("auth_required", "quota", "rate_limited", "timeout"):
            with self.subTest(code=code):
                bot, update = self.fixture(error=PiChannelError("SYNTHETIC_SECRET", code=code))
                lines, finish = await self.run_and_logs(bot, update)
                self.assertIn("outcome=failed", finish)
                self.assertIn(f"error_code={code}", finish)
                self.assertEqual(sum("pi_request_failed" in line for line in lines), 1)
                self.assertEqual(bot._finish_progress.await_args.args[2], error_message(code))
                bot.memory.append_turn.assert_not_awaited()

    async def test_cancel_is_not_an_outage(self):
        bot, update = self.fixture(error=PiChannelError(code="cancelled"))
        lines, finish = await self.run_and_logs(bot, update)
        self.assertIn("outcome=cancelled", finish)
        self.assertFalse(any("pi_request_failed" in line for line in lines))

    async def test_shutdown_cancellation_has_terminal_event_and_is_reraised(self):
        bot, update = self.fixture(error=asyncio.CancelledError())
        with self.assertLogs("mini_hermes.gateway.telegram.bot", level="INFO") as capture:
            with self.assertRaises(asyncio.CancelledError):
                await bot._run_pi(update, "test")
        finished = [r.getMessage() for r in capture.records if "pi_request_finished" in r.getMessage()]
        self.assertEqual(len(finished), 1)
        self.assertIn("outcome=cancelled", finished[0])
        bot._finish_progress.assert_not_awaited()

    async def test_unexpected_error_is_safe(self):
        bot, update = self.fixture(error=RuntimeError("SYNTHETIC_SECRET"))
        _, finish = await self.run_and_logs(bot, update)
        self.assertIn("outcome=failed error_code=upstream_error", finish)
        self.assertEqual(bot._finish_progress.await_args.args[2], error_message("upstream_error"))

    async def test_success_mentions_error_codes_without_false_failure(self):
        bot, update = self.fixture(answer="Explain HTTP 401 and 429 quota errors")
        _, finish = await self.run_and_logs(bot, update)
        self.assertIn("outcome=success error_code=none", finish)

    async def test_empty_result_is_failure(self):
        bot, update = self.fixture(answer=" ")
        _, finish = await self.run_and_logs(bot, update)
        self.assertIn("outcome=failed error_code=invalid_response", finish)

    async def test_typing_or_memory_failure_does_not_mislabel_model_success(self):
        bot, update = self.fixture()
        bot.application.bot.send_chat_action.side_effect = RuntimeError("SYNTHETIC_SECRET")
        bot.memory.append_turn.side_effect = RuntimeError("SYNTHETIC_SECRET")
        _, finish = await self.run_and_logs(bot, update)
        self.assertIn("outcome=success", finish)
        self.assertEqual(bot._finish_progress.await_args.args[2], "safe answer")


if __name__ == "__main__":
    unittest.main()
