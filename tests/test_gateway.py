from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from mini_hermes.gateway.telegram import MiniHermesBot, ReportDeliveryError
from mini_hermes.overseas_market import OverseasMarketSnapshot
from mini_hermes.us_market import UsIndexQuote


class GatewayRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_text_always_routes_to_pi(self) -> None:
        bot = object.__new__(MiniHermesBot)
        bot._run_pi = AsyncMock()
        update = SimpleNamespace(effective_message=SimpleNamespace(text="hello pi"))

        await bot.text_message(update, None)

        bot._run_pi.assert_awaited_once_with(update, "hello pi")

    async def test_pi_rejects_non_private_chat(self) -> None:
        bot = object.__new__(MiniHermesBot)
        bot._authorized = AsyncMock(return_value=True)
        bot._reply = AsyncMock()
        bot.pi_channel = SimpleNamespace(prompt=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=-1001, type="group"),
        )

        await bot._run_pi(update, "run a command")

        bot._reply.assert_awaited_once()
        bot.pi_channel.prompt.assert_not_awaited()

    async def test_global_report_uses_no_agent_or_memory_and_keeps_exact_quotes(self) -> None:
        bot = object.__new__(MiniHermesBot)
        bot.settings = SimpleNamespace(timezone="Asia/Shanghai")
        bot.agent = None
        bot.memory = SimpleNamespace(
            append_turn=AsyncMock(),
            recent_messages=AsyncMock(),
            list_watch=AsyncMock(),
        )
        snapshot = OverseasMarketSnapshot(
            us_indices=(
                UsIndexQuote(
                    code="int_dji",
                    name="道琼斯工业指数",
                    price="54349.1211",
                    change="263.2400",
                    percent="0.49",
                    as_of="2026-08-06 05:15:59",
                    observed_at=datetime(
                        2026,
                        8,
                        5,
                        17,
                        15,
                        tzinfo=ZoneInfo("America/New_York"),
                    ),
                ),
                UsIndexQuote(
                    code="int_nasdaq",
                    name="纳斯达克综合指数",
                    price="26363.4393",
                    change="-221.5537",
                    percent="-0.83",
                    as_of="2026-08-06 05:15:59",
                    observed_at=datetime(
                        2026,
                        8,
                        5,
                        17,
                        15,
                        tzinfo=ZoneInfo("America/New_York"),
                    ),
                ),
                UsIndexQuote(
                    code="int_sp500",
                    name="标普500指数",
                    price="7723.5498",
                    change="-12.9700",
                    percent="-0.17",
                    as_of="2026-08-06 05:15:59",
                    observed_at=datetime(
                        2026,
                        8,
                        5,
                        17,
                        15,
                        tzinfo=ZoneInfo("America/New_York"),
                    ),
                ),
            ),
            assets=(),
            treasury=None,
        )

        with patch("mini_hermes.gateway.telegram.datetime") as clock:
            clock.now.return_value = datetime(
                2026,
                8,
                6,
                8,
                30,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            )
            report = await bot._generate_global_report(snapshot)

        self.assertIn("26,363.4393", report)
        bot.memory.append_turn.assert_not_awaited()
        bot.memory.recent_messages.assert_not_awaited()
        bot.memory.list_watch.assert_not_awaited()

    async def test_global_report_fetches_snapshot_when_not_injected(self) -> None:
        bot = object.__new__(MiniHermesBot)
        bot.settings = SimpleNamespace(timezone="Asia/Shanghai")
        snapshot = OverseasMarketSnapshot(us_indices=(), assets=(), treasury=None)

        with (
            patch(
                "mini_hermes.gateway.telegram.fetch_overseas_snapshot",
                new=AsyncMock(return_value=snapshot),
            ) as fetch_snapshot,
            patch(
                "mini_hermes.gateway.telegram.compose_global_report",
                return_value="overseas report",
            ) as compose,
            patch("mini_hermes.gateway.telegram.datetime") as clock,
        ):
            clock.now.return_value = datetime(
                2026,
                8,
                6,
                8,
                30,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            )
            report = await bot._generate_global_report()

        self.assertEqual(report, "overseas report")
        fetch_snapshot.assert_awaited_once_with()
        compose.assert_called_once()
        self.assertEqual(compose.call_args.args[1], snapshot)
        self.assertEqual(
            compose.call_args.kwargs["generated_at"],
            clock.now.return_value,
        )

    async def test_deliver_report_reuses_progress_and_returns_all_ids(self) -> None:
        bot = object.__new__(MiniHermesBot)
        progress = SimpleNamespace(message_id=55, edit_text=AsyncMock())
        api = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=56))
        )

        message_ids = await bot._deliver_report(api, 99, "x" * 4100, progress=progress)

        self.assertEqual(message_ids, [55, 56])
        progress.edit_text.assert_awaited_once_with("x" * 4000)
        api.send_message.assert_awaited_once_with(chat_id=99, text="x" * 100)

    async def test_deliver_report_rejects_empty_text(self) -> None:
        bot = object.__new__(MiniHermesBot)
        api = SimpleNamespace(send_message=AsyncMock())

        with self.assertRaises(ValueError):
            await bot._deliver_report(api, 99, "   ")

        api.send_message.assert_not_awaited()

    async def test_delivery_error_keeps_partial_message_ids(self) -> None:
        bot = object.__new__(MiniHermesBot)
        api = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=[
                    SimpleNamespace(message_id=56),
                    RuntimeError("Telegram unavailable"),
                ]
            )
        )

        with self.assertRaises(ReportDeliveryError) as raised:
            await bot._deliver_report(api, 99, "x" * 8100)

        self.assertEqual(raised.exception.message_ids, (56,))

    async def test_daily_report_generates_once_for_multiple_subscribers(self) -> None:
        bot = object.__new__(MiniHermesBot)
        bot.agent = object()
        bot.settings = SimpleNamespace(
            timezone="Asia/Shanghai", allowed_user_ids=frozenset({42, 43})
        )
        bot.memory = SimpleNamespace(
            subscriptions=AsyncMock(return_value=[(42, 99), (43, 100)])
        )
        bot._generate_global_report = AsyncMock(return_value="overseas report")
        send_message = AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=123),
                SimpleNamespace(message_id=124),
            ]
        )
        context = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))

        with patch("mini_hermes.gateway.telegram.datetime") as clock:
            clock.now.return_value = datetime(2026, 8, 8, 8, 30)
            await bot.daily_report_job(context)

        bot._generate_global_report.assert_awaited_once_with()
        self.assertEqual(send_message.await_count, 2)
        send_message.assert_any_await(chat_id=99, text="overseas report")
        send_message.assert_any_await(chat_id=100, text="overseas report")

    async def test_one_delivery_failure_does_not_block_other_subscribers(self) -> None:
        bot = object.__new__(MiniHermesBot)
        bot.agent = object()
        bot.settings = SimpleNamespace(
            timezone="Asia/Shanghai", allowed_user_ids=frozenset({42, 43})
        )
        bot.memory = SimpleNamespace(
            subscriptions=AsyncMock(return_value=[(42, 99), (43, 100)])
        )
        bot._generate_global_report = AsyncMock(return_value="overseas report")
        bot._deliver_report = AsyncMock(
            side_effect=[RuntimeError("Telegram unavailable"), [124]]
        )
        context = SimpleNamespace(bot=object())

        with patch("mini_hermes.gateway.telegram.datetime") as clock:
            clock.now.return_value = datetime(2026, 8, 4, 8, 30)
            await bot.daily_report_job(context)

        bot._generate_global_report.assert_awaited_once_with()
        self.assertEqual(bot._deliver_report.await_count, 2)

    async def test_daily_report_skips_generation_without_allowed_recipients(self) -> None:
        bot = object.__new__(MiniHermesBot)
        bot.agent = object()
        bot.settings = SimpleNamespace(
            timezone="Asia/Shanghai", allowed_user_ids=frozenset({42})
        )
        bot.memory = SimpleNamespace(
            subscriptions=AsyncMock(return_value=[(99, 100)])
        )
        bot._generate_global_report = AsyncMock()
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

        with patch("mini_hermes.gateway.telegram.datetime") as clock:
            clock.now.return_value = datetime(2026, 8, 4, 8, 30)
            await bot.daily_report_job(context)

        bot._generate_global_report.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()

    async def test_daily_report_skips_days_without_recent_us_close(self) -> None:
        bot = object.__new__(MiniHermesBot)
        bot.agent = object()
        bot.settings = SimpleNamespace(
            timezone="Asia/Shanghai", allowed_user_ids=frozenset({42})
        )
        bot.memory = SimpleNamespace(subscriptions=AsyncMock())
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

        with patch("mini_hermes.gateway.telegram.datetime") as clock:
            clock.now.return_value = datetime(2026, 8, 2, 8, 30)
            await bot.daily_report_job(context)

        bot.memory.subscriptions.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()

        with patch("mini_hermes.gateway.telegram.datetime") as clock:
            clock.now.return_value = datetime(2026, 8, 3, 8, 30)
            await bot.daily_report_job(context)

        bot.memory.subscriptions.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
