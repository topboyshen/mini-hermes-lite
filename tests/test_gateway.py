from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mini_hermes.gateway.telegram import MiniHermesBot


class GatewayRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_text_always_routes_to_pi(self) -> None:
        bot = object.__new__(MiniHermesBot)
        bot._run_pi = AsyncMock()
        update = SimpleNamespace(effective_message=SimpleNamespace(text="hello pi"))

        await bot.text_message(update, None)

        bot._run_pi.assert_awaited_once_with(update, "hello pi")

    async def test_pi_rejects_non_private_chat(self) -> None:
        bot = object.__new__(MiniHermesBot)
        bot._authorized = AsyncMock(return_value=True)
        bot._reply = AsyncMock()
        bot.pi_channel = SimpleNamespace(prompt=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=-1001, type="group"),
        )

        await bot._run_pi(update, "run a command")

        bot._reply.assert_awaited_once()
        bot.pi_channel.prompt.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
