from __future__ import annotations

import logging
import time

from telegram.error import (
    BadRequest,
    ChatMigrated,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TimedOut,
)

try:
    from httpx import ConnectError as HttpxConnectError
    from httpx import ReadError as HttpxReadError
except ImportError:  # pragma: no cover - httpx always present with PTB
    HttpxConnectError = ()  # type: ignore[misc, assignment]
    HttpxReadError = ()  # type: ignore[misc, assignment]

# Full stacktrace at most once per cooldown window per error class name.
_DEFAULT_STACKTRACE_COOLDOWN_SECONDS = 300.0
_last_stacktrace_at: dict[str, float] = {}

# In PTB, BadRequest/Forbidden/etc. subclass NetworkError — exclude those.
_PERSISTENT_TELEGRAM = (
    BadRequest,
    ChatMigrated,
    Conflict,
    Forbidden,
    InvalidToken,
)
_TRANSIENT_EXACT = (TimedOut, RetryAfter)
if isinstance(HttpxConnectError, type):
    _TRANSIENT_HTTPX: tuple[type[BaseException], ...] = (
        HttpxConnectError,
        HttpxReadError,
    )
else:
    _TRANSIENT_HTTPX = ()


def reset_error_log_cooldowns() -> None:
    """Clear rate-limit state (for tests)."""
    _last_stacktrace_at.clear()


def is_transient_network_error(error: BaseException | None) -> bool:
    """Return True for flaky Telegram / HTTP transport failures.

    Note: python-telegram-bot places BadRequest/Forbidden under NetworkError;
    those are API/application errors and must not be treated as transient.
    """
    if error is None:
        return False
    if isinstance(error, _PERSISTENT_TELEGRAM):
        return False
    if isinstance(error, _TRANSIENT_EXACT):
        return True
    # Bare NetworkError (and unknown transport subclasses not in persistent set).
    if isinstance(error, NetworkError):
        return True
    if _TRANSIENT_HTTPX and isinstance(error, _TRANSIENT_HTTPX):
        return True
    cause = error.__cause__ or error.__context__
    if cause is not None and cause is not error:
        return is_transient_network_error(cause)
    return False


def should_emit_stacktrace(
    error: BaseException,
    *,
    now: float | None = None,
    cooldown_seconds: float = _DEFAULT_STACKTRACE_COOLDOWN_SECONDS,
    state: dict[str, float] | None = None,
) -> bool:
    """True at most once per cooldown for the same error class name."""
    bucket = state if state is not None else _last_stacktrace_at
    key = type(error).__name__
    current = time.monotonic() if now is None else now
    last = bucket.get(key)
    if last is not None and (current - last) < cooldown_seconds:
        return False
    bucket[key] = current
    return True


def log_handler_error(
    logger: logging.Logger,
    error: BaseException | None,
    *,
    cooldown_seconds: float = _DEFAULT_STACKTRACE_COOLDOWN_SECONDS,
    state: dict[str, float] | None = None,
) -> str:
    """Log a Telegram error_handler failure; return the logging mode used."""
    if error is None:
        logger.exception("telegram_update_failed")
        return "exception"
    if is_transient_network_error(error):
        if should_emit_stacktrace(
            error, cooldown_seconds=cooldown_seconds, state=state
        ):
            logger.exception(
                "telegram_update_failed transient=%s",
                type(error).__name__,
                exc_info=error,
            )
            return "exception_rate_limited"
        logger.warning(
            "telegram_update_failed transient=%s error=%s",
            type(error).__name__,
            error,
        )
        return "warning"
    logger.exception(
        "telegram_update_failed error_type=%s",
        type(error).__name__,
        exc_info=error,
    )
    return "exception"
