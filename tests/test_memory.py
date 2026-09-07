from pathlib import Path
import tempfile
import unittest

from mini_hermes.memory import MemoryStore


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tempdir.name) / "memory.db")
        await self.store.initialize()
        await self.store.touch_user(42, "tester", "Test", 99)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.tempdir.cleanup()

    async def test_watchlist_and_profile(self) -> None:
        self.assertTrue(await self.store.has_user(42))
        self.assertFalse(await self.store.has_user(404))
        self.assertEqual(await self.store.get_last_chat_id(42), 99)
        self.assertTrue(await self.store.add_watch(42, "600875"))
        self.assertFalse(await self.store.add_watch(42, "600875"))
        self.assertEqual(await self.store.list_watch(42), ["600875"])
        await self.store.set_style(42, "稳健中线")
        self.assertEqual(await self.store.get_style(42), "稳健中线")

    async def test_history_and_subscription(self) -> None:
        await self.store.append_turn(42, "分析东方电气", "研究结果")
        self.assertEqual(
            await self.store.recent_messages(42, 2),
            [("user", "分析东方电气"), ("assistant", "研究结果")],
        )
        await self.store.set_subscription(42, 99, True)
        self.assertEqual(await self.store.subscriptions(), [(42, 99)])

    async def test_hermes_and_pi_channels_are_isolated(self) -> None:
        await self.store.append_turn(42, "分析东方电气", "hermes答复", channel="hermes")
        await self.store.append_turn(42, "fix bug", "pi答复", channel="pi")

        self.assertEqual(
            await self.store.recent_messages(42, 10, channel="hermes"),
            [("user", "分析东方电气"), ("assistant", "hermes答复")],
        )
        self.assertEqual(
            await self.store.recent_messages(42, 10, channel="pi"),
            [("user", "fix bug"), ("assistant", "pi答复")],
        )
        # Default channel remains hermes so AgentService never sees pi turns.
        self.assertEqual(
            await self.store.recent_messages(42, 10),
            [("user", "分析东方电气"), ("assistant", "hermes答复")],
        )


if __name__ == "__main__":
    unittest.main()
