"""Structured logging: console + rotating file, configured from YAML."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from config.settings import LoggingSettings

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging(settings: LoggingSettings) -> None:
    root = logging.getLogger()
    root.setLevel(settings.level.upper())

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if settings.file:
        log_path = Path(settings.file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=settings.max_bytes,
            backupCount=settings.backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Third-party noise reduction; our own logs stay at the configured level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
