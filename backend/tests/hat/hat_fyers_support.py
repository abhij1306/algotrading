"""Shared setup for the Fyers session, login and token-store properties (K2).

Everything runs on a temporary token file, a controlled clock and recording stand-ins for the
Fyers SDK (`hat.fakes.fyers_sdk`). Nothing reaches Fyers, and no real token file is read.
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.hat.execution.fyers_tokens import TokenStore

T0 = datetime(2026, 10, 21, 4, 30, tzinfo=UTC)  # 10:00 IST on a Wednesday


class Clock:
    """A settable UTC clock for `FyersSession(now=...)`."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class Ticker:
    """A settable monotonic clock for `FyersAuthService(clock=...)`."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class LogCapture(logging.Handler):
    """Every log record of the process, formatted with its traceback."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []
        self._formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self._formatter.format(record))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@contextmanager
def captured_logs() -> Iterator[LogCapture]:
    handler = LogCapture()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


@contextmanager
def token_store() -> Iterator[TokenStore]:
    """A `TokenStore` on a fresh temporary directory (removed afterwards)."""
    with tempfile.TemporaryDirectory(prefix="hat-fyers-") as directory:
        yield TokenStore(Path(directory) / "config" / "access_token.json")


def write_token(
    store: TokenStore,
    *,
    access_token: str = "tok-A",  # noqa: S107
    client_id: str | None = "CLIENT-100",
    expires_at: datetime | None = None,
) -> None:
    """A token file as the login flow writes it, with the given expiry (default: tomorrow)."""
    document: dict[str, Any] = {"access_token": access_token, "fy_id": "XA1234"}
    if client_id is not None:
        document["client_id"] = client_id
    if expires_at is not None:
        document["expires_at"] = expires_at.isoformat()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(document), encoding="utf-8")
