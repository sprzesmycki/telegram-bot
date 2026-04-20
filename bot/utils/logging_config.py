"""Centralised logging configuration.

Reads ``LOG_LEVEL`` (default INFO), ``LOG_FILE`` (default ./data/logs/bot.log),
and ``DEBUG`` (any truthy value enables DEBUG level and verbose 3rd-party logs).

Usage (call once, as early as possible):

    from bot.utils.logging_config import setup_logging
    setup_logging()
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Console format — concise, one line per event
_CONSOLE_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
# File format — adds module+line for debugging
_FILE_FMT = "%(asctime)s [%(levelname)s] %(name)s %(module)s:%(lineno)d: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Chatty third-party libs — keep them quiet unless DEBUG is on
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "telegram.ext._updater",
    "telegram.ext._application",
    "apscheduler.scheduler",
    "apscheduler.executors.default",
)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on", "debug")


def setup_logging() -> logging.Logger:
    """Configure the root logger and return it."""
    from bot.config import get_config
    cfg = get_config().logging

    debug_mode = cfg.debug
    level_name = cfg.level
    level = getattr(logging, level_name, logging.INFO)

    log_file = Path(cfg.file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # Clear any handlers that basicConfig or libraries may have attached
    root.handlers.clear()
    root.setLevel(logging.DEBUG)  # root takes everything, handlers filter

    # --- Console handler ---
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, _DATE_FMT))
    root.addHandler(console)

    # --- Rotating file handler ---
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,  # 5 MB per file
        backupCount=10,  # keep 10 rotated files = 50 MB total
        encoding="utf-8",
    )
    # File always captures DEBUG so you can diff a crash after the fact
    file_handler.setLevel(logging.DEBUG if debug_mode else level)
    file_handler.setFormatter(logging.Formatter(_FILE_FMT, _DATE_FMT))
    root.addHandler(file_handler)

    # Quiet noisy libraries unless DEBUG mode is on
    noisy_level = logging.DEBUG if debug_mode else logging.WARNING
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)

    root.info(
        "Logging configured: level=%s file=%s debug=%s",
        level_name, log_file, debug_mode,
    )
    return root
