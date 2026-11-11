from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram.ext import ContextTypes

LOGGER = logging.getLogger(__name__)

DEFAULT_BRIEFING_DIR = Path("/opt/mini-hermes/data/briefing")
OVERSEAS_SKIP = "今日无新美股收盘"


def write_briefing_artifact(directory: Path, name: str, text: str) -> Path:
    """Atomically write a generate-only briefing file for Signal Gate to stage."""
    if Path(name).name != name or "/" in name:
        raise ValueError("invalid briefing artifact name")
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        path = directory / name
        os.replace(temporary, path)
        os.chmod(path, 0o640)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


class JobsMixin:
    """Scheduled daily / weekly report generation (delivery is owned by Signal Gate)."""

    def _briefing_dir(self) -> Path:
        override = getattr(self, "briefing_output_dir", None)
        if override:
            return Path(override)
        raw = os.environ.get("BRIEFING_OUTPUT_DIR", "").strip()
        return Path(raw) if raw else DEFAULT_BRIEFING_DIR

    def _write_briefing(self, kind: str, day: str, text: str, suffix: str = ".md") -> Path | None:
        name = f"{kind}-{day}{suffix}"
        try:
            path = write_briefing_artifact(self._briefing_dir(), name, text)
        except Exception:  # noqa: BLE001 - generation must not crash the scheduler
            LOGGER.exception("briefing_file_write_failed kind=%s date=%s", kind, day)
            return None
        LOGGER.info(
            "briefing_file_written kind=%s date=%s chars=%s delivery=deferred",
            kind,
            day,
            len(text),
        )
        return path

    async def daily_report_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        day = now.date().isoformat()
        # At 08:20 Asia/Shanghai, US Monday-Friday closes arrive Tuesday-Saturday.
        if now.weekday() in {0, 6}:
            LOGGER.info("daily_report_skipped reason=no_recent_us_close date=%s", day)
            self._write_briefing("daily", day, f"status=skipped\nreason=no_recent_us_close\ndate={day}\n", suffix=".skip")
            self._write_briefing("daily", day, OVERSEAS_SKIP)
            return

        LOGGER.info("report_generation_started trigger=scheduled date=%s delivery=deferred", day)
        try:
            bundle = await self._generate_global_report_bundle()
        except Exception:  # noqa: BLE001 - one failed run must not stop the scheduler
            LOGGER.exception("report_generation_failed trigger=scheduled date=%s", day)
            return
        try:
            await self.memory.save_market_report(
                bundle.report_date, bundle.full, vix_evidence=bundle.vix_evidence,
                trigger="scheduled",
            )
        except Exception:  # noqa: BLE001 - history failure must not block file output
            LOGGER.exception("report_history_save_failed date=%s", day)
        self._write_briefing("daily", day, bundle.brief)

    async def weekly_report_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        if now.weekday() != 5:
            return
        day = now.date().isoformat()
        LOGGER.info("weekly_report_generation_started date=%s delivery=deferred", day)
        try:
            report = await self._generate_weekly_report()
        except Exception:  # noqa: BLE001 - scheduler must stay alive
            LOGGER.exception("weekly_report_generation_failed date=%s", day)
            return
        self._write_briefing("weekly", day, report)
