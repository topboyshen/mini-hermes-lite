import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mini_hermes.gateway.telegram.progress import TaskProgress, progress_stage


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    def test_tool_names_map_to_fixed_safe_descriptions(self):
        self.assertEqual(progress_stage("正在调用工具：read"), "正在读取资料")
        self.assertEqual(progress_stage("正在调用工具：bash"), "正在运行工作区任务")
        self.assertEqual(progress_stage("正在调用工具：SECRET https://private"), "正在使用工具处理")
        self.assertNotIn("SECRET", progress_stage("SECRET https://private"))

    def test_wait_retry_and_generation_are_not_fake_percentages(self):
        for stage in ("正在等待前一个任务", "连接波动，正在自动重试", "正在生成回复"):
            self.assertNotIn("%", progress_stage(stage))
            self.assertNotIn("完成！", progress_stage(stage))

    def test_elapsed_time_uses_monotonic_clock(self):
        tracker = TaskProgress(object(), started=100)
        with patch("mini_hermes.gateway.telegram.progress.time.monotonic", return_value=173):
            self.assertIn("1 分 13 秒", tracker.render())
            self.assertIn("/abort", tracker.render())

    async def test_callback_does_not_wait_for_telegram(self):
        message = SimpleNamespace(edit_text=AsyncMock())
        tracker = TaskProgress(message)
        await tracker.update("正在生成回复")
        self.assertEqual(tracker.stage, "正在生成回复")
        message.edit_text.assert_not_awaited()

    async def test_fast_request_creates_no_extra_progress_edits(self):
        message = SimpleNamespace(edit_text=AsyncMock())
        tracker = TaskProgress(message)
        tracker.start()
        await tracker.update("正在调用工具：read")
        await tracker.stop()
        message.edit_text.assert_not_awaited()
        self.assertIsNone(tracker._task)

    async def test_stop_cancels_inflight_edit_before_final_answer(self):
        entered, ended = asyncio.Event(), asyncio.Event()

        async def slow_edit(text):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

        tracker = TaskProgress(SimpleNamespace(edit_text=slow_edit))
        with patch("mini_hermes.gateway.telegram.progress.UPDATE_INTERVAL", 0):
            tracker.start()
            await asyncio.wait_for(entered.wait(), 1)
            await tracker.stop()
        self.assertTrue(ended.is_set())

    async def test_telegram_failure_does_not_log_raw_error(self):
        called = asyncio.Event()

        async def failing_edit(text):
            called.set()
            raise RuntimeError("SYNTHETIC_SECRET")

        tracker = TaskProgress(SimpleNamespace(edit_text=failing_edit))
        with patch("mini_hermes.gateway.telegram.progress.UPDATE_INTERVAL", 0.001):
            with self.assertLogs("mini_hermes.gateway.telegram.progress", level="DEBUG") as logs:
                tracker.start()
                await asyncio.wait_for(called.wait(), 1)
                await tracker.stop()
        self.assertNotIn("SYNTHETIC_SECRET", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
