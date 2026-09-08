"""Offline admission tests; loading this pure module requires no Telegram SDK."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


_PATH = Path(__file__).resolve().parents[1] / "app/mini_hermes/gateway/telegram/background.py"
_SPEC = importlib.util.spec_from_file_location("mini_hermes_background_ux_tests", _PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
bounded_background = _MODULE.bounded_background
BACKGROUND_BUSY_TEXT = _MODULE.BACKGROUND_BUSY_TEXT


class BareBot:
    def __init__(self):
        raise AssertionError("tests must not initialize a real bot")


class BackgroundAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tasks = []
        self.bot = object.__new__(BareBot)
        self.bot.settings = SimpleNamespace(allowed_user_ids={42})
        self.bot._authorized = AsyncMock(return_value=False)
        self.bot._reply = AsyncMock()
        self.update = SimpleNamespace(effective_user=SimpleNamespace(id=42))
        self.context = object()

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def launch(self, handler, update=None):
        task = asyncio.create_task(handler(update or self.update, self.context))
        self.tasks.append(task)
        return task

    async def two_blocked(self, *, separate_wrappers=False):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def work(update, context):
            calls.append((update, context))
            if len(calls) == 2:
                entered.set()
            await release.wait()
            return "done"

        first = bounded_background(self.bot, work)
        second = bounded_background(self.bot, work) if separate_wrappers else first
        tasks = (self.launch(first), self.launch(second))
        await asyncio.wait_for(entered.wait(), timeout=1)
        self.assertEqual(self.bot._ux_background_active, 2)
        return first, tasks, calls, release

    async def test_two_long_tasks_reject_third_without_blocking_short_control(self):
        handler, tasks, calls, release = await self.two_blocked()
        await handler(self.update, self.context)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.bot._ux_background_active, 2)
        self.bot._reply.assert_awaited_once_with(self.update, BACKGROUND_BUSY_TEXT)
        control = AsyncMock(return_value="control finished")
        self.assertEqual(await control(self.update, self.context), "control finished")
        self.assertTrue(all(not task.done() for task in tasks))
        release.set()
        self.assertEqual(await asyncio.gather(*tasks), ["done", "done"])
        self.assertEqual(self.bot._ux_background_active, 0)
        self.assertEqual(len(calls), 2, "rejected work must not be queued")

    async def test_different_wrappers_share_the_same_bot_limit(self):
        _, _, calls, _ = await self.two_blocked(separate_wrappers=True)
        other = AsyncMock()
        await bounded_background(self.bot, other)(self.update, self.context)
        other.assert_not_awaited()
        self.assertEqual(len(calls), 2)
        self.bot._reply.assert_awaited_once()

    async def test_cancellation_releases_a_slot_and_propagates(self):
        _, tasks, _, _ = await self.two_blocked()
        tasks[0].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tasks[0]
        self.assertEqual(self.bot._ux_background_active, 1)
        callback = AsyncMock(return_value="accepted")
        result = await bounded_background(self.bot, callback)(self.update, self.context)
        self.assertEqual(result, "accepted")
        callback.assert_awaited_once_with(self.update, self.context)
        self.assertEqual(self.bot._ux_background_active, 1)
        self.bot._reply.assert_not_awaited()

    async def test_exception_releases_slot_and_propagates(self):
        callback = AsyncMock(side_effect=RuntimeError("offline failure"))
        handler = bounded_background(self.bot, callback)
        with self.assertRaisesRegex(RuntimeError, "offline failure"):
            await handler(self.update, self.context)
        self.assertEqual(self.bot._ux_background_active, 0)
        callback.side_effect = None
        callback.return_value = "retry ok"
        self.assertEqual(await handler(self.update, self.context), "retry ok")
        self.assertEqual(self.bot._ux_background_active, 0)

    async def test_unauthorized_user_never_takes_slot_even_if_authorizer_returns_true(self):
        denied = SimpleNamespace(effective_user=SimpleNamespace(id=99))
        self.bot._authorized.return_value = True
        callback = AsyncMock()
        await bounded_background(self.bot, callback)(denied, self.context)
        self.bot._authorized.assert_awaited_once_with(denied)
        callback.assert_not_awaited()
        self.bot._reply.assert_not_awaited()
        self.assertFalse(hasattr(self.bot, "_ux_background_active"))

    async def test_unauthorized_user_during_full_load_is_not_given_busy_reply(self):
        handler, _, _, _ = await self.two_blocked()
        denied = SimpleNamespace(effective_user=SimpleNamespace(id=99))
        await handler(denied, self.context)
        self.bot._authorized.assert_awaited_once_with(denied)
        self.bot._reply.assert_not_awaited()
        self.assertEqual(self.bot._ux_background_active, 2)

    async def test_slow_unauthorized_check_does_not_take_a_slot(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def authorize(update):
            entered.set()
            await release.wait()
            return False

        self.bot._authorized.side_effect = authorize
        callback = AsyncMock()
        handler = bounded_background(self.bot, callback)
        denied_task = self.launch(handler, SimpleNamespace(effective_user=SimpleNamespace(id=99)))
        await asyncio.wait_for(entered.wait(), timeout=1)
        self.assertFalse(hasattr(self.bot, "_ux_background_active"))
        await handler(self.update, self.context)
        callback.assert_awaited_once_with(self.update, self.context)
        release.set()
        await denied_task
        self.assertEqual(self.bot._ux_background_active, 0)

    async def test_missing_user_is_denied_without_callback(self):
        callback = AsyncMock()
        update = SimpleNamespace(effective_user=None)
        await bounded_background(self.bot, callback)(update, self.context)
        self.bot._authorized.assert_awaited_once_with(update)
        callback.assert_not_awaited()
        self.assertFalse(hasattr(self.bot, "_ux_background_active"))

    async def test_lazily_initialized_slot_is_reserved_before_callback_first_await(self):
        observed = []

        async def callback(update, context):
            observed.append(self.bot._ux_background_active)
            await asyncio.sleep(0)
            return "one result"

        self.assertFalse(hasattr(self.bot, "_ux_background_active"))
        self.assertEqual(await bounded_background(self.bot, callback)(self.update, self.context), "one result")
        self.assertEqual(observed, [1])
        self.assertEqual(self.bot._ux_background_active, 0)
        self.bot._authorized.assert_not_awaited()

    async def test_different_bot_instances_have_independent_limits(self):
        await self.two_blocked()
        other = object.__new__(BareBot)
        other.settings = self.bot.settings
        other._authorized = AsyncMock()
        other._reply = AsyncMock()
        callback = AsyncMock(return_value="independent")
        self.assertEqual(await bounded_background(other, callback)(self.update, self.context), "independent")
        self.assertEqual(other._ux_background_active, 0)
        self.assertEqual(self.bot._ux_background_active, 2)
        other._reply.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
