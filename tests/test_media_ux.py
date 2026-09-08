import asyncio
import base64
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from mini_hermes.gateway.telegram.media import MediaMixin, MAX_IMAGE_BYTES, MAX_TEXT_BYTES


class MediaIntakeTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, data=b"hello", filename="notes.txt", photo=False, **message_fields):
        remote = SimpleNamespace(file_size=len(data), download_as_bytearray=AsyncMock(return_value=bytearray(data)))
        async def download(remote, limit):
            return await remote.download_as_bytearray()
        patched = patch("mini_hermes.gateway.telegram.media.download_telegram_file", side_effect=download)
        patched.start()
        self.addCleanup(patched.stop)
        source = SimpleNamespace(file_name=filename, file_size=len(data), width=640, height=480,
                                 get_file=AsyncMock(return_value=remote))
        message = SimpleNamespace(
            photo=[source] if photo else [], document=None if photo else source,
            caption="请总结", media_group_id=None,
            reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())),
        )
        for key, value in message_fields.items():
            setattr(message, key, value)
        update = SimpleNamespace(effective_chat=SimpleNamespace(type="private", id=42), effective_message=message)
        bot = MediaMixin()
        bot._authorized = AsyncMock(return_value=True)
        bot._reply = AsyncMock()
        bot._finish_progress = AsyncMock()
        bot._run_pi = AsyncMock()
        return bot, update, source, remote

    async def test_text_file_forwards_caption_and_sanitized_name(self):
        bot, update, source, remote = self.fixture(filename="PRIVATE_secret.txt")
        await bot.media_message(update, None)
        call = bot._run_pi.await_args
        self.assertEqual(call.args, (update, "请总结"))
        attachment = call.kwargs["attachments"][0]
        self.assertEqual(attachment["name"], "attachment.txt")
        self.assertEqual(base64.b64decode(attachment["data"]), b"hello")
        self.assertIs(call.kwargs["progress"], update.effective_message.reply_text.return_value)

    async def test_no_caption_has_clear_default(self):
        bot, update, *_ = self.fixture(caption=None)
        await bot.media_message(update, None)
        self.assertIn("概括", bot._run_pi.await_args.args[1])

    async def test_unauthorized_never_downloads(self):
        bot, update, source, _ = self.fixture()
        bot._authorized.return_value = False
        await bot.media_message(update, None)
        source.get_file.assert_not_awaited()
        bot._run_pi.assert_not_awaited()

    async def test_group_never_downloads(self):
        bot, update, source, _ = self.fixture()
        update.effective_chat.type = "supergroup"
        await bot.media_message(update, None)
        source.get_file.assert_not_awaited()
        bot._run_pi.assert_not_awaited()

    async def test_album_is_not_silently_split(self):
        bot, update, source, _ = self.fixture(media_group_id="synthetic")
        await bot.media_message(update, None)
        source.get_file.assert_not_awaited()
        bot._run_pi.assert_not_awaited()

    async def test_same_album_has_one_notice_and_cache_is_bounded(self):
        bot, update, source, _ = self.fixture(media_group_id="synthetic")
        await asyncio.gather(*(bot.media_message(update, None) for _ in range(10)))
        bot._reply.assert_awaited_once()
        for index in range(200):
            bot._album_notice_due(42, str(index))
        self.assertLessEqual(len(bot._media_album_notices), 128)
        source.get_file.assert_not_awaited()

    async def test_album_cache_expires_and_separates_chats(self):
        bot, *_ = self.fixture()
        with patch("mini_hermes.gateway.telegram.media.time.monotonic", return_value=100):
            self.assertTrue(bot._album_notice_due(42, "one"))
            self.assertFalse(bot._album_notice_due(42, "one"))
            self.assertTrue(bot._album_notice_due(43, "one"))
        with patch("mini_hermes.gateway.telegram.media.time.monotonic", return_value=221):
            self.assertTrue(bot._album_notice_due(42, "one"))

    async def test_unsupported_types_rejected_before_download(self):
        for filename in ("a.pdf", "a.docx", "a.zip", "a.sh", "a.jpg.exe", None):
            with self.subTest(filename=filename):
                bot, update, source, _ = self.fixture(filename=filename)
                await bot.media_message(update, None)
                source.get_file.assert_not_awaited()
                bot._run_pi.assert_not_awaited()

    async def test_size_checks_reject_unknown_bool_zero_negative_large(self):
        for size in (None, True, 0, -1, MAX_TEXT_BYTES + 1):
            with self.subTest(size=size):
                bot, update, source, _ = self.fixture()
                source.file_size = size
                await bot.media_message(update, None)
                source.get_file.assert_not_awaited()
                bot._run_pi.assert_not_awaited()

    async def test_download_metadata_is_rechecked(self):
        bot, update, _, remote = self.fixture()
        remote.file_size = MAX_TEXT_BYTES + 1
        await bot.media_message(update, None)
        remote.download_as_bytearray.assert_not_awaited()
        bot._run_pi.assert_not_awaited()

    async def test_actual_size_cannot_bypass_declared_size(self):
        bot, update, _, remote = self.fixture()
        remote.download_as_bytearray.return_value = bytearray(b"a" * (MAX_TEXT_BYTES + 1))
        await bot.media_message(update, None)
        bot._run_pi.assert_not_awaited()

    async def test_invalid_utf8_does_not_call_pi(self):
        bot, update, *_ = self.fixture(data=b"\xff\xff")
        await bot.media_message(update, None)
        bot._run_pi.assert_not_awaited()
        self.assertIn("编码", bot._finish_progress.await_args.args[2])

    async def test_fake_image_does_not_call_pi(self):
        bot, update, *_ = self.fixture(data=b"not a jpeg", filename="a.jpg")
        await bot.media_message(update, None)
        bot._run_pi.assert_not_awaited()

    async def test_photo_uses_largest_bounded_variant(self):
        bot, update, source, remote = self.fixture(photo=True)
        oversized = SimpleNamespace(file_size=MAX_IMAGE_BYTES + 1, width=4000, height=3000,
                                    get_file=AsyncMock())
        update.effective_message.photo.append(oversized)
        with patch("mini_hermes.pi_channel.attachments.validate_attachments", return_value=[]):
            await bot.media_message(update, None)
        source.get_file.assert_awaited_once()
        oversized.get_file.assert_not_awaited()
        self.assertEqual(bot._run_pi.await_args.kwargs["attachments"][0]["mime_type"], "image/jpeg")

    async def test_unknown_photo_size_fails_closed(self):
        bot, update, source, _ = self.fixture(photo=True)
        source.file_size = None
        await bot.media_message(update, None)
        source.get_file.assert_not_awaited()

    async def test_download_failure_does_not_leak_private_url(self):
        bot, update, source, _ = self.fixture()
        source.get_file.side_effect = RuntimeError("https://api.telegram.org/file/botSYNTHETIC_SECRET/private")
        with self.assertLogs("mini_hermes.gateway.telegram.media", level="WARNING") as logs:
            await bot.media_message(update, None)
        self.assertNotIn("SYNTHETIC_SECRET", " ".join(logs.output))
        self.assertNotIn("SYNTHETIC_SECRET", bot._finish_progress.await_args.args[2])
        bot._run_pi.assert_not_awaited()

    async def test_cancellation_is_not_reported_as_download_failure(self):
        bot, update, source, _ = self.fixture()
        source.get_file.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await bot.media_message(update, None)
        bot._finish_progress.assert_not_awaited()

    async def test_unsupported_message_explains_supported_formats(self):
        bot, update, *_ = self.fixture(document=None, photo=[])
        await bot.media_message(update, None)
        self.assertIn("语音暂不支持", bot._reply.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
