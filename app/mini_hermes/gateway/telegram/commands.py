from __future__ import annotations

import logging
from hashlib import sha256

from telegram.ext import ContextTypes
from telegram import Update

from mini_hermes.gateway.telegram.delivery import ReportDeliveryError
from mini_hermes.gateway.telegram.routing import (
    FULL_REPORT_CALLBACK,
    STOCK_CODE,
    classify_plain_text_stock_intent,
)
from mini_hermes.monitor import system_status
from mini_hermes.pi_channel import PiChannelError

LOGGER = logging.getLogger(__name__)


class CommandsMixin:
    """Telegram command and callback handlers."""

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
            await self._reply(
                update, "已中止当前 pi 任务。" if aborted else "当前没有运行中的 pi 任务。"
            )
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
