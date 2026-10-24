"""`evaluate_signal`: the single risk authority for a HAT signal (HAT-RISK-100).

It calls the pure `decide` (which reads the session from the context's trading calendar) and
returns ACCEPT with a quantity or REJECT with a typed reason. It never raises: a missing or
unreadable input is a REJECT. An exit or a protective-stop move is always accepted. The live
price is checked at placement, not here (invariant 4: `data.live.usable_price`, the router's
`placement_refusal`).

Moved here from the legacy `RiskManager.evaluate_signal` (K1, backend review C5), so HAT's risk
authority depends on nothing outside `app.hat`. Two things that method did are gone:
- the DEV_MODE bypass: HAT never has one (the session comes only from the trading calendar);
- the read of the legacy VCP halt flag: HAT's own kill switches (`KillState`) are the only
  halt (HAT-RISK-104).

`decide` is the only caller of `size_position` (CONF `risk.single_sizing_path`).
"""

from __future__ import annotations

from .decision import RiskDecision, SignalContext, decide


def evaluate_signal(ctx: SignalContext) -> RiskDecision:
    """ACCEPT with a quantity or REJECT with a typed reason for one signal (HAT-RISK-100). An entry
    needs the context's trading calendar to say OPEN at its instant (`decide`), so a holiday, a
    closed day and a day the calendar does not hold are closed and a special session is open."""
    return decide(ctx)
