"""Shared setup for the Fyers session, login and token-store properties.

Everything runs on a temporary token file, a controlled clock and recording stand-ins for the
Fyers SDK (`hat.fakes.fyers_sdk`). Nothing reaches Fyers, and no real token file is read.
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.hat.execution.fyers_tokens import TokenStore

# Fyers replies recorded live on 2026-10-22 (PII redacted): the -16 answers to a bad token and an
# ok profile, whose `fy_id` is the user id.
LIVE_REPLIES: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "fyers_live_replies.json").read_text(encoding="utf-8")
)


def profile_ok(user_id: str) -> dict[str, Any]:
    """The recorded ok profile reply for `user_id`."""
    reply = LIVE_REPLIES["profile_ok"]
    return {**reply, "data": {**reply["data"], "fy_id": user_id}}


def token_reply(token: str) -> dict[str, Any]:
    """A `generate_token` reply as Fyers documents it: no user id in it."""
    return {"s": "ok", "code": 200, "message": "", "access_token": token, "refresh_token": "R"}


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
