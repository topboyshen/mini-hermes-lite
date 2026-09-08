"""Private, owner-bound inline preferences. Never route selections to an LLM."""
import asyncio
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from mini_hermes.thinking import THINKING_LEVELS, validate_thinking

LOGGER = logging.getLogger(__name__)
THINKING_CALLBACK = re.compile(r"^think:v1:([1-9][0-9]{0,19}):(chat|research):(low|medium|high|xhigh)$")
LABELS = {"low": "快速", "medium": "均衡", "high": "深入", "xhigh": "最深入"}
SCOPES = {"chat": "普通聊天", "research": "A 股分析"}


def thinking_menu(user_id: int, levels: dict[str, str]):
    rows = []
    for scope, short in (("chat", "聊天"), ("research", "研究")):
        current = validate_thinking(levels[scope])
        buttons = [InlineKeyboardButton(
            f"{'✓ ' if level == current else ''}{short}·{LABELS[level]}",
            callback_data=f"think:v1:{user_id}:{scope}:{level}",
        ) for level in THINKING_LEVELS]
        rows.extend((buttons[:2], buttons[2:]))
    text = (
        "思考强度｜Grok 4.6\n\n"
        f"普通聊天：{LABELS[levels['chat']]}（{levels['chat']}）\n"
        f"A 股分析：{LABELS[levels['research']]}（{levels['research']}）\n\n"
        "快速：更重视速度；均衡：增加分析深度。\n"
        "深入 / 最深入：适合复杂问题，等待通常更长，也可能超时。\n\n"
        "点击即保存，从下一次请求生效；已开始或排队的任务保持原档位。\n"
        "不清空聊天，不重启服务；/new 和服务重启后仍保留选择。"
    )
    return text, InlineKeyboardMarkup(rows)


class ThinkingMenuMixin:
    async def _thinking_menu(self, user_id):
        levels = {scope: await self.memory.get_thinking(user_id, scope) for scope in SCOPES}
        return thinking_menu(user_id, levels)

    async def thinking_command(self, update, context):
        if not await self._private_feature_authorized(update):
            return
        try:
            text, markup = await self._thinking_menu(update.effective_user.id)
        except Exception:
            LOGGER.warning("thinking_preferences_read_failed")
            await self._reply(update, "思考强度设置暂时无法读取，请稍后发送 /thinking 重试。")
            return
        await self._deliver_report(self.application.bot, update.effective_chat.id,
                                   text, reply_markup=markup)

    async def _thinking_ack(self, query, text="", *, alert=False):
        try:
            async with asyncio.timeout(2):
                await query.answer(text, show_alert=alert)
        except Exception:
            LOGGER.warning("thinking_callback_ack_failed")

    async def thinking_callback(self, update, context):
        query, user, chat = update.callback_query, update.effective_user, update.effective_chat
        if query is None:
            return
        if (user is None or chat is None or chat.type != "private"
                or user.id not in self.settings.allowed_user_ids):
            await self._thinking_ack(query, "仅限已授权用户在机器人私聊中操作。", alert=True)
            return
        match = THINKING_CALLBACK.fullmatch(query.data) if type(query.data) is str else None
        if match is None or int(match[1]) != user.id:
            await self._thinking_ack(query, "菜单不属于你或已失效，请发送 /thinking。", alert=True)
            return
        await self._thinking_ack(query)
        scope, level = match[2], match[3]
        try:
            await self._touch(update)
            await self.memory.set_thinking(user.id, scope, level)
        except Exception:
            LOGGER.warning("thinking_preferences_save_failed")
            await self._reply(update, "设置未能确认保存，请发送 /thinking 查看当前档位后重试。")
            return
        LOGGER.info("thinking_preference_saved scope=%s level=%s", scope, level)
        try:
            text, markup = await self._thinking_menu(user.id)
            async with asyncio.timeout(5):
                await query.edit_message_text(text=text, reply_markup=markup)
        except Exception:
            # Saving and Telegram UI refresh are independent; do not report a
            # successful commit as a failed change if Telegram editing fails.
            LOGGER.warning("thinking_menu_refresh_failed")
            await self._reply(update, f"已保存：{SCOPES[scope]} → {LABELS[level]}（{level}）。"
                              "下一次请求生效；发送 /thinking 可查看设置。")
