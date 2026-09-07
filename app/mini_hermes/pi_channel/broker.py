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


LOGGER = logging.getLogger(__name__)
RESEARCH_TIMEOUT_SECONDS = 120

RESEARCH_ERRORS = {
    "auth_required": "Grok 账号授权不可用，请重新授权后重试",
    "quota": "Grok 账号额度不足，请检查订阅状态",
    "rate_limited": "Grok 请求限流，请稍后重试",
    "timeout": "Grok 研究请求超时，请缩小范围后重试",
    "invalid_request": "Grok 研究请求格式不受支持",
    "model_unavailable": "Grok 4.6 研究模型配置不一致",
    "output_limit": "Grok 输出达到长度限制，请缩小研究范围",
    "upstream_error": "Grok 服务暂时不可用，请稍后重试",
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
            process.kill()
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
        await self._send(writer, {"type": "result", "ok": False, "error": RESEARCH_ERRORS[error_code]})

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
