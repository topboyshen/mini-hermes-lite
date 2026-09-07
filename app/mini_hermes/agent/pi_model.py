from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic_ai.exceptions import UnexpectedModelBehavior, UserError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from mini_hermes.pi_channel.client import PiChannelClient, PiChannelError


_NATIVE_ASSISTANT = "pi_native_assistant"


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _token_count(usage: dict[str, Any], name: str) -> int:
    value = usage.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UnexpectedModelBehavior("Research model returned invalid token usage")
    return value


class PiResearchModel(Model):
    """Text/function-tool model using the credential-isolated pi broker.

    The broker performs one native completion, never a pi agent/tool loop. MCP
    tools remain owned by PydanticAI in the main Bot. Native assistant messages
    are kept on each response, not in a shared cache, to preserve signed replay
    data without mixing concurrent requests or exposing it as Telegram text.
    """

    def __init__(self, socket_path: Path, timeout_seconds: int = 120) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Research model timeout must be positive")
        super().__init__(
            profile={
                "supports_tools": True,
                "supports_json_schema_output": False,
                "supports_json_object_output": False,
                "supports_image_output": False,
            }
        )
        self._client = PiChannelClient(socket_path, timeout_seconds)
        self._timeout_seconds = timeout_seconds

    @property
    def model_name(self) -> str:
        return "grok-4.6"

    @property
    def system(self) -> str:
        return "xai"

    def _request_options(
        self, settings: ModelSettings | None, params: ModelRequestParameters
    ) -> tuple[int, float]:
        if (
            params.output_mode != "text"
            or params.output_tools
            or params.output_object is not None
            or params.native_tools
            or params.allow_image_output
            or not params.allow_text_output
        ):
            raise UserError("Research model supports only text output and function tools")
        values = settings or {}
        if set(values) - {"max_tokens", "timeout"}:
            raise UserError("Unsupported research model settings")
        max_tokens = values.get("max_tokens", 4096)
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or not 1 <= max_tokens <= 8192
        ):
            raise UserError("Research model max_tokens must be an integer from 1 to 8192")
        timeout = values.get("timeout", self._timeout_seconds)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (float, int))
            or not 0 < timeout <= self._timeout_seconds
        ):
            raise UserError("Research model timeout must be positive and within its configured limit")
        return max_tokens, float(timeout)

    def _map_request(
        self, messages: list[ModelMessage], params: ModelRequestParameters, max_tokens: int
    ) -> dict[str, object]:
        system_parts: list[str] = []
        mapped: list[dict[str, Any]] = []
        tool_calls: dict[str, str] = {}
        returned_calls: set[str] = set()

        def check_tool_result(call_id: str, name: str) -> None:
            if tool_calls.get(call_id) != name or call_id in returned_calls:
                raise UserError("Research tool result does not match a preceding function call")
            returned_calls.add(call_id)

        # Pinned PydanticAI 2.22 helper also preserves instructions across a
        # tool-return-only request; relying on only the latest request loses them.
        instructions = self._get_instruction_parts(messages, params)
        for message in messages:
            if isinstance(message, ModelResponse):
                details = message.provider_details or {}
                native = details.get(_NATIVE_ASSISTANT)
                if not isinstance(native, dict):
                    raise UserError("Research model cannot replay unsigned or foreign assistant history")
                self._validate_native(native)
                for item in native["content"]:
                    if item["type"] == "toolCall":
                        if item["id"] in tool_calls:
                            raise UserError("Research history contains duplicate function call IDs")
                        tool_calls[item["id"]] = item["name"]
                mapped.append(deepcopy(native))
                continue
            if not isinstance(message, ModelRequest):
                raise UserError("Unsupported research model message")
            for part in message.parts:
                if isinstance(part, SystemPromptPart):
                    system_parts.append(part.content)
                elif isinstance(part, UserPromptPart):
                    content = [part.content] if isinstance(part.content, str) else part.content
                    if not all(isinstance(item, str) for item in content):
                        raise UserError("Research model accepts text-only user input")
                    mapped.append(
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": item} for item in content],
                            "timestamp": _milliseconds(part.timestamp),
                        }
                    )
                elif isinstance(part, ToolReturnPart):
                    check_tool_result(part.tool_call_id, part.tool_name)
                    text, files = part.model_response_str_and_user_content()
                    if files:
                        raise UserError("Research model accepts text-only tool results")
                    mapped.append(
                        {
                            "role": "toolResult",
                            "toolCallId": part.tool_call_id,
                            "toolName": part.tool_name,
                            "content": [{"type": "text", "text": text}],
                            "isError": part.outcome != "success",
                            "timestamp": _milliseconds(part.timestamp),
                        }
                    )
                elif isinstance(part, RetryPromptPart):
                    entry: dict[str, Any] = {
                        "role": "toolResult" if part.tool_name else "user",
                        "content": [{"type": "text", "text": part.model_response()}],
                        "timestamp": _milliseconds(part.timestamp),
                    }
                    if part.tool_name:
                        check_tool_result(part.tool_call_id, part.tool_name)
                        entry.update(
                            toolCallId=part.tool_call_id, toolName=part.tool_name, isError=True
                        )
                    mapped.append(entry)
                else:
                    raise UserError("Unsupported research model request part")
        system_parts.extend(part.content for part in instructions or [])
        tools = []
        for tool in params.function_tools:
            if tool.strict is True:
                raise UserError("Research model does not support provider-enforced strict tools")
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": deepcopy(tool.parameters_json_schema),
                }
            )
        return {
            "systemPrompt": "\n\n".join(system_parts),
            "messages": mapped,
            "tools": tools,
            "maxTokens": max_tokens,
        }

    def _validate_native(self, native: dict[str, Any]) -> None:
        if (
            native.get("role") != "assistant"
            or native.get("model") != self.model_name
            or native.get("provider") != self.system
            or not isinstance(native.get("content"), list)
        ):
            raise UnexpectedModelBehavior("Research model returned an invalid assistant message")
        for item in native["content"]:
            if not isinstance(item, dict):
                raise UnexpectedModelBehavior("Research model returned an invalid content part")
            kind = item.get("type")
            if not isinstance(kind, str):
                raise UnexpectedModelBehavior("Research model returned an invalid content type")
            if kind in {"text", "thinking"}:
                if not isinstance(item.get(kind), str):
                    raise UnexpectedModelBehavior("Research model returned invalid text content")
            elif kind == "toolCall":
                if (
                    not isinstance(item.get("id"), str)
                    or not item["id"]
                    or not isinstance(item.get("name"), str)
                    or not item["name"]
                    or not isinstance(item.get("arguments"), dict)
                ):
                    raise UnexpectedModelBehavior("Research model returned an invalid tool call")
            else:
                raise UnexpectedModelBehavior("Research model returned unsupported content")

    def _map_response(self, raw: dict[str, Any]) -> ModelResponse:
        if not isinstance(raw, dict):
            raise UnexpectedModelBehavior("Research model returned an invalid response")
        stop_reason = raw.get("stopReason")
        if not isinstance(stop_reason, str):
            raise UnexpectedModelBehavior("Research model returned an invalid finish reason")
        if stop_reason in {"error", "aborted"}:
            # Never echo provider errorMessage or signed response contents.
            raise PiChannelError("Research model completion failed")
        self._validate_native(raw)
        parts: list[TextPart | ThinkingPart | ToolCallPart] = []
        for item in raw["content"]:
            kind = item["type"]
            if kind == "text" and item["text"]:
                parts.append(TextPart(item["text"]))
            elif kind == "thinking":
                parts.append(ThinkingPart(item["thinking"], provider_name=self.system))
            elif kind == "toolCall":
                parts.append(
                    ToolCallPart(item["name"], deepcopy(item["arguments"]), tool_call_id=item["id"])
                )
        if not any(isinstance(part, (TextPart, ToolCallPart)) for part in parts):
            raise UnexpectedModelBehavior("Research model returned no text or function call")
        reasons = {"stop": "stop", "length": "length", "toolUse": "tool_call"}
        reason = reasons.get(stop_reason)
        if reason is None:
            raise UnexpectedModelBehavior("Research model returned an invalid finish reason")
        usage = raw.get("usage", {})
        if not isinstance(usage, dict):
            raise UnexpectedModelBehavior("Research model returned invalid token usage")
        cache_read = _token_count(usage, "cacheRead")
        cache_write = _token_count(usage, "cacheWrite")
        return ModelResponse(
            parts=parts,
            model_name=self.model_name,
            provider_name=self.system,
            provider_details={_NATIVE_ASSISTANT: deepcopy(raw)},
            timestamp=datetime.now(UTC),
            finish_reason=reason,
            usage=RequestUsage(
                input_tokens=_token_count(usage, "input") + cache_read + cache_write,
                output_tokens=_token_count(usage, "output"),
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            ),
        )

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        # Validate before prepare_request can discard unsupported settings/tools.
        max_tokens, timeout = self._request_options(model_settings, model_request_parameters)
        settings, params = self.prepare_request(model_settings, model_request_parameters)
        self._request_options(settings, params)
        payload = self._map_request(messages, params, max_tokens)
        try:
            async with asyncio.timeout(timeout):
                raw = await self._client.research_completion(payload)
        except TimeoutError as exc:
            raise PiChannelError("Research model request timed out") from exc
        return self._map_response(raw)
