"""Builders and strategies shared by the risk properties (sizing, caps, exposure, kill).

`valid_ctx()` is a signal that every check passes: tests change one thing about it and read the
decision of the real `evaluate_signal` (`app.hat.risk.service`). Money and weights are Decimal
throughout.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from hypothesis import strategies as st

from app.hat.risk.caps import OpenPosition
from app.hat.risk.decision import RiskDecision, SignalContext
from app.hat.risk.exposure import LadderState
from app.hat.risk.kill import KillState
from app.hat.risk.policy import PolicyVersion, RiskPolicy, parse_policy, seed_policy
from app.hat.risk.service import evaluate_signal
from app.hat.risk.sessions import SessionCalendar, SessionDay
from app.hat.time import IST

D = Decimal
BOT = uuid.UUID("00000000-0000-7000-8000-0000000000b1")
OTHER_BOT = uuid.UUID("00000000-0000-7000-8000-0000000000b2")
S1 = "stage2-contraction-breakout"
S2 = "momentum-breakout-ep"
TRADE_DAY = date(2026, 10, 21)  # a Wednesday
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def session_day(day: date) -> SessionDay:
    return SessionDay(
        True,
        datetime.combine(day, time(9, 15), tzinfo=IST),
        datetime.combine(day, time(15, 30), tzinfo=IST),
    )


def calendar_around(day: date = TRADE_DAY, span: int = 10) -> SessionCalendar:
    """Sessions on every weekday within `span` days of `day`; weekends are closed rows."""
    days = {}
    for offset in range(-span, span + 1):
        d = day + timedelta(days=offset)
        days[d] = session_day(d) if d.weekday() < 5 else SessionDay(False)
    return SessionCalendar(days)


CALENDAR = calendar_around()
AT = datetime.combine(TRADE_DAY, time(10, 30), tzinfo=IST)


def policy_with(**changes: object) -> RiskPolicy:
    """The seed policy with top-level fields of the JSON document replaced."""
    doc = seed_policy().to_json()
    doc.update(changes)  # type: ignore[arg-type]
    return parse_policy(doc)


def version_of(policy: RiskPolicy, *, number: int = 1, at: datetime = EPOCH) -> PolicyVersion:
    return PolicyVersion(uuid.uuid4(), number, at, None, policy)


def valid_ctx(**changes: object) -> SignalContext:
    """A signal that every check accepts: E 1,000,000, r 0.25%, entry 100, stop 95 gives q = 500."""
    base = SignalContext(
        signal_id=uuid.uuid4(),
        bot_id=BOT,
        action="entry",
        strategy_key=S1,
        at=AT,
        instrument_id=uuid.uuid4(),
        industry="banks",
        is_virtual=False,
        entry_price=D(100),
        stop_price=D(95),
        trigger_level=D(100),
        locked_upper_circuit=False,
        configured_max_chase_pct=None,
        equity=D(1_000_000),
        allocation_risk_pct=D("0.0025"),
        regime_mult=D(1),
        health_mult=D(1),
        free_cash=D(10_000_000),
        adv20=D(1_000_000),
        total_equity=D(2_000_000),
        policy_versions=[version_of(seed_policy())],
        ladder=LadderState("L2"),
        open_positions=[],
        entries_today=0,
        stopouts_today=0,
        gate_on=True,
        feed_stale=False,
        quality_failed=False,
        kill=KillState(),
        calendar=CALENDAR,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def decide(ctx: SignalContext) -> RiskDecision:
    return evaluate_signal(ctx)


HUNDRED, NINETY_FIVE = D(100), D(95)


def position(
    *,
    bot: uuid.UUID = OTHER_BOT,
    industry: str | None = None,
    qty: int = 100,
    avg_entry: Decimal = HUNDRED,
    last_price: Decimal | None = None,
    stop: Decimal = NINETY_FIVE,
    instrument: uuid.UUID | None = None,
    virtual: bool = False,
) -> OpenPosition:
    return OpenPosition(
        bot_id=bot,
        instrument_id=instrument or uuid.uuid4(),
        industry=industry or f"industry-{uuid.uuid4()}",
        qty=qty,
        avg_entry=avg_entry,
        last_price=avg_entry if last_price is None else last_price,
        current_stop=stop,
        is_virtual=virtual,
    )


# --- strategies (Decimal only) ---------------------------------------------------------------


def money(low: str, high: str, places: int = 2) -> st.SearchStrategy[Decimal]:
    return st.decimals(min_value=D(low), max_value=D(high), places=places)


fractions_open = st.decimals(min_value=D("0.0001"), max_value=D(1), places=4)  # (0, 1]
equities = money("1000", "5000000")
prices = money("1", "5000")
volumes = st.integers(min_value=0, max_value=50_000_000).map(D)
cash_amounts = money("0", "50000000")
levels = st.sampled_from(("L1", "L2", "L3"))


@st.composite
def entry_and_stop(draw: st.DrawFn) -> tuple[Decimal, Decimal]:
    """A long entry with a stop strictly below it and above zero."""
    entry = draw(money("2", "5000"))
    stop = draw(st.decimals(min_value=D("0.01"), max_value=entry - D("0.01"), places=2))
    return entry, stop


# --- rows for the service-level properties (SQLite in memory, foreign keys not enforced) --------
