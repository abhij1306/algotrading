"""NSE market sessions: the default session times and the IST zone.

The cash session is 09:15:00-15:30:00 IST, both ends inclusive: 15:30:00 exactly still counts as
open, any later instant is closed. These are the market's default session (`hat_market`), which
`data.calendar.with_weekdays` writes onto every ordinary weekday's calendar row and the bar
builders use as their default window. Whether the market is open at an instant is read only from
the trading calendar (`risk.sessions.SessionCalendar`, loaded by `data.calendar.session_calendar`):
there is no weekday rule and no DEV_MODE bypass.

HAT's own module since K1 (backend review C1); the legacy `app.utils.market_hours` is deleted.
"""

from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
