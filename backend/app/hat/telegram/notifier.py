"""The Telegram `Notifier`: HAT's health alerts reach the allowlisted chat (08 §2; HAT-TG-024,
HAT-DEC-T15, HAT-DATA-065, HAT-OPS-071).

It replaces `LogNotifier` for the alerts the router, the trade manager, the stop desk, fill sync,
reconciliation and the nightly ingest raise: each becomes one message with the code, the trade
date and the detail's fixed codes and ids (`render.alert_text` prints anything else as `?`, so no
broker or server text reaches the chat, HAT-TG-014). `gtt.unverified` and `gtt.unfilled` (PR 15c)
and `fill.excess` (PR 28) are sent as URGENT (`render.URGENT_ALERTS`); so is the event
`corporate_action.unhandled` (PR 31), which the relay sends.

Two alerts also exist as events, `order.rejected` and the quality gate's `quality.failed`; the
event relay sends those (`relay.HEALTH_EVENTS`), so the notifier only logs them and each failure
is one message. An alert never raises into its caller: a failed send is logged by its code.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from ..notify import AlertDetail
from ..time import utc_now
from . import render, store
from .client import BotClient, TelegramError

__all__ = ["RELAYED_CODES", "TelegramNotifier"]

logger = logging.getLogger(__name__)

# Alert codes whose event the relay already turns into the one health alert.
RELAYED_CODES = frozenset({"order.rejected", "quality_failed"})


class TelegramNotifier:
    def __init__(
        self,
        client: BotClient,
        session_factory: sessionmaker[Session] | Callable[[], Session],
    ) -> None:
        self._client = client
        self._factory = session_factory

    def alert(self, code: str, trade_date: date, detail: AlertDetail) -> None:
        if code in RELAYED_CODES:
            logger.info("health alert %s left to the event relay", code)
            return
        text = render.alert_text(code, trade_date, detail)
        try:
            message_id = self._client.send(text)
        except TelegramError as exc:
            logger.warning("telegram alert %s not sent: %s", code, exc.code)
            return
        try:
            with self._factory() as session:
                store.record_message(
                    session,
                    chat_id=self._client.chat_id,
                    kind=f"alert:{code}"[:64],
                    ref_id=None,
                    message_id=message_id,
                    sent_at=utc_now(),
                )
                session.commit()
        except Exception as exc:  # the alert went out; a lost link row must not raise
            logger.warning("telegram alert %s sent, link not stored: %s", code, type(exc).__name__)
