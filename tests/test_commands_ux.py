"""Offline menu/status regressions: no Telegram, model, database, or settings I/O."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mini_hermes.gateway.telegram.commands import (
    CommandsMixin,
    FEATURE_MENU_TEXTS,
    attachment_help_text,
    feature_menu_markup,
    format_pi_status,
)
from mini_hermes.pi_channel import PiChannelError


class PrivateObject:
    def __str__(self):
        raise AssertionError("private objects must not be formatted")

    def __repr__(self):
        raise AssertionError("private objects must not be logged")


class StatusFormattingTests(unittest.TestCase):
    def test_current_model_and_idle_state_are_readable(self):
        result = format_pi_status({"model": "xai/grok-4.6", "busy": False, "research_active": 0})
        self.assertIn("Grok 4.6", result)
        self.assertIn("当前空闲", result)
        self.assertIn("当前无活动模型请求", result)
        self.assertIn("不代表整个研究任务已结束", result)

    def test_busy_and_research_count_use_public_fields_only(self):
        result = format_pi_status({"model": "xai/grok-4.6", "busy": True, "research_active": 2,
                                   "active_user_id": PrivateObject(), "session_path": PrivateObject()})
        self.assertIn("正在处理任务", result)
        self.assertIn("A 股模型通道：当前有模型请求处理中", result)
        self.assertNotIn("2 个任务", result)
        self.assertIn("/abort", result)
        self.assertNotIn("active_user_id", result)
        self.assertNotIn("session_path", result)

    def test_unknown_busy_is_never_presented_as_idle(self):
        for busy in (None, "false", 0, [], PrivateObject()):
            result = format_pi_status({"busy": busy, "research_active": "0"})
            self.assertNotIn("当前空闲", result)
            self.assertIn("忙碌状态暂未确认", result)
            self.assertIn("模型通道：请求状态暂未确认", result)

    def test_malicious_model_objects_and_private_values_are_not_displayed(self):
        for model in (PrivateObject(), "/opt/private/credentials", "https://example.invalid/SECRET",
                      "xai/SECRET_PRIVATE_VALUE", "xai/grok-4.6-SECRET_PRIVATE_VALUE",
                      "xai/grok-4.6\nSECRET_PRIVATE_VALUE", "openai/gpt-SECRET_PRIVATE_VALUE", 123456789):
            result = format_pi_status({"model": model, "busy": False, "research_active": 0})
            self.assertIn("名称暂未确认", result)
            self.assertNotIn("SECRET", result)
            self.assertNotIn("/opt", result)
            self.assertNotIn("https", result)

    def test_invalid_status_object_is_safe(self):
        self.assertIn("暂时无法确认", format_pi_status(PrivateObject()))

    def test_untrusted_dict_subclass_is_not_called(self):
        class UnsafeDict(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("must not call user-defined get")
        self.assertIn("暂时无法确认", format_pi_status(UnsafeDict()))

    def test_research_count_rejects_boolean_negative_and_unbounded_values(self):
        for count in (True, -1, 999999, PrivateObject()):
            result = format_pi_status({"busy": False, "research_active": count})
            self.assertIn("模型通道：请求状态暂未确认", result)

    def test_menu_contains_five_text_actions(self):
        markup = feature_menu_markup()
        self.assertEqual(tuple(button.text for row in markup.keyboard for button in row), FEATURE_MENU_TEXTS)
        self.assertEqual(tuple(len(row) for row in markup.keyboard), (2, 2, 1))
        self.assertTrue(markup.resize_keyboard)
        for row in markup.keyboard:
            for button in row:
                self.assertFalse(button.request_contact)
                self.assertFalse(button.request_location)
                self.assertNotIn("订阅", button.text)
                self.assertNotIn("批准", button.text)

    def test_attachment_help_does_not_claim_support_before_enablement(self):
        self.assertIn("当前请先直接发文字", attachment_help_text(False))
        enabled = attachment_help_text(True)
        self.assertNotIn("即将开放", enabled)
        for expected in ("JPG/PNG/WebP", "1 MiB", "TXT/MD/CSV/JSON", "128 KiB", "已授权私聊",
                         "暂不支持 PDF、Office 文件或语音"):
            self.assertIn(expected, enabled)


class CommandEntryTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, authorized=True, chat_type="private", text="功能帮助"):
        bot = object.__new__(CommandsMixin)
        bot._authorized = AsyncMock(return_value=authorized)
        bot._reply = AsyncMock()
        bot._deliver_report = AsyncMock(return_value=[1])
        bot.application = SimpleNamespace(bot=object())
        bot.pi_channel = SimpleNamespace(status=AsyncMock(return_value={
            "model": "xai/grok-4.6", "busy": False, "research_active": 0}))
        bot._run_report = AsyncMock()
        bot._run_weekly_report = AsyncMock()
        bot._run_pi = AsyncMock()
        bot._run_agent = AsyncMock()
        bot.memory = SimpleNamespace(set_subscription=AsyncMock(), add_watch=AsyncMock(), remove_watch=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=42),
                                 effective_chat=SimpleNamespace(id=42, type=chat_type),
                                 effective_message=SimpleNamespace(text=text))
        context = SimpleNamespace(args=[])
        return bot, update, context

    async def test_start_is_one_mobile_message_with_menu_and_truthful_attachment_flag(self):
        bot, update, context = self.fixture()
        await bot.start_command(update, context)
        bot._authorized.assert_awaited_once_with(update, onboarding=True)
        bot._deliver_report.assert_awaited_once()
        args = bot._deliver_report.await_args
        text = args.args[2]
        self.assertLess(len(text), 1000)
        self.assertIn("不用切换模式", text)
        self.assertIn("不用录入真实持仓", text)
        self.assertIn("当前请先直接发文字", text)
        self.assertIn("reply_markup", args.kwargs)
        bot._run_pi.assert_not_awaited()

    async def test_help_preserves_existing_command_discovery(self):
        bot, update, context = self.fixture()
        bot.attachments_enabled = True
        await bot.help_command(update, context)
        text = bot._deliver_report.await_args.args[2]
        for command in ("/stock", "/market", "/report", "/weekly", "/watch", "/style", "/subscribe",
                        "/unsubscribe", "/status", "/new", "/abort", "/pistatus"):
            self.assertIn(command, text)
        self.assertNotIn("即将开放", text)
        self.assertLess(len(text), 1500)

    async def test_private_authorization_denial_blocks_all_feature_entries(self):
        for name in ("start_command", "help_command", "pi_status_command", "feature_menu_message"):
            with self.subTest(handler=name):
                bot, update, context = self.fixture(authorized=False)
                await getattr(bot, name)(update, context)
                bot._deliver_report.assert_not_awaited()
                bot.pi_channel.status.assert_not_awaited()
                bot._run_report.assert_not_awaited()

    async def test_non_private_rejection_precedes_authorization_touch(self):
        for name in ("start_command", "help_command", "pi_status_command", "feature_menu_message"):
            with self.subTest(handler=name):
                bot, update, context = self.fixture(chat_type="group")
                await getattr(bot, name)(update, context)
                bot._authorized.assert_not_awaited()
                bot._deliver_report.assert_not_awaited()
                bot.pi_channel.status.assert_not_awaited()
                self.assertEqual(bot._reply.await_args.args[1], "请在与机器人的私聊中使用此功能。")

    async def test_morning_and_weekly_menu_use_existing_handlers_without_subscription_changes(self):
        for label, expected in (("海外晨报", "_run_report"), ("本周简报", "_run_weekly_report")):
            bot, update, context = self.fixture(text=label)
            await bot.feature_menu_message(update, context)
            getattr(bot, expected).assert_awaited_once_with(update)
            bot.memory.set_subscription.assert_not_awaited()
            bot.memory.add_watch.assert_not_awaited()
            bot.memory.remove_watch.assert_not_awaited()
            bot._run_pi.assert_not_awaited()
            bot._run_agent.assert_not_awaited()

    async def test_help_and_status_buttons_do_not_become_pi_prompts(self):
        for label in ("功能帮助", "任务状态"):
            bot, update, context = self.fixture(text=label)
            await bot.feature_menu_message(update, context)
            bot._run_pi.assert_not_awaited()
            bot._run_agent.assert_not_awaited()
            if label == "任务状态":
                bot.pi_channel.status.assert_awaited_once()
                self.assertIn("Grok 4.6", bot._reply.await_args.args[1])

    async def test_unknown_menu_text_has_no_side_effects(self):
        bot, update, context = self.fixture(text="批准订单")
        await bot.feature_menu_message(update, context)
        bot._authorized.assert_not_awaited()
        bot._reply.assert_not_awaited()
        bot._run_pi.assert_not_awaited()
        bot._run_report.assert_not_awaited()

    async def test_status_errors_do_not_echo_exception_details(self):
        for error in (PiChannelError("SECRET_PRIVATE_VALUE"), RuntimeError("SECRET_PRIVATE_VALUE")):
            bot, update, context = self.fixture()
            bot.pi_channel.status.side_effect = error
            with self.assertLogs("mini_hermes.gateway.telegram.commands", level="WARNING") as logs:
                await bot.pi_status_command(update, context)
            self.assertEqual(bot._reply.await_args.args[1], "任务状态暂时无法读取，请稍后再试 /pistatus。")
            self.assertNotIn("SECRET", "\n".join(logs.output))

    async def test_existing_plain_text_and_stock_routing_remain_unchanged(self):
        bot, update, context = self.fixture(text="帮我解释这个报错")
        await bot.text_message(update, context)
        bot._run_pi.assert_awaited_once_with(update, update.effective_message.text)
        update.effective_message.text = "600875"
        await bot.text_message(update, context)
        bot._run_agent.assert_awaited_once()
        self.assertIn("600875", bot._run_agent.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
