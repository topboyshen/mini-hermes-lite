from __future__ import annotations

import logging
from logging.handlers import WatchedFileHandler
from pathlib import Path


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    # Rotation is owned by /etc/logrotate.d/mini-hermes. WatchedFileHandler
    # detects the inode change after logrotate renames the file and reopens the
    # newly created path on the next log record.
    file_handler = WatchedFileHandler(
        log_dir / "mini-hermes.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)
