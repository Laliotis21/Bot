"""Structured logging utilities.

Never log API keys, secrets, or private credentials.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "secret_key",
        "binance_api_key",
        "binance_secret_key",
        "password",
        "token",
        "authorization",
    }
)


class RedactingFilter(logging.Filter):
    """Redact sensitive key/value pairs from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, dict):
            record.args = {
                k: ("***REDACTED***" if str(k).lower() in SENSITIVE_KEYS else v)
                for k, v in record.args.items()
            }
        msg = str(record.getMessage())
        for key in SENSITIVE_KEYS:
            # Avoid leaking secrets if accidentally interpolated
            if key in msg.lower() and "=" in msg:
                record.msg = "[redacted message containing sensitive key]"
                record.args = ()
                break
        return True


def setup_logging(
    level: str = "INFO",
    logs_dir: str | Path = "logs",
    name: str = "trading",
) -> logging.Logger:
    """Configure structured console + file logging."""
    logger = logging.getLogger(name)
    if logger.handlers:
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    fmt.converter = __import__("time").gmtime  # type: ignore[attr-defined]

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(RedactingFilter())
    logger.addHandler(console)

    path = Path(logs_dir)
    path.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(path / "trading.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(RedactingFilter())
    logger.addHandler(file_handler)

    return logger


def get_logger(name: str = "trading") -> logging.Logger:
    """Return a named logger (call setup_logging first for handlers)."""
    return logging.getLogger(name)


def safe_extra(**kwargs: Any) -> dict[str, Any]:
    """Build a log-extra dict with secrets stripped."""
    return {
        k: ("***REDACTED***" if k.lower() in SENSITIVE_KEYS else v) for k, v in kwargs.items()
    }
