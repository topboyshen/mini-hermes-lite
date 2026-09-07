from hashlib import sha256
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

    async def test_market_report_history_is_dated_and_updatable(self) -> None:
        await self.store.save_market_report("2026-08-28", "first")
        await self.store.save_market_report("2026-08-29", "second")
        await self.store.save_market_report("2026-08-28", "first updated")

        self.assertEqual(
            await self.store.market_reports_between("2026-08-23", "2026-08-29"),
            [
                ("2026-08-28", "first updated"),
                ("2026-08-29", "second"),
            ],
        )
        self.assertEqual(await self.store.market_report("2026-08-28"), "first updated")
        self.assertIsNone(await self.store.market_report("2026-08-27"))

        first_id = sha256(b"first").hexdigest()[:32]
        updated_id = sha256(b"first updated").hexdigest()[:32]
        self.assertEqual(await self.store.market_report_detail(first_id), "first")
        self.assertEqual(
            await self.store.market_report_detail(updated_id),
            "first updated",
        )
        self.assertIsNone(await self.store.market_report_detail("invalid"))

    async def test_empty_market_report_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.store.save_market_report("2026-08-29", "   ")

if __name__ == "__main__":
    unittest.main()
