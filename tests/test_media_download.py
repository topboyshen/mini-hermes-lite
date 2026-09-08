import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mini_hermes.gateway.telegram.media import download_telegram_file

TEST_URL = "https://api.telegram.org/file/bot123:SYNTHETIC/photos/file_1.jpg"


class StreamingDownloadTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, chunks=(b"abc",), headers=None, status=200, failure=None):
        state = {"reads": 0, "closed": False}

        class Response:
            status_code = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                state["closed"] = True

            async def aiter_bytes(self, chunk_size):
                for chunk in chunks:
                    state["reads"] += 1
                    yield chunk
                if failure is not None:
                    raise failure

        response = Response()
        response.headers = headers or {}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def stream(self, method, url, headers):
                state.update(method=method, url=url, headers=headers)
                return response

        client_patch = patch("mini_hermes.gateway.telegram.media.httpx.AsyncClient", return_value=Client())
        client = client_patch.start()
        self.addCleanup(client_patch.stop)
        return SimpleNamespace(file_path=TEST_URL), state, client

    async def test_streaming_success_without_content_length(self):
        remote, state, client = self.fixture(chunks=(b"ab", b"cd"))
        self.assertEqual(await download_telegram_file(remote, 4), b"abcd")
        self.assertTrue(state["closed"])
        self.assertFalse(client.call_args.kwargs["follow_redirects"])
        self.assertFalse(client.call_args.kwargs["trust_env"])
        self.assertEqual(state["headers"], {"Accept-Encoding": "identity"})

    async def test_invalid_urls_never_open_http(self):
        remote, state, client = self.fixture()
        for url in ("/tmp/private", "file:///etc/passwd", "http://api.telegram.org/file/botx/x",
                    "https://127.0.0.1/file/botx/x", "https://api.telegram.org.evil/file/botx/x",
                    "https://api.telegram.org:8443/file/botx/x", "https://user@api.telegram.org/file/botx/x",
                    "https://api.telegram.org/botx/getMe", TEST_URL + "?token=secret", TEST_URL + "#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await download_telegram_file(SimpleNamespace(file_path=url), 10)
        client.assert_not_called()

    async def test_declared_oversize_is_rejected_without_reading_body(self):
        remote, state, _ = self.fixture(headers={"content-length": "100"})
        with self.assertRaises(ValueError):
            await download_telegram_file(remote, 4)
        self.assertEqual(state["reads"], 0)
        self.assertTrue(state["closed"])

    async def test_false_small_length_cannot_bypass_actual_limit(self):
        remote, state, _ = self.fixture(chunks=(b"1234", b"5", b"should_not_read"), headers={"content-length": "1"})
        with self.assertRaises(ValueError):
            await download_telegram_file(remote, 4)
        self.assertEqual(state["reads"], 2)
        self.assertTrue(state["closed"])

    async def test_compressed_and_redirect_responses_are_rejected(self):
        for status, headers in ((302, {}), (200, {"content-encoding": "gzip"})):
            remote, state, _ = self.fixture(status=status, headers=headers)
            with self.assertRaises((ValueError, RuntimeError)):
                await download_telegram_file(remote, 10)
            self.assertEqual(state["reads"], 0)
            self.assertTrue(state["closed"])

    async def test_timeout_and_cancellation_close_stream(self):
        for failure in (TimeoutError(), asyncio.CancelledError()):
            remote, state, _ = self.fixture(failure=failure)
            with self.assertRaises(type(failure)):
                await download_telegram_file(remote, 10)
            self.assertTrue(state["closed"])


if __name__ == "__main__":
    unittest.main()
