from __future__ import annotations

import os
from pathlib import Path
import shutil

from mini_hermes.us_market import us_market_feed_status


def _meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, raw = line.split(":", 1)
        result[key] = int(raw.strip().split()[0]) * 1024
    return result


def _human_bytes(value: int) -> str:
    return f"{value / (1024 ** 3):.2f} GiB"


def system_status(data_dir: Path) -> dict[str, str]:
    memory = _meminfo()
    total = memory["MemTotal"]
    available = memory["MemAvailable"]
    disk = shutil.disk_usage(data_dir)
    load_1, load_5, load_15 = os.getloadavg()
    cpu_count = os.cpu_count() or 1
    feed = us_market_feed_status()
    if feed["ok"]:
        us_feed = "ok"
    else:
        us_feed = (
            f"degraded failures={feed['consecutive_failures']}"
            f" last_error={feed['last_error']}"
        )
    return {
        "cpu": f"load {load_1:.2f}/{load_5:.2f}/{load_15:.2f} ({cpu_count} vCPU)",
        "ram": f"{_human_bytes(total - available)} / {_human_bytes(total)}",
        "disk": f"{_human_bytes(disk.used)} / {_human_bytes(disk.total)}",
        "us_feed": us_feed,
    }
