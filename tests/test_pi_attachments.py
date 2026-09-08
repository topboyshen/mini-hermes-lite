"""Offline attachment and lifecycle regressions: no live pi, model or Telegram."""
import asyncio
import base64
import json
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zlib

from mini_hermes.pi_channel.attachments import (
    MAX_IMAGE_BYTES, MAX_TEXT_BYTES, staged_attachments, validate_attachments,
)
from mini_hermes.pi_channel.broker import PiChannelBroker
from mini_hermes.pi_channel.client import MAX_MESSAGE_BYTES, PiChannelClient, PiChannelError


def attachment(data=b"Synthetic UTF-8 text\n", name="notes.txt", mime="text/plain"):
    return {"name": name, "mime_type": mime, "data": base64.b64encode(data).decode("ascii")}


def png(width=1, height=1, animated=False):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    animation = chunk(b"acTL", struct.pack(">II", 1, 0)) if animated else b""
    return b"\x89PNG\r\n\x1a\n" + header + animation + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\xff")) + chunk(b"IEND", b"")


def jpeg_header(width=1, height=1):
    # Synthetic structural fixture; the validator deliberately does not decode pixels.
    frame = b"\xff\xc0" + struct.pack(">H", 11) + b"\x08" + struct.pack(">HH", height, width) + b"\x01\x01\x11\x00"
    scan = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
    return b"\xff\xd8" + frame + scan + b"\x00" + b"\xff\xd9"


def webp_header(width=1, height=1):
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + bits.to_bytes(4, "little") + b"\0"
    chunk = b"VP8L" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(chunk) + 4) + b"WEBP" + chunk


class ValidationTests(unittest.TestCase):
    def test_text_extensions_and_canonical_mime(self):
        for name, expected in (("说明.txt", "text/plain"), ("notes.MD", "text/markdown"),
                               ("table.csv", "text/csv"), ("broken.json", "application/json")):
            value = attachment("仅作为数据，不执行\n".encode(), name)
            result = validate_attachments([value])
            self.assertEqual(result[0]["mime_type"], expected)
            self.assertEqual(result[0]["data"], value["data"])
        self.assertEqual(validate_attachments([]), [])

    def test_png_jpeg_webp_headers(self):
        for data, name, mime in ((png(), "photo.png", "image/png"),
                                 (jpeg_header(), "photo.jpeg", "image/jpeg"),
                                 (webp_header(), "photo.webp", "image/webp")):
            self.assertEqual(validate_attachments([attachment(data, name, mime)])[0]["mime_type"], mime)

    def test_rejects_paths_controls_and_executable_suffixes(self):
        for name in ("../secret.txt", "/private.txt", "C:\\secret.txt", ".hidden.txt",
                     "bad\n.txt", "bad\u202e.txt", "script.py", "script.sh", "image.svg", "a" * 129 + ".txt"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "^invalid attachment$"):
                validate_attachments([attachment(name=name)])

    def test_rejects_unknown_keys_count_and_types(self):
        good = attachment()
        cases = (None, {}, "bad", [good, good], [None], [{**good, "path": "/secret"}],
                 [{**good, "data": b"bytes"}], [{**good, "mime_type": None}], [{**good, "name": None}])
        for value in cases:
            with self.subTest(value_type=type(value)), self.assertRaisesRegex(ValueError, "^invalid attachment$"):
                validate_attachments(value)

    def test_rejects_mime_mismatch_and_disguised_binary(self):
        for value in (attachment(png(), "data.txt"), attachment(b"not an image", "photo.png", "image/png"),
                      attachment(png(), "photo.jpg", "image/jpeg"), attachment(mime="application/octet-stream"),
                      attachment(b"\0binary"), attachment(b"\xffnot-utf8"), attachment(b"\x1b[31m")):
            with self.assertRaises(ValueError):
                validate_attachments([value])

    def test_strict_canonical_base64(self):
        for encoded in ("", "aGVsbG8=\n", "aGVsbG8===", "%%%%", "非ASCII", "Zh=="):
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                validate_attachments([{**attachment(), "data": encoded}])

    def test_size_limits_before_and_after_decoding(self):
        self.assertEqual(len(validate_attachments([attachment(b"a" * MAX_TEXT_BYTES)])), 1)
        for value in (attachment(b"a" * (MAX_TEXT_BYTES + 1)),
                      attachment(png() + b"a" * MAX_IMAGE_BYTES, "large.png", "image/png")):
            with self.assertRaises(ValueError):
                validate_attachments([value])

    def test_image_dimensions_and_broken_structures(self):
        values = [attachment(png(8193, 1), "a.png", "image/png"),
                  attachment(png(5000, 5000), "a.png", "image/png"),
                  attachment(png(animated=True), "a.png", "image/png"),
                  attachment(png()[:-1], "a.png", "image/png"),
                  attachment(png() + b"trailing", "a.png", "image/png"),
                  attachment(jpeg_header(0, 1), "a.jpg", "image/jpeg"),
                  attachment(jpeg_header()[:-2], "a.jpg", "image/jpeg"),
                  attachment(webp_header(8193, 1), "a.webp", "image/webp"),
                  attachment(webp_header()[:-1], "a.webp", "image/webp")]
        for value in values:
            with self.assertRaises(ValueError):
                validate_attachments([value])

    def test_stage_random_private_names_and_cleanup_on_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            path = None
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                with staged_attachments(workspace, validate_attachments([attachment(name="private-original.txt")])) as (paths, cleanup):
                    path = paths[0]
                    self.assertNotEqual(path.name, "private-original.txt")
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                    self.assertEqual(path.read_bytes(), b"Synthetic UTF-8 text\n")
                    self.assertTrue(path.is_relative_to(workspace))
                    raise RuntimeError("synthetic")
            self.assertFalse(path.exists())
            self.assertEqual(list(workspace.iterdir()), [])


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

    def events(self):
        return [json.loads(line) for line in self.data.splitlines()]


def finished():
    answer = {"role": "assistant", "content": [{"type": "text", "text": "synthetic answer"}], "stopReason": "stop"}
    return [{"type": "session"}, {"type": "message_end", "message": answer},
            {"type": "agent_end", "messages": [answer]}, {"type": "agent_settled"}]


class Process:
    def __init__(self, events=(), blocking=False):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.returncode = None
        self.finished = asyncio.Event()
        self.terminated = False
        for event in events:
            self.stdout.feed_data((json.dumps(event) + "\n").encode())
        if not blocking:
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.finished.set()

    async def wait(self):
        await self.finished.wait()
        if self.returncode is None:
            self.returncode = 0
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


class BrokerAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.broker = PiChannelBroker()
        self.broker.workspace = self.workspace
        self.broker._session_path = lambda user: self.workspace / "not-created.jsonl"
        self.writer = Writer()
        self.paths = []
        self.spawned = asyncio.Event()

    async def spawn(self, *command, **kwargs):
        self.command = command
        self.paths = [Path(arg[1:]) for arg in command if arg.startswith("@")]
        for path in self.paths:
            self.assertTrue(path.is_file())
            self.assertTrue(path.is_relative_to(self.workspace))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.spawned.set()
        return self.process

    def reader(self, action="prompt", user=42, **kwargs):
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps({"action": action, "user_id": user, "message": "synthetic", **kwargs}) + "\n").encode())
        return reader

    def assert_clean(self):
        self.assertEqual(list(self.workspace.glob(".pi-input-*")), [])
        self.assertTrue(all(not path.exists() for path in self.paths))

    async def test_valid_attachment_command_and_final_cleanup(self):
        self.process = Process(finished())
        with patch("mini_hermes.pi_channel.broker.asyncio.create_subprocess_exec", self.spawn):
            await self.broker._run_prompt(42, "explain", self.writer, attachments=[attachment(png(), "original.png", "image/png")])
        self.assertEqual(len(self.paths), 1)
        self.assertEqual(self.command[0], "/usr/bin/pi")
        self.assertLess(self.command.index("--"), self.command.index("@" + str(self.paths[0])))
        self.assertEqual(self.command[-1], "explain")
        self.assertNotIn("original.png", " ".join(self.command))
        self.assertTrue(self.writer.events()[-1]["ok"])
        self.assert_clean()

    async def test_only_broker_paths_become_at_file_arguments(self):
        for message in ("@/credential/path", "--api-key", "- Summarize"):
            self.process = Process(finished())
            with patch("mini_hermes.pi_channel.broker.asyncio.create_subprocess_exec", self.spawn):
                await self.broker._run_prompt(42, message, self.writer)
            self.assertEqual(self.paths, [])
            self.assertEqual(self.command[-2], "--")
            self.assertFalse(self.command[-1].startswith("@"))
        self.assert_clean()

    async def test_bad_broker_attachment_never_starts_process(self):
        value = {**attachment(), "path": "/private"}
        with patch("mini_hermes.pi_channel.broker.asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            await self.broker._handle(self.reader(attachments=[value]), self.writer)
        spawn.assert_not_awaited()
        result = self.writer.events()[-1]
        self.assertEqual(result["error_code"], "invalid_request")
        self.assertNotIn("/private", self.writer.data.decode())
        self.assert_clean()

    async def test_cleanup_when_process_spawn_fails(self):
        with patch("mini_hermes.pi_channel.broker.asyncio.create_subprocess_exec", AsyncMock(side_effect=OSError("synthetic"))):
            with self.assertRaises(OSError):
                await self.broker._run_prompt(42, "synthetic", self.writer, attachments=[attachment()])
        self.assert_clean()

    async def test_timeout_and_task_cancel_cleanup(self):
        for cancel in (False, True):
            self.process = Process(blocking=True)
            self.spawned.clear()
            self.broker.timeout_seconds = 10 if cancel else 0.01
            with patch("mini_hermes.pi_channel.broker.asyncio.create_subprocess_exec", self.spawn):
                task = asyncio.create_task(self.broker._run_prompt(42, "synthetic", self.writer, attachments=[attachment()]))
                await self.spawned.wait()
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    await task
            self.assertTrue(self.process.terminated)
            self.assert_clean()

    async def test_upload_removed_after_pi_input_loaded_before_completion(self):
        self.process = Process([{"type": "session"}], blocking=True)
        with patch("mini_hermes.pi_channel.broker.asyncio.create_subprocess_exec", self.spawn):
            task = asyncio.create_task(self.broker._run_prompt(42, "synthetic", self.writer, attachments=[attachment()]))
            await self.spawned.wait()
            for _ in range(10):
                if all(not path.exists() for path in self.paths):
                    break
                await asyncio.sleep(0)
            self.assert_clean()
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_other_user_cannot_abort_active_prompt(self):
        self.process = Process(blocking=True)
        self.broker._process, self.broker._active_user_id = self.process, 42
        await self.broker._handle(self.reader("abort", 99), self.writer)
        self.assertFalse(self.writer.events()[-1]["aborted"])
        self.assertFalse(self.process.terminated)
        self.assertFalse(self.broker._cancel_requested)
        await self.broker._handle(self.reader("abort", 42), self.writer)
        self.assertTrue(self.writer.events()[-1]["aborted"])
        self.assertTrue(self.process.terminated)

    async def test_new_while_busy_fails_immediately_without_mutating_session(self):
        await self.broker._lock.acquire()
        try:
            with patch.object(self.broker, "_new_session_path") as new_session:
                await asyncio.wait_for(self.broker._handle(self.reader("new"), self.writer), timeout=0.2)
            new_session.assert_not_called()
            self.assertEqual(self.writer.events()[-1]["error_code"], "channel_busy")
            self.assertIn("/abort", self.writer.events()[-1]["error"])
            self.assertTrue(self.broker._lock.locked())
        finally:
            self.broker._lock.release()

    async def test_handler_cancellation_keeps_lock_until_runner_cleanup(self):
        entered, cleanup_started, cleanup_allowed, successor = (asyncio.Event() for _ in range(4))

        async def run(user, message, writer):
            if user == 42:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await cleanup_allowed.wait()
            else:
                successor.set()

        with patch.object(self.broker, "_run_prompt", run):
            first = asyncio.create_task(self.broker._handle(self.reader(user=42), self.writer))
            await entered.wait()
            first.cancel()
            await cleanup_started.wait()
            self.assertTrue(self.broker._lock.locked())
            second = asyncio.create_task(self.broker._handle(self.reader(user=99), Writer()))
            await asyncio.sleep(0)
            self.assertFalse(successor.is_set())
            cleanup_allowed.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            await second
            self.assertTrue(successor.is_set())
            self.assertFalse(self.broker._lock.locked())

    async def test_disconnected_queued_prompt_never_runs(self):
        await self.broker._lock.acquire()
        reader = self.reader(attachments=[attachment()])
        queued = asyncio.Event()
        original_send = self.broker._send

        async def observe_send(writer, payload):
            await original_send(writer, payload)
            if payload == {"type": "progress", "text": "正在等待前一个任务"}:
                queued.set()

        with patch.object(self.broker, "_run_prompt", AsyncMock()) as run, \
             patch.object(self.broker, "_send", observe_send):
            task = asyncio.create_task(self.broker._handle(reader, self.writer))
            released = False
            try:
                # Synchronize with actual enqueue progress, not a guessed
                # number of event-loop turns (readline itself creates a task).
                await asyncio.wait_for(queued.wait(), timeout=1)
                reader.feed_eof()
                self.broker._lock.release()
                released = True
                await asyncio.wait_for(task, timeout=1)
            finally:
                if not released:
                    self.broker._lock.release()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        run.assert_not_awaited()
        self.assertIn("正在等待前一个任务", self.writer.data.decode())
        self.assert_clean()

    async def test_running_disconnect_stops_process_and_cleans_upload(self):
        self.process = Process(blocking=True)
        reader = self.reader(attachments=[attachment()])
        with patch("mini_hermes.pi_channel.broker.asyncio.create_subprocess_exec", self.spawn):
            task = asyncio.create_task(self.broker._handle(reader, self.writer))
            await self.spawned.wait()
            reader.feed_eof()
            await task
        self.assertTrue(self.process.terminated)
        self.assertFalse(self.broker._lock.locked())
        self.assert_clean()

    async def test_retry_and_generation_progress_are_fixed_and_secret_free(self):
        events = [{"type": "auto_retry_start", "errorMessage": "SYNTHETIC_PRIVATE_DETAIL"},
                  {"type": "message_start", "message": {"role": "assistant"}}, *finished()]
        self.process = Process(events)
        with patch("mini_hermes.pi_channel.broker.asyncio.create_subprocess_exec", self.spawn):
            await self.broker._run_prompt(42, "synthetic", self.writer)
        text = self.writer.data.decode()
        self.assertIn("连接波动，正在自动重试", text)
        self.assertIn("正在生成回复", text)
        self.assertNotIn("SYNTHETIC_PRIVATE_DETAIL", text)


class ClientAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_positional_progress_and_new_keyword_contract(self):
        client = PiChannelClient(Path("/not-opened.sock"))
        progress = AsyncMock()
        with patch.object(client, "_request", AsyncMock(return_value={"text": "ok"})) as request:
            self.assertEqual(await client.prompt(42, "hello", progress), "ok")
            self.assertNotIn("attachments", request.call_args.args[0])
            self.assertIs(request.call_args.args[1], progress)
            await client.prompt(42, "hello", progress, attachments=[attachment(name="data.md")])
            self.assertEqual(request.call_args.args[0]["attachments"][0]["mime_type"], "text/markdown")

    async def test_invalid_attachment_fails_before_connection(self):
        client = PiChannelClient(Path("/not-opened.sock"))
        with patch.object(client, "_connect", AsyncMock()) as connect:
            with self.assertRaises(PiChannelError) as caught:
                await client.prompt(42, "hello", attachments=[attachment(name="../../secret.txt")])
        self.assertEqual(caught.exception.code, "invalid_request")
        connect.assert_not_awaited()

    async def test_socket_limit_is_preserved(self):
        client = PiChannelClient(Path("/not-opened.sock"))
        with patch.object(client, "_connect", AsyncMock()) as connect:
            with self.assertRaises(PiChannelError) as caught:
                await client.prompt(42, "x" * MAX_MESSAGE_BYTES, attachments=[attachment()])
        self.assertEqual(caught.exception.code, "invalid_request")
        connect.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
