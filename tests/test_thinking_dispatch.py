"""Real Telegram dispatch + SQLite with synthetic updates; never contacts Telegram."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User
from telegram.ext import Application, ExtBot

from mini_hermes.gateway.telegram.bot import MiniHermesBot
from mini_hermes.memory import MemoryStore
from test_media_dispatch import NoNetworkRequest


class ThinkingDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_and_all_eight_buttons_dispatch_with_long_task_capacity_full(self):
        requests = (NoNetworkRequest(), NoNetworkRequest())
        telegram_bot = ExtBot(token="123:TEST", request=requests[0], get_updates_request=requests[1])
        identity = User(123456, "Synthetic bot", True, username="offline_thinking_bot")
        user, chat = User(42, "Synthetic", False), Chat(42, "private")
        date = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryStore(Path(directory) / "memory.db")
            await memory.initialize()
            owner = object.__new__(MiniHermesBot)
            owner.settings = SimpleNamespace(allowed_user_ids={42})
            owner.memory = memory
            owner._deliver_report = AsyncMock()
            owner._run_pi = AsyncMock()
            owner._run_agent = AsyncMock()
            owner._ux_background_active = 2
            errors = []
            owner.error_handler = AsyncMock(side_effect=lambda update, context: errors.append(type(context.error).__name__))
            command = Message(1, date, chat, from_user=user, text="/thinking",
                              entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, 9)])
            command.set_bot(telegram_bot)
            update = Update(1, message=command)
            update.set_bot(telegram_bot)
            with patch.object(ExtBot, "initialize", new_callable=AsyncMock), \
                    patch.object(ExtBot, "bot", new_callable=PropertyMock, return_value=identity), \
                    patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as acknowledge, \
                    patch.object(CallbackQuery, "edit_message_text", new_callable=AsyncMock) as edit:
                app = Application.builder().bot(telegram_bot).updater(None).job_queue(None).build()
                owner.application = app
                owner._register_handlers()
                await app.initialize()
                await app.start()
                try:
                    await asyncio.wait_for(app.process_update(update), 2)
                    owner._deliver_report.assert_awaited_once()
                    markup = owner._deliver_report.await_args.kwargs["reply_markup"]
                    message = Message(2, date, chat, from_user=identity, text="synthetic menu")
                    message.set_bot(telegram_bot)
                    for index, button in enumerate([button for row in markup.inline_keyboard for button in row]):
                        query = CallbackQuery(f"synthetic-{index}", user, "synthetic-instance", message=message,
                                              data=button.callback_data)
                        query.set_bot(telegram_bot)
                        selection = Update(3 + index, callback_query=query)
                        selection.set_bot(telegram_bot)
                        await asyncio.wait_for(app.process_update(selection), 2)
                        _, _, _, scope, level = button.callback_data.split(":")
                        self.assertEqual(await memory.get_thinking(42, scope), level)
                    self.assertEqual(acknowledge.await_count, 8)
                    self.assertEqual(edit.await_count, 8)
                    self.assertEqual(owner._ux_background_active, 2)
                    self.assertEqual(await memory.recent_messages(42, 10), [])
                    self.assertEqual(errors, [])
                    owner._run_pi.assert_not_awaited()
                    owner._run_agent.assert_not_awaited()
                finally:
                    await app.stop()
                    await app.shutdown()
                    await memory.close()
        self.assertEqual([request.calls for request in requests], [0, 0])
