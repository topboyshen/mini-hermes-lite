from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from telegram import Chat, Message, PhotoSize, Update, User
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from mini_hermes.gateway.telegram import MiniHermesBot


class HandlerRegistrationTests(unittest.TestCase):
    def bot(self):
        bot = object.__new__(MiniHermesBot)
        handlers = []
        bot.application = SimpleNamespace(add_handler=handlers.append, add_error_handler=lambda handler: None)
        bot._register_handlers()
        return bot, handlers

    def update(self, *, text=None, photo=None):
        return Update(1, message=Message(
            message_id=1, date=datetime.now(timezone.utc), chat=Chat(42, "private"),
            from_user=User(42, "Synthetic", False), text=text, photo=photo,
        ))

    def test_long_commands_are_nonblocking_but_controls_remain_direct(self):
        bot, handlers = self.bot()
        commands = {next(iter(handler.commands)): handler for handler in handlers if isinstance(handler, CommandHandler)}
        for name in ("stock", "market", "report", "weekly", "pi"):
            self.assertFalse(commands[name].block, name)
        for name in ("abort", "new", "help", "pistatus"):
            self.assertTrue(commands[name].block, name)
        self.assertEqual(commands["abort"].callback, bot.abort_pi_command)

    def test_menu_matches_before_generic_text(self):
        bot, handlers = self.bot()
        for label in ("功能帮助", "任务状态", "海外晨报", "本周简报"):
            update = self.update(text=label)
            matching = [handler for handler in handlers if isinstance(handler, MessageHandler) and handler.check_update(update)]
            self.assertEqual(matching[0].callback, bot._feature_menu_dispatch)
            self.assertFalse(matching[0].block)

    def test_plain_text_and_media_use_nonblocking_handlers(self):
        bot, handlers = self.bot()
        updates = (self.update(text="普通问题"), self.update(text="600000"),
                   self.update(photo=[PhotoSize("synthetic", "synthetic", 10, 10, 100)]))
        for update in updates:
            matching = [handler for handler in handlers if isinstance(handler, MessageHandler) and handler.check_update(update)]
            self.assertTrue(matching)
            self.assertFalse(matching[0].block)
        self.assertTrue(bot.attachments_enabled)

    def test_signal_gate_and_report_callbacks_remain_registered(self):
        _, handlers = self.bot()
        patterns = [handler.pattern.pattern for handler in handlers if isinstance(handler, CallbackQueryHandler)]
        self.assertIn(r"^sg:(ok|no):\d+$", patterns)
        self.assertTrue(any("market_full" in pattern for pattern in patterns))


class MenuDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_menu_does_not_consume_background_capacity(self):
        bot = object.__new__(MiniHermesBot)
        bot.feature_menu_message = AsyncMock()
        update = SimpleNamespace(effective_message=SimpleNamespace(text="任务状态"))
        await bot._feature_menu_dispatch(update, None)
        bot.feature_menu_message.assert_awaited_once_with(update, None)


if __name__ == "__main__":
    unittest.main()
