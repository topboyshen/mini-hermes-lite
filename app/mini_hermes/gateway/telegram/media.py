"""Bounded attachment intake; no user-provided paths or original-file storage."""
from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import PurePosixPath
import time
from urllib.parse import urlsplit

import httpx

from telegram import Update
from telegram.ext import ContextTypes

LOGGER = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 128 * 1024
ATTACHMENT_TYPES = {
    ".jpg": ("image/jpeg", MAX_IMAGE_BYTES),
    ".jpeg": ("image/jpeg", MAX_IMAGE_BYTES),
    ".png": ("image/png", MAX_IMAGE_BYTES),
    ".webp": ("image/webp", MAX_IMAGE_BYTES),
    ".txt": ("text/plain", MAX_TEXT_BYTES),
    ".md": ("text/markdown", MAX_TEXT_BYTES),
    ".csv": ("text/csv", MAX_TEXT_BYTES),
    ".json": ("application/json", MAX_TEXT_BYTES),
}
MEDIA_HELP = (
    "支持 JPG、PNG、WebP 图片（每张最多 1 MiB），以及 UTF-8 编码的 "
    "TXT、MD、CSV、JSON 文本（最多 128 KiB）。\n"
    "iPhone 实况照片只分析静态画面，不处理动态部分。\n"
    "请一次发一个附件，可在说明中写上想问的问题。PDF、Office、压缩包和语音暂不支持。"
)


def valid_size(value: object, limit: int) -> bool:
    return type(value) is int and 0 < value <= limit


async def download_telegram_file(remote: object, limit: int) -> bytearray:
    """Stop reading at the limit; PTB's convenience downloader buffers everything."""
    url = getattr(remote, "file_path", None)
    if not isinstance(url, str):
        raise ValueError("invalid attachment URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "api.telegram.org"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not parsed.path.startswith("/file/bot")):
        raise ValueError("invalid attachment URL")
    result = bytearray()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20, connect=10), follow_redirects=False, trust_env=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    ) as client:
        async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            if response.status_code != 200:
                raise RuntimeError("attachment download unavailable")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("encoded attachment response")
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or not 0 < int(length) <= limit):
                raise ValueError("invalid attachment size")
            async for chunk in response.aiter_bytes(chunk_size=min(65536, limit + 1)):
                if len(result) + len(chunk) > limit:
                    raise ValueError("invalid attachment size")
                result.extend(chunk)
    if not result:
        raise ValueError("empty attachment")
    return result


class MediaMixin:
    def _album_notice_due(self, chat_id: int, group_id: str) -> bool:
        now = time.monotonic()
        cache = getattr(self, "_media_album_notices", {})
        cache = {key: expiry for key, expiry in cache.items() if expiry > now}
        self._media_album_notices = cache
        key = (chat_id, group_id)
        if key in cache:
            return False
        while len(cache) >= 128:
            cache.pop(next(iter(cache)))
        cache[key] = now + 120
        return True

    async def media_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        chat, message = update.effective_chat, update.effective_message
        if chat is None or message is None:
            return
        live_photo = getattr(message, "live_photo", None)
        photos = getattr(message, "photo", None) or ()
        document = getattr(message, "document", None)
        media_kind = ("live_photo" if live_photo is not None else "photo" if photos
                      else "document" if document is not None else "unsupported")
        # Fixed categories only: never log captions, file IDs, names or URLs.
        LOGGER.info("attachment_received kind=%s", media_kind)
        if chat.type != "private":
            await self._reply(update, "图片和文件分析只允许在机器人私聊中使用。")
            return
        if getattr(message, "media_group_id", None):
            if self._album_notice_due(chat.id, message.media_group_id):
                await self._reply(update, "暂不合并相册；请一次单独发送一张图片或一个文件。")
            return
        if live_photo is not None:
            # LivePhoto.file_id is the VIDEO, not the still image. Only its
            # nested PhotoSize variants may enter the existing image pipeline.
            photos = getattr(live_photo, "photo", None) or ()
            if not photos:
                LOGGER.info("attachment_rejected reason=live_photo_without_still")
                await self._reply(
                    update, "收到了实况照片，但 Telegram 没有提供可分析的静态画面。"
                    "请关闭 LIVE 后作为普通照片发送，或发送一张截图。"
                )
                return
        if photos:
            candidates = [photo for photo in photos if valid_size(photo.file_size, MAX_IMAGE_BYTES)]
            if not candidates:
                await self._reply(update, "图片过大或大小未知，请压缩后重新发送。\n" + MEDIA_HELP)
                return
            source = max(candidates, key=lambda photo: photo.width * photo.height)
            name, mime_type, limit = "photo.jpg", "image/jpeg", MAX_IMAGE_BYTES
        elif document is not None:
            supplied_name = getattr(document, "file_name", None)
            if not isinstance(supplied_name, str):
                await self._reply(update, "无法识别文件类型。\n" + MEDIA_HELP)
                return
            suffix = PurePosixPath(supplied_name).suffix.lower()
            if suffix not in ATTACHMENT_TYPES:
                await self._reply(update, "暂不支持这种文件。\n" + MEDIA_HELP)
                return
            mime_type, limit = ATTACHMENT_TYPES[suffix]
            # Never forward original filenames; names may contain secrets or paths.
            name, source = "attachment" + suffix, document
            if not valid_size(source.file_size, limit):
                await self._reply(update, "文件过大或大小未知，请缩小后重新发送。\n" + MEDIA_HELP)
                return
        else:
            await self._reply(update, MEDIA_HELP)
            return
        progress = await message.reply_text(
            "📎 收到实况照片，正在检查静态画面（不分析动态部分）…"
            if live_photo is not None else "📎 正在接收并检查附件…"
        )
        try:
            async with asyncio.timeout(45):
                remote = await source.get_file()
                if not valid_size(remote.file_size, limit):
                    raise ValueError("invalid attachment size")
                raw = await download_telegram_file(remote, limit)
            if not raw or len(raw) > limit:
                raise ValueError("invalid attachment size")
            attachment = {
                "name": name,
                "mime_type": mime_type,
                "data": base64.b64encode(raw).decode("ascii"),
            }
            # Revalidated by the broker. Imported lazily to keep intake independent.
            from mini_hermes.pi_channel.attachments import validate_attachments
            validate_attachments([attachment])
        except ValueError:
            await self._finish_progress(
                update, progress, "附件内容或编码不符合要求，请换一份有效文件。\n" + MEDIA_HELP
            )
            return
        except Exception:  # Never log signed Telegram file URLs or attachment content.
            LOGGER.warning("attachment_download_failed")
            await self._finish_progress(update, progress, "附件下载暂时失败，请稍后重新发送。")
            return
        LOGGER.info("attachment_ready kind=%s", media_kind)
        caption = getattr(message, "caption", None)
        prompt = caption.strip() if isinstance(caption, str) else ""
        if not prompt:
            prompt = (
                "请描述这张图片的主要内容，标明看不清或无法确定的部分。"
                if mime_type.startswith("image/") else
                "请用中文概括这份文本，列出要点、疑问和需要我核对的内容。"
            )
        if live_photo is not None:
            prompt = (
                "附件是实况照片的单张静态画面；没有提供动态视频。"
                "请在回答中明确只分析了静态画面，不推测动作或声音。\n" + prompt
            )
        await self._run_pi(update, prompt, attachments=[attachment], progress=progress)
