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
    def __init__(self, message_ids: Sequence[int], *, outcome: str = "unknown") -> None:
        super().__init__("Telegram report delivery was only partially completed")
        self.message_ids = tuple(message_ids)
        self.outcome = outcome


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
    """Retry only Telegram's explicit rate-limit rejection.

    A timeout/network error may follow acceptance. It must reach the durable
    delivery caller rather than causing another sendMessage call.
    """
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
        except (NetworkError, TimedOut):
            LOGGER.warning("telegram_progress_edit_unconfirmed")
            raise
        except Exception:  # noqa: BLE001 - only the first edit gets a fresh-message fallback
            LOGGER.exception("telegram_progress_edit_failed")
            await self._reply(update, chunks[0])
        message = update.effective_message
        if message is not None:
            for index, chunk in enumerate(chunks[1:], start=1):
                try:
                    await message.reply_text(chunk)
                except Exception:
                    LOGGER.warning("telegram_reply_part_unconfirmed part=%s", index)
                    raise

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
        delivery_key: tuple[str, str] | None = None,
    ) -> list[int]:
        if not report.strip():
            raise ValueError("Refusing to deliver an empty report")
        chunks = chunk_text(report)
        message_ids: list[int] = []
        pending_markup = reply_markup
        if delivery_key is not None and progress is not None:
            raise ValueError("scheduled report cannot reuse an interactive progress message")
        if progress is not None:
            try:
                edit_kwargs = {"reply_markup": pending_markup} if pending_markup is not None else {}
                await progress.edit_text(chunks[0], **edit_kwargs)
                message_ids.append(progress.message_id)
                chunks = chunks[1:]
                pending_markup = None
            except (NetworkError, TimedOut) as exc:
                raise ReportDeliveryError(message_ids) from exc
            except Exception:  # noqa: BLE001 - explicit edit failure can use a fresh message
                LOGGER.exception("report_progress_edit_failed chat_id=%s", chat_id)
        content_hash = sha256(report.encode("utf-8")).hexdigest()
        for index, chunk in enumerate(chunks):
            claim = None
            if delivery_key is not None:
                claim = await self.memory.claim_report_part(
                    *delivery_key, chat_id, content_hash, len(chunks), index,
                    sha256(chunk.encode("utf-8")).hexdigest(),
                )
                if claim["status"] == "confirmed":
                    message_ids.append(int(claim["message_id"]))
                    pending_markup = None
                    continue
                if claim["status"] != "claimed":
                    raise ReportDeliveryError(message_ids, outcome=str(claim["status"]))
            try:
                send_kwargs = {"reply_markup": pending_markup} if pending_markup is not None else {}
                message = await send_message_with_light_retry(
                    bot, chat_id=chat_id, text=chunk, **send_kwargs
                )
            except Exception as exc:  # noqa: BLE001 - retain partial delivery audit
                # Only a rate-limit rejection (after its bounded retries) proves
                # non-acceptance. All other failures may have followed a send.
                outcome = "failed" if isinstance(exc, RetryAfter) else "unknown"
                if claim is not None:
                    await self.memory.finish_report_part(
                        *delivery_key, chat_id, index, str(claim["claim_id"]), outcome,
                    )
                raise ReportDeliveryError(message_ids, outcome=outcome) from exc
            if type(message.message_id) is not int or message.message_id <= 0:
                if claim is not None:
                    await self.memory.finish_report_part(
                        *delivery_key, chat_id, index, str(claim["claim_id"]), "unknown",
                    )
                raise ReportDeliveryError(message_ids)
            message_ids.append(message.message_id)
            if claim is not None:
                try:
                    await self.memory.finish_report_part(
                        *delivery_key, chat_id, index, str(claim["claim_id"]),
                        "confirmed", message.message_id,
                    )
                except Exception as exc:
                    # A sending record is intentionally left behind. Restart
                    # converts it to unknown; no subsequent run may resend it.
                    raise ReportDeliveryError(message_ids) from exc
            pending_markup = None
        return message_ids
