"""An owner command from the admin command line, as the engine operations it reaches read it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from ..risk.sessions import SessionCalendar, SessionState
from ..time import trade_date_of


@dataclass(frozen=True)
class OwnerCommand:
    """`chat_id` is the allowlisted chat the command is recorded under, `now` the instant it runs
    and `calendar` the session calendar that keeps it outside market hours."""

    chat_id: int
    now: datetime
    calendar: SessionCalendar

    @property
    def in_session(self) -> bool:
        """Whether the market is not closed at `now` (an owner command is refused then)."""
        return self.calendar.state(self.now) is not SessionState.CLOSED

    @property
    def trade_date(self) -> date:
        return trade_date_of(self.now)
