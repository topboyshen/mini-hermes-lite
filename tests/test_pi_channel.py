from pathlib import Path
import asyncio
import json
import tempfile
import unittest

from mini_hermes.pi_channel.broker import _message_text, _tool_name
from mini_hermes.pi_channel.client import PiChannelClient


class BrokerHelperTests(unittest.TestCase):
    def test_extracts_final_text_and_tool_name(self) -> None:
        message = {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "done"},
            ],
        }
        self.assertEqual(_message_text(message), "done")
        self.assertEqual(_tool_name({"toolName": "bash"}), "bash")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tempdir.name) / "pi.sock"

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            request = json.loads(await reader.readline())
            self.assertEqual(request["action"], "prompt")
            writer.write(b'{"type":"progress","text":"working"}\n')
            writer.write(b'{"type":"result","ok":true,"text":"answer"}\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        self.server = await asyncio.start_unix_server(handler, path=self.socket_path)

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()
        self.tempdir.cleanup()

    async def test_prompt_streams_progress_and_returns_text(self) -> None:
        progress: list[str] = []
        client = PiChannelClient(self.socket_path, timeout_seconds=2)

        async def on_progress(text: str) -> None:
            progress.append(text)

        self.assertEqual(await client.prompt(42, "hello", on_progress), "answer")
        self.assertEqual(progress, ["working"])


if __name__ == "__main__":
    unittest.main()
