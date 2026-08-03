from __future__ import annotations

import asyncio
import grp
import json
import logging
import os
from pathlib import Path
import signal
import uuid


LOGGER = logging.getLogger(__name__)


def _message_text(message: object) -> str:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text", ""))
        for item in content
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
    ).strip()


def _tool_name(event: dict[str, object]) -> str:
    for key in ("toolName", "name"):
        value = event.get(key)
        if value:
            return str(value)
    tool_call = event.get("toolCall")
    if isinstance(tool_call, dict):
        return str(tool_call.get("name", "tool"))
    return "tool"


class PiChannelBroker:
    def __init__(self) -> None:
        self.socket_path = Path(os.getenv("PI_CHANNEL_SOCKET", "/run/mini-hermes-pi/pi.sock"))
        self.state_dir = Path(os.getenv("PI_CHANNEL_STATE_DIR", "/var/lib/mini-hermes-pi"))
        self.workspace = Path(os.getenv("PI_CHANNEL_WORKSPACE", "/opt/mini-hermes/workspace"))
        self.model = os.getenv("PI_CHANNEL_MODEL", "ollama-cloud/deepseek-v4-flash")
        self.thinking = os.getenv("PI_CHANNEL_THINKING", "off")
        self.timeout_seconds = int(os.getenv("PI_CHANNEL_TIMEOUT_SECONDS", "600"))
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._active_user_id: int | None = None

    def _prepare_paths(self) -> None:
        group_id = grp.getgrnam("mini-hermes").gr_gid
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chown(self.socket_path.parent, -1, group_id)
        self.socket_path.parent.chmod(0o750)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.workspace.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists() or self.socket_path.is_socket():
            self.socket_path.unlink()

    def _user_dir(self, user_id: int) -> Path:
        path = self.state_dir / "sessions" / str(user_id)
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

    def _session_path(self, user_id: int) -> Path:
        directory = self._user_dir(user_id)
        pointer = directory / "current"
        if pointer.exists():
            name = pointer.read_text(encoding="utf-8").strip()
            if name and Path(name).name == name:
                return directory / name
        return self._new_session_path(user_id)

    def _new_session_path(self, user_id: int) -> Path:
        directory = self._user_dir(user_id)
        name = f"session-{uuid.uuid4().hex}.jsonl"
        (directory / "current").write_text(name, encoding="utf-8")
        return directory / name

    async def _send(self, writer: asyncio.StreamWriter, payload: dict[str, object]) -> None:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

    async def _stop_process(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            self._process = None
            self._active_user_id = None
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            process.kill()
            await process.wait()
        self._process = None
        self._active_user_id = None

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> str:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            while size > 32768 and chunks:
                size -= len(chunks.pop(0))
        return b"".join(chunks).decode("utf-8", errors="replace")

    async def _run_prompt(
        self, user_id: int, message: str, writer: asyncio.StreamWriter
    ) -> None:
        session_path = self._session_path(user_id)
        command = [
            "/usr/bin/pi",
            "--offline",
            "--mode",
            "json",
            "--session",
            str(session_path),
            "--name",
            f"telegram-{user_id}",
            "--model",
            self.model,
            "--thinking",
            self.thinking,
            "--append-system-prompt",
            "You are interacting through Telegram. Keep final answers readable on mobile and report command or file changes clearly.",
            "-p",
            message,
        ]
        await self._send(writer, {"type": "progress", "text": "pi 会话已启动"})
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=2 * 1024 * 1024,
        )
        self._process = process
        self._active_user_id = user_id
        assert process.stdout is not None and process.stderr is not None
        stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
        answer = ""
        tools_seen: set[str] = set()
        completed = False
        try:
            async with asyncio.timeout(self.timeout_seconds):
                while True:
                    raw = await process.stdout.readline()
                    if not raw:
                        break
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    event_type = event.get("type")
                    if event_type == "tool_execution_start":
                        tool_name = _tool_name(event)
                        if tool_name not in tools_seen:
                            tools_seen.add(tool_name)
                            await self._send(
                                writer,
                                {"type": "progress", "text": f"正在调用工具：{tool_name}"},
                            )
                    elif event_type in {"message_end", "turn_end"}:
                        candidate = _message_text(event.get("message"))
                        if candidate:
                            answer = candidate
                    elif event_type == "agent_end":
                        messages = event.get("messages", [])
                        if isinstance(messages, list):
                            for item in reversed(messages):
                                candidate = _message_text(item)
                                if candidate:
                                    answer = candidate
                                    break
                        completed = True
                        break
        except TimeoutError:
            await self._stop_process()
            await self._send(
                writer,
                {
                    "type": "result",
                    "ok": False,
                    "error": f"pi request timed out after {self.timeout_seconds}s",
                },
            )
            return
        finally:
            await self._stop_process()
        stderr = await stderr_task
        if completed and answer:
            await self._send(
                writer,
                {
                    "type": "result",
                    "ok": True,
                    "text": answer,
                    "session": session_path.name,
                    "tools": sorted(tools_seen),
                },
            )
            return
        error = stderr.strip() or "pi ended without a final answer"
        await self._send(writer, {"type": "result", "ok": False, "error": error[-2000:]})

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            request = json.loads(raw)
            action = str(request.get("action", ""))
            if action == "status":
                await self._send(
                    writer,
                    {
                        "type": "result",
                        "ok": True,
                        "busy": self._lock.locked(),
                        "active_user_id": self._active_user_id,
                        "model": self.model,
                    },
                )
                return
            user_id = int(request.get("user_id", 0))
            if user_id <= 0:
                raise ValueError("invalid user_id")
            if action == "abort":
                aborted = self._process is not None and self._process.returncode is None
                await self._stop_process()
                await self._send(writer, {"type": "result", "ok": True, "aborted": aborted})
                return
            async with self._lock:
                if action == "new":
                    session = self._new_session_path(user_id)
                    await self._send(
                        writer,
                        {"type": "result", "ok": True, "session": session.name},
                    )
                    return
                if action != "prompt":
                    raise ValueError("unsupported action")
                message = str(request.get("message", "")).strip()
                if not message:
                    raise ValueError("empty prompt")
                if len(message) > 50000:
                    raise ValueError("prompt is too long")
                await self._run_prompt(user_id, message, writer)
        except (ValueError, json.JSONDecodeError, TimeoutError) as exc:
            await self._send(writer, {"type": "result", "ok": False, "error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.info("pi_channel_client_disconnected")
        except Exception as exc:  # noqa: BLE001 - broker must keep accepting requests
            LOGGER.exception("pi_channel_request_failed")
            try:
                await self._send(writer, {"type": "result", "ok": False, "error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def run(self) -> None:
        self._prepare_paths()
        server = await asyncio.start_unix_server(self._handle, path=self.socket_path)
        group_id = grp.getgrnam("mini-hermes").gr_gid
        os.chown(self.socket_path, -1, group_id)
        self.socket_path.chmod(0o660)
        LOGGER.info("pi_channel_started socket=%s model=%s", self.socket_path, self.model)
        async with server:
            await server.serve_forever()


async def _main() -> None:
    broker = PiChannelBroker()
    try:
        await broker.run()
    finally:
        await broker._stop_process()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
