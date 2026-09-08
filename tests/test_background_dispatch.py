"""Real PTB dispatch regression, using no polling, Telegram requests or model."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import Application, CommandHandler, ExtBot, MessageHandler, filters
from telegram.request import BaseRequest

from mini_hermes.gateway.telegram.background import bounded_background


class NoNetworkRequest(BaseRequest):
    """Fail closed even if a future PTB change unexpectedly requests the API."""

    def __init__(self):
        self.calls = 0

    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("Telegram network requests are forbidden in this test")


class BackgroundDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_ptb_dispatch_keeps_abort_available_with_two_long_handlers(self):
        requests = (NoNetworkRequest(), NoNetworkRequest())
        telegram_bot = ExtBot(
            token="123:TEST",
            request=requests[0], get_updates_request=requests[1],
        )
        identity = User(123456, "Offline bot", True, username="offline_test_bot")
        owner = SimpleNamespace(
            settings=SimpleNamespace(allowed_user_ids={42}),
            _authorized=AsyncMock(return_value=False), _reply=AsyncMock(),
        )
        release = asyncio.Event()
        entered = {"slow-one": asyncio.Event(), "slow-two": asyncio.Event()}
        finished = set()
        abort_calls = []
        errors = []

        async def work(update, context):
            label = update.effective_message.text
            entered[label].set()
            try:
                await release.wait()
            finally:
                finished.add(label)

        async def abort(update, context):
            abort_calls.append((update.effective_message.text, context.application))

        async def error_handler(update, context):
            errors.append(type(context.error).__name__)

        def make_update(index, text):
            entities = [MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text))] if text.startswith("/") else None
            message = Message(
                message_id=index, date=datetime.now(timezone.utc),
                chat=Chat(42, "private"), from_user=User(42, "Offline user", False),
                text=text, entities=entities,
            )
            message.set_bot(telegram_bot)
            update = Update(index, message=message)
            update.set_bot(telegram_bot)
            return update

        # Application initialization remains real, but Bot.initialize cannot
        # call getMe. The public bot identity property is synthetic as well.
        with patch.object(ExtBot, "initialize", new_callable=AsyncMock) as initialize, \
                patch.object(ExtBot, "bot", new_callable=PropertyMock, return_value=identity):
            app = (Application.builder().bot(telegram_bot).updater(None).job_queue(None)
                   .concurrent_updates(2).build())
            app.add_handler(MessageHandler(
                filters.Regex(r"^slow-one$"), bounded_background(owner, work), block=False,
            ))
            app.add_handler(MessageHandler(
                filters.Regex(r"^slow-two$"), bounded_background(owner, work), block=False,
            ))
            app.add_handler(CommandHandler("abort", abort, block=True))
            app.add_error_handler(error_handler)
            await app.initialize()
            await app.start()
            try:
                # Use the real update processor too, so the production limit
                # of two concurrent updates is exercised, not bypassed.
                async def dispatch(update):
                    await app.update_processor.process_update(update, app.process_update(update))

                for index, label in enumerate(entered, start=1):
                    await asyncio.wait_for(dispatch(make_update(index, label)), timeout=2)
                    await asyncio.wait_for(entered[label].wait(), timeout=2)
                self.assertEqual(owner._ux_background_active, 2)
                self.assertEqual(finished, set())

                await asyncio.wait_for(dispatch(make_update(3, "/abort")), timeout=2)
                self.assertEqual(abort_calls, [("/abort", app)])
                self.assertEqual(finished, set(), "abort must dispatch before either long request finishes")
                self.assertEqual(owner._ux_background_active, 2)
                self.assertEqual(errors, [])
                owner._reply.assert_not_awaited()
                owner._authorized.assert_not_awaited()
            finally:
                release.set()
                # stop() awaits PTB-owned background callbacks: no private task
                # registry or independently managed task pool is needed.
                await asyncio.wait_for(app.stop(), timeout=3)
                await app.shutdown()
            initialize.assert_awaited_once()

        self.assertEqual(finished, {"slow-one", "slow-two"})
        self.assertEqual(owner._ux_background_active, 0)
        self.assertEqual([request.calls for request in requests], [0, 0])


if __name__ == "__main__":
    unittest.main()
