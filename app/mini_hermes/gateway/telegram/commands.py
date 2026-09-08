from __future__ import annotations

import logging
from hashlib import sha256

from telegram.ext import ContextTypes
from telegram import ReplyKeyboardMarkup, Update

from mini_hermes.gateway.telegram.delivery import ReportDeliveryError
from mini_hermes.gateway.telegram.thinking import ThinkingMenuMixin
from mini_hermes.gateway.telegram.routing import (
    FULL_REPORT_CALLBACK,
    STOCK_CODE,
    classify_plain_text_stock_intent,
)
from mini_hermes.monitor import system_status
from mini_hermes.pi_channel import PiChannelError

LOGGER = logging.getLogger(__name__)
FEATURE_MENU_TEXTS = ("功能帮助", "任务状态", "海外晨报", "本周简报", "思考强度")


def feature_menu_markup() -> ReplyKeyboardMarkup:
    """Text-only private-chat shortcuts; no URLs or state-changing actions."""
    return ReplyKeyboardMarkup(
        [list(FEATURE_MENU_TEXTS[:2]), list(FEATURE_MENU_TEXTS[2:4]), [FEATURE_MENU_TEXTS[4]]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="直接发问题，或选择下方功能",
    )


def attachment_help_text(enabled: bool) -> str:
    introduction = (
        "发图或文本附件，并附一句你想让我做什么。"
        if enabled is True else "附件功能即将开放；当前请先直接发文字。"
    )
    return (
        introduction + "\n"
        "格式与上限：JPG/PNG/WebP 单张 ≤1 MiB；"
        "TXT/MD/CSV/JSON 单个文件 ≤128 KiB。\n"
        "仅限已授权私聊；暂不支持 PDF、Office 文件或语音。"
    )


def _model_label(value: object) -> str:
    # A status response is untrusted. Never stringify arbitrary objects or
    # display paths/URLs/tokens masquerading as a model name.
    if type(value) is not str or len(value) > 96:
        return "名称暂未确认"
    known = {"xai/grok-4.6": "Grok 4.6（xAI）", "xai/grok-4.5": "Grok 4.5（xAI）"}
    if value in known:
        return known[value]
    return "名称暂未确认"


def format_pi_status(value: object) -> str:
    """Show only the three public status fields, with no guessed idle state."""
    if type(value) is not dict:
        return "任务状态暂时无法确认，请稍后再用 /pistatus 查询。"
    busy = value.get("busy")
    if busy is True:
        activity = "普通聊天：通道正在处理任务。"
        next_step = "需要停止当前 pi 任务，可发送 /abort。"
    elif busy is False:
        activity = "普通聊天：当前空闲，可以直接发消息。"
        next_step = "想重新开始一段对话，可发送 /new。"
    else:
        activity = "普通聊天：忙碌状态暂未确认。"
        next_step = "请稍后再用 /pistatus 查询。"
    research = value.get("research_active")
    if type(research) is int and 0 <= research <= 128:
        research_line = (
            "A 股模型通道：当前有模型请求处理中。" if research else
            "A 股模型通道：当前无活动模型请求（不代表整个研究任务已结束）。"
        )
    else:
        research_line = "A 股模型通道：请求状态暂未确认。"
    return "\n".join(("任务状态", "当前模型：" + _model_label(value.get("model")),
                      activity, research_line, "", next_step))


class CommandsMixin(ThinkingMenuMixin):
    """Telegram command and callback handlers."""

    async def _private_feature_authorized(self, update: Update, *, onboarding: bool = False) -> bool:
        chat = update.effective_chat
        if chat is None:
            return False
        # Check chat type before _authorized/_touch, so a group menu cannot
        # overwrite the user's stored private-chat address.
        if getattr(chat, "type", None) != "private":
            await self._reply(update, "请在与机器人的私聊中使用此功能。")
            return False
        return await self._authorized(update, onboarding=onboarding)

    async def _reply_with_feature_menu(self, update: Update, text: str) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        # No progress edit: Telegram reply keyboards attach to fresh messages.
        await self._deliver_report(
            self.application.bot, chat.id, text, reply_markup=feature_menu_markup()
        )

    async def feature_menu_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Registered before generic TEXT by the bot shell; dispatch exact labels."""
        message = update.effective_message
        if message is None or type(message.text) is not str or message.text not in FEATURE_MENU_TEXTS:
            return
        if not await self._private_feature_authorized(update):
            return
        handlers = {
            "功能帮助": self.help_command,
            "任务状态": self.pi_status_command,
            "海外晨报": self.report_command,
            "本周简报": self.weekly_command,
            "思考强度": self.thinking_command,
        }
        await handlers[message.text](update, context)

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
                LOGGER.warning(
                    "report_callback_answer_failed authorized=false user_id=%s", user.id
                )
            return
        try:
            await query.answer()
        except Exception:  # noqa: BLE001 - still try to deliver the requested detail
            LOGGER.warning(
                "report_callback_answer_failed authorized=true user_id=%s", user.id
            )
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
                LOGGER.exception(
                    "report_callback_error_notice_failed user_id=%s", user.id
                )
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

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._private_feature_authorized(update, onboarding=True):
            return
        await self._reply_with_feature_menu(
            update,
            "我是 Mini Hermes。直接告诉我你想做什么，不用切换模式。\n\n"
            "普通聊天：解释问题、整理文字、处理代码。\n"
            "A 股研究：/stock 600875 看个股，/market 看市场；不用录入真实持仓。\n"
            "市场简报：/report 看海外晨报，/weekly 看本周简报。\n\n"
            + attachment_help_text(getattr(self, "attachments_enabled", False))
            + "\n\n下方按钮可打开常用功能；/help 查看完整说明。",
        )

    async def whoami_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is not None:
            await self._reply(update, f"你的 Telegram 用户 ID：{user.id}")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._private_feature_authorized(update):
            return
        await self._reply_with_feature_menu(
            update,
            "功能帮助\n\n"
            "直接聊天\n"
            "发文字即可，例如：帮我整理这段内容、解释这个报错。无需切换模式。\n\n"
            "图片与文本附件\n"
            + attachment_help_text(getattr(self, "attachments_enabled", False))
            + "\n\nA 股与市场简报（不需要真实持仓）\n"
            "/stock 600875 — 研究一只 A 股\n"
            "/market — A 股市场概览\n"
            "/report — 立即生成海外晨报\n"
            "/weekly — 本周海外简报\n\n"
            "任务控制\n"
            "/pistatus — 查看模型和忙碌状态\n"
            "/abort — 请求中止当前 pi 任务\n"
            "/new — 开始新的 pi 对话\n"
            "/status — 查看服务器概况\n\n"
            "按需设置，不会因打开菜单而修改\n"
            "/thinking — 分别选择聊天和 A 股分析的思考强度\n"
            "/watch list — 查看自选\n"
            "/watch add 600875 / /watch remove 600875 — 增删自选\n"
            "/style 稳健中线 — 保存偏好\n"
            "/subscribe — 订阅定时晨报与周报\n"
            "/unsubscribe — 取消订阅\n\n"
            "下方按钮只是功能入口，不会自动订阅或批准任何事项。",
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
            await self._reply(
                update, "已中止当前 pi 任务。" if aborted else "当前没有运行中的 pi 任务。"
            )
        except PiChannelError as exc:
            await self._reply(update, f"无法中止 pi 任务：{exc}")

    async def pi_status_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._private_feature_authorized(update):
            return
        try:
            status = await self.pi_channel.status()
        except Exception:  # noqa: BLE001 - status errors must not expose provider details
            LOGGER.warning("pi_status_unavailable")
            await self._reply(update, "任务状态暂时无法读取，请稍后再试 /pistatus。")
            return
        await self._reply(update, format_pi_status(status))

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
            await self._reply(
                update, "用法：/watch add 600875 | /watch remove 600875 | /watch list"
            )
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

    async def subscribe_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
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

    async def unsubscribe_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
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
            f"A股研究模型: {self.settings.model}\n"
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
