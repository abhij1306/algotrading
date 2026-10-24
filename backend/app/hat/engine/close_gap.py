"""The 15:25 close against the official close (HAT-OPS-034): the live price a 15:20-15:25
`close_below_sma` or `close_above_sma` evaluation used, and its gap to the official close the
nightly ingest brings.

Both records are events on the position (`exit.close_evaluated`, `exit.close_gap`); neither moves
a trade's state, and nothing here reaches a broker. They work in the caller's transaction and
never commit.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatEvent, HatPosition
from ..money import require_exact
from ..time import day_bounds
from . import exits, journal
from .positions import TradeError, exit_rules_of, is_veto, signal_and_bot

__all__ = ["CLOSE_EVALUATED", "CLOSE_GAP", "record_close_evaluation", "record_close_gaps"]

CLOSE_EVALUATED = "exit.close_evaluated"
CLOSE_GAP = "exit.close_gap"
CLOSE_RULES = ("close_below_sma", "close_above_sma")
_PRICE_PLACES = Decimal("0.0001")


def record_close_evaluation(
    session: Session,
    position_id: uuid.UUID,
    view: exits.ExitView,
    result: exits.Evaluation,
    trade_date: date,
    now: datetime,
) -> bool:
    """Record the live price a 15:20-15:25 evaluation used for the position's SMA exit rules
    (`exit.close_evaluated`), whether or not one fired. False, writing nothing, when the
    position has no enabled SMA exit rule, the view has no price, or the trade date's
    evaluation is already recorded (a retried job)."""
    position = session.get(HatPosition, position_id)
    if position is None:
        raise TradeError("unknown_position")
    signal, _ = signal_and_bot(session, position)
    rules = exit_rules_of(session, signal)
    close_rules = [
        (exits.rule_id(index, rule), rule)
        for index, rule in enumerate(rules)
        if rule.enabled and rule.rule in CLOSE_RULES
    ]
    if not close_rules or view.price is None or _close_recorded(session, position.id, trade_date):
        return False
    ids = [rid for rid, _ in close_rules]
    levels = {
        str(rule.sma): str(view.sma[rule.sma])  # type: ignore[union-attr]
        for _, rule in close_rules
        if rule.sma in view.sma  # type: ignore[union-attr]
    }
    payload = {
        "trade_date": trade_date.isoformat(),
        "live_price": str(view.price),
        "rules": ids,
        "sma": levels,
        "fired": [rid for rid in result.fired if rid in ids],
        "is_virtual": bool(position.is_virtual),
    }
    journal.record(
        session,
        signal,
        journal.on_position(
            position, CLOSE_EVALUATED, payload, now=now, veto=is_veto(signal, position)
        ),
    )
    return True


def _close_recorded(session: Session, position_id: uuid.UUID, trade_date: date) -> bool:
    payloads = session.scalars(
        select(HatEvent.payload).where(
            HatEvent.event_type == CLOSE_EVALUATED, HatEvent.aggregate_id == position_id
        )
    )
    day = trade_date.isoformat()
    return any((payload or {}).get("trade_date") == day for payload in payloads)


def _gap_pct(gap: Decimal, live: Decimal) -> Decimal:
    return (gap * 100 / live).quantize(_PRICE_PLACES)


def record_close_gaps(
    session: Session, trade_date: date, closes: Mapping[uuid.UUID, Decimal], now: datetime
) -> int:
    """For every `exit.close_evaluated` of `trade_date` that has no gap yet, record the official
    close of the position's instrument (`closes`, from the nightly ingest) and its gap to the
    live price used (`exit.close_gap`, against the same position). An instrument without a close
    is left for a later run. Returns the number of gaps recorded."""
    start, end = day_bounds(trade_date)
    rows = session.execute(
        select(HatEvent.aggregate_id, HatEvent.event_type, HatEvent.payload).where(
            HatEvent.event_type.in_((CLOSE_EVALUATED, CLOSE_GAP)),
            HatEvent.occurred_at >= start,
            HatEvent.occurred_at < end + (end - start),  # a gap is recorded the night after
        )
    ).all()
    day = trade_date.isoformat()
    done = {
        aggregate
        for aggregate, kind, payload in rows
        if kind == CLOSE_GAP and (payload or {}).get("trade_date") == day
    }
    recorded = 0
    for aggregate, kind, payload in rows:
        data = payload or {}
        if kind != CLOSE_EVALUATED or data.get("trade_date") != day or aggregate in done:
            continue
        position = session.get(HatPosition, aggregate)
        if position is None:
            continue
        official = closes.get(position.instrument_id)
        if official is None or not official.is_finite() or official <= 0:
            continue
        signal, _ = signal_and_bot(session, position)
        live = require_exact(data["live_price"], "live_price", floats=True)
        gap = official - live
        sma = {
            int(k): require_exact(v, "sma", floats=True) for k, v in (data.get("sma") or {}).items()
        }
        payload = {
            "trade_date": day,
            "live_price": str(live),
            "official_close": str(official),
            "gap": str(gap),
            "gap_pct": str(_gap_pct(gap, live)),
            "below_sma_live": sorted(str(n) for n, level in sma.items() if live < level),
            "below_sma_close": sorted(str(n) for n, level in sma.items() if official < level),
            "above_sma_live": sorted(str(n) for n, level in sma.items() if live > level),
            "above_sma_close": sorted(str(n) for n, level in sma.items() if official > level),
        }
        journal.record(
            session,
            signal,
            journal.on_position(
                position, CLOSE_GAP, payload, now=now, veto=is_veto(signal, position)
            ),
        )
        done.add(aggregate)
        recorded += 1
    return recorded
