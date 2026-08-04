import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mini_hermes.agent.service import AgentService


class AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_interactive_agent_uses_and_persists_memory(self) -> None:
        service = object.__new__(AgentService)
        service._started = True
        service._semaphore = asyncio.Semaphore(1)
        service.settings = SimpleNamespace(
            agent_timeout_seconds=5,
            max_history_messages=8,
        )
        service._agent = SimpleNamespace(
            run=AsyncMock(return_value=SimpleNamespace(output="研究结果"))
        )
        service.memory = SimpleNamespace(
            recent_messages=AsyncMock(return_value=[("user", "上一轮")]),
            list_watch=AsyncMock(return_value=["600875"]),
            get_style=AsyncMock(return_value="稳健"),
            append_turn=AsyncMock(),
        )

        result = await service.run(42, "分析东方电气")

        self.assertEqual(result, "研究结果")
        prompt = service._agent.run.await_args.args[0]
        self.assertIn("投资风格：稳健", prompt)
        self.assertIn("自选股：600875", prompt)
        service.memory.append_turn.assert_awaited_once_with(
            42, "分析东方电气", "研究结果"
        )


if __name__ == "__main__":
    unittest.main()
