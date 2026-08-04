"""Generate one report locally without sending it to Telegram."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mini_hermes.config import Settings
from mini_hermes.reporting import A_SHARE_TERMS, compose_global_report
from mini_hermes.us_market import fetch_us_quotes


async def generate(env_file: Path) -> str:
    settings = Settings.from_env(env_file)
    quotes = await fetch_us_quotes()
    report_date = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    report = compose_global_report(report_date, quotes)
    if any(term.casefold() in report.casefold() for term in A_SHARE_TERMS):
        raise RuntimeError("Prohibited mainland-equity content survived sanitization")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, required=True)
    args = parser.parse_args()
    print(asyncio.run(generate(args.env)))


if __name__ == "__main__":
    main()
