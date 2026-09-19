"""Offline preference storage, Telegram ownership and in-flight isolation."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mini_hermes.agent.service import AgentService
from mini_hermes.gateway.telegram.bot import MiniHermesBot
from mini_hermes.gateway.telegram.commands import CommandsMixin
from mini_hermes.gateway.telegram.thinking import THINKING_CALLBACK, thinking_menu
from mini_hermes.memory import MemoryStore
from mini_hermes.pi_channel.broker import PiChannelBroker
from mini_hermes.pi_channel.client import PiChannelClient, PiChannelError
from mini_hermes.thinking import THINKING_LEVELS, validate_thinking
from test_pi_errors import Process, Writer, assistant, finished

import os

# Tests exercise the deployed contract: the unit declares the model explicitly.
os.environ.setdefault("PI_CHANNEL_MODEL", "xai/grok-4.6")


class PreferenceStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "memory.db"
        self.store = MemoryStore(self.path)
        await self.store.initialize()
        for identity in (42, 43):
            await self.store.touch_user(identity, None, None, identity)

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def test_defaults_do_not_create_preferences_or_alter_history(self):
        await self.store.append_turn(42, "synthetic history", "synthetic reply")
        for scope in ("chat", "research"):
            self.assertEqual(await self.store.get_thinking(42, scope), "low")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM thinking_preferences").fetchone()[0], 0)
        self.assertEqual(len(await self.store.recent_messages(42, 10)), 2)

    async def test_scope_user_isolation_and_restart_persistence(self):
        await self.store.set_thinking(42, "chat", "medium")
        await self.store.set_thinking(42, "research", "xhigh")
        await self.store.close()
        self.store = MemoryStore(self.path)
        await self.store.initialize()
        self.assertEqual(await self.store.get_thinking(42, "chat"), "medium")
        self.assertEqual(await self.store.get_thinking(42, "research"), "xhigh")
        self.assertEqual(await self.store.get_thinking(43, "research"), "low")

    async def test_invalid_values_never_overwrite_saved_selection(self):
        await self.store.set_thinking(42, "chat", "high")
        for bad in (None, True, [], {}, "off", "minimal", "max", "HIGH", "high;exit", "low\n"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                await self.store.set_thinking(42, "chat", bad)
        for bad in (None, [], "provider", "chat;DROP TABLE users"):
            with self.assertRaises(ValueError):
                await self.store.set_thinking(42, bad, "low")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.store.set_thinking(99, "chat", "low")
        self.assertEqual(await self.store.get_thinking(42, "chat"), "high")

    async def test_old_database_adds_only_new_table_and_keeps_business_data(self):
        await self.store.append_turn(42, "old prompt", "old answer")
        await self.store.set_subscription(42, 42, True)
        await self.store.close()
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE thinking_preferences")
        await self.store.initialize()
        self.assertEqual(await self.store.get_thinking(42, "chat"), "low")
        self.assertEqual(await self.store.subscriptions(), [(42, 42)])
        self.assertEqual(await self.store.recent_messages(42, 10), [("user", "old prompt"), ("assistant", "old answer")])


class ThinkingMenuTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, identity=42, chat_type="private", data="think:v1:42:chat:high"):
        levels = {"chat": "low", "research": "medium"}
        bot = object.__new__(CommandsMixin)
        bot.settings = SimpleNamespace(allowed_user_ids={42, 43})
        bot._authorized = AsyncMock(return_value=True)
        bot._touch = AsyncMock()
        bot._reply = AsyncMock()
        bot._deliver_report = AsyncMock()
        bot.application = SimpleNamespace(bot=object())
        async def set_level(user, scope, level):
            levels[scope] = level
        bot.memory = SimpleNamespace(get_thinking=AsyncMock(side_effect=lambda user, scope: levels[scope]),
                                     set_thinking=AsyncMock(side_effect=set_level))
        query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=identity),
                                 effective_chat=SimpleNamespace(id=identity, type=chat_type))
        return bot, update, levels

    async def test_open_menu_is_read_only_and_all_buttons_are_bounded(self):
        bot, update, _ = self.fixture()
        await bot.thinking_command(update, None)
        bot.memory.set_thinking.assert_not_awaited()
        text = bot._deliver_report.await_args.args[2]
        self.assertIn("普通聊天：快速（low）", text)
        markup = bot._deliver_report.await_args.kwargs["reply_markup"]
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertEqual(len(buttons), 8)
        for button in buttons:
            self.assertIsNotNone(THINKING_CALLBACK.fullmatch(button.callback_data))
            self.assertLessEqual(len(button.callback_data.encode()), 64)
            self.assertIsNone(button.url)

    async def test_private_owner_selection_changes_only_selected_scope(self):
        bot, update, levels = self.fixture()
        await bot.thinking_callback(update, None)
        self.assertEqual(levels, {"chat": "high", "research": "medium"})
        bot.memory.set_thinking.assert_awaited_once_with(42, "chat", "high")
        self.assertIn("✓ 聊天·深入", str(update.callback_query.edit_message_text.await_args))

    async def test_unauthorized_group_and_other_owners_cannot_change_or_read(self):
        for options in ({"identity":99}, {"identity":43}, {"chat_type":"group"},
                        {"chat_type":"supergroup"}, {"data":"think:v1:42:chat:max"},
                        {"data":"think:v1:42:chat:high\n"}, {"data":[]}, {"data":None}):
            bot, update, _ = self.fixture(**options)
            await bot.thinking_callback(update, None)
            bot.memory.set_thinking.assert_not_awaited()
            bot.memory.get_thinking.assert_not_awaited()
            bot._touch.assert_not_awaited()

    async def test_storage_failure_does_not_claim_success(self):
        bot, update, _ = self.fixture()
        bot.memory.set_thinking.side_effect = RuntimeError("SYNTHETIC_PRIVATE")
        await bot.thinking_callback(update, None)
        self.assertIn("未能确认保存", bot._reply.await_args.args[1])
        self.assertNotIn("SYNTHETIC", bot._reply.await_args.args[1])
        update.callback_query.edit_message_text.assert_not_awaited()

    async def test_ui_failure_does_not_undo_saved_choice(self):
        bot, update, levels = self.fixture()
        update.callback_query.answer.side_effect = RuntimeError("network")
        update.callback_query.edit_message_text.side_effect = RuntimeError("network")
        await bot.thinking_callback(update, None)
        self.assertEqual(levels["chat"], "high")
        self.assertIn("已保存", bot._reply.await_args.args[1])

    async def test_menu_open_fails_closed_on_auth_and_storage(self):
        bot, update, _ = self.fixture()
        bot._authorized.return_value = False
        await bot.thinking_command(update, None)
        bot.memory.get_thinking.assert_not_awaited()
        bot._authorized.return_value = True
        bot.memory.get_thinking.side_effect = RuntimeError("SYNTHETIC_PRIVATE")
        await bot.thinking_command(update, None)
        bot._deliver_report.assert_not_awaited()
        self.assertIn("暂时无法读取", bot._reply.await_args.args[1])


class ThinkingSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_snapshot_survives_setting_change_during_progress(self):
        from test_pi_gateway_errors import PiGatewayFailureTests
        bot, update = PiGatewayFailureTests().fixture()
        bot.memory.get_thinking.return_value = "medium"
        progress = SimpleNamespace(edit_text=AsyncMock())
        async def receipt(*args, **kwargs):
            bot.memory.get_thinking.return_value = "xhigh"
            return progress
        update.effective_message.reply_text.side_effect = receipt
        await bot._run_pi(update, "synthetic")
        self.assertEqual(bot.pi_channel.prompt.await_args.kwargs["thinking"], "medium")
        bot.memory.get_thinking.assert_awaited_once_with(42, "chat")

    async def test_research_snapshot_is_per_run_even_when_queued(self):
        service = object.__new__(AgentService)
        service._started = True
        service.settings = SimpleNamespace(model="pi:grok-4.6", agent_timeout_seconds=2, max_history_messages=4)
        service._semaphore = asyncio.Semaphore(0)
        read = asyncio.Event()
        async def history(*args, **kwargs):
            read.set()
            return []
        service.memory = SimpleNamespace(get_thinking=AsyncMock(return_value="high"),
            recent_messages=AsyncMock(side_effect=history), list_watch=AsyncMock(return_value=[]),
            get_style=AsyncMock(return_value=""), append_turn=AsyncMock())
        service._agent = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(output="synthetic")))
        task = asyncio.create_task(service.run(42, "synthetic"))
        await asyncio.wait_for(read.wait(), 1)
        service.memory.get_thinking.return_value = "low"
        service._semaphore.release()
        await asyncio.wait_for(task, 1)
        self.assertEqual(service._agent.run.await_args.kwargs, {"model_settings":{"pi_thinking":"high"}})
        await service.run(42, "next synthetic")
        self.assertEqual(service._agent.run.await_args.kwargs, {"model_settings":{"pi_thinking":"low"}})

    async def test_cli_uses_all_four_levels_without_mutating_broker_default(self):
        broker = PiChannelBroker()
        broker.thinking = "low"
        broker._session_path = lambda user: Path("/tmp/synthetic-not-created.jsonl")
        broker._maintain_context = AsyncMock()
        for level in THINKING_LEVELS:
            process = Process(finished(assistant("synthetic")))
            with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
                writer = Writer()
                await broker._run_prompt(42, "synthetic", writer, thinking=level)
            command = spawn.await_args.args
            self.assertEqual(command[command.index("--thinking") + 1], level)
            self.assertEqual(broker.thinking, "low")
            self.assertTrue(writer.results()[0]["ok"])

    async def test_client_rejects_bad_values_without_connecting(self):
        client = PiChannelClient(Path("/tmp/synthetic-none.sock"))
        client._connect = AsyncMock()
        for bad in ("off", "max", [], 1, "high --model other"):
            with self.assertRaises(PiChannelError):
                await client.prompt(42, "synthetic", thinking=bad)
        client._connect.assert_not_awaited()

    async def test_broker_snapshots_explicit_level_before_waiting_for_lock(self):
        broker = PiChannelBroker()
        broker.thinking = "low"
        broker._authorize_research = Mock()
        arrived = asyncio.Event()
        async def send(writer, event):
            if event["type"] == "progress":
                arrived.set()
        broker._send = send
        broker._run_prompt = AsyncMock()
        await broker._lock.acquire()
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps({"action":"prompt", "user_id":42, "message":"synthetic", "thinking":"high"}) + "\n").encode())
        task = asyncio.create_task(broker._handle(reader, Writer()))
        await asyncio.wait_for(arrived.wait(), 1)
        broker.thinking = "medium"
        broker._lock.release()
        await asyncio.wait_for(task, 1)
        self.assertEqual(broker._run_prompt.await_args.kwargs, {"thinking":"high"})
        broker._authorize_research.assert_called_once()

    async def test_untrusted_peer_or_level_cannot_launch_a_prompt(self):
        for bad_peer, level in ((True,"high"), (False,"off"), (False,None), (False,{})):
            broker = PiChannelBroker()
            broker._authorize_research = Mock(side_effect=ValueError("not authorized") if bad_peer else None)
            broker._run_prompt = AsyncMock()
            reader = asyncio.StreamReader()
            reader.feed_data((json.dumps({"action":"prompt", "user_id":42, "message":"synthetic", "thinking":level}) + "\n").encode())
            writer = Writer()
            await broker._handle(reader, writer)
            self.assertEqual(writer.results()[0]["error_code"], "invalid_request")
            broker._run_prompt.assert_not_awaited()
