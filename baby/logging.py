"""Structured logging for BABY."""

import logging
import sys
from typing import Any, Optional

import structlog

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def resolve_log_level(level: str) -> int:
    """Map a level name to its ``logging`` constant; unknown names fall back to INFO."""
    name = (level or "").strip().upper()
    return getattr(logging, name) if name in VALID_LOG_LEVELS else logging.INFO


class _StderrLogger:
    """Writes each rendered event to the *current* ``sys.stderr`` (which test runners and
    supervisors may replace after logging is configured)."""

    def msg(self, message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    debug = info = warning = warn = error = critical = exception = fatal = log = msg


def _stderr_logger_factory(*args: Any) -> _StderrLogger:
    return _StderrLogger()


def configure_logging(level: str = "INFO") -> None:
    """Configure structured logging for the current process.

    Events below ``level`` are dropped; the rest are written to stderr as one line each
    (timestamp, level, message, key=value context). Safe to call more than once.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    numeric_level = resolve_log_level(level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=_stderr_logger_factory,
        cache_logger_on_first_use=False,
    )
    logging.getLogger().setLevel(numeric_level)


class Logger:
    """Wrapper around structlog logger."""

    def __init__(self, name: str) -> None:
        self._logger = structlog.get_logger(name)

    def info(self, message: str, **kwargs: Any) -> None:
        """Log info message."""
        self._logger.info(message, **kwargs)

    def debug(self, message: str, **kwargs: Any) -> None:
        """Log debug message."""
        self._logger.debug(message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        """Log warning message."""
        self._logger.warning(message, **kwargs)

    def error(self, message: str, exc: Optional[Exception] = None, **kwargs: Any) -> None:
        """Log error message."""
        if exc:
            kwargs["exception"] = str(exc)
        self._logger.error(message, **kwargs)

    def critical(self, message: str, **kwargs: Any) -> None:
        """Log critical message."""
        self._logger.critical(message, **kwargs)


def get_logger(name: str) -> Logger:
    """Get a logger instance.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance
    """
    return Logger(name)
