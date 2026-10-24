"""A held instrument's series change (PR 47, HAT-EXEC-146; research G17).

An instrument is (market, ISIN) (HAT-DATA-020, audit D5): when NSE moves a stock from EQ to BE
or BZ, the weekly reference refresh keeps the instrument, sets its `series` and `series_since`
(the refresh day) and rewrites its Fyers symbol for the new series (`-BE`; BZ has none). A
position opened before that day was protected under the old series' symbol (`-EQ`), which takes
no order, and the broker cancels its GTT. Such a position is found here: open, and its
instrument's series changed after the day it opened. Its exits keep running on the continuous
prices (D210; `daily/live.py:ExitViews`, none for a series with no Fyers symbol), its broker
stops go out under the instrument's current symbol (`StopDesk._target`), and the morning GTT
check (`TradeManager.check_gtts`) alerts it URGENT on each run until it is closed.

ASM and GSM are surveillance lists, not series: the instrument master holds no such flag, so
they are not seen here.

Reads only; nothing reaches a broker.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatBrokerSymbol, HatInstrument, HatPosition
from ..settings import active_broker
from ..time import as_utc, trade_date_of

__all__ = ["ALERT_SERIES_CHANGED", "broker_symbol", "series_changed"]

ALERT_SERIES_CHANGED = "position.series_changed"


def series_changed(session: Session, position: HatPosition) -> HatInstrument | None:
    """The instrument of an open position when its series changed after the position's trade
    date (so its stops went out under the old series' symbol), else None."""
    if position.closed_at is not None:
        return None
    instrument = session.get(HatInstrument, position.instrument_id)
    if instrument is None or instrument.series_since is None:
        return None
    if instrument.series_since <= trade_date_of(as_utc(position.opened_at)):
        return None
    return instrument


def broker_symbol(
    session: Session, instrument_id: uuid.UUID, broker: str | None = None
) -> str | None:
    """The stored symbol of the instrument at `broker` (default the active broker,
    `settings.active_broker`: the one its orders go to), if any."""
    return session.scalar(
        select(HatBrokerSymbol.broker_symbol).where(
            HatBrokerSymbol.instrument_id == instrument_id,
            HatBrokerSymbol.broker == (broker or active_broker()),
        )
    )
