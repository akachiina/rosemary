"""Logging setup for Rosemary with colored console output.

Uses plain ANSI escape codes (no external dependency). Colors are applied to the
console handler only; the file handler stays plain so log files remain
grep-friendly.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

# ANSI escape codes (no colorama dependency).
_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"

_LEVEL_COLORS = {
    logging.DEBUG: "\x1b[38;5;245m",  # grey
    logging.INFO: "\x1b[38;5;114m",  # green
    logging.WARNING: "\x1b[38;5;215m",  # amber
    logging.ERROR: "\x1b[38;5;203m",  # red
    logging.CRITICAL: "\x1b[38;5;196m",
}

_TIMESTAMP_COLOR = "\x1b[38;5;250m"
_MSG_COLOR = "\x1b[38;5;231m"

# Console: compact, colored, readable.
_CONSOLE_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
# File: plain, structured, grep-friendly.
_FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_LOG_FILE = "bot.log"
_LEVEL_WIDTH = 8  # length of "CRITICAL", the longest level name


class _ColorFormatter(logging.Formatter):
    """Colorizes timestamp and level column on TTY consoles.

    The line is rebuilt by hand so ANSI escape codes never shift column
    alignment, regardless of level-name length.
    """

    def __init__(self, fmt: str, *, use_color: bool) -> None:
        super().__init__(fmt, datefmt="%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            message = f"{message}\n{record.exc_text}"

        if not self.use_color:
            return (
                f"{self.formatTime(record, self.datefmt)}"
                f"  {record.levelname:<{_LEVEL_WIDTH}}"
                f"  {message}"
            )

        asctime = self.formatTime(record, self.datefmt)
        level_color = _LEVEL_COLORS.get(record.levelno, _RESET)
        return (
            f"{_TIMESTAMP_COLOR}{asctime}{_RESET}"
            f"  {level_color}{_BOLD}{record.levelname:<{_LEVEL_WIDTH}}{_RESET}"
            f"  {_MSG_COLOR}{message}{_RESET}"
        )


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with colored console and plain file handlers.

    Idempotent: repeated calls do not duplicate handlers on the root logger.
    """
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        return

    # Suppress py-cord's gateway INFO logs: it dumps the READY `_trace` raw,
    # which now contains a verbose Discord instrument onboard object.
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)

    use_color = sys.stdout.isatty()

    console = logging.StreamHandler()
    console.setFormatter(_ColorFormatter(_CONSOLE_FORMAT, use_color=use_color))

    file_handler = RotatingFileHandler(_LOG_FILE, maxBytes=1_000_000, backupCount=3)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))

    root.addHandler(console)
    root.addHandler(file_handler)
