"""Logging setup: one root configuration shared by every module's `logging.getLogger(__name__)`."""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import MemoryHandler, RotatingFileHandler
from typing import Optional

LOG_FORMAT = '%(asctime)s - %(levelname)-8s - %(name)-25s - %(funcName)-25s - %(message)s'
LEVELS = ('DEBUG', 'INFO', 'WARNING', 'WARN', 'ERROR')
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5

# Third-party loggers that are far too chatty at INFO/DEBUG
QUIET_LOGGERS = {
    'schedule': logging.ERROR,
    'requests': logging.WARNING,
    'urllib3': logging.WARNING,
    'sqlitedict': logging.WARNING,
    'git': logging.WARNING,
    'apprise': logging.WARNING,
}

# (pattern, replacement) pairs applied to every formatted line, tracebacks included
_REDACTIONS = (
    (re.compile(r'(\w+://)[^/@\s:]+:[^/@\s]+@'), r'\1***:***@'),                    # user:pass@ in URLs
    (re.compile(r'((?:X-Plex-Token|apikey|api_key|token)["\']?\s*[=:]\s*["\']?)[\w-]{8,}', re.I), r'\1***'),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


_console_handler: Optional[logging.Handler] = None
_file_handler: Optional[logging.Handler] = None
# Holds records logged before the log file path is known (it comes from CLI args/env)
_startup_buffer: Optional[MemoryHandler] = None
STARTUP_BUFFER_RECORDS = 1000


def setup_console(level: str = 'INFO') -> None:
    """Attach the stdout handler; call before anything logs so startup messages are kept."""
    global _console_handler, _startup_buffer
    root = logging.getLogger()
    if _console_handler is None:
        _console_handler = logging.StreamHandler(sys.stdout)
        _console_handler.setFormatter(RedactingFormatter(LOG_FORMAT))
        root.addHandler(_console_handler)
        # never auto-flushes (no target, flushLevel above CRITICAL); setup_file() replays it
        _startup_buffer = MemoryHandler(STARTUP_BUFFER_RECORDS, flushLevel=logging.CRITICAL + 1)
        root.addHandler(_startup_buffer)
        for name, quiet_level in QUIET_LOGGERS.items():
            logging.getLogger(name).setLevel(quiet_level)
        logging.captureWarnings(True)
    root.setLevel(level)


def setup_file(logfile: str) -> None:
    """Attach the rotating log file handler and replay anything logged during startup (idempotent)."""
    global _file_handler, _startup_buffer
    if _file_handler is not None:
        return
    _file_handler = RotatingFileHandler(logfile, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding='utf-8')
    _file_handler.setFormatter(RedactingFormatter(LOG_FORMAT))
    root = logging.getLogger()
    root.addHandler(_file_handler)

    if _startup_buffer is not None:
        root.removeHandler(_startup_buffer)
        _startup_buffer.setTarget(_file_handler)
        _startup_buffer.close()  # flushes buffered records into the file
        _startup_buffer = None


def set_level(level: str) -> None:
    logging.getLogger().setLevel('WARNING' if level == 'WARN' else level)
