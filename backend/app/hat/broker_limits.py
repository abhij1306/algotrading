"""The Fyers app's request limits, shared by the broker adapter and the minute-history loader
(audit D3): one Fyers app has one daily request limit, whoever spends it.

Fyers answers every call with code -360 ("Standard plan limit of 5,000 requests/day") once the
app has spent its plan's daily requests, as a normal error reply: nothing succeeds until the next
day (HAT-BRK-082).
"""

from __future__ import annotations

__all__ = ["FYERS_CODE_RATE_LIMITED", "PLAN_CALLS_PER_DAY"]

FYERS_CODE_RATE_LIMITED = -360
PLAN_CALLS_PER_DAY = 5_000  # the Standard plan's daily requests (the settings' default)
