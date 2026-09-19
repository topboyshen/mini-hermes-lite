from __future__ import annotations

import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from mini_hermes.pi_channel.broker import PiChannelBroker
from mini_hermes.pi_channel.errors import PI_ERRORS, classify_pi_failure, error_message, normalize_error_code

import os

# Tests exercise the deployed contract: the unit declares the model explicitly.
os.environ.setdefault("PI_CHANNEL_MODEL", "xai/grok-4.6")


def assistant(text="", *, stop="stop", error="", tool=False):
    content = [{"type": "text", "text": text}] if text else []
    if tool:
        content.append({"type": "toolCall", "id": "synthetic-call", "name": "bash", "arguments": {}})
    message = {"role": "assistant", "content": content, "stopReason": stop}
    if error:
        message["errorMessage"] = error
    return message


def finished(message):
    return [
        {"type": "message_end", "message": message},
        {"type": "agent_end", "messages": [message]},
        {"type": "agent_settled"},
    ]


class Writer:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass

    def results(self):
        return [value for line in self.data.splitlines() if (value := json.loads(line))["type"] == "result"]


class Process:
    def __init__(self, events=(), *, stderr="", exit_code=0, blocking=False):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self.exit_code = exit_code
        self.finished = asyncio.Event()
        self.terminated = False
        for event in events:
            self.stdout.feed_data((json.dumps(event) + "\n").encode())
        self.stderr.feed_data(stderr.encode())
        if not blocking:
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.finished.set()

    async def wait(self):
        await self.finished.wait()
        if self.returncode is None:
            self.returncode = self.exit_code
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.finished.set()

    def kill(self):
        self.terminate()
        self.returncode = -9


class ClassificationTests(unittest.TestCase):
    def test_failure_categories_and_quota_precedence(self):
        cases = {
            "401 Unauthorized token=synthetic-secret": "auth_required",
            "OAuth access token expired": "auth_required",
            "No API key found": "auth_required",
            'OpenAI API error (429): {"code":"insufficient_quota"}': "quota",
            "403 your subscription payment is past due": "quota",
            "insufficient credits": "quota",
            "429 Too Many Requests": "rate_limited",
            "per minute quota exceeded": "rate_limited",
            "monthly quota exceeded": "quota",
            "Read timed out": "timeout",
            "Request aborted due to timeout": "timeout",
            "HTTP 503 upstream cancelled the request": "upstream_error",
            "model grok-4.6 not found": "model_unavailable",
            "400 invalid_request": "invalid_request",
            "maximum context length exceeded": "invalid_request",
            "output reached maximum output tokens": "output_limit",
            "Retry cancelled": "cancelled",
            "pi channel unavailable: Connection refused": "channel_unavailable",
            "unknown failure with https://secret.invalid/token": "upstream_error",
        }
        for text, code in cases.items():
            with self.subTest(code=code, text=text):
                self.assertEqual(classify_pi_failure(text), code)
        self.assertEqual(classify_pi_failure(stop_reason="length"), "output_limit")
        self.assertEqual(classify_pi_failure(stop_reason="aborted"), "cancelled")
        self.assertEqual(classify_pi_failure(stop_reason="error"), "upstream_error")
        self.assertEqual(classify_pi_failure(exit_code=1), "upstream_error")
        self.assertEqual(classify_pi_failure(exit_code=0), "invalid_response")

    def test_unknown_codes_never_echo_input(self):
        for code in ("https://secret.invalid", None, [], {"token": "synthetic"}):
            self.assertEqual(normalize_error_code(code), "upstream_error")
            self.assertEqual(error_message(code), PI_ERRORS["upstream_error"])

    def test_only_bounded_failure_tail_is_examined(self):
        self.assertEqual(classify_pi_failure("401 " + "x" * 40000), "upstream_error")
        self.assertEqual(classify_pi_failure("x" * 40000 + " 429"), "rate_limited")


class BrokerFailureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.broker = PiChannelBroker()
        self.broker._session_path = lambda user_id: Path("/tmp/synthetic-not-created.jsonl")
        self.writer = Writer()

    async def run_process(self, process):
        with patch("mini_hermes.pi_channel.broker.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            await self.broker._run_prompt(42, "synthetic prompt", self.writer)
        results = self.writer.results()
        self.assertEqual(len(results), 1)
        return results[0]

    def assert_failure(self, result, code):
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], code)
        self.assertEqual(result["error"], error_message(code))

    async def test_final_error_overrides_prior_preamble_even_with_exit_zero(self):
        preamble = assistant("I will investigate.")
        final_error = assistant("partial answer must not escape", stop="error", error="403 subscription payment is past due; token=synthetic-secret")
        events = [
            {"type": "message_end", "message": preamble},
            {"type": "message_end", "message": final_error},
            {"type": "agent_end", "messages": [preamble, final_error]},
            {"type": "agent_settled"},
        ]
        with self.assertLogs("mini_hermes.pi_channel.broker", level="WARNING") as logs:
            result = await self.run_process(Process(events))
        self.assert_failure(result, "quota")
        self.assertNotIn("synthetic-secret", str(logs.output) + self.writer.data.decode())
        self.assertNotIn("partial answer", self.writer.data.decode())

    async def test_tool_only_or_tool_preface_is_not_a_final_answer(self):
        for text in ("", "I am using a tool."):
            self.writer = Writer()
            result = await self.run_process(Process(finished(assistant(text, stop="toolUse", tool=True))))
            self.assert_failure(result, "invalid_response")

    async def test_native_retry_continues_past_first_agent_end_and_clears_error(self):
        failed = assistant(stop="error", error="429 synthetic rate limit")
        good = assistant("recovered final answer")
        events = [
            {"type": "message_end", "message": failed},
            {"type": "agent_end", "messages": [failed], "willRetry": True},
            {"type": "auto_retry_start", "errorMessage": "429 synthetic rate limit"},
            {"type": "auto_retry_end", "success": True},
            {"type": "turn_start"},
            *finished(good),
        ]
        process = Process(events)
        result = await self.run_process(process)
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "recovered final answer")
        self.assertFalse(process.terminated)

    async def test_cancelled_retry_is_not_reported_as_its_original_rate_error(self):
        failed = assistant(stop="error", error="429 rate limit")
        events = [
            {"type": "message_end", "message": failed},
            {"type": "agent_end", "messages": [failed], "willRetry": True},
            {"type": "auto_retry_end", "success": False, "finalError": "Retry cancelled"},
            {"type": "agent_settled"},
        ]
        self.assert_failure(await self.run_process(Process(events)), "cancelled")

    async def test_normal_answer_and_success_stderr_are_not_classified(self):
        answer = "Explain what 403 payment past due and 429 mean."
        result = await self.run_process(Process(finished(assistant(answer)), stderr="debug note 401 synthetic"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], answer)

    async def test_stderr_failure_is_bounded_and_never_forwarded(self):
        process = Process(stderr="x" * 60000 + " 401 token=synthetic-secret", exit_code=1)
        self.assert_failure(await self.run_process(process), "auth_required")
        self.assertNotIn("synthetic-secret", self.writer.data.decode())

    async def test_nonzero_exit_is_not_hidden_by_final_text(self):
        result = await self.run_process(Process(finished(assistant("not a successful result")), stderr="429 rate limit", exit_code=2))
        self.assert_failure(result, "rate_limited")

    async def test_output_limit_and_native_abort_are_distinct(self):
        for reason, code in (("length", "output_limit"), ("aborted", "cancelled")):
            self.writer = Writer()
            self.assert_failure(await self.run_process(Process(finished(assistant("partial", stop=reason)))), code)

    async def test_timeout_terminates_process_and_drains_stderr(self):
        self.broker.timeout_seconds = 0.01
        process = Process(stderr="synthetic provider detail", blocking=True)
        drained = False
        original_drain = self.broker._drain_stderr

        async def drain(stream):
            nonlocal drained
            try:
                return await original_drain(stream)
            finally:
                drained = True

        self.broker._drain_stderr = drain
        self.assert_failure(await self.run_process(process), "timeout")
        self.assertTrue(process.terminated)
        self.assertTrue(drained)
        self.assertNotIn("synthetic provider", self.writer.data.decode())

    async def test_user_abort_returns_cancelled_to_pending_prompt(self):
        process = Process(blocking=True)
        spawned = asyncio.Event()

        async def spawn(*args, **kwargs):
            spawned.set()
            return process

        with patch("mini_hermes.pi_channel.broker.asyncio.create_subprocess_exec", spawn):
            running = asyncio.create_task(self.broker._run_prompt(42, "synthetic prompt", self.writer))
            await spawned.wait()
            reader = asyncio.StreamReader()
            reader.feed_data(b'{"action":"abort","user_id":42}\n')
            reader.feed_eof()
            abort_writer = Writer()
            await self.broker._handle(reader, abort_writer)
            second_reader = asyncio.StreamReader()
            second_reader.feed_data(b'{"action":"abort","user_id":42}\n')
            second_reader.feed_eof()
            await self.broker._handle(second_reader, Writer())
            await running
        self.assertTrue(abort_writer.results()[0]["aborted"])
        self.assert_failure(self.writer.results()[0], "cancelled")

    async def test_bad_request_does_not_echo_invalid_field(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"action":"prompt","user_id":"synthetic-secret"}\n')
        reader.feed_eof()
        await self.broker._handle(reader, self.writer)
        self.assert_failure(self.writer.results()[0], "invalid_request")
        self.assertNotIn("synthetic-secret", self.writer.data.decode())

    async def test_incomplete_followup_does_not_reuse_previous_final(self):
        events = [*finished(assistant("old answer")), {"type": "turn_start"}]
        self.assert_failure(await self.run_process(Process(events)), "invalid_response")


if __name__ == "__main__":
    unittest.main()
