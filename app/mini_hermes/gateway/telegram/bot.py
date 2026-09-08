from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import re
import time
import uuid
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from mini_hermes.agent import AgentService
from mini_hermes.config import Settings
from mini_hermes.gateway.telegram.background import bounded_background
from mini_hermes.gateway.telegram.commands import CommandsMixin, FEATURE_MENU_TEXTS
from mini_hermes.gateway.telegram.delivery import DeliveryMixin, ReportDeliveryError
from mini_hermes.gateway.telegram.errors import log_handler_error
from mini_hermes.gateway.telegram.jobs import JobsMixin
from mini_hermes.gateway.telegram.media import MediaMixin
from mini_hermes.gateway.telegram.progress import TaskProgress
from mini_hermes.gateway.telegram.routing import FULL_REPORT_CALLBACK
from mini_hermes.gateway.telegram.signal_gate_review_forward import signal_gate_review_callback
from mini_hermes.memory import MemoryStore
from mini_hermes.overseas_market import OverseasMarketSnapshot, fetch_overseas_snapshot
from mini_hermes.pi_channel import PiChannelClient, PiChannelError
from mini_hermes.pi_channel.errors import error_message
from mini_hermes.reporting import (
    GlobalReportBundle,
    compose_global_report_bundle,
    compose_weekly_report,
)
from mini_hermes.scheduler import parse_daily_time

LOGGER = logging.getLogger(__name__)

# Sensible HTTPX timeouts for Bot API calls (defaults are 5s/5s/5s/1s).
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0
_WRITE_TIMEOUT = 30.0
_POOL_TIMEOUT = 5.0
# Long-polling getUpdates needs a read timeout at least as long as the poll window.
_GET_UPDATES_READ_TIMEOUT = 30.0


class MiniHermesBot(DeliveryMixin, CommandsMixin, JobsMixin, MediaMixin):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.memory = MemoryStore(settings.database_path)
        model_missing = [
            name
            for name in settings.missing_runtime_settings()
            if name != "TELEGRAM_TOKEN"
        ]
        self.agent: AgentService | None = None
        if not model_missing:
            try:
                self.agent = AgentService(settings, self.memory)
            except Exception:  # noqa: BLE001 - keep pi and reports available
                LOGGER.exception(
                    "agent_initialization_failed a_share_commands_disabled=true"
                )
        self.pi_channel = PiChannelClient(
            settings.pi_channel_socket, settings.pi_channel_timeout_seconds
        )
        if model_missing:
            LOGGER.warning("agent_disabled missing=%s", ",".join(model_missing))
        self.application = (
            Application.builder()
            .token(settings.telegram_token)
            .concurrent_updates(settings.max_concurrent_agent_runs)
            .connection_pool_size(max(4, settings.max_concurrent_agent_runs * 2))
            .connect_timeout(_CONNECT_TIMEOUT)
            .read_timeout(_READ_TIMEOUT)
            .write_timeout(_WRITE_TIMEOUT)
            .pool_timeout(_POOL_TIMEOUT)
            .get_updates_connect_timeout(_CONNECT_TIMEOUT)
            .get_updates_read_timeout(_GET_UPDATES_READ_TIMEOUT)
            .get_updates_write_timeout(_WRITE_TIMEOUT)
            .get_updates_pool_timeout(_POOL_TIMEOUT)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        self._register_handlers()

    def _register_handlers(self) -> None:
        commands: list[tuple[str, Callable[..., Awaitable[None]]]] = [
            ("start", self.start_command),
            ("help", self.help_command),
            ("whoami", self.whoami_command),
            ("stock", self.stock_command),
            ("market", self.market_command),
            ("report", self.report_command),
            ("weekly", self.weekly_command),
            ("watch", self.watch_command),
            ("style", self.style_command),
            ("subscribe", self.subscribe_command),
            ("unsubscribe", self.unsubscribe_command),
            ("status", self.status_command),
            ("pi", self.pi_command),
            ("hermes", self.hermes_command),
            ("new", self.new_pi_session_command),
            ("abort", self.abort_pi_command),
            ("pistatus", self.pi_status_command),
        ]
        for name, callback in commands:
            background = name in {"stock", "market", "report", "weekly", "pi"}
            self.application.add_handler(CommandHandler(
                name, bounded_background(self, callback) if background else callback,
                block=not background,
            ))
        self.application.add_handler(
            CallbackQueryHandler(
                self.full_report_callback,
                pattern=FULL_REPORT_CALLBACK,
            )
        )
        self.application.add_handler(
            CallbackQueryHandler(
                signal_gate_review_callback,
                pattern=re.compile(r"^sg:(ok|no):\d+$"),
            )
        )
        self.application.add_handler(
            MessageHandler(
                filters.PHOTO | filters.LIVE_PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO
                | filters.VIDEO | filters.ANIMATION | filters.Sticker.ALL,
                bounded_background(self, self.media_message), block=False,
            )
        )
        self.attachments_enabled = True
        self.application.add_handler(
            MessageHandler(
                filters.TEXT & filters.Regex(
                    re.compile("^(?:" + "|".join(re.escape(name) for name in FEATURE_MENU_TEXTS) + ")$")
                ),
                self._feature_menu_dispatch, block=False,
            )
        )
        self.application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                bounded_background(self, self.text_message), block=False,
            )
        )
        self.application.add_error_handler(self.error_handler)

    async def _feature_menu_dispatch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is not None and message.text in {"海外晨报", "本周简报"}:
            await bounded_background(self, self.feature_menu_message)(update, context)
        else:
            await self.feature_menu_message(update, context)

    async def _post_init(self, application: Application) -> None:
        await self.memory.initialize()
        if self.agent is not None:
            try:
                await self.agent.start()
            except Exception:  # noqa: BLE001 - keep pi and reports available
                LOGGER.exception("agent_start_failed a_share_commands_disabled=true")
                self.agent = None
        await application.bot.set_my_commands(
            [
                BotCommand("help", "功能菜单、图片和文件使用说明"),
                BotCommand("stock", "个股研究：/stock 600875"),
                BotCommand("market", "市场概览"),
                BotCommand("report", "立即生成海外市场晨报"),
                BotCommand("weekly", "查看本周海外市场简报"),
                BotCommand("watch", "管理自选股"),
                BotCommand("style", "设置投资风格"),
                BotCommand("subscribe", "订阅海外市场晨报"),
                BotCommand("status", "节点状态"),
                BotCommand("new", "新建 pi 会话"),
                BotCommand("abort", "中止当前 pi 任务"),
                BotCommand("pistatus", "当前模型和任务状态"),
                BotCommand("whoami", "显示 Telegram 用户 ID"),
            ]
        )
        if application.job_queue is None:
            raise RuntimeError("Telegram JobQueue is unavailable")
        application.job_queue.run_daily(
            self.daily_report_job,
            time=parse_daily_time(self.settings.daily_report_time, self.settings.timezone),
            name="daily-report",
        )
        application.job_queue.run_daily(
            self.weekly_report_job,
            time=parse_daily_time("09:00", self.settings.timezone),
            name="weekly-report",
        )
        LOGGER.info(
            "telegram_started daily_report=%s weekly_report=saturday-09:00",
            self.settings.daily_report_time,
        )

    async def _post_shutdown(self, application: Application) -> None:
        if self.agent is not None:
            await self.agent.stop()
        await self.memory.close()

    async def _touch(self, update: Update) -> None:
        user, chat = update.effective_user, update.effective_chat
        if user is None or chat is None:
            return
        await self.memory.touch_user(user.id, user.username, user.first_name, chat.id)

    async def _authorized(self, update: Update, onboarding: bool = False) -> bool:
        user = update.effective_user
        if user is None:
            return False
        if user.id in self.settings.allowed_user_ids:
            await self._touch(update)
            return True
        if onboarding:
            await self._reply(
                update,
                f"你的 Telegram 用户 ID：{user.id}\n"
                "当前未授权。把该 ID 写入服务器 .env 的 TELEGRAM_ALLOWED_USER_IDS 后重启服务。",
            )
            return False
        await self._reply(update, f"未授权。发送 /whoami 获取用户 ID。当前 ID：{user.id}")
        return False

    async def _run_agent(self, update: Update, prompt: str) -> None:
        if not await self._authorized(update):
            return
        if self.agent is None:
            await self._reply(
                update,
                "A 股研究 Agent 暂不可用，请用 /status 查看状态或检查服务日志。"
                "普通 pi 对话和海外晨报不受影响。",
            )
            return
        user, chat = update.effective_user, update.effective_chat
        assert user is not None and chat is not None
        message = update.effective_message
        if message is None:
            return
        progress = await message.reply_text("⏳ 正在获取数据并分析，请稍候…")
        started = time.monotonic()
        LOGGER.info("agent_request_started user_id=%s prompt_chars=%s", user.id, len(prompt))
        await self.application.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
        try:
            response = await self.agent.run(user.id, prompt)
        except asyncio.TimeoutError:
            LOGGER.warning("agent_timeout user_id=%s", user.id)
            response = "分析超时，请稍后重试或缩小查询范围。"
        except Exception:  # noqa: BLE001 - keep Telegram handler alive
            LOGGER.exception("agent_run_failed user_id=%s", user.id)
            response = "Agent 或数据工具暂时不可用，请稍后重试；可用 /status 查看节点状态。"
        LOGGER.info(
            "agent_request_finished user_id=%s seconds=%.2f response_chars=%s",
            user.id,
            time.monotonic() - started,
            len(response),
        )
        await self._finish_progress(update, progress, response)

    async def _generate_global_report_bundle(
        self, snapshot: OverseasMarketSnapshot | None = None
    ) -> GlobalReportBundle:
        generated_at = datetime.now(ZoneInfo(self.settings.timezone))
        if snapshot is None:
            snapshot = await fetch_overseas_snapshot(now=generated_at)
        report_date = generated_at.date().isoformat()
        return compose_global_report_bundle(
            report_date,
            snapshot,
            generated_at=generated_at,
        )

    async def _generate_global_report(
        self, snapshot: OverseasMarketSnapshot | None = None
    ) -> str:
        """Compatibility wrapper for callers that still need the full text."""
        return (await self._generate_global_report_bundle(snapshot)).full

    async def _generate_weekly_report(self) -> str:
        generated_at = datetime.now(ZoneInfo(self.settings.timezone))
        end_date = generated_at.date()
        start_date = end_date - timedelta(days=6)
        reports = await self.memory.market_reports_between(
            start_date.isoformat(), end_date.isoformat()
        )
        return compose_weekly_report(end_date.isoformat(), reports)

    async def _run_report(self, update: Update) -> None:
        if not await self._authorized(update):
            return
        user, chat = update.effective_user, update.effective_chat
        message = update.effective_message
        if user is None or chat is None or message is None:
            return
        progress = await message.reply_text("⏳ 正在生成海外市场晨报，请稍候…")
        started = time.monotonic()
        LOGGER.info("report_generation_started trigger=manual user_id=%s", user.id)
        try:
            bundle = await self._generate_global_report_bundle()
        except asyncio.TimeoutError:
            LOGGER.warning(
                "report_generation_failed trigger=manual reason=timeout user_id=%s",
                user.id,
            )
            await self._finish_progress(update, progress, "报告生成超时，请稍后重试。")
            return
        except Exception:  # noqa: BLE001 - keep Telegram handler alive
            LOGGER.exception("report_generation_failed trigger=manual user_id=%s", user.id)
            await self._finish_progress(
                update, progress, "海外市场晨报暂时不可用，请稍后重试。"
            )
            return
        try:
            await self.memory.save_market_report(bundle.report_date, bundle.full)
            report_saved = True
        except Exception:  # noqa: BLE001 - detail storage must not block the brief
            LOGGER.exception(
                "report_history_save_failed trigger=manual date=%s", bundle.report_date
            )
            report_saved = False
        try:
            message_ids = await self._deliver_report(
                self.application.bot,
                chat.id,
                bundle.brief,
                progress=progress,
                reply_markup=self._full_report_markup(bundle) if report_saved else None,
            )
        except ReportDeliveryError as exc:
            LOGGER.exception(
                "report_delivery_failed trigger=manual user_id=%s chat_id=%s "
                "partial_message_ids=%s",
                user.id,
                chat.id,
                exc.message_ids,
            )
            await self._reply(update, "晨报投递未完整完成，请稍后重试。")
            return
        except Exception:  # noqa: BLE001 - keep Telegram handler alive
            LOGGER.exception(
                "report_delivery_failed trigger=manual user_id=%s chat_id=%s",
                user.id,
                chat.id,
            )
            await self._reply(update, "晨报投递失败，请稍后重试。")
            return
        LOGGER.info(
            "report_delivery_succeeded trigger=manual user_id=%s chat_id=%s "
            "message_ids=%s chars=%s seconds=%.2f",
            user.id,
            chat.id,
            message_ids,
            len(bundle.brief),
            time.monotonic() - started,
        )

    async def _run_weekly_report(self, update: Update) -> None:
        if not await self._authorized(update):
            return
        user, chat = update.effective_user, update.effective_chat
        message = update.effective_message
        if user is None or chat is None or message is None:
            return
        progress = await message.reply_text("⏳ 正在汇总本周海外市场，请稍候…")
        try:
            report = await self._generate_weekly_report()
            await self._deliver_report(
                self.application.bot,
                chat.id,
                report,
                progress=progress,
            )
        except Exception:  # noqa: BLE001 - keep Telegram handler alive
            LOGGER.exception("weekly_report_failed trigger=manual user_id=%s", user.id)
            await self._finish_progress(update, progress, "周报暂时不可用，请稍后重试。")

    async def _run_pi(
        self, update: Update, prompt: str, *,
        attachments: list[dict[str, str]] | None = None, progress: object | None = None,
    ) -> None:
        if not await self._authorized(update):
            return
        user, chat = update.effective_user, update.effective_chat
        assert user is not None and chat is not None
        if chat.type != "private":
            LOGGER.warning(
                "pi_request_rejected_non_private user_id=%s chat_id=%s", user.id, chat.id
            )
            await self._reply(update, "出于安全考虑，pi 远程执行只允许在机器人私聊中使用。")
            return
        message = update.effective_message
        if message is None:
            return
        if progress is None:
            progress = await message.reply_text("⏳ 已接收，正在连接助手…\n/abort 可中止当前 pi 任务")
        started = time.monotonic()
        tracker = TaskProgress(progress, started=started)
        tracker.start()

        request_id = uuid.uuid4().hex
        outcome, error_code, response = "failed", "upstream_error", ""
        LOGGER.info(
            "pi_request_started request_id=%s user_id=%s prompt_chars=%s",
            request_id, user.id, len(prompt),
        )
        try:
            try:
                await self.application.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
            except Exception:  # typing status must not prevent the actual request
                LOGGER.debug("pi_typing_status_unavailable request_id=%s", request_id)
            if attachments:
                response = await self.pi_channel.prompt(
                    user.id, prompt, tracker.update, attachments=attachments
                )
            else:
                response = await self.pi_channel.prompt(user.id, prompt, tracker.update)
            if not isinstance(response, str) or not response.strip():
                raise PiChannelError(code="invalid_response")
            outcome, error_code = "success", "none"
            try:
                memory_prompt = "[附带一份图片或文本；原附件不存入本数据库]\n" + prompt if attachments else prompt
                await self.memory.append_turn(user.id, memory_prompt, response, channel="pi")
            except Exception:  # keep a successful answer available if memory fails
                LOGGER.warning("pi_memory_write_failed request_id=%s", request_id)
        except asyncio.CancelledError:
            outcome, error_code = "cancelled", "cancelled"
            raise
        except PiChannelError as exc:
            error_code = exc.code
            outcome = "cancelled" if error_code == "cancelled" else "failed"
            response = error_message(error_code)
        except Exception:  # provider diagnostics must not leak into logs or chat
            response = error_message(error_code)
        finally:
            await tracker.stop()
            if outcome == "failed":
                LOGGER.warning(
                    "pi_request_failed request_id=%s user_id=%s error_code=%s",
                    request_id, user.id, error_code,
                )
            LOGGER.info(
                "pi_request_finished request_id=%s user_id=%s outcome=%s "
                "error_code=%s seconds=%.2f response_chars=%s",
                request_id, user.id, outcome, error_code,
                time.monotonic() - started, len(response),
            )
        await self._finish_progress(update, progress, response)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # PTB already retries getUpdates on transient network errors; classify here
        # so handler-path NetworkError/TimedOut/RetryAfter do not spam EXCEPTION logs.
        log_handler_error(LOGGER, context.error)

    def run(self) -> None:
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)
