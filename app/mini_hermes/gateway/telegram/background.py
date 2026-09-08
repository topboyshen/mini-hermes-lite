"""Non-queuing admission for PTB-managed, non-blocking long handlers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any


MAX_BACKGROUND_TASKS = 2
BACKGROUND_BUSY_TEXT = (
    "当前已有 2 个任务在处理，这次请求没有排队。"
    "请稍后再试；/pistatus 仍可查看状态。"
)
_ACTIVE_ATTRIBUTE = "_ux_background_active"


def bounded_background(
    bot: Any, callback: Callable[[Any, Any], Awaitable[Any]]
) -> Callable[[Any, Any], Awaitable[Any]]:
    """Share two slots per bot; PTB owns task creation and cancellation.

    Register the returned handler with ``block=False``. Short controls must
    remain unwrapped so they do not wait for these long-running handlers.
    The synchronous counter operations intentionally contain no await: all
    wrappers execute on the bot's single asyncio event loop.
    """
    @wraps(callback)
    async def handler(update: Any, context: Any) -> Any:
        # Check only fields before taking a slot. _authorized may await I/O;
        # denied callers must never delay or consume authorized work slots.
        user_id = getattr(getattr(update, "effective_user", None), "id", None)
        allowed = getattr(getattr(bot, "settings", None), "allowed_user_ids", ())
        if user_id is None or user_id not in allowed:
            await bot._authorized(update)
            return None

        active = getattr(bot, _ACTIVE_ATTRIBUTE, 0)
        if active >= MAX_BACKGROUND_TASKS:
            await bot._reply(update, BACKGROUND_BUSY_TEXT)
            return None

        # Lazily initialized for all wrappers of this bot, including instances
        # constructed with object.__new__ in offline tests. No task/queue here.
        setattr(bot, _ACTIVE_ATTRIBUTE, active + 1)
        try:
            return await callback(update, context)
        finally:
            # Covers success, errors and asyncio cancellation without awaiting.
            setattr(bot, _ACTIVE_ATTRIBUTE, getattr(bot, _ACTIVE_ATTRIBUTE) - 1)

    return handler
