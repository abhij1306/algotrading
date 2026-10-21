"""Structured HAT logs: one JSON object per record (HAT-OPS-070)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

# Context keys carried on a record (pass them in `extra=`) and copied into the JSON object.
CONTEXT_KEYS = ("job", "trade_date", "bot_id", "signal_id")
HAT_LOGGER_NAME = "app.hat"


class JsonFormatter(logging.Formatter):
    """`ts`, `level`, `msg` and any context key present, as one JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        for key in CONTEXT_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = str(value)
        if record.exc_info:
            entry["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(entry, ensure_ascii=False, default=str)


class _JsonHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marks the handler `configure_hat_logging` installed, so it is installed once."""


def configure_hat_logging() -> None:
    """Send `app.hat` records to a JSON handler once; stop them reaching the plain handlers."""
    logger = logging.getLogger(HAT_LOGGER_NAME)
    if any(isinstance(h, _JsonHandler) for h in logger.handlers):
        return
    handler = _JsonHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False
