"""Offline delivery tests; no bot credentials or network are used."""
import asyncio
from hashlib import sha256
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram.error import NetworkError, RetryAfter, TimedOut

from mini_hermes.gateway.telegram.delivery import (
    DeliveryMixin, ReportDeliveryError, send_message_with_light_retry,
)
from mini_hermes.memory import MemoryStore
from mini_hermes.gateway.telegram.bot import MiniHermesBot


class DeliveryIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "memory.db"
        self.store = MemoryStore(self.path)
        await self.store.initialize()
        self.delivery = DeliveryMixin()
        self.delivery.memory = self.store
        self.key = ("daily", "2026-09-28")

    async def asyncTearDown(self):
        await self.store.close()
        self.tmp.cleanup()

    async def test_timeout_is_not_retried_by_transport(self):
        for error in (TimedOut(), NetworkError("acceptance unknown")):
            api = SimpleNamespace(send_message=AsyncMock(side_effect=error))
            with self.assertRaises(NetworkError):
                await send_message_with_light_retry(api, chat_id=42, text="fact")
            self.assertEqual(api.send_message.await_count, 1)

    async def test_explicit_rate_limit_can_retry(self):
        api = SimpleNamespace(send_message=AsyncMock(side_effect=[RetryAfter(0), SimpleNamespace(message_id=1)]))
        with patch("mini_hermes.gateway.telegram.delivery.asyncio.sleep", new=AsyncMock()):
            result = await send_message_with_light_retry(api, chat_id=42, text="fact")
        self.assertEqual(result.message_id, 1)
        self.assertEqual(api.send_message.await_count, 2)

    async def test_partial_report_keeps_receipt_and_unknown_chunk_on_restart(self):
        api = SimpleNamespace(send_message=AsyncMock(side_effect=[SimpleNamespace(message_id=10), TimedOut()]))
        text = "a" * 4000 + "b" * 4000 + "c"
        with self.assertRaises(ReportDeliveryError) as exc:
            await self.delivery._deliver_report(api, 42, text, delivery_key=self.key)
        self.assertEqual(exc.exception.message_ids, (10,))
        await self.store.close()
        self.store = MemoryStore(self.path)
        await self.store.initialize()
        self.delivery.memory = self.store
        api.send_message.reset_mock()
        with self.assertRaises(ReportDeliveryError) as exc:
            await self.delivery._deliver_report(api, 42, text, delivery_key=self.key)
        self.assertEqual(exc.exception.message_ids, (10,))
        self.assertEqual(exc.exception.outcome, "unknown")
        api.send_message.assert_not_called()

    async def test_confirmed_report_does_not_send_again(self):
        api = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=11)))
        for _ in range(2):
            self.assertEqual(await self.delivery._deliver_report(api, 42, "fact", delivery_key=self.key), [11])
        self.assertEqual(api.send_message.await_count, 1)
        with self.assertRaises(ValueError):
            await self.delivery._deliver_report(api, 42, "changed", delivery_key=self.key)
        self.assertEqual(api.send_message.await_count, 1)

    async def test_two_connections_claim_one_send(self):
        other_store = MemoryStore(self.path)
        await other_store.initialize()
        other = DeliveryMixin()
        other.memory = other_store
        started, release = asyncio.Event(), asyncio.Event()
        async def sender(**kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(message_id=12)
        api = SimpleNamespace(send_message=AsyncMock(side_effect=sender))
        task = asyncio.create_task(self.delivery._deliver_report(api, 42, "fact", delivery_key=self.key))
        await started.wait()
        try:
            with self.assertRaises(ReportDeliveryError):
                await other._deliver_report(api, 42, "fact", delivery_key=self.key)
            release.set()
            self.assertEqual(await task, [12])
            self.assertEqual(api.send_message.await_count, 1)
        finally:
            release.set()
            await other_store.close()

    async def test_cancelled_send_becomes_unknown_after_restart(self):
        api = SimpleNamespace(send_message=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await self.delivery._deliver_report(api, 42, "fact", delivery_key=self.key)
        await self.store.close()
        self.store = MemoryStore(self.path)
        await self.store.initialize()
        self.delivery.memory = self.store
        with self.assertRaises(ReportDeliveryError):
            await self.delivery._deliver_report(api, 42, "fact", delivery_key=self.key)
        self.assertEqual(api.send_message.await_count, 1)

    async def test_receipt_is_immutable(self):
        digest = sha256(b"fact").hexdigest()
        claim = await self.store.claim_report_part(*self.key, 42, digest, 1, 0, digest)
        await self.store.finish_report_part(*self.key, 42, 0, claim["claim_id"], "confirmed", 15)
        with self.assertRaises(RuntimeError):
            await self.store.finish_report_part(*self.key, 42, 0, claim["claim_id"], "confirmed", 16)
        result = await self.store.claim_report_part(*self.key, 42, digest, 1, 0, digest)
        self.assertEqual(result["message_id"], 15)

    async def test_missing_ack_blocks_resend(self):
        api = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=None)))
        for _ in range(2):
            with self.assertRaises(ReportDeliveryError):
                await self.delivery._deliver_report(api, 42, "fact", delivery_key=self.key)
        self.assertEqual(api.send_message.await_count, 1)

    async def test_third_reply_failure_does_not_repeat_prefix(self):
        progress = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(reply_text=AsyncMock(side_effect=[None, TimedOut()]))
        update = SimpleNamespace(effective_message=message)
        with self.assertRaises(TimedOut):
            await self.delivery._finish_progress(update, progress, "a" * 4000 + "b" * 4000 + "c")
        progress.edit_text.assert_awaited_once_with("a" * 4000)
        self.assertEqual([call.args[0] for call in message.reply_text.await_args_list], ["b" * 4000, "c"])

    async def test_edit_timeout_has_no_new_message_fallback(self):
        progress = SimpleNamespace(edit_text=AsyncMock(side_effect=TimedOut()))
        message = SimpleNamespace(reply_text=AsyncMock())
        with self.assertRaises(TimedOut):
            await self.delivery._finish_progress(SimpleNamespace(effective_message=message), progress, "fact")
        message.reply_text.assert_not_called()

    async def test_definite_edit_failure_falls_back_only_for_first_part(self):
        progress = SimpleNamespace(edit_text=AsyncMock(side_effect=ValueError("not editable")))
        message = SimpleNamespace(reply_text=AsyncMock())
        await self.delivery._finish_progress(SimpleNamespace(effective_message=message), progress, "a" * 4000 + "b")
        self.assertEqual([call.args[0] for call in message.reply_text.await_args_list], ["a" * 4000, "b"])

    async def test_weekly_delivery_error_keeps_already_sent_first_part(self):
        bot = object.__new__(MiniHermesBot)
        progress = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(reply_text=AsyncMock(return_value=progress))
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=42), effective_message=message)
        bot._authorized = AsyncMock(return_value=True)
        bot._generate_weekly_report = AsyncMock(return_value="a" * 4100)
        bot.application = SimpleNamespace(bot=object())
        bot._deliver_report = AsyncMock(side_effect=ReportDeliveryError([10]))
        bot._finish_progress = AsyncMock()
        bot._reply = AsyncMock()
        await bot._run_weekly_report(update)
        bot._finish_progress.assert_not_called()
        self.assertIn("勿重复提交", bot._reply.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
