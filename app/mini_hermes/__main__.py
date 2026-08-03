from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from mini_hermes.config import Settings
from mini_hermes.gateway import MiniHermesBot
from mini_hermes.logging_setup import setup_logging
from mini_hermes.selftest import run_selftest


def main() -> int:
    parser = argparse.ArgumentParser(description="Mini Hermes Lite")
    parser.add_argument("--env-file", type=Path, default=Path("/opt/mini-hermes/.env"))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    settings = Settings.from_env(args.env_file)
    setup_logging(settings.log_dir)
    if args.self_test:
        return asyncio.run(run_selftest(settings))

    if not settings.telegram_token:
        parser.error("missing required setting: TELEGRAM_TOKEN")
    MiniHermesBot(settings).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
