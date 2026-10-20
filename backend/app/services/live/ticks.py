"""Pure tick transforms for the live feed: no I/O, no service state."""

from __future__ import annotations

from typing import Any

TickPayload = dict[str, Any]


def normalize_tick(tick: TickPayload, db_symbol: str) -> TickPayload:
    """Copy of a Fyers tick keyed by DB symbol, with change_pct, change and volume.

    The input tick is not modified. Fields already present on the tick win over the
    derived ones, except `change_pct`, which always follows `chp` when Fyers sends it.
    """
    tick_db = tick.copy()
    tick_db["symbol"] = db_symbol

    # Fyers sends 'chp' (change percent); otherwise derive it from the previous close.
    if "chp" in tick_db:
        tick_db["change_pct"] = tick_db["chp"]
    elif "ltp" in tick_db and "prev_close_price" in tick_db:
        prev_close = tick_db["prev_close_price"]
        if prev_close and prev_close > 0:
            tick_db["change_pct"] = ((tick_db["ltp"] - prev_close) / prev_close) * 100

    # Normalize absolute change for consumers expecting `change`
    if "ch" in tick_db and "change" not in tick_db:
        tick_db["change"] = tick_db["ch"]

    # Ensure volume is passed through (Fyers sends 'volume' or 'v')
    if "volume" not in tick_db:
        if "v" in tick_db:
            tick_db["volume"] = tick_db["v"]
        elif "vol_traded_today" in tick_db:
            tick_db["volume"] = tick_db["vol_traded_today"]
    return tick_db
