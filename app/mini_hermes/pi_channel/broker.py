from __future__ import annotations

import asyncio
import grp
import json
import logging
import os
from pathlib import Path
import pwd
import socket
import struct
import uuid

from mini_hermes.pi_channel.client import MAX_MESSAGE_BYTES
from mini_hermes.pi_channel.errors import classify_pi_failure, error_message, normalize_error_code


LOGGER = logging.getLogger(__name__)
RESEARCH_TIMEOUT_SECONDS = 120

RESEARCH_ERRORS = {
    code: error_message(code)
    for code in (
        "auth_required", "quota", "rate_limited", "timeout", "invalid_request",
        "model_unavailable", "output_limit", "upstream_error",
    )
}


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
        self.model = os.getenv("PI_CHANNEL_MODEL", "ollama-cloud/deepseek-v4-pro")
        self.thinking = os.getenv("PI_CHANNEL_THINKING", "off")
        self.timeout_seconds = int(os.getenv("PI_CHANNEL_TIMEOUT_SECONDS", "600"))
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._active_user_id: int | None = None
        self._cancel_requested = False
        self._research_slots = asyncio.Semaphore(1)
        self._research_processes: set[asyncio.subprocess.Process] = set()

    def _authorize_research(self, writer: asyncio.StreamWriter) -> None:
        """Keep the credential-backed model relay local to the main Bot and root."""
        connection = writer.get_extra_info("socket")
        if connection is None or not hasattr(socket, "SO_PEERCRED"):
            raise ValueError("research peer identity unavailable")
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack("3i", credentials)
        if uid not in {0, pwd.getpwnam("mini-hermes").pw_uid}:
            raise ValueError("research peer is not authorized")

    @staticmethod
    async def _terminate_research(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    async def _research_exchange(
        self, process: asyncio.subprocess.Process, encoded: bytes
    ) -> bytes:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(encoded)
        await process.stdin.drain()
        process.stdin.close()
        chunks = []
        size = 0
        while chunk := await process.stdout.read(min(65536, MAX_MESSAGE_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_MESSAGE_BYTES:
                raise ValueError("research response exceeds size limit")
        await process.wait()
        return b"".join(chunks)

    async def _run_research_completion(
        self, request: object, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._authorize_research(writer)
        if not isinstance(request, dict) or set(request) - {"systemPrompt", "messages", "tools", "maxTokens"}:
            raise ValueError("invalid research request")
        encoded = json.dumps(request, ensure_ascii=False).encode("utf-8")
        process = None
        exchange = None
        disconnected = asyncio.create_task(reader.read(1))
        error_code = "upstream_error"
        try:
            # Includes queue time. This relay has no relation to the coding-chat process.
            async with asyncio.timeout(RESEARCH_TIMEOUT_SECONDS):
                async with self._research_slots:
                    if disconnected.done():
                        return
                    if self.model != "xai/grok-4.6" or self.thinking != "low":
                        error_code = "model_unavailable"
                    else:
                        process = await asyncio.create_subprocess_exec(
                            "/usr/bin/node",
                            "--max-old-space-size=192",
                            str(Path(__file__).with_name("research_completion.mjs")),
                            cwd=self.workspace,
                            env={**os.environ, "PI_OFFLINE": "1"},
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        self._research_processes.add(process)
                        exchange = asyncio.create_task(self._research_exchange(process, encoded))
                        done, _pending = await asyncio.wait(
                            {exchange, disconnected}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if disconnected in done:
                            return
                        output = await exchange
                        result = json.loads(output)
                        if not isinstance(result, dict):
                            raise ValueError("invalid research result")
                        if process.returncode == 0 and result.get("ok") is True:
                            message = result.get("message")
                            if not isinstance(message, dict) or message.get("role") != "assistant":
                                raise ValueError("invalid research message")
                            reply = {"type": "result", "ok": True, "message": message}
                            if len((json.dumps(reply, ensure_ascii=False, separators=(",", ":")) + "\n").encode()) > MAX_MESSAGE_BYTES:
                                error_code = "output_limit"
                            else:
                                await self._send(writer, reply)
                                return
                        else:
                            candidate = result.get("error_code")
                            if isinstance(candidate, str) and candidate in RESEARCH_ERRORS:
                                error_code = candidate
        except TimeoutError:
            error_code = "timeout"
        except (ValueError, OSError, json.JSONDecodeError):
            error_code = "upstream_error"
        finally:
            if process is not None:
                await self._terminate_research(process)
                self._research_processes.discard(process)
            for task in (exchange, disconnected):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (exchange, disconnected) if task is not None), return_exceptions=True)
        LOGGER.warning("research_completion_failed code=%s", error_code)
        await self._send_failure(writer, error_code)

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
        writer.write((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        await writer.drain()

    async def _send_failure(self, writer: asyncio.StreamWriter, code: object) -> None:
        normalized = normalize_error_code(code)
        await self._send(
            writer,
            {"type": "result", "ok": False, "error_code": normalized, "error": error_message(normalized)},
        )

    async def _stop_process(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            self._process = None
            self._active_user_id = None
            return
        try:
            process.terminate()
        except ProcessLookupError:
            self._process = None
            self._active_user_id = None
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
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
        self._cancel_requested = False
        assert process.stdout is not None and process.stderr is not None
        stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
        latest_assistant: dict[str, object] | None = None
        event_failure = ""
        tools_seen: set[str] = set()
        completed = False
        timed_out = False
        stderr = ""
        exit_code = None

        def observe_assistant(candidate: object) -> None:
            nonlocal latest_assistant, event_failure
            if isinstance(candidate, dict) and candidate.get("role") == "assistant":
                latest_assistant = candidate
                # A successful retry must not inherit the previous attempt's error.
                if candidate.get("stopReason") == "stop" and not candidate.get("errorMessage"):
                    event_failure = ""

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
                    if not isinstance(event, dict):
                        continue
                    event_type = event.get("type")
                    if event_type in {"turn_start", "message_start", "auto_retry_start"}:
                        completed = False
                    if event_type == "tool_execution_start":
                        completed = False
                        tool_name = _tool_name(event)
                        if tool_name not in tools_seen:
                            tools_seen.add(tool_name)
                            await self._send(
                                writer,
                                {"type": "progress", "text": f"正在调用工具：{tool_name}"},
                            )
                    elif event_type in {"message_end", "turn_end"}:
                        observe_assistant(event.get("message"))
                    elif event_type == "agent_end":
                        messages = event.get("messages", [])
                        if isinstance(messages, list):
                            for item in reversed(messages):
                                if isinstance(item, dict) and item.get("role") == "assistant":
                                    observe_assistant(item)
                                    break
                        # agent_end may precede native retries/compaction. Do not
                        # terminate pi here: consume through settled/clean EOF.
                        completed = event.get("willRetry") is not True
                    elif event_type == "agent_settled":
                        completed = True
                    elif event_type == "auto_retry_end":
                        final_error = event.get("finalError")
                        if isinstance(final_error, str) and final_error:
                            event_failure = final_error[-32768:]
                        elif event.get("success") is True:
                            event_failure = ""
                    elif event_type in {"error", "fatal_error"}:
                        failure = event.get("error") or event.get("message")
                        if isinstance(failure, str):
                            event_failure = failure[-32768:]
                await process.wait()
                exit_code = process.returncode
        except TimeoutError:
            timed_out = True
        finally:
            try:
                await self._stop_process()
            finally:
                try:
                    stderr = await asyncio.wait_for(stderr_task, timeout=3)
                except TimeoutError:
                    stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)
        latest = latest_assistant or {}
        stop_reason = latest.get("stopReason", "")
        stop_reason = stop_reason if isinstance(stop_reason, str) else ""
        native_error = latest.get("errorMessage", "")
        native_error = native_error if isinstance(native_error, str) else ""
        content = latest.get("content", [])
        has_tool_calls = isinstance(content, list) and any(
            isinstance(item, dict) and item.get("type") == "toolCall" for item in content
        )
        answer = _message_text(latest)
        if self._cancel_requested:
            code = "cancelled"
        elif timed_out:
            code = "timeout"
        elif event_failure and classify_pi_failure(event_failure) == "cancelled":
            code = "cancelled"
        elif native_error or stop_reason in {"error", "aborted", "length"}:
            code = classify_pi_failure(native_error, stop_reason, exit_code)
            if code == "upstream_error" and not native_error:
                code = classify_pi_failure(event_failure or stderr, stop_reason, exit_code)
        elif event_failure:
            code = classify_pi_failure(event_failure, exit_code=exit_code)
        elif exit_code not in {None, 0}:
            code = classify_pi_failure(stderr, exit_code=exit_code)
        elif completed and answer and stop_reason in {"", "stop"} and not has_tool_calls:
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
        else:
            code = classify_pi_failure(stderr, exit_code=exit_code)
        LOGGER.warning("pi_completion_failed code=%s", code)
        await self._send_failure(writer, code)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            if len(raw) > MAX_MESSAGE_BYTES:
                raise ValueError("request exceeds size limit")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
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
                        "research_active": len(self._research_processes),
                    },
                )
                return
            if action == "research_completion":
                if set(request) != {"action", "request"}:
                    raise ValueError("invalid research envelope")
                await self._run_research_completion(request["request"], reader, writer)
                return
            user_id = int(request.get("user_id", 0))
            if user_id <= 0:
                raise ValueError("invalid user_id")
            if action == "abort":
                aborted = self._process is not None and self._process.returncode is None
                if aborted:
                    self._cancel_requested = True
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
        except (ValueError, json.JSONDecodeError):
            await self._send_failure(writer, "invalid_request")
        except TimeoutError:
            await self._send_failure(writer, "timeout")
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.info("pi_channel_client_disconnected")
        except OSError:
            LOGGER.warning("pi_channel_request_failed code=channel_unavailable")
            try:
                await self._send_failure(writer, "channel_unavailable")
            except (BrokenPipeError, ConnectionResetError):
                pass
        except Exception:  # noqa: BLE001 - broker must keep accepting requests
            LOGGER.warning("pi_channel_request_failed code=upstream_error")
            try:
                await self._send_failure(writer, "upstream_error")
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def run(self) -> None:
        self._prepare_paths()
        server = await asyncio.start_unix_server(
            self._handle, path=self.socket_path, limit=MAX_MESSAGE_BYTES + 1
        )
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
