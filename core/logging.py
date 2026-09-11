"""Structured, rotated logging system with credential sanitization."""

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# Sensitive patterns to sanitize
_SENSITIVE_PATTERNS = [
    # Bot token: 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    # URL query tokens and auth signatures
    re.compile(r"(token|sig|signature|auth|key|secret)=([a-zA-Z0-9_\-\.%]+)", re.IGNORECASE),
    # Cookies
    re.compile(r"(Cookie:\s*)([^\r\n]+)", re.IGNORECASE),
    # Authorization header
    re.compile(r"(Authorization:\s*)([^\r\n]+)", re.IGNORECASE),
]


class SanitizingFormatter(logging.Formatter):
    """Formatter that strips sensitive information from log messages."""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        for pattern in _SENSITIVE_PATTERNS:
            msg = pattern.sub(r"\1=***REDACTED***", msg) if r"\1" in pattern.pattern else pattern.sub("***REDACTED***", msg)
        return msg


def setup_logging(
    log_level: str = "INFO",
    log_dir: Optional[Path] = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5
) -> None:
    """Configure root logger with console and rotated file handlers."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = SanitizingFormatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear existing handlers
    root_logger.handlers.clear()

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File Handler (if log_dir is provided)
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=log_dir / "bot.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8"
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # Suppress verbose third-party loggers
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
