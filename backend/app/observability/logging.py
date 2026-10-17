from __future__ import annotations

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler

from app.security.secrets import redact
from app.timeutils import BEIJING


class SafeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, BEIJING).isoformat(timespec="milliseconds")

    def format(self, record):
        saved = record.exc_info
        record.exc_info = None
        try:
            return redact(super().format(record))
        finally:
            record.exc_info = saved


def configure_logging(config, process):
    logger = logging.getLogger("caw")
    if any(getattr(handler, "caw_process", None) == process for handler in logger.handlers):
        return
    handler = RotatingFileHandler(
        config.data_dir / "logs" / f"{process}.log", maxBytes=10_000_000, backupCount=14, encoding="utf-8"
    )
    handler.caw_process = process
    handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
