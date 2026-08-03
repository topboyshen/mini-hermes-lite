from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable

from telegram import BotCommand, Message, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from mini_hermes.agent import AgentService
from mini_hermes.config import Settings
from mini_hermes.memory import MemoryStore
from mini_hermes.monitor import system_status
from mini_hermes.pi_channel import PiChannelClient, PiChannelError
from mini_hermes.scheduler import parse_daily_time


LOGGER = logging.getLogger(__name__)
STOCK_CODE = re.compile(r"^[0-9]{6}$")


class MiniHermesBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.memory = MemoryStore(settings.database_path)
        model_missing = [
            name
            for name in settings.missing_runtime_settings()
            if name != "TELEGRAM_TOKEN"
        ]
        self.agent = None if model_missing else AgentService(settings, self.memory)
        self.pi_channel = PiChannelClient(
            settings.pi_channel_socket, settings.pi_channel_timeout_seconds
        )
        self._pi_mode_users: set[int] = set()
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
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))
        self.application.add_error_handler(self.error_handler)

    async def _post_init(self, application: Application) -> None:
        await self.memory.initialize()
        if self.agent is not None:
            await self.agent.start()
        await application.bot.set_my_commands(
            [
                BotCommand("stock", "个股研究：/stock 600875"),
                BotCommand("market", "市场概览"),
                BotCommand("report", "立即生成晨报"),
                BotCommand("watch", "管理自选股"),
                BotCommand("style", "设置投资风格"),
                BotCommand("subscribe", "订阅每日晨报"),
                BotCommand("status", "节点状态"),
                BotCommand("pi", "进入 pi 完整交互模式"),
                BotCommand("hermes", "退出 pi 模式，返回 A 股 Agent"),
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
        LOGGER.info("telegram_started daily_report=%s", self.settings.daily_report_time)

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
                "Telegram 入口已在线，但模型 API Key 尚未配置。请填写服务器 .env 中与 MODEL 对应的 Key 后重启服务。",
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

    async def _run_pi(self, update: Update, prompt: str) -> None:
        if not await self._authorized(update):
            return
        user, chat = update.effective_user, update.effective_chat
        assert user is not None and chat is not None
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
            await self.memory.append_turn(user.id, prompt, response)
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
            "Mini Hermes Lite 已在线。默认是 A 股研究 Agent；发送 /pi 可进入完整 pi 交互模式。",
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
            "/report — 立即生成晨报\n"
            "/watch add 600875 — 添加自选\n"
            "/watch remove 600875 — 删除自选\n"
            "/watch list — 查看自选\n"
            "/style 稳健中线 — 设置投资风格\n"
            "/subscribe — 订阅 08:30 晨报\n"
            "/unsubscribe — 取消晨报\n"
            "/status — 节点状态\n\n"
            "pi 完整交互：\n"
            "/pi — 进入 pi 模式（后续普通消息都交给 pi）\n"
            "/pi 任务内容 — 立即交给 pi 并进入 pi 模式\n"
            "/new — 新建 pi 会话\n"
            "/abort — 中止当前 pi 任务\n"
            "/pistatus — 查看 pi channel 状态\n"
            "/hermes — 退出 pi 模式，回到 A 股 Agent",
        )

    async def pi_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        user = update.effective_user
        assert user is not None
        self._pi_mode_users.add(user.id)
        if context.args:
            await self._run_pi(update, " ".join(context.args))
            return
        await self._reply(
            update,
            "已进入 pi 完整交互模式。后续普通消息会保持同一会话；pi 可在隔离工作区读写文件、运行命令和调用工具。\n"
            "使用 /new 开新会话，/abort 中止任务，/hermes 返回 A 股 Agent。",
        )

    async def hermes_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        user = update.effective_user
        assert user is not None
        self._pi_mode_users.discard(user.id)
        await self._reply(update, "已退出 pi 模式，回到 Mini Hermes A 股研究 Agent。")

    async def new_pi_session_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._authorized(update):
            return
        user = update.effective_user
        assert user is not None
        try:
            session = await self.pi_channel.new_session(user.id)
            self._pi_mode_users.add(user.id)
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
                f"busy: {status.get('busy')}\n"
                f"mode: {'pi' if user.id in self._pi_mode_users else 'hermes'}",
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
        await self._run_agent(update, "生成今日 A 股晨报，并重点覆盖我的自选股。")

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
        await self._reply(update, f"已订阅每日 {self.settings.daily_report_time} A 股晨报。")

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
            f"MCP: {'ready' if mcp_ready else 'waiting for model key'} ({toolset_count} server)\n"
            f"API: {'configured' if api_ready else 'missing key'}\n"
            f"Memory: {stats['users']} user / {stats['conversations']} message",
        )

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is not None and message.text:
            user = update.effective_user
            if user is not None and user.id in self._pi_mode_users:
                await self._run_pi(update, message.text)
            else:
                await self._run_agent(update, message.text)

    async def daily_report_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.agent is None:
            LOGGER.warning("daily_report_skipped reason=model_key_missing")
            return
        for user_id, chat_id in await self.memory.subscriptions():
            if user_id not in self.settings.allowed_user_ids:
                continue
            try:
                report = await self.agent.run(user_id, "生成今日 A 股晨报，并重点覆盖我的自选股。")
                for index in range(0, len(report), 4000):
                    await context.bot.send_message(chat_id=chat_id, text=report[index : index + 4000])
            except Exception:  # noqa: BLE001 - one subscriber must not block others
                LOGGER.exception("daily_report_failed user_id=%s chat_id=%s", user_id, chat_id)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOGGER.exception("telegram_update_failed", exc_info=context.error)

    def run(self) -> None:
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)
