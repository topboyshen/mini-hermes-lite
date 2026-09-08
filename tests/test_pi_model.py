from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior, UserError
from pydantic_ai.messages import (
    ImageUrl,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.native_tools import WebSearchTool
from pydantic_ai.tools import ToolDefinition

from mini_hermes.agent.pi_model import PiResearchModel
from mini_hermes.pi_channel.client import PiChannelError


def native(content=None, stop_reason="stop") -> dict:
    return {
        "role": "assistant",
        "content": content if content is not None else [
            {"type": "text", "text": "synthetic final", "textSignature": "signed-text"}
        ],
        "api": "openai-responses",
        "provider": "xai",
        "model": "grok-4.6",
        "stopReason": stop_reason,
        "timestamp": 1788820000000,
        "usage": {"input": 10, "output": 4, "cacheRead": 7, "cacheWrite": 2},
    }


def tool_response() -> dict:
    return native(
        [
            {"type": "thinking", "thinking": "private reasoning", "thinkingSignature": "signed-thinking"},
            {"type": "text", "text": "checking", "textSignature": "signed-preface"},
            {"type": "toolCall", "id": "call_x|fc_x", "name": "local_quote", "arguments": {"symbol": "600875"}},
        ],
        "toolUse",
    )


class PiResearchModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = PiResearchModel(Path("/tmp/not-a-real-pi-socket"))
        self.completion = AsyncMock(return_value=native())
        self.model._client = SimpleNamespace(research_completion=self.completion)

    async def request(self, messages=None, settings=None, params=None):
        return await self.model.request(
            messages or [ModelRequest(parts=[UserPromptPart("synthetic question")])],
            settings,
            params or ModelRequestParameters(),
        )

    async def test_no_openai_environment_or_http_dependency(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = await self.request()
        payload = self.completion.await_args.args[0]
        self.assertEqual(set(payload), {"systemPrompt", "messages", "tools", "maxTokens", "thinking"})
        self.assertEqual(payload["thinking"], "low")
        self.assertEqual(payload["maxTokens"], 4096)
        self.assertEqual(result.model_name, "grok-4.6")
        self.assertEqual(result.provider_name, "xai")
        self.assertEqual(result.usage.input_tokens, 19)
        self.assertEqual(result.usage.output_tokens, 4)
        self.assertEqual(result.usage.cache_read_tokens, 7)
        self.assertEqual(result.usage.cache_write_tokens, 2)

    async def test_real_agent_tool_loop_preserves_native_signatures_and_ids(self) -> None:
        first, final = tool_response(), native()
        self.completion.side_effect = [first, final]
        executed: list[str] = []

        async def local_quote(symbol: str) -> str:
            """Return a synthetic quote without external calls."""
            executed.append(symbol)
            return "synthetic quote"

        agent = Agent(self.model, instructions="synthetic system instruction", tools=[local_quote])
        result = await agent.run("synthetic question", model_settings={"pi_thinking": "xhigh"})
        self.assertEqual(result.output, "synthetic final")
        self.assertEqual(executed, ["600875"])
        self.assertEqual(self.completion.await_count, 2)
        self.assertEqual([call.args[0]["thinking"] for call in self.completion.await_args_list], ["xhigh", "xhigh"])
        payload = self.completion.await_args.args[0]
        self.assertIn("synthetic system instruction", payload["systemPrompt"])
        self.assertEqual(payload["tools"][0]["name"], "local_quote")
        self.assertEqual(payload["tools"][0]["parameters"]["properties"]["symbol"]["type"], "string")
        assistant = next(item for item in payload["messages"] if item["role"] == "assistant")
        self.assertEqual(assistant, first)
        self.assertIsNot(assistant, first)
        returned = next(item for item in payload["messages"] if item["role"] == "toolResult")
        self.assertEqual(returned["toolCallId"], "call_x|fc_x")
        self.assertEqual(returned["toolName"], "local_quote")
        self.assertFalse(returned["isError"])
        self.assertIn("synthetic quote", returned["content"][0]["text"])
        self.assertEqual(result.all_messages()[-1].provider_details["pi_native_assistant"], final)
        self.assertNotIn("private reasoning", result.output)

    async def test_tool_failure_and_validation_retry_are_error_results(self) -> None:
        response = self.model._map_response(tool_response())
        for part in (
            ToolReturnPart("local_quote", {"error": "synthetic tool failure"}, tool_call_id="call_x|fc_x", outcome="failed"),
            RetryPromptPart("synthetic validation error", tool_name="local_quote", tool_call_id="call_x|fc_x"),
        ):
            with self.subTest(part=type(part).__name__):
                await self.request([ModelRequest(parts=[UserPromptPart("question")]), response, ModelRequest(parts=[part])])
                result = self.completion.await_args.args[0]["messages"][-1]
                self.assertEqual(result["role"], "toolResult")
                self.assertTrue(result["isError"])
                self.assertEqual(result["toolCallId"], "call_x|fc_x")

    async def test_system_text_and_non_tool_retry_remain_messages(self) -> None:
        await self.request([
            ModelRequest(parts=[SystemPromptPart("synthetic policy"), UserPromptPart(["first", "second"])], instructions="synthetic instructions"),
            ModelRequest(parts=[RetryPromptPart("try again")]),
        ])
        payload = self.completion.await_args.args[0]
        self.assertIn("synthetic policy", payload["systemPrompt"])
        self.assertIn("synthetic instructions", payload["systemPrompt"])
        self.assertEqual(payload["messages"][0]["content"], [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}])
        self.assertEqual(payload["messages"][-1]["role"], "user")
        self.assertIn("try again", payload["messages"][-1]["content"][0]["text"])

    async def test_rejects_orphan_mismatched_and_duplicate_tool_results(self) -> None:
        response = self.model._map_response(tool_response())
        valid = ToolReturnPart("local_quote", "ok", tool_call_id="call_x|fc_x")
        cases = [
            [ModelRequest(parts=[valid])],
            [response, ModelRequest(parts=[ToolReturnPart("different", "no", tool_call_id="call_x|fc_x")])],
            [response, ModelRequest(parts=[valid, valid])],
            [response, response, ModelRequest(parts=[valid])],
        ]
        for messages in cases:
            with self.subTest(messages=len(messages)), self.assertRaises(UserError):
                await self.request(messages)
        self.completion.assert_not_awaited()

    async def test_rejects_media_structured_output_native_tools_and_strict_tools(self) -> None:
        with self.assertRaises(UserError):
            await self.request([ModelRequest(parts=[UserPromptPart([ImageUrl("https://example.invalid/image.png")])])])
        for params in (
            ModelRequestParameters(output_mode="native"),
            ModelRequestParameters(output_tools=[ToolDefinition(name="structured")]),
            ModelRequestParameters(native_tools=[WebSearchTool()]),
            ModelRequestParameters(allow_image_output=True),
            ModelRequestParameters(allow_text_output=False),
            ModelRequestParameters(function_tools=[ToolDefinition(name="strict", strict=True)]),
        ):
            with self.subTest(params=params), self.assertRaises(UserError):
                await self.request(params=params)
        self.completion.assert_not_awaited()

    async def test_rejects_unsupported_settings_and_token_limits(self) -> None:
        for settings in (
            {"max_tokens": 0}, {"max_tokens": 8193}, {"max_tokens": True},
            {"temperature": 0.5}, {"thinking": "low"}, {"timeout": 121},
            {"timeout": -1}, {"extra_headers": {"Authorization": "synthetic"}},
            {"pi_thinking": "off"}, {"pi_thinking": "max"}, {"pi_thinking": None}, {"pi_thinking": []},
        ):
            with self.subTest(settings=settings), self.assertRaises(UserError):
                await self.request(settings=settings)
        self.completion.assert_not_awaited()
        await self.request(settings={"max_tokens": 8192, "timeout": 2})
        self.assertEqual(self.completion.await_args.args[0]["maxTokens"], 8192)

    async def test_rejects_unsigned_and_foreign_assistant_history(self) -> None:
        with self.assertRaises(UserError):
            await self.request([ModelResponse(parts=[TextPart("foreign")])])
        response = self.model._map_response(tool_response())
        response.provider_details["pi_native_assistant"]["model"] = "different"
        with self.assertRaises(UnexpectedModelBehavior):
            await self.request([response])
        self.completion.assert_not_awaited()

    async def test_errors_are_not_rewritten_as_a_success_or_leaked(self) -> None:
        self.completion.side_effect = PiChannelError("safe broker message")
        with self.assertRaisesRegex(PiChannelError, "safe broker message"):
            await self.request()
        self.completion.side_effect = None
        response = native(stop_reason="error")
        response["errorMessage"] = "secret-synthetic-provider-error"
        self.completion.return_value = response
        with self.assertRaises(PiChannelError) as raised:
            await self.request()
        self.assertNotIn("secret-synthetic", str(raised.exception))

    async def test_rejects_invalid_or_empty_native_response(self) -> None:
        invalid_part = native([{"type": "image", "data": "synthetic"}])
        invalid_call = tool_response()
        invalid_call["content"][-1]["arguments"] = "not-an-object"
        invalid_usage = native()
        invalid_usage["usage"]["input"] = -1
        for response in (native([]), invalid_part, invalid_call, invalid_usage, native(stop_reason="unknown")):
            self.completion.return_value = response
            with self.subTest(response=response["stopReason"]), self.assertRaises(UnexpectedModelBehavior):
                await self.request()

    async def test_native_response_copy_is_not_shared_between_runs(self) -> None:
        original = native()
        self.completion.return_value = original
        first = await self.request()
        second = await self.request()
        first.provider_details["pi_native_assistant"]["content"][0]["textSignature"] = "changed"
        self.assertEqual(second.provider_details["pi_native_assistant"], original)
        self.assertEqual(original, deepcopy(native()))

    async def test_local_timeout_cancels_completion(self) -> None:
        cancelled = False

        async def wait_for_cancellation(payload):
            nonlocal cancelled
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled = True
                raise

        self.completion.side_effect = wait_for_cancellation
        with self.assertRaisesRegex(PiChannelError, "timed out"):
            await self.request(settings={"timeout": 0.01})
        self.assertTrue(cancelled)


if __name__ == "__main__":
    unittest.main()
