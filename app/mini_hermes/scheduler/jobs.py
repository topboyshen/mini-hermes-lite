from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo


def parse_daily_time(value: str, timezone: str) -> time:
    hour_text, minute_text = value.split(":", 1)
    hour, minute = int(hour_text), int(minute_text)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("DAILY_REPORT_TIME must be HH:MM")
    return time(hour=hour, minute=minute, tzinfo=ZoneInfo(timezone))

