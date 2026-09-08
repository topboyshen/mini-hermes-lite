from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable

from .attachments import validate_attachments
from .errors import classify_pi_failure, error_message, normalize_error_code

ProgressCallback = Callable[[str], Awaitable[None]]
MAX_MESSAGE_BYTES = 2 * 1024 * 1024


class PiChannelError(RuntimeError):
    def __init__(self, message: str = "", *, code: str = "upstream_error") -> None:
        self.code = normalize_error_code(code)
        super().__init__(message or error_message(self.code))


class PiChannelClient:
    def __init__(self, socket_path: Path, timeout_seconds: int = 600) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await asyncio.open_unix_connection(
                self.socket_path, limit=MAX_MESSAGE_BYTES + 1
            )
        except OSError as exc:
            raise PiChannelError(code="channel_unavailable") from exc

    async def _request(
        self,
        payload: dict[str, object],
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, object]:
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise PiChannelError(code="invalid_request")
        reader, writer = await self._connect()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                writer.write(encoded)
                await writer.drain()
                while True:
                    raw = await reader.readline()
                    if not raw:
                        raise PiChannelError(code="channel_unavailable")
                    if len(raw) > MAX_MESSAGE_BYTES:
                        raise PiChannelError(code="invalid_response")
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise PiChannelError(code="invalid_response")
                    if event.get("type") == "progress":
                        if on_progress is not None:
                            await on_progress(str(event.get("text", "pi is working")))
                        continue
                    if event.get("type") != "result":
                        continue
                    if event.get("ok") is not True:
                        # Older brokers may not supply a code; never echo their raw
                        # diagnostics, which can contain provider URLs or secrets.
                        code = (
                            normalize_error_code(event["error_code"])
                            if "error_code" in event
                            else classify_pi_failure(str(event.get("error", "")))
                        )
                        raise PiChannelError(code=code)
                    return event
        except TimeoutError as exc:
            raise PiChannelError(code="timeout") from exc
        except ValueError as exc:
            raise PiChannelError(code="invalid_response") from exc
        except (ConnectionError, OSError) as exc:
            raise PiChannelError(code="channel_unavailable") from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def prompt(
        self,
        user_id: int,
        message: str,
        on_progress: ProgressCallback | None = None,
        *,
        attachments: list[dict[str, str]] | None = None,
    ) -> str:
        payload: dict[str, object] = {"action": "prompt", "user_id": user_id, "message": message}
        if attachments is not None:
            try:
                payload["attachments"] = validate_attachments(attachments)
            except ValueError:
                raise PiChannelError(code="invalid_request") from None
        result = await self._request(
            payload,
            on_progress,
        )
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            raise PiChannelError(code="invalid_response")
        return text

    async def new_session(self, user_id: int) -> str:
        result = await self._request({"action": "new", "user_id": user_id})
        return str(result.get("session", ""))

    async def abort(self, user_id: int) -> bool:
        result = await self._request({"action": "abort", "user_id": user_id})
        return bool(result.get("aborted"))

    async def status(self) -> dict[str, object]:
        return await self._request({"action": "status"})

    async def research_completion(self, payload: dict[str, object]) -> dict[str, object]:
        """One model turn only; tools remain under the research Agent's control."""
        result = await self._request({"action": "research_completion", "request": payload})
        message = result.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise PiChannelError(code="invalid_response")
        return message
