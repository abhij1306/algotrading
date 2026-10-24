"""Cancelling the unfilled rest of a real entry at the broker (F2, PR 49).

- **The entry remainder** (`StopDesk.cancel_entry`, F2): once the position holds no shares, or
  before a sale of all of them (`make_room` to 0), the unfilled rest of a part-filled entry is
  cancelled at the broker, so it cannot fill under a position HAT is closing. The position closes
  only once the broker shows the entry ended; a fill that lands first adds to it and is
  protected. Each entry's cancel is sent once per desk, again only after the broker could not be
  reached.
- **An entry nothing of which filled** (`cancel_unfilled`, PR 49): cancelled at the broker the
  same way when its break failed or it waited too long (`entry_timeout.EntryWatch` decides). One
  that filled in part keeps its remainder working (the partial-entry policy).

The order's status is left to the broker's book (`FillSync`). A cancel is never an entry, so no
gate holds it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatOrder, HatPosition
from ..time import as_utc, trade_date_of
from .broker import CODES_NOT_SENT, HatBroker
from .stop_plans import LIVE

__all__ = ["EntryCancel"]

logger = logging.getLogger(__name__)

ENTRY = "entry"


class EntryCancel:
    """Entry cancels for one broker account's session."""

    def __init__(self, broker: HatBroker) -> None:
        self._broker = broker
        # Entries whose remainder the broker was asked to cancel: not asked again.
        self._asked: set[uuid.UUID] = set()
        # Why HAT asked the broker to cancel an entry, for its `order.cancelled` (`reason_of`).
        self._reasons: dict[uuid.UUID, str] = {}

    def reason_of(self, order_id: uuid.UUID) -> str | None:
        """Why HAT cancelled the entry order, when this desk asked it (a restart forgets it)."""
        return self._reasons.get(order_id)

    def cancel_unfilled(
        self, session: Session, signal_id: uuid.UUID, now: datetime, reason: str
    ) -> bool:
        """Cancel the signal's real entry while nothing of it has filled (PR 49): its break
        failed or it waited too long. An entry with a fill (a real position, or a part fill the
        broker reported) is left working: the partial-entry policy keeps the remainder until the
        close. The signal ends Cancelled when the fill sync sees the order end with nothing
        filled, and a fill that lands first opens the position as any fill does. True when the
        broker took a cancel. `reason` (`entry_timeout.BREAK_FAILED`, `TIMED_OUT`) goes into
        the order's `order.cancelled`."""
        filled = session.scalar(
            select(HatPosition.id).where(
                HatPosition.signal_id == signal_id, HatPosition.is_virtual.is_(False)
            )
        )
        part = session.scalar(
            select(HatOrder.id).where(
                HatOrder.signal_id == signal_id,
                HatOrder.purpose == ENTRY,
                HatOrder.status.in_(("partially_filled", "filled")),
            )
        )
        if filled is not None or part is not None:
            return False
        return self.cancel_rest(session, signal_id, trade_date_of(now), reason)

    def cancel_rest(
        self, session: Session, signal_id: uuid.UUID, today: date, reason: str | None = None
    ) -> bool:
        """Ask the broker to cancel what of the signal's live entries of `today` has not filled,
        each once (again only after the broker could not be reached; a refusal means it filled
        or ended). An entry of an earlier trade date expired at its close; one whose answer was
        lost has no broker id and ends at the close. True when the broker took a cancel."""
        asked = False
        for row in session.scalars(
            select(HatOrder).where(
                HatOrder.signal_id == signal_id,
                HatOrder.purpose == ENTRY,
                HatOrder.is_virtual.is_(False),
                HatOrder.status.in_(LIVE),
                HatOrder.placed_at.is_not(None),
            )
        ):
            if (
                row.broker_order_id is None
                or row.id in self._asked
                or trade_date_of(as_utc(row.placed_at)) < today
            ):
                continue
            ack = self._broker.cancel_order(row.broker_order_id)
            if ack.ok or ack.code not in CODES_NOT_SENT:
                self._asked.add(row.id)
            if ack.ok:
                asked = True
                if reason is not None:
                    self._reasons[row.id] = reason
            else:
                logger.warning("entry %s remainder not cancelled (%s)", row.tag, ack.code)
        return asked
