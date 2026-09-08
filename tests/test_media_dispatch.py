"""Production registration + real PTB photo dispatch, with all I/O mocked.

Does not construct Settings or MiniHermesBot, start polling, open a database,
read environment files, fetch an attachment, connect to pi or call a model.
"""
import asyncio
import base64
from datetime import datetime, timezone
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

from telegram import Chat, File, LivePhoto, Message, PhotoSize, Update, User
from telegram.ext import Application, ExtBot
from telegram.request import BaseRequest

from mini_hermes.gateway.telegram.bot import MiniHermesBot


class NoNetworkRequest(BaseRequest):
    def __init__(self):
        self.calls = 0

    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("real Telegram requests are forbidden")


def synthetic_jpeg():
    # Structural fixture for our bounded validator. Pixel decoding is covered
    # separately by test_pi_cli_attachments.mjs against the installed pi SDK.
    frame = b"\xff\xc0" + struct.pack(">H", 11) + b"\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    scan = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
    return b"\xff\xd8" + frame + scan + b"\x00\xff\xd9"


class ActualMediaDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, live=False, live_static=True, allowed=True):
        requests = (NoNetworkRequest(), NoNetworkRequest())
        telegram_bot = ExtBot(token="123:TEST", request=requests[0], get_updates_request=requests[1])
        identity = User(123456, "Synthetic bot", True, username="offline_media_bot")
        chat = Chat(42, "private")
        user = User(42, "Synthetic user", False)
        date = datetime.now(timezone.utc)
        raw = synthetic_jpeg()
        photo = PhotoSize("synthetic-photo", "synthetic-unique", 1, 1, file_size=len(raw))
        photo.set_bot(telegram_bot)
        remote = File("synthetic-photo", "synthetic-unique", file_size=len(raw))
        remote.set_bot(telegram_bot)
        fields = {"photo": [photo]}
        if live:
            fields = {"live_photo": LivePhoto(
                "synthetic-video", "synthetic-live", 1, 1, 1,
                photo=[photo] if live_static else None, file_size=200,
            )}
        message = Message(1, date, chat, from_user=user, caption="概括这张测试图片", **fields)
        message.set_bot(telegram_bot)
        update = Update(1, message=message)
        update.set_bot(telegram_bot)

        owner = object.__new__(MiniHermesBot)
        owner.settings = SimpleNamespace(allowed_user_ids={42} if allowed else set())
        owner.memory = SimpleNamespace(touch_user=AsyncMock(), append_turn=AsyncMock())
        owner.pi_channel = SimpleNamespace(prompt=AsyncMock(return_value="离线图片分析完成"))
        errors, sends, edits = [], [], []
        owner.error_handler = AsyncMock(side_effect=lambda update, context: errors.append(type(context.error).__name__))

        async def reply(target, text, **kwargs):
            sends.append(text)
            progress = Message(100 + len(sends), date, chat, from_user=identity, text=text)
            progress.set_bot(telegram_bot)
            return progress

        async def edit(target, text, **kwargs):
            edits.append(text)
            return target

        with patch.object(ExtBot, "initialize", new_callable=AsyncMock), \
                patch.object(ExtBot, "bot", new_callable=PropertyMock, return_value=identity), \
                patch.object(ExtBot, "send_chat_action", new_callable=AsyncMock) as typing, \
                patch.object(Message, "reply_text", new=reply), \
                patch.object(Message, "edit_text", new=edit), \
                patch.object(LivePhoto, "get_file", new_callable=AsyncMock,
                             side_effect=AssertionError("the live video must never be downloaded")) as get_video, \
                patch.object(PhotoSize, "get_file", new_callable=AsyncMock, return_value=remote) as get_file, \
                patch("mini_hermes.gateway.telegram.media.download_telegram_file", \
                      new_callable=AsyncMock, return_value=bytearray(raw)) as download:
            app = (Application.builder().bot(telegram_bot).updater(None).job_queue(None)
                   .concurrent_updates(2).build())
            owner.application = app
            # The production registration itself is under test, not a fake
            # MessageHandler made by the test. No handlers are replaced.
            owner._register_handlers()
            matching = [handler.callback.__name__ for handler in app.handlers[0]
                        if handler.check_update(update)]
            await app.initialize()
            await app.start()
            try:
                await asyncio.wait_for(
                    app.update_processor.process_update(update, app.process_update(update)), timeout=2,
                )
            finally:
                # PTB stop awaits every block=False callback, including actual
                # media_message -> _run_pi -> progress delivery and cleanup.
                await asyncio.wait_for(app.stop(), timeout=3)
                await app.shutdown()

        self.assertEqual(errors, [])
        self.assertEqual([request.calls for request in requests], [0, 0])
        get_video.assert_not_awaited()
        return SimpleNamespace(
            owner=owner, matching=matching, sends=sends, edits=edits, raw=raw,
            typing=typing, get_file=get_file, download=download,
        )

    async def test_registered_single_photo_receives_ack_pi_attachment_and_final_reply(self):
        result = await self.exercise()
        self.assertEqual(result.matching, ["media_message"])
        self.assertEqual(result.sends, ["📎 正在接收并检查附件…"])
        self.assertEqual(result.edits, ["离线图片分析完成"])
        result.get_file.assert_awaited_once()
        result.download.assert_awaited_once()
        result.typing.assert_awaited_once()
        result.owner.pi_channel.prompt.assert_awaited_once()
        args, kwargs = result.owner.pi_channel.prompt.await_args
        self.assertEqual(args[:2], (42, "概括这张测试图片"))
        self.assertEqual(kwargs["attachments"], [{
            "name": "photo.jpg", "mime_type": "image/jpeg",
            "data": base64.b64encode(result.raw).decode("ascii"),
        }])
        result.owner.memory.append_turn.assert_awaited_once()
        self.assertEqual(result.owner._ux_background_active, 0)

    async def test_registered_unauthorized_photo_is_replied_to_without_download_or_model(self):
        result = await self.exercise(allowed=False)
        self.assertEqual(result.matching, ["media_message"])
        self.assertEqual(len(result.sends), 1)
        self.assertIn("未授权", result.sends[0])
        result.get_file.assert_not_awaited()
        result.download.assert_not_awaited()
        result.owner.pi_channel.prompt.assert_not_awaited()
        result.owner.memory.touch_user.assert_not_awaited()

    async def test_registered_live_photo_uses_static_photo_without_fetching_video(self):
        result = await self.exercise(live=True)
        self.assertEqual(result.matching, ["media_message"])
        self.assertEqual(len(result.sends), 1)
        self.assertIn("静态", result.sends[0])
        self.assertEqual(result.edits, ["离线图片分析完成"])
        result.get_file.assert_awaited_once()
        result.download.assert_awaited_once()
        result.owner.pi_channel.prompt.assert_awaited_once()
        self.assertEqual(result.owner.pi_channel.prompt.await_args.kwargs["attachments"], [{
            "name": "photo.jpg", "mime_type": "image/jpeg",
            "data": base64.b64encode(result.raw).decode("ascii"),
        }])

    async def test_registered_live_photo_without_static_frame_explains_rejection(self):
        result = await self.exercise(live=True, live_static=False)
        self.assertEqual(result.matching, ["media_message"])
        self.assertEqual(len(result.sends), 1)
        self.assertIn("静态", result.sends[0])
        self.assertEqual(result.edits, [])
        result.get_file.assert_not_awaited()
        result.download.assert_not_awaited()
        result.owner.pi_channel.prompt.assert_not_awaited()

    async def test_registered_unauthorized_live_photo_never_downloads_static_or_video(self):
        result = await self.exercise(live=True, allowed=False)
        self.assertEqual(result.matching, ["media_message"])
        self.assertEqual(len(result.sends), 1)
        self.assertIn("未授权", result.sends[0])
        self.assertEqual(result.edits, [])
        result.get_file.assert_not_awaited()
        result.download.assert_not_awaited()
        result.owner.pi_channel.prompt.assert_not_awaited()
        result.owner.memory.touch_user.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
