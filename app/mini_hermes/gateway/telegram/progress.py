"""Best-effort, single-message progress without exposing model/tool internals."""
from __future__ import annotations

import asyncio
import logging
import time

LOGGER = logging.getLogger(__name__)
UPDATE_INTERVAL = 8.0
EDIT_TIMEOUT = 4.0


def progress_stage(event: str) -> str:
    # The socket is private, but provider/tool text still must not reach the UI.
    stages = {
        "pi 会话已启动": "正在处理你的请求",
        "正在等待前一个任务": "正在等待前一个任务完成",
        "正在生成回复": "正在生成回复",
        "连接波动，正在自动重试": "连接波动，正在自动重试",
    }
    if event in stages:
        return stages[event]
    tool_stages = {
        "read": "正在读取资料",
        "grep": "正在查找资料",
        "find": "正在查找资料",
        "ls": "正在查看工作区",
        "write": "正在编写文件",
        "edit": "正在修改文件",
        "bash": "正在运行工作区任务",
    }
    prefix = "正在调用工具："
    if event.startswith(prefix):
        return tool_stages.get(event[len(prefix):], "正在使用工具处理")
    return "正在处理你的请求"


class TaskProgress:
    """Progress cannot delay the model request, spam chat, or outlive the answer."""

    def __init__(self, message: object, *, started: float | None = None) -> None:
        self.message = message
        self.started = time.monotonic() if started is None else started
        self.stage = "正在连接助手"
        self._task: asyncio.Task | None = None

    async def update(self, event: str) -> None:
        self.stage = progress_stage(event)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def render(self) -> str:
        elapsed = max(0, int(time.monotonic() - self.started))
        duration = f"{elapsed} 秒" if elapsed < 60 else f"{elapsed // 60} 分 {elapsed % 60} 秒"
        hint = (
            "当前正在排队；/abort 只中止正在运行的 pi 任务"
            if self.stage == "正在等待前一个任务完成" else
            "/abort 可中止当前 pi 任务"
        )
        return f"⏳ {self.stage}\n已用时 {duration} · {hint}"

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(UPDATE_INTERVAL)
                try:
                    async with asyncio.timeout(EDIT_TIMEOUT):
                        await self.message.edit_text(self.render())
                except Exception:  # No raw Telegram URLs or request content in logs.
                    LOGGER.debug("task_progress_unavailable")
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
