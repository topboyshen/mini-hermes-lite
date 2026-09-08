import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mini_hermes.agent.service import AgentService


class AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_pi_model_does_not_use_legacy_openai_credentials(self) -> None:
        service = object.__new__(AgentService)
        service.settings = SimpleNamespace(
            pi_channel_socket=Path('/tmp/research-test.sock'),
            agent_timeout_seconds=120,
        )
        with patch.dict(os.environ, {
            'OPENAI_BASE_URL': 'https://legacy.invalid/v1',
            'OPENAI_API_KEY': 'not-a-real-key',
        }), patch('mini_hermes.agent.service.OpenAIProvider') as provider:
            model = service._build_model('pi:grok-4.6')
        self.assertEqual(model.model_name, 'grok-4.6')
        provider.assert_not_called()

    async def test_unknown_pi_model_does_not_fall_back(self) -> None:
        service = object.__new__(AgentService)
        with patch('mini_hermes.agent.service.OpenAIProvider') as provider:
            with self.assertRaisesRegex(ValueError, 'unsupported pi research model'):
                service._build_model('pi:unknown')
        provider.assert_not_called()

    async def test_interactive_agent_uses_and_persists_memory(self) -> None:
        service = object.__new__(AgentService)
        service._started = True
        service._semaphore = asyncio.Semaphore(1)
        service.settings = SimpleNamespace(
            agent_timeout_seconds=5,
            max_history_messages=8,
            model="pi:grok-4.6",
        )
        service._agent = SimpleNamespace(
            run=AsyncMock(return_value=SimpleNamespace(output="研究结果"))
        )
        service.memory = SimpleNamespace(
            recent_messages=AsyncMock(return_value=[("user", "上一轮")]),
            list_watch=AsyncMock(return_value=["600875"]),
            get_style=AsyncMock(return_value="稳健"),
            append_turn=AsyncMock(),
            get_thinking=AsyncMock(return_value="high"),
        )

        result = await service.run(42, "分析东方电气")

        self.assertEqual(result, "研究结果")
        prompt = service._agent.run.await_args.args[0]
        self.assertEqual(service._agent.run.await_args.kwargs, {"model_settings": {"pi_thinking": "high"}})
        self.assertIn("投资风格：稳健", prompt)
        self.assertIn("自选股：600875", prompt)
        service.memory.recent_messages.assert_awaited_once_with(
            42, 8, channel="hermes"
        )
        service.memory.append_turn.assert_awaited_once_with(
            42, "分析东方电气", "研究结果", channel="hermes"
        )


if __name__ == "__main__":
    unittest.main()
