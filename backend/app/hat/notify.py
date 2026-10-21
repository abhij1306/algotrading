"""The `Notifier` protocol: how HAT code raises a health alert (HAT-DATA-065, HAT-OPS-071).

An alert carries a stable code and a few counts, never raw server or database text
(HAT-DATA-035). Telegram implements the protocol in PR 16; until then `LogNotifier` writes the
alert to the log, so the nightly path runs without a bot.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from datetime import date
from typing import Protocol

logger = logging.getLogger(__name__)

AlertDetail = Mapping[str, str | int]


class Notifier(Protocol):
    def alert(self, code: str, trade_date: date, detail: AlertDetail) -> None:
        """Raise one health alert named by `code` for `trade_date`."""
        ...


class LogNotifier:
    """Writes each alert to the log (the sink until Telegram implements `Notifier`)."""

    def alert(self, code: str, trade_date: date, detail: AlertDetail) -> None:
        logger.warning(
            "health alert", extra={"code": code, "trade_date": trade_date.isoformat(), **detail}
        )


class RecordingNotifier:
    """Keeps every alert in order; for runs that need to read back what was raised."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.alerts: list[tuple[str, date, dict[str, str | int]]] = []

    def alert(self, code: str, trade_date: date, detail: AlertDetail) -> None:
        with self._lock:
            self.alerts.append((code, trade_date, dict(detail)))
