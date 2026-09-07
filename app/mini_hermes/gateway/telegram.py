from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from hashlib import sha256
import logging
import re
import time
from typing import Awaitable, Callable, Sequence
from zoneinfo import ZoneInfo

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
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
from mini_hermes.memory import MemoryStore
from mini_hermes.monitor import system_status
from mini_hermes.overseas_market import OverseasMarketSnapshot, fetch_overseas_snapshot
from mini_hermes.pi_channel import PiChannelClient, PiChannelError
from mini_hermes.reporting import (
    GlobalReportBundle,
    compose_global_report_bundle,
    compose_weekly_report,
)
from mini_hermes.scheduler import parse_daily_time


LOGGER = logging.getLogger(__name__)
STOCK_CODE = re.compile(r"^[0-9]{6}$")
_STOCK_CODE_IN_TEXT = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_STOCK_ACTION = re.compile(r"(看|分析|研究)")
_STRONG_ASHARE = re.compile(
    r"(A\s*股|沪深|上证|深证|创业板|科创板|北交所|大盘走势|市场概览|今日行情|个股研究)"
)


FULL_REPORT_CALLBACK = re.compile(
    r"^market_full:([0-9a-f]{32})$"
)


def classify_plain_text_stock_intent(text: str) -> tuple[str, str | None]:
    """Conservative plain-text stock router for the single Telegram channel.

    Returns:
      ("agent", code) — auto-route to Mini Hermes A-share agent
      ("hint", None) — ambiguous A-share phrase without a code
      ("pi", None) — default coding / general pi path
    """
    stripped = text.strip()
    if not stripped:
        return ("pi", None)
    if STOCK_CODE.fullmatch(stripped):
        return ("agent", stripped)

    code_match = _STOCK_CODE_IN_TEXT.search(stripped)
    if code_match and _STOCK_ACTION.search(stripped):
        return ("agent", code_match.group(1))

    if code_match:
        # Short messages where a 6-digit token is the clear subject (e.g. "600875 怎么样")
        tokens = re.findall(r"\S+", stripped)
        cleaned = [token.strip(".,!?;:，。！？、") for token in tokens]
        code_tokens = [token for token in cleaned if STOCK_CODE.fullmatch(token)]
        if code_tokens and len(cleaned) <= 4:
            return ("agent", code_tokens[0])

    if _STRONG_ASHARE.search(stripped) and code_match is None:
        return ("hint", None)
    return ("pi", None)


class ReportDeliveryError(RuntimeError):
    def __init__(self, message_ids: Sequence[int]) -> None:
        super().__init__("Telegram report delivery was only partially completed")
        self.message_ids = tuple(message_ids)


class MiniHermesBot:
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
                LOGGER.exception("agent_initialization_failed a_share_commands_disabled=true")
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
            self.application.add_handler(CommandHandler(name, callback))
        self.application.add_handler(
            CallbackQueryHandler(
                self.full_report_callback,
                pattern=FULL_REPORT_CALLBACK,
            )
        )
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))
        self.application.add_error_handler(self.error_handler)

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
                BotCommand("pistatus", "pi channel 状态"),
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

    async def _reply(self, update: Update, text: str) -> None:
        message = update.effective_message
        if message is None:
            return
        chunks = [text[index : index + 4000] for index in range(0, len(text), 4000)] or [""]
        for chunk in chunks:
            await message.reply_text(chunk)

    async def _finish_progress(self, update: Update, progress: Message, text: str) -> None:
        chunks = [text[index : index + 4000] for index in range(0, len(text), 4000)] or [
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

    @staticmethod
    def _full_report_markup(bundle: GlobalReportBundle) -> InlineKeyboardMarkup:
        report_id = sha256(bundle.full.encode("utf-8")).hexdigest()[:32]
        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "查看完整数据",
                    callback_data=f"market_full:{report_id}",
                )
            ]]
        )

    @staticmethod
    async def _reply_to_callback(query: object, bot: Bot, chat_id: int, text: str) -> None:
        message = getattr(query, "message", None)
        if message is not None:
            await message.reply_text(text)
            return
        await bot.send_message(chat_id=chat_id, text=text)

    async def _generate_weekly_report(self) -> str:
        generated_at = datetime.now(ZoneInfo(self.settings.timezone))
        end_date = generated_at.date()
        start_date = end_date - timedelta(days=6)
        reports = await self.memory.market_reports_between(
            start_date.isoformat(), end_date.isoformat()
        )
        return compose_weekly_report(end_date.isoformat(), reports)

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
        chunks = [report[index : index + 4000] for index in range(0, len(report), 4000)]
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
                message = await bot.send_message(chat_id=chat_id, text=chunk, **send_kwargs)
            except Exception as exc:  # noqa: BLE001 - retain partial delivery audit
                raise ReportDeliveryError(message_ids) from exc
            message_ids.append(message.message_id)
            pending_markup = None
        return message_ids

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
            LOGGER.warning("report_generation_failed trigger=manual reason=timeout user_id=%s", user.id)
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

    async def full_report_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        user = update.effective_user
        chat = update.effective_chat
        if query is None or user is None or chat is None:
            return
        if user.id not in self.settings.allowed_user_ids:
            try:
                await query.answer("未授权", show_alert=True)
            except Exception:  # noqa: BLE001 - authorization still fails closed
                LOGGER.warning("report_callback_answer_failed authorized=false user_id=%s", user.id)
            return
        try:
            await query.answer()
        except Exception:  # noqa: BLE001 - still try to deliver the requested detail
            LOGGER.warning("report_callback_answer_failed authorized=true user_id=%s", user.id)
        match = FULL_REPORT_CALLBACK.fullmatch(query.data or "")
        if match is None:
            return
        report_id = match.group(1)
        try:
            await self._touch(update)
            report = await self.memory.market_report_detail(report_id)
        except Exception:  # noqa: BLE001 - keep callback responsive on storage errors
            LOGGER.exception("report_callback_storage_failed user_id=%s", user.id)
            try:
                await self._reply_to_callback(
                    query,
                    context.bot,
                    chat.id,
                    "完整数据暂时无法读取，请稍后再点一次。",
                )
            except Exception:  # noqa: BLE001 - nothing else can be delivered
                LOGGER.exception("report_callback_error_notice_failed user_id=%s", user.id)
            return
        if report is None or sha256(report.encode("utf-8")).hexdigest()[:32] != report_id:
            await self._reply_to_callback(
                query,
                context.bot,
                chat.id,
                "这份完整数据已过期，请发送 /report 重新生成。",
            )
            return
        try:
            await self._deliver_report(context.bot, chat.id, report)
        except ReportDeliveryError:
            LOGGER.exception("report_callback_delivery_partial user_id=%s", user.id)
            await self._reply_to_callback(
                query,
                context.bot,
                chat.id,
                "完整数据只发送了一部分，请稍后再点一次。",
            )
        except Exception:  # noqa: BLE001 - keep callback handler alive
            LOGGER.exception("report_callback_delivery_failed user_id=%s", user.id)
            await self._reply_to_callback(
                query,
                context.bot,
                chat.id,
                "完整数据发送失败，请稍后再点一次。",
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

    async def _run_pi(self, update: Update, prompt: str) -> None:
        if not await self._authorized(update):
            return
        user, chat = update.effective_user, update.effective_chat
        assert user is not None and chat is not None
        if chat.type != "private":
            LOGGER.warning("pi_request_rejected_non_private user_id=%s chat_id=%s", user.id, chat.id)
            await self._reply(update, "出于安全考虑，pi 远程执行只允许在机器人私聊中使用。")
            return
        message = update.effective_message
        if message is None:
            return
        progress = await message.reply_text("🧠 pi 已接收，正在处理…")
        started = time.monotonic()
        last_progress = ""

        async def on_progress(text: str) -> None:
            nonlocal last_progress
            display = f"🧠 {text}…"
            if display == last_progress:
                return
            last_progress = display
            try:
                await progress.edit_text(display)
            except Exception:  # noqa: BLE001 - progress updates are best effort
                LOGGER.debug("pi_progress_edit_failed", exc_info=True)

        LOGGER.info("pi_request_started user_id=%s prompt_chars=%s", user.id, len(prompt))
        await self.application.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
        try:
            response = await self.pi_channel.prompt(user.id, prompt, on_progress)
            if not response:
                response = "pi 已完成，但没有返回可显示文本。"
            await self.memory.append_turn(user.id, prompt, response, channel="pi")
        except PiChannelError as exc:
            LOGGER.warning("pi_request_failed user_id=%s error=%s", user.id, exc)
            response = f"pi channel 暂时不可用：{exc}"
        except Exception:  # noqa: BLE001 - keep Telegram handler alive
            LOGGER.exception("pi_request_failed user_id=%s", user.id)
            response = "pi channel 发生异常，请使用 /pistatus 检查。"
        LOGGER.info(
            "pi_request_finished user_id=%s seconds=%.2f response_chars=%s",
            user.id,
            time.monotonic() - started,
            len(response),
        )
        await self._finish_progress(update, progress, response)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update, onboarding=True):
            return
        await self._reply(
            update,
            "Mini Hermes Lite 已在线。普通消息由 pi 处理；A 股研究使用 /stock、/market，"
            "海外晨报使用 /report。",
        )

    async def whoami_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is not None:
            await self._reply(update, f"你的 Telegram 用户 ID：{user.id}")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        await self._reply(
            update,
            "/stock 600875 — 个股研究\n"
            "/market — A 股市场概览\n"
            "/report — 立即生成海外市场晨报\n"
            "/weekly — 查看本周海外市场简报\n"
            "/watch add 600875 — 添加自选\n"
            "/watch remove 600875 — 删除自选\n"
            "/watch list — 查看自选\n"
            "/style 稳健中线 — 设置投资风格\n"
            "/subscribe — 订阅 08:30 晨报和周六 09:00 周报\n"
            "/unsubscribe — 取消晨报\n"
            "/status — 节点状态\n\n"
            "pi 完整交互（普通消息默认进入 pi）：\n"
            "/new — 新建 pi 会话\n"
            "/abort — 中止当前 pi 任务\n"
            "/pistatus — 查看 pi channel 状态",
        )

    async def pi_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        if context.args:
            await self._run_pi(update, " ".join(context.args))
            return
        await self._reply(
            update,
            "普通消息已经默认由 pi 处理，无需切换模式。pi 可在隔离工作区读写文件、运行命令和调用工具。\n"
            "使用 /new 开新会话，/abort 中止任务；A 股研究使用 /stock、/market，"
            "海外晨报使用 /report。",
        )

    async def hermes_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        await self._reply(
            update,
            "现在是单一通道，无需切换模式。普通消息由 pi 处理；A 股研究使用 /stock、/market，"
            "海外晨报使用 /report。",
        )

    async def new_pi_session_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._authorized(update):
            return
        user = update.effective_user
        assert user is not None
        try:
            session = await self.pi_channel.new_session(user.id)
            await self._reply(update, f"已新建 pi 会话：{session[:24]}")
        except PiChannelError as exc:
            await self._reply(update, f"无法新建 pi 会话：{exc}")

    async def abort_pi_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._authorized(update):
            return
        user = update.effective_user
        assert user is not None
        try:
            aborted = await self.pi_channel.abort(user.id)
            await self._reply(update, "已中止当前 pi 任务。" if aborted else "当前没有运行中的 pi 任务。")
        except PiChannelError as exc:
            await self._reply(update, f"无法中止 pi 任务：{exc}")

    async def pi_status_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._authorized(update):
            return
        user = update.effective_user
        assert user is not None
        try:
            status = await self.pi_channel.status()
            await self._reply(
                update,
                "pi channel: ready\n"
                f"model: {status.get('model')}\n"
                f"busy: {status.get('busy')}",
            )
        except PiChannelError as exc:
            await self._reply(update, f"pi channel: unavailable\n{exc}")

    async def stock_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args or not STOCK_CODE.fullmatch(context.args[0]):
            await self._reply(update, "用法：/stock 600875")
            return
        code = context.args[0]
        await self._run_agent(
            update,
            f"分析 A 股 {code}。必须调用 AKShare MCP 获取尽可能新的行情数据，并按行情、趋势、催化、风险、观察点输出。",
        )

    async def market_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_agent(
            update,
            "生成当前 A 股市场概览。调用 AKShare MCP，覆盖主要指数、成交、涨跌分布、热点与风险。",
        )

    async def report_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_report(update)

    async def weekly_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_weekly_report(update)

    async def watch_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        user = update.effective_user
        assert user is not None
        if not context.args or context.args[0] == "list":
            symbols = await self.memory.list_watch(user.id)
            await self._reply(update, "自选股：" + (", ".join(symbols) if symbols else "暂无"))
            return
        if len(context.args) != 2 or context.args[0] not in {"add", "remove"}:
            await self._reply(update, "用法：/watch add 600875 | /watch remove 600875 | /watch list")
            return
        action, symbol = context.args
        if not STOCK_CODE.fullmatch(symbol):
            await self._reply(update, "股票代码应为 6 位数字。")
            return
        changed = (
            await self.memory.add_watch(user.id, symbol)
            if action == "add"
            else await self.memory.remove_watch(user.id, symbol)
        )
        await self._reply(update, "已更新。" if changed else "自选股没有变化。")

    async def style_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        if not context.args:
            await self._reply(update, "用法：/style 稳健中线")
            return
        user = update.effective_user
        assert user is not None
        style = " ".join(context.args)[:200]
        await self.memory.set_style(user.id, style)
        await self._reply(update, f"投资风格已保存：{style}")

    async def subscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        user, chat = update.effective_user, update.effective_chat
        assert user is not None and chat is not None
        await self.memory.set_subscription(user.id, chat.id, True)
        await self._reply(
            update,
            f"已订阅美股交易日收盘后的 {self.settings.daily_report_time} 海外市场晨报"
            "（上海时间通常为周二至周六），以及周六 09:00 周报。",
        )

    async def unsubscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        user, chat = update.effective_user, update.effective_chat
        assert user is not None and chat is not None
        await self.memory.set_subscription(user.id, chat.id, False)
        await self._reply(update, "已取消每日晨报。")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        status = system_status(self.settings.data_dir)
        stats = await self.memory.stats()
        api_ready = self.agent is not None
        mcp_ready = self.agent is not None and self.agent.ready
        toolset_count = self.agent.toolset_count if self.agent is not None else 0
        await self._reply(
            update,
            "Mini Hermes Lite v0.1\n"
            f"CPU: {status['cpu']}\nRAM: {status['ram']}\n磁盘: {status['disk']}\n"
            f"US feed: {status['us_feed']}\n"
            f"MCP: {'ready' if mcp_ready else 'waiting for model key'} ({toolset_count} server)\n"
            f"API: {'configured' if api_ready else 'missing key'}\n"
            f"Memory: {stats['users']} user / {stats['conversations']} message",
        )

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or not message.text:
            return
        text = message.text
        kind, code = classify_plain_text_stock_intent(text)
        if kind == "agent" and code is not None:
            await self._run_agent(
                update,
                f"分析 A 股 {code}。必须调用 AKShare MCP 获取尽可能新的行情数据，"
                "并按行情、趋势、催化、风险、观察点输出。",
            )
            return
        if kind == "hint":
            if not await self._authorized(update):
                return
            await self._reply(
                update,
                "看起来像 A 股相关问题。请使用 /stock <6位代码> 做个股研究，"
                "或 /market 看市场概览；普通编程/工具问题可直接发消息给 pi。",
            )
            return
        await self._run_pi(update, text)

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
                message_ids = await self._deliver_report(context.bot, chat_id, report)
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

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOGGER.exception("telegram_update_failed", exc_info=context.error)

    def run(self) -> None:
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)
