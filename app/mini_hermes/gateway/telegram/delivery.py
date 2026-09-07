from __future__ import annotations

import asyncio
import logging
from hashlib import sha256
from typing import Sequence

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import NetworkError, RetryAfter, TimedOut

from mini_hermes.reporting import GlobalReportBundle

LOGGER = logging.getLogger(__name__)

_SEND_RETRY_DELAYS = (0.5, 1.5)


class ReportDeliveryError(RuntimeError):
    def __init__(self, message_ids: Sequence[int]) -> None:
        super().__init__("Telegram report delivery was only partially completed")
        self.message_ids = tuple(message_ids)


def chunk_text(text: str, size: int = 4000) -> list[str]:
    if not text:
        return [""]
    return [text[index : index + size] for index in range(0, len(text), size)] or [""]


def full_report_markup(bundle: GlobalReportBundle) -> InlineKeyboardMarkup:
    report_id = sha256(bundle.full.encode("utf-8")).hexdigest()[:32]
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "查看完整数据",
                callback_data=f"market_full:{report_id}",
            )
        ]]
    )


async def send_message_with_light_retry(
    bot: Bot,
    *,
    chat_id: int,
    text: str,
    **send_kwargs: object,
) -> Message:
    """Retry a couple of times on transient Telegram network errors."""
    attempt = 0
    while True:
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **send_kwargs)
        except RetryAfter as exc:
            if attempt >= len(_SEND_RETRY_DELAYS):
                raise
            retry_after = exc.retry_after
            delay = (
                retry_after.total_seconds()
                if hasattr(retry_after, "total_seconds")
                else float(retry_after)
            )
            await asyncio.sleep(delay + 0.1)
        except (NetworkError, TimedOut):
            if attempt >= len(_SEND_RETRY_DELAYS):
                raise
            await asyncio.sleep(_SEND_RETRY_DELAYS[attempt])
        attempt += 1


class DeliveryMixin:
    """Reply / report delivery helpers shared by the bot shell."""

    async def _reply(self, update: Update, text: str) -> None:
        message = update.effective_message
        if message is None:
            return
        for chunk in chunk_text(text):
            await message.reply_text(chunk)

    async def _finish_progress(self, update: Update, progress: Message, text: str) -> None:
        chunks = chunk_text(text) if text.strip() else [
            "分析完成，但模型没有返回可显示的文本。"
        ]
        try:
            await progress.edit_text(chunks[0])
            message = update.effective_message
            if message is not None:
                for chunk in chunks[1:]:
                    await message.reply_text(chunk)
        except Exception:  # noqa: BLE001 - fall back to a fresh message
            LOGGER.exception("telegram_progress_edit_failed")
            await self._reply(update, text)

    @staticmethod
    def _full_report_markup(bundle: GlobalReportBundle) -> InlineKeyboardMarkup:
        return full_report_markup(bundle)

    @staticmethod
    async def _reply_to_callback(query: object, bot: Bot, chat_id: int, text: str) -> None:
        message = getattr(query, "message", None)
        if message is not None:
            await message.reply_text(text)
            return
        await bot.send_message(chat_id=chat_id, text=text)

    async def _deliver_report(
        self,
        bot: Bot,
        chat_id: int,
        report: str,
        *,
        progress: Message | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> list[int]:
        if not report.strip():
            raise ValueError("Refusing to deliver an empty report")
        chunks = chunk_text(report)
        message_ids: list[int] = []
        pending_markup = reply_markup
        if progress is not None:
            try:
                edit_kwargs = {"reply_markup": pending_markup} if pending_markup is not None else {}
                await progress.edit_text(chunks[0], **edit_kwargs)
                message_ids.append(progress.message_id)
                chunks = chunks[1:]
                pending_markup = None
            except Exception:  # noqa: BLE001 - retry as fresh Telegram messages
                LOGGER.exception("report_progress_edit_failed chat_id=%s", chat_id)
        for chunk in chunks:
            try:
                send_kwargs = {"reply_markup": pending_markup} if pending_markup is not None else {}
                message = await send_message_with_light_retry(
                    bot, chat_id=chat_id, text=chunk, **send_kwargs
                )
            except Exception as exc:  # noqa: BLE001 - retain partial delivery audit
                raise ReportDeliveryError(message_ids) from exc
            message_ids.append(message.message_id)
            pending_markup = None
        return message_ids
