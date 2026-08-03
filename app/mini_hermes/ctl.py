from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from telegram import Bot

from mini_hermes.config import Settings
from mini_hermes.memory import MemoryStore


STOCK_CODE = re.compile(r"^[0-9]{6}$")


def _bridge_user_id(settings: Settings) -> int:
    configured = os.getenv("HERMES_BRIDGE_USER_ID", "").strip()
    if configured:
        return int(configured)
    if len(settings.allowed_user_ids) == 1:
        return next(iter(settings.allowed_user_ids))
    raise RuntimeError(
        "Set HERMES_BRIDGE_USER_ID when TELEGRAM_ALLOWED_USER_IDS does not contain exactly one user"
    )


def _service_state() -> str:
    result = subprocess.run(
        ["systemctl", "is-active", "mini-hermes"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


async def _status(settings: Settings, store: MemoryStore, user_id: int) -> None:
    _emit(
        {
            "ok": True,
            "service": _service_state(),
            "model": settings.model,
            "mcp_configured": settings.mcp_config.exists(),
            "user_id": user_id,
            "style": await store.get_style(user_id),
            "watchlist": await store.list_watch(user_id),
            "stats": await store.stats(),
        }
    )


async def _watch(
    store: MemoryStore, user_id: int, action: str, symbol: str | None
) -> None:
    if not await store.has_user(user_id):
        raise RuntimeError("Bridge user does not exist in Mini Hermes memory")
    if action == "list":
        _emit({"ok": True, "action": action, "watchlist": await store.list_watch(user_id)})
        return
    if symbol is None or not STOCK_CODE.fullmatch(symbol):
        raise ValueError("symbol must be a 6-digit A-share code")
    changed = (
        await store.add_watch(user_id, symbol)
        if action == "add"
        else await store.remove_watch(user_id, symbol)
    )
    _emit(
        {
            "ok": True,
            "action": action,
            "symbol": symbol,
            "changed": changed,
            "watchlist": await store.list_watch(user_id),
        }
    )


async def _notify(settings: Settings, store: MemoryStore, user_id: int, text: str) -> None:
    if not text.strip():
        raise ValueError("notification text must not be empty")
    chat_id = await store.get_last_chat_id(user_id)
    if chat_id is None:
        raise RuntimeError("No Telegram chat is recorded for the bridge user")
    message_ids: list[int] = []
    chunks = [text[index : index + 4000] for index in range(0, len(text), 4000)]
    async with Bot(settings.telegram_token) as bot:
        for chunk in chunks:
            message = await bot.send_message(chat_id=chat_id, text=chunk)
            message_ids.append(message.message_id)
    _emit({"ok": True, "chat_id": chat_id, "message_ids": message_ids})


async def _run(args: argparse.Namespace) -> int:
    settings = Settings.from_env(args.env_file)
    user_id = _bridge_user_id(settings)
    store = MemoryStore(settings.database_path)
    await store.initialize()
    try:
        if args.command == "status":
            await _status(settings, store, user_id)
        elif args.command == "watch":
            await _watch(store, user_id, args.action, args.symbol)
        elif args.command == "notify":
            await _notify(settings, store, user_id, args.text)
        else:
            raise RuntimeError(f"Unsupported command: {args.command}")
    finally:
        await store.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mini Hermes local control CLI")
    parser.add_argument(
        "--env-file", type=Path, default=Path("/opt/mini-hermes/.env")
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    watch = subparsers.add_parser("watch")
    watch.add_argument("action", choices=("list", "add", "remove"))
    watch.add_argument("symbol", nargs="?")
    notify = subparsers.add_parser("notify")
    notify.add_argument("text")
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 - CLI returns machine-readable errors
        _emit({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
