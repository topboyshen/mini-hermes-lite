import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mini_hermes.pi_channel.client import PiChannelClient, PiChannelError
from mini_hermes.pi_channel.errors import error_message


class ClientFailureTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, event=None, raw=None, delay=False):
        reader = asyncio.StreamReader()
        if not delay:
            if raw is None:
                raw = (json.dumps(event) + "\n").encode()
            reader.feed_data(raw)
            reader.feed_eof()
        writer = SimpleNamespace(write=lambda data: None, drain=AsyncMock(),
                                 close=lambda: None, wait_closed=AsyncMock())
        client = PiChannelClient(Path("/unused/pi.sock"), timeout_seconds=0.01)
        with patch.object(client, "_connect", AsyncMock(return_value=(reader, writer))):
            try:
                return await client.prompt(42, "test")
            finally:
                writer.wait_closed.assert_awaited_once()

    async def test_typed_errors_are_actionable_and_hide_raw_provider_text(self):
        for code in ("auth_required", "quota", "rate_limited", "timeout", "cancelled"):
            with self.subTest(code=code), self.assertRaises(PiChannelError) as caught:
                await self.request({"type": "result", "ok": False,
                                    "error_code": code, "error": "SYNTHETIC_SECRET"})
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(str(caught.exception), error_message(code))
            self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))

    async def test_unknown_code_is_safe_and_not_reclassified_from_text(self):
        for code in ("SYNTHETIC_SECRET", None, ["auth_required"], {"unexpected": "secret"}):
            with self.subTest(code=code), self.assertRaises(PiChannelError) as caught:
                await self.request({"type": "result", "ok": False,
                                    "error_code": code, "error": "401 token=secret"})
            self.assertEqual(caught.exception.code, "upstream_error")
            self.assertNotIn("secret", str(caught.exception))

    async def test_legacy_error_can_be_classified_without_echo(self):
        with self.assertRaises(PiChannelError) as caught:
            await self.request({"type": "result", "ok": False,
                                "error": "HTTP 401 unauthorized SYNTHETIC_SECRET"})
        self.assertEqual(caught.exception.code, "auth_required")
        self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))

    async def test_malformed_or_empty_success_is_not_success(self):
        for raw in (b"[]\n", b"invalid\n", b'{"type":"result","ok":true,"text":" "}\n',
                    b'{"type":"result","ok":true,"text":null}\n'):
            with self.subTest(raw=raw), self.assertRaises(PiChannelError) as caught:
                await self.request(raw=raw)
            self.assertEqual(caught.exception.code, "invalid_response")

    async def test_closed_connection_is_distinct_from_timeout(self):
        with self.assertRaises(PiChannelError) as caught:
            await self.request(raw=b"")
        self.assertEqual(caught.exception.code, "channel_unavailable")
        with self.assertRaises(PiChannelError) as caught:
            await self.request(delay=True)
        self.assertEqual(caught.exception.code, "timeout")

    async def test_connection_error_does_not_expose_local_paths(self):
        client = PiChannelClient(Path("/unused/SYNTHETIC_SECRET.sock"))
        with patch("asyncio.open_unix_connection", AsyncMock(side_effect=FileNotFoundError("secret"))):
            with self.assertRaises(PiChannelError) as caught:
                await client.status()
        self.assertEqual(caught.exception.code, "channel_unavailable")
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
