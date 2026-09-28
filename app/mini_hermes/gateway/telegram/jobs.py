from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram.ext import ContextTypes

from mini_hermes.gateway.telegram.delivery import ReportDeliveryError

LOGGER = logging.getLogger(__name__)


class JobsMixin:
    """Scheduled daily / weekly report jobs."""

    async def daily_report_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        # At 08:30 Asia/Shanghai, US Monday-Friday closes arrive Tuesday-Saturday.
        if now.weekday() in {0, 6}:
            LOGGER.info("daily_report_skipped reason=no_recent_us_close date=%s", now.date())
            return
        subscriptions = await self.memory.subscriptions()
        if not subscriptions:
            LOGGER.info("daily_report_skipped reason=no_subscribers date=%s", now.date())
            return

        recipients: list[tuple[int, int]] = []
        for user_id, chat_id in subscriptions:
            if user_id not in self.settings.allowed_user_ids:
                LOGGER.warning(
                    "daily_report_skipped reason=user_not_allowed user_id=%s chat_id=%s",
                    user_id,
                    chat_id,
                )
                continue
            recipients.append((user_id, chat_id))
        if not recipients:
            LOGGER.info("daily_report_skipped reason=no_allowed_recipients date=%s", now.date())
            return

        LOGGER.info(
            "report_generation_started trigger=scheduled date=%s recipients=%s",
            now.date(),
            len(recipients),
        )
        try:
            bundle = await self._generate_global_report_bundle()
        except Exception:  # noqa: BLE001 - one failed run must not stop the scheduler
            LOGGER.exception("report_generation_failed trigger=scheduled date=%s", now.date())
            return
        try:
            await self.memory.save_market_report(bundle.report_date, bundle.full)
            report_saved = True
        except Exception:  # noqa: BLE001 - history failure must not block delivery
            LOGGER.exception("report_history_save_failed date=%s", now.date())
            report_saved = False

        for user_id, chat_id in recipients:
            try:
                message_ids = await self._deliver_report(
                    context.bot,
                    chat_id,
                    bundle.brief,
                    reply_markup=self._full_report_markup(bundle) if report_saved else None,
                    delivery_key=("daily", now.date().isoformat()),
                )
                LOGGER.info(
                    "report_delivery_succeeded trigger=scheduled user_id=%s chat_id=%s "
                    "message_ids=%s chars=%s",
                    user_id,
                    chat_id,
                    message_ids,
                    len(bundle.brief),
                )
            except ReportDeliveryError as exc:
                LOGGER.exception(
                    "report_delivery_failed trigger=scheduled user_id=%s chat_id=%s "
                    "partial_message_ids=%s",
                    user_id,
                    chat_id,
                    exc.message_ids,
                )
            except Exception:  # noqa: BLE001 - one subscriber must not block others
                LOGGER.exception(
                    "report_delivery_failed trigger=scheduled user_id=%s chat_id=%s",
                    user_id,
                    chat_id,
                )

    async def weekly_report_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        if now.weekday() != 5:
            return
        subscriptions = await self.memory.subscriptions()
        recipients = [
            (user_id, chat_id)
            for user_id, chat_id in subscriptions
            if user_id in self.settings.allowed_user_ids
        ]
        if not recipients:
            LOGGER.info("weekly_report_skipped reason=no_allowed_recipients date=%s", now.date())
            return
        try:
            report = await self._generate_weekly_report()
        except Exception:  # noqa: BLE001 - scheduler must stay alive
            LOGGER.exception("weekly_report_generation_failed date=%s", now.date())
            return
        for user_id, chat_id in recipients:
            try:
                message_ids = await self._deliver_report(
                    context.bot, chat_id, report,
                    delivery_key=("weekly", now.date().isoformat()),
                )
                LOGGER.info(
                    "weekly_report_delivery_succeeded user_id=%s chat_id=%s "
                    "message_ids=%s chars=%s",
                    user_id,
                    chat_id,
                    message_ids,
                    len(report),
                )
            except Exception:  # noqa: BLE001 - one subscriber must not block others
                LOGGER.exception(
                    "weekly_report_delivery_failed user_id=%s chat_id=%s",
                    user_id,
                    chat_id,
                )
