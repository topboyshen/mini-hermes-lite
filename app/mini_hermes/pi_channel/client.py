from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable


ProgressCallback = Callable[[str], Awaitable[None]]
MAX_MESSAGE_BYTES = 2 * 1024 * 1024


class PiChannelError(RuntimeError):
    pass


class PiChannelClient:
    def __init__(self, socket_path: Path, timeout_seconds: int = 600) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await asyncio.open_unix_connection(
                self.socket_path, limit=MAX_MESSAGE_BYTES + 1
            )
        except (FileNotFoundError, ConnectionRefusedError, PermissionError) as exc:
            raise PiChannelError(f"pi channel unavailable: {exc}") from exc

    async def _request(
        self,
        payload: dict[str, object],
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, object]:
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise PiChannelError("pi request exceeds the size limit")
        reader, writer = await self._connect()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                writer.write(encoded)
                await writer.drain()
                while True:
                    raw = await reader.readline()
                    if not raw:
                        raise PiChannelError("pi channel closed before returning a result")
                    if len(raw) > MAX_MESSAGE_BYTES:
                        raise PiChannelError("pi response exceeds the size limit")
                    event = json.loads(raw)
                    if event.get("type") == "progress":
                        if on_progress is not None:
                            await on_progress(str(event.get("text", "pi is working")))
                        continue
                    if event.get("type") != "result":
                        continue
                    if not event.get("ok"):
                        raise PiChannelError(str(event.get("error", "pi channel failed")))
                    return event
        except TimeoutError as exc:
            raise PiChannelError(f"pi channel timed out after {self.timeout_seconds}s") from exc
        except ValueError as exc:
            raise PiChannelError("pi channel returned malformed or oversized data") from exc
        finally:
            writer.close()
            await writer.wait_closed()

    async def prompt(
        self,
        user_id: int,
        message: str,
        on_progress: ProgressCallback | None = None,
    ) -> str:
        result = await self._request(
            {"action": "prompt", "user_id": user_id, "message": message},
            on_progress,
        )
        return str(result.get("text", ""))

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
            raise PiChannelError("research model returned an invalid response")
        return message
