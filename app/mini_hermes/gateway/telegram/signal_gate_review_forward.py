"""Forward signal-gate review inline-button callbacks to local review_bot HTTP.

Registered from MiniHermesBot.setup_handlers. Keeps getUpdates ownership in mini-hermes
while signal-gate-review.service owns approve/reject + Quant POST logic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request

from telegram import Update
from telegram.ext import ContextTypes

LOGGER = logging.getLogger(__name__)

FORWARD_URL = os.getenv(
    "SIGNAL_GATE_REVIEW_FORWARD_URL",
    "http://127.0.0.1:8096/hooks/v1/telegram-callback",
).strip()
FORWARD_SECRET = os.getenv("SIGNAL_GATE_REVIEW_FORWARD_SECRET", "").strip()


def _post_sync(callback_query_payload: dict) -> tuple[int, str]:
    raw = json.dumps({"callback_query": callback_query_payload}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if FORWARD_SECRET:
        headers["X-Review-Forward-Secret"] = FORWARD_SECRET
    req = urllib.request.Request(FORWARD_URL, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


async def signal_gate_review_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return
    # Build a plain dict that review_bot.handle_callback understands.
    msg = query.message
    payload = {
        "id": query.id,
        "data": query.data,
        "from": {
            "id": query.from_user.id if query.from_user else None,
            "username": query.from_user.username if query.from_user else None,
            "first_name": query.from_user.first_name if query.from_user else None,
        },
        "message": {
            "message_id": msg.message_id if msg else None,
            "chat": {"id": msg.chat_id if msg else None},
            "text": msg.text if msg else None,
        },
    }
    try:
        status, body = await asyncio.to_thread(_post_sync, payload)
        LOGGER.info(
            "signal_gate_review_forward status=%s data=%s body=%s",
            status,
            query.data,
            body[:200],
        )
        if status >= 400:
            try:
                await query.answer(f"review forward HTTP {status}", show_alert=False)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        LOGGER.exception("signal_gate_review_forward failed data=%s", query.data)
        try:
            await query.answer("review service unavailable", show_alert=True)
        except Exception:  # noqa: BLE001
            pass
