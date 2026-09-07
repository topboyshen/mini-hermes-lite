"""Offline regression for the existing Signal Gate callback transport.

All identities, messages, secrets and responses below are synthetic. HTTP,
Telegram acknowledgement and thread dispatch are mocked; these tests neither
exercise approval decisions nor contact Signal Gate, Quant, Telegram or models.
"""
from __future__ import annotations

from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from urllib.error import HTTPError

from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from mini_hermes.gateway.telegram import MiniHermesBot
from mini_hermes.gateway.telegram import signal_gate_review_forward as forward


class CallbackRegistrationTests(unittest.TestCase):
    def test_signal_callback_is_registered_without_replacing_existing_handlers(self):
        handlers, error_handlers = [], []
        bot = object.__new__(MiniHermesBot)
        bot.application = SimpleNamespace(
            add_handler=handlers.append,
            add_error_handler=error_handlers.append,
        )
        bot._register_handlers()

        signal_handlers = [
            item for item in handlers
            if isinstance(item, CallbackQueryHandler)
            and item.callback is forward.signal_gate_review_callback
        ]
        self.assertEqual(len(signal_handlers), 1)
        pattern = signal_handlers[0].pattern
        self.assertEqual(pattern.pattern, r"^sg:(ok|no):\d+$")
        for data in ("sg:ok:123", "sg:no:456"):
            self.assertIsNotNone(pattern.match(data))
        for data in ("sg:other:123", "sg:ok:abc", "sg:ok:", "prefix:sg:ok:1", "market_full:123"):
            self.assertIsNone(pattern.match(data))

        self.assertTrue(any(
            isinstance(item, CallbackQueryHandler) and item.callback == bot.full_report_callback
            for item in handlers
        ))
        self.assertTrue(any(
            isinstance(item, MessageHandler) and item.callback == bot.text_message
            for item in handlers
        ))
        self.assertTrue(any(
            isinstance(item, CommandHandler) and "stock" in item.commands for item in handlers
        ))
        self.assertEqual(error_handlers, [bot.error_handler])


class ForwardTransportTests(unittest.TestCase):
    def setUp(self):
        self.url = "http://127.0.0.1:8096/synthetic-offline-test"
        self.payload = {"id": "synthetic-query", "data": "sg:ok:123"}
        self.url_patch = patch.object(forward, "FORWARD_URL", self.url)
        self.url_patch.start()
        self.addCleanup(self.url_patch.stop)

    def response(self, body=b'{"ok":true}', status=200):
        response = MagicMock()
        response.__enter__.return_value = SimpleNamespace(
            status=status, read=Mock(return_value=body)
        )
        return response

    def test_post_uses_existing_envelope_header_and_timeout(self):
        with (
            patch.object(forward, "FORWARD_SECRET", "synthetic-test-secret"),
            patch.object(forward.urllib.request, "urlopen", return_value=self.response()) as opened,
        ):
            status, body = forward._post_sync(self.payload)
        self.assertEqual((status, body), (200, '{"ok":true}'))
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, self.url)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"callback_query": self.payload})
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["x-review-forward-secret"], "synthetic-test-secret")
        self.assertEqual(opened.call_args.kwargs, {"timeout": 20})

    def test_empty_secret_preserves_existing_header_omission(self):
        with (
            patch.object(forward, "FORWARD_SECRET", ""),
            patch.object(forward.urllib.request, "urlopen", return_value=self.response()) as opened,
        ):
            forward._post_sync(self.payload)
        headers = {key.lower(): value for key, value in opened.call_args.args[0].header_items()}
        self.assertNotIn("x-review-forward-secret", headers)

    def test_http_error_is_returned_to_callback_without_real_request(self):
        failure = HTTPError(self.url, 403, "synthetic denial", {}, BytesIO(b'{"ok":false}'))
        with (
            patch.object(forward, "FORWARD_SECRET", ""),
            patch.object(forward.urllib.request, "urlopen", side_effect=failure),
        ):
            self.assertEqual(forward._post_sync(self.payload), (403, '{"ok":false}'))

    def test_response_decoding_replaces_invalid_utf8(self):
        with (
            patch.object(forward, "FORWARD_SECRET", ""),
            patch.object(forward.urllib.request, "urlopen", return_value=self.response(b"invalid-\xff")),
        ):
            self.assertEqual(forward._post_sync(self.payload), (200, "invalid-\ufffd"))


class ForwardCallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.thread_patch = patch.object(forward.asyncio, "to_thread", new_callable=AsyncMock)
        self.to_thread = self.thread_patch.start()
        self.to_thread.return_value = (200, "synthetic response")
        self.addCleanup(self.thread_patch.stop)
        # Avoid emitting even synthetic callback bodies in the test runner logs.
        self.log_patch = patch.object(forward, "LOGGER", Mock())
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.network_patch = patch.object(
            forward.urllib.request, "urlopen",
            side_effect=AssertionError("Real HTTP is forbidden in callback unit tests"),
        )
        self.network_patch.start()
        self.addCleanup(self.network_patch.stop)
        self.query = SimpleNamespace(
            id="synthetic-query",
            data="sg:ok:123",
            from_user=SimpleNamespace(id=42, username="synthetic_user", first_name="Synthetic"),
            message=SimpleNamespace(message_id=100, chat_id=99, text="synthetic card text"),
            answer=AsyncMock(),
        )
        self.update = SimpleNamespace(callback_query=self.query)

    async def test_success_forwards_payload_and_leaves_acknowledgement_to_receiver(self):
        await forward.signal_gate_review_callback(self.update, None)
        self.to_thread.assert_awaited_once_with(
            forward._post_sync,
            {
                "id": "synthetic-query", "data": "sg:ok:123",
                "from": {"id": 42, "username": "synthetic_user", "first_name": "Synthetic"},
                "message": {"message_id": 100, "chat": {"id": 99}, "text": "synthetic card text"},
            },
        )
        self.query.answer.assert_not_awaited()

    async def test_missing_callback_performs_no_action(self):
        await forward.signal_gate_review_callback(SimpleNamespace(callback_query=None), None)
        self.to_thread.assert_not_awaited()

    async def test_missing_optional_message_or_user_is_forwarded_as_nulls(self):
        self.query.message = None
        self.query.from_user = None
        await forward.signal_gate_review_callback(self.update, None)
        payload = self.to_thread.await_args.args[1]
        self.assertEqual(payload["from"], {"id": None, "username": None, "first_name": None})
        self.assertEqual(payload["message"], {"message_id": None, "chat": {"id": None}, "text": None})

    async def test_http_failure_acknowledges_with_existing_status_message(self):
        self.to_thread.return_value = (503, "synthetic response body")
        await forward.signal_gate_review_callback(self.update, None)
        self.query.answer.assert_awaited_once_with("review forward HTTP 503", show_alert=False)

    async def test_transport_failure_uses_existing_unavailable_message(self):
        self.to_thread.side_effect = OSError("synthetic network failure")
        await forward.signal_gate_review_callback(self.update, None)
        self.query.answer.assert_awaited_once_with("review service unavailable", show_alert=True)

    async def test_failed_telegram_acknowledgement_is_tolerated(self):
        self.query.answer.side_effect = OSError("synthetic acknowledgement failure")
        self.to_thread.return_value = (403, "synthetic denial")
        await forward.signal_gate_review_callback(self.update, None)
        self.to_thread.side_effect = OSError("synthetic transport failure")
        await forward.signal_gate_review_callback(self.update, None)
        self.assertEqual(self.query.answer.await_count, 2)


if __name__ == "__main__":
    unittest.main()
