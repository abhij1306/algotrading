"""PBT caps.* (HAT-RISK-030..035, 050, 060/061, 070..076, 100..104; HAT-GOV-090/091/093/094):
the caps, invariants, kill thresholds and policy-change rules, on the real
`app.hat.risk.service.evaluate_signal` and the pure rules it composes. Money is Decimal."""

import dataclasses
import inspect
import os
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

import pytest
from hat_risk_support import (
    AT,
    BOT,
    CALENDAR,
    OTHER_BOT,
    S1,
    S2,
    TRADE_DAY,
    D,
    calendar_around,
    decide,
    entry_and_stop,
    money,
    policy_with,
    position,
    valid_ctx,
)
from hat_support import memory_engine
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.hat.data.sessions import IST
from app.hat.models import HatEvent, HatRiskPolicyVersion
from app.hat.risk import kill
from app.hat.risk.decision import REASON_CODES, Reason, RiskDecision, SignalContext
from app.hat.risk.exposure import LadderState, max_positions
from app.hat.risk.kill import EquityPoint, KillRow, KillState
from app.hat.risk.policy import (
    MIN_LIVE_TRADES,
    GateError,
    TradeOutcome,
    build_gate_evidence,
    classify_direction,
    effective_version,
    increase_gate_ok,
    parse_policy,
    require_increase_gate,
    seed_policy,
)
from app.hat.risk.policy_service import (
    PolicyChangeError,
    apply_due_versions,
    load_versions,
    request_policy_change,
    seed_initial_policy,
)
from app.hat.risk.service import evaluate_signal, price_band_ok
from app.hat.risk.sessions import SessionDay
from app.hat.settings import load_settings

SEED = seed_policy()
BIG = D(10**12)


def accepted_risk(parts: dict) -> Decimal:
    """The risk amount of the base entry with an unreachable global cap."""
    decision = decide(valid_ctx(**parts, total_equity=BIG))
    assert decision.accepted
    return decision.risk_amount


# --- HAT-RISK-030: global open risk ---


@given(
    st.lists(
        st.tuples(
            money("10", "1000"), st.integers(min_value=0, max_value=90), st.integers(1, 5000)
        ),
        max_size=8,
    ),
    money("1000", "3000000"),
)
def test_global_open_risk(others: list, total_equity: Decimal) -> None:
    """An entry is accepted only if the open risk of every bot's positions plus its own risk is
    within 1.5% of total equity (D128), and is rejected with `global_open_risk` when it is not."""
    positions = [
        position(
            bot=OTHER_BOT, avg_entry=entry, stop=entry - min(entry - D("0.01"), D(gap)), qty=qty
        )
        for entry, gap, qty in others
    ]
    open_risk = sum(((p.avg_entry - p.current_stop) * p.qty for p in positions), D(0))
    candidate = accepted_risk({})
    decision = decide(valid_ctx(open_positions=positions, total_equity=total_equity))
    over = open_risk + candidate > D("0.015") * total_equity
    if over:
        assert decision.reason == Reason.GLOBAL_OPEN_RISK
    else:
        assert decision.accepted
        assert open_risk + decision.risk_amount <= D("0.015") * total_equity


def test_global_open_risk_boundary() -> None:
    """Exactly at the cap is allowed; one paisa over is not."""
    candidate = accepted_risk({})
    exact_equity = candidate / D("0.015")
    assert decide(valid_ctx(total_equity=exact_equity)).accepted
    assert (
        decide(valid_ctx(total_equity=exact_equity - D("0.01"))).reason == Reason.GLOBAL_OPEN_RISK
    )


# --- HAT-RISK-031..034: per-bot caps ---


@given(levels_=st.sampled_from(("L1", "L2", "L3")), attempts=st.integers(1, 12))
def test_max_positions(levels_: str, attempts: int) -> None:
    """After any sequence of accepted entries the bot holds at most min(6, level maximum)
    positions; at the limit the next entry is rejected with `max_positions`, below it accepted."""
    limit = max_positions(levels_, SEED)
    held = []
    for _ in range(attempts):
        decision = decide(valid_ctx(open_positions=held, ladder=LadderState(levels_)))
        if len(held) >= limit:
            assert decision.reason == Reason.MAX_POSITIONS
        else:
            assert decision.accepted, decision.reason
            held.append(position(bot=BOT))
        assert len(held) <= limit


@given(
    st.lists(st.sampled_from(("banks", "it", "pharma")), min_size=1, max_size=10),
)
def test_industry(industries: list) -> None:
    """A bot never holds more than 2 open positions in one industry; the entry that would is
    rejected with `industry_cap`."""
    held = []
    for industry in industries:
        count = sum(1 for p in held if p.industry == industry)
        decision = decide(
            valid_ctx(open_positions=held, industry=industry, ladder=LadderState("L3"))
        )
        if len(held) >= 6:
            assert decision.reason == Reason.MAX_POSITIONS
        elif count >= SEED.max_positions_per_industry:
            assert decision.reason == Reason.INDUSTRY_CAP
        else:
            assert decision.accepted, decision.reason
            held.append(position(bot=BOT, industry=industry))
        for name in {p.industry for p in held}:
            assert sum(1 for p in held if p.industry == name) <= SEED.max_positions_per_industry


@given(st.integers(min_value=1, max_value=10))
def test_daily_entries(triggers: int) -> None:
    """Within one trade date a bot accepts at most `max_entries_per_day` entries."""
    entries, accepted = 0, 0
    for _ in range(triggers):
        decision = decide(valid_ctx(entries_today=entries, ladder=LadderState("L3")))
        if decision.accepted:
            accepted += 1
            entries += 1
        else:
            assert decision.reason == Reason.ENTRY_CAP
    assert accepted <= SEED.max_entries_per_day
    assert accepted == min(triggers, SEED.max_entries_per_day)


@given(st.integers(min_value=0, max_value=8))
def test_daily_stopouts(stopouts: int) -> None:
    """Once `max_stopouts_per_day` positions were stopped out in the day, every later entry is
    rejected with `stopout_cap`."""
    decision = decide(valid_ctx(stopouts_today=stopouts))
    if stopouts >= SEED.max_stopouts_per_day:
        assert decision.reason == Reason.STOPOUT_CAP
    else:
        assert decision.accepted


# --- HAT-RISK-035: exits are never blocked ---

junk = st.one_of(
    st.none(),
    st.floats(),
    st.integers(),
    st.decimals(allow_nan=True, allow_infinity=True),
    st.text(max_size=4),
    st.booleans(),
    st.lists(st.integers(), max_size=2),
)
FIELDS = [name for name in SignalContext.__dataclass_fields__ if name != "action"]


@given(
    st.sampled_from(("exit", "stop_move")),
    st.booleans(),
    st.booleans(),
    st.booleans(),
    st.booleans(),
    st.dictionaries(st.sampled_from(FIELDS), junk, max_size=6),
)
def test_exits_never_blocked(
    action: str, account: bool, bot: bool, health: bool, paused: bool, spoiled: dict
) -> None:
    """An exit or a protective-stop move is accepted in every risk state: any kill, a pause, a
    breached cap, a closed market, and even an unreadable context."""
    state = {
        "action": action,
        "kill": KillState(account, bot, health, paused),
        "gate_on": False,
        "feed_stale": True,
        "entries_today": 99,
        "stopouts_today": 99,
        "open_positions": [position(bot=BOT) for _ in range(9)],
    }
    ctx = valid_ctx(**{**state, **spoiled})
    decision = decide(ctx)
    assert decision.accepted
    assert decision.reason is None


# --- HAT-RISK-050: the regime gate ---


@given(st.sampled_from((S1, S2)))
def test_regime_gate_blocks_entries(strategy: str) -> None:
    """With the gate off every entry is rejected with `regime_gate`; an exit is unaffected."""
    entry = decide(valid_ctx(strategy_key=strategy, gate_on=False))
    assert entry.reason == Reason.REGIME_GATE
    assert decide(valid_ctx(strategy_key=strategy, gate_on=False, action="exit")).accepted
    assert decide(valid_ctx(strategy_key=strategy, gate_on=True)).accepted


# --- HAT-RISK-060/061: kill thresholds ---

STEPS = st.lists(
    st.decimals(min_value=D("50"), max_value=D("200"), places=2), min_size=1, max_size=15
)


def running_peak_series(equities: list) -> list[EquityPoint]:
    peak, points = D(0), []
    for index, equity in enumerate(equities):
        peak = max(peak, equity)
        points.append(EquityPoint(AT + timedelta(days=index), equity, peak))
    return points


@given(STEPS, st.one_of(st.none(), st.integers(min_value=0, max_value=14)))
def test_kill_thresholds(equities: list, rearm_after: int | None) -> None:
    """The account kill trips exactly when (peak - equity)/peak >= 10% has occurred since the last
    re-arm, and a bot kill exactly at 10% (both D128); a snapshot at or before the re-arm does
    not count."""
    series = running_peak_series(equities)
    since = None if rearm_after is None else AT + timedelta(days=rearm_after)
    for threshold, percent in ((SEED.account_drawdown_pct, 10), (SEED.bot_drawdown_pct, 10)):
        expected = any(
            p.at > since if since else True
            for p in series
            if (p.peak - p.equity) * 100 >= percent * p.peak
        )
        assert kill.tripped_since(series, threshold, since) is expected


@given(st.decimals(min_value=D("1"), max_value=D("1000000"), places=2))
def test_kill_threshold_boundary(peak: Decimal) -> None:
    """Exactly 10% below the peak trips the account kill (D128); a paisa less of a drop does not."""
    at_limit = peak * D("0.90")
    assert kill.drawdown_breached(at_limit, peak, SEED.account_drawdown_pct)
    assert not kill.drawdown_breached(at_limit + D("0.01"), peak, SEED.account_drawdown_pct)


@given(
    st.lists(st.tuples(st.integers(0, 5), st.integers(0, 1), money("1000", "9000")), max_size=20)
)
def test_kill_isolation(snapshots: list) -> None:
    """A bot's own kill never makes another bot manage-only; the account kill does it for all."""
    b1, b2 = uuid.uuid4(), uuid.uuid4()
    rows = [KillRow("bot", b1, "tripped", AT, "drawdown_bot")]
    assert kill.kill_state_of(rows, b1, paused=False).manage_only
    assert not kill.kill_state_of(rows, b2, paused=False).manage_only
    account = [KillRow("account", None, "tripped", AT, "manual")]
    assert kill.kill_state_of(account, b1, paused=False).manage_only
    assert kill.kill_state_of(account, b2, paused=False).manage_only
    healthy = valid_ctx(kill=kill.kill_state_of(rows, b2, paused=False))
    assert decide(healthy).accepted
    assert decide(valid_ctx(kill=kill.kill_state_of(rows, b1, paused=False))).reason == (
        Reason.KILL_BOT
    )
    assert decide(valid_ctx(kill=kill.kill_state_of(account, b2, paused=False))).reason == (
        Reason.KILL_ACCOUNT
    )
    series = kill.account_series((AT + timedelta(days=d), equity) for d, _, equity in snapshots)
    totals = {}
    for d, _, equity in snapshots:
        totals[d] = totals.get(d, D(0)) + equity
    assert [p.equity for p in series] == [totals[d] for d in sorted(totals)]


# --- HAT-RISK-071/072: no adding to a position ---


@given(
    money("10", "1000"),
    money("0.01", "500"),
    st.sampled_from(("below", "equal", "above")),
    st.booleans(),
)
def test_no_add_to_loser(avg: Decimal, gap: Decimal, where: str, same_bot: bool) -> None:
    """A buy for an instrument the bot holds below its average entry is rejected with
    `add_to_loser`; held at or above, with `position_exists`; another bot's holding is no bar."""
    last = {"below": avg - min(gap, avg - D("0.01")), "equal": avg, "above": avg + gap}[where]
    instrument = uuid.uuid4()
    held = position(
        bot=BOT if same_bot else OTHER_BOT, instrument=instrument, avg_entry=avg, last_price=last
    )
    decision = decide(valid_ctx(instrument_id=instrument, open_positions=[held], total_equity=BIG))
    if not same_bot:
        assert decision.accepted
    elif where == "below":
        assert decision.reason == Reason.ADD_TO_LOSER
    else:
        assert decision.reason == Reason.POSITION_EXISTS


@given(st.integers(min_value=1, max_value=4), st.sampled_from((S1, S2)))
def test_no_pyramiding(held_count: int, strategy: str) -> None:
    """With any open position in an instrument, no new entry for it is accepted."""
    instrument = uuid.uuid4()
    held = [position(bot=BOT, instrument=instrument) for _ in range(held_count)]
    decision = decide(
        valid_ctx(strategy_key=strategy, instrument_id=instrument, open_positions=held)
    )
    assert not decision.accepted
    assert decision.reason == Reason.POSITION_EXISTS


# --- HAT-RISK-073..076: invariants 3 to 6 ---


@given(entry_and_stop(), st.one_of(st.none(), money("0", "6000")))
def test_entry_requires_stop(pair: tuple, override: Decimal | None) -> None:
    """An accepted entry always carries a stop with 0 < stop < entry; no stop is a REJECT."""
    entry, stop = pair
    ctx = valid_ctx(entry_price=entry, trigger_level=entry, ltp=entry, stop_price=override)
    decision = decide(ctx)
    if override is None:
        assert decision.reason == Reason.MISSING_INPUT
    elif decision.accepted:
        assert 0 < decision.stop < entry
        assert decision.stop == override
    ok = decide(valid_ctx(entry_price=entry, trigger_level=entry, ltp=entry, stop_price=stop))
    assert not ok.accepted or 0 < ok.stop < entry


@given(st.booleans(), st.booleans())
def test_stale_blocks_entry(feed_stale: bool, quality_failed: bool) -> None:
    """No entry is accepted while the feed is stale or the day's data failed its gate."""
    decision = decide(valid_ctx(feed_stale=feed_stale, quality_failed=quality_failed))
    if feed_stale:
        assert decision.reason == Reason.STALE_FEED
    elif quality_failed:
        assert decision.reason == Reason.QUALITY_FAILED
    else:
        assert decision.accepted


@st.composite
def instants(draw: st.DrawFn) -> tuple[datetime, bool]:
    """An instant on the calendar and whether it lies inside a session (built, not looked up):
    a weekday inside 09:15 to 15:30 IST is in; a weekend, or a weekday outside those hours is
    out (holidays are applied to the calendar by the test)."""
    kind = draw(st.sampled_from(("inside", "before", "after", "weekend")))
    offset = draw(st.integers(min_value=-6, max_value=6))
    day = TRADE_DAY + timedelta(days=offset)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    if kind == "weekend":
        day = day + timedelta(days=(5 - day.weekday()))
        minutes = draw(st.integers(0, 24 * 60 - 1))
    elif kind == "inside":
        minutes = draw(st.integers(9 * 60 + 15, 15 * 60 + 30))
    elif kind == "before":
        minutes = draw(st.integers(0, 9 * 60 + 14))
    else:
        minutes = draw(st.integers(15 * 60 + 31, 24 * 60 - 1))
    at = datetime.combine(day, time(minutes // 60, minutes % 60), tzinfo=IST)
    return at, kind == "inside"


@given(instants(), st.sets(st.integers(min_value=-6, max_value=6), max_size=3))
def test_market_hours_only(drawn: tuple, holiday_offsets: set) -> None:
    """No entry is accepted outside the session of the trading calendar: a weekend, a holiday, or
    outside 09:15 to 15:30 IST is `market_closed`, even with DEV_MODE on."""
    at, inside = drawn
    holidays = {TRADE_DAY + timedelta(days=o) for o in holiday_offsets}
    days = {
        day: (SessionDay(False) if day in holidays else row) for day, row in CALENDAR.days.items()
    }
    calendar = type(CALENDAR)(days)
    with mock.patch.dict(os.environ, {"DEV_MODE": "true"}):
        decision = decide(valid_ctx(at=at, calendar=calendar))
    on_holiday = at.date() in holidays
    if inside and not on_holiday:
        assert decision.accepted, decision.reason
    else:
        assert decision.reason == Reason.MARKET_CLOSED


@given(
    st.integers(min_value=0, max_value=3),
    st.integers(min_value=-90, max_value=90),
)
def test_special_session_follows_calendar(weeks: int, minutes: int) -> None:
    """A special session the calendar holds (a Saturday evening, as on Muhurat day) is open from
    its own open to its own close with the default policy, and closed a minute outside it; the
    weekday session does not leak onto it. Harm: a calendar-approved session is refused."""
    saturday = TRADE_DAY + timedelta(days=(5 - TRADE_DAY.weekday()) % 7 + 7 * weeks)
    opens = datetime.combine(saturday, time(18, 15), tzinfo=IST)
    closes = datetime.combine(saturday, time(19, 15), tzinfo=IST)
    days = {**CALENDAR.days, saturday: SessionDay(True, opens, closes)}
    calendar = type(CALENDAR)(days)
    at = opens + timedelta(minutes=minutes)
    decision = decide(valid_ctx(at=at, calendar=calendar))
    if opens <= at <= closes:
        assert decision.accepted, decision.reason
    else:
        assert decision.reason == Reason.MARKET_CLOSED


@given(st.integers(min_value=40, max_value=400))
def test_market_hours_unknown_date_fails_closed(days_out: int) -> None:
    """A trade date the calendar does not hold is not guessed open: the entry is rejected."""
    at = datetime.combine(TRADE_DAY + timedelta(days=days_out), time(11, 0), tzinfo=IST)
    assert decide(valid_ctx(at=at)).reason == Reason.MISSING_INPUT


CONFIGURED = st.one_of(st.none(), st.decimals(min_value=D("0.5"), max_value=D(10), places=1))


@given(
    st.sampled_from((S1, S2)),
    CONFIGURED,
    st.decimals(min_value=D("0.9"), max_value=D("1.2"), places=3),
    st.booleans(),
)
def test_max_chase_always(
    strategy: str, configured: Decimal | None, factor: Decimal, locked: bool
) -> None:
    """An entry above the trigger level by more than the effective max-chase percent, or in a
    stock locked at its upper circuit, is rejected with `max_chase`; the percent is the last
    configured one, else the strategy's default (5 for S1, 2 for S2)."""
    pct = {S1: D(5), S2: D(2)}[strategy] if configured is None else configured
    level = D(100)
    price = (level * factor).quantize(D("0.01"))
    decision = decide(
        valid_ctx(
            strategy_key=strategy,
            configured_max_chase_pct=configured,
            entry_price=price,
            trigger_level=level,
            ltp=price,
            stop_price=(price * D("0.9")).quantize(D("0.01")),
            locked_upper_circuit=locked,
        )
    )
    if price * 100 > level * (100 + pct) or locked:
        assert decision.reason == Reason.MAX_CHASE
    else:
        assert decision.accepted, decision.reason


# --- HAT-RISK-104: HAT's own kill switches are the halt ---


@given(st.booleans(), st.booleans(), st.booleans(), st.booleans())
def test_hat_kill_is_authority(account: bool, bot: bool, health: bool, paused: bool) -> None:
    """An entry is halted by HAT's own kill state and by nothing else: for every combination of
    the account kill, the bot kill, the health kill and the pause, the entry is rejected with the
    first tripped switch's reason, and accepted when none is tripped. The decision reads no legacy
    halt flag: the context has no such field and `evaluate_signal` takes no database session."""
    decision = decide(valid_ctx(kill=KillState(account, bot, health, paused)))
    expected = next(
        (
            reason
            for tripped, reason in (
                (account, Reason.KILL_ACCOUNT),
                (bot, Reason.KILL_BOT),
                (health, Reason.KILL_HEALTH),
                (paused, Reason.BOT_PAUSED),
            )
            if tripped
        ),
        None,
    )
    assert decision.reason == expected
    assert decision.accepted is (expected is None)
    assert not any("halt" in f.name for f in dataclasses.fields(SignalContext))
    assert set(inspect.signature(evaluate_signal).parameters) == {"ctx", "hours", "band_pct"}


# --- the price band (HAT-RISK-100, HAT-RISK-101) ---


@given(
    money("1", "5000"),
    st.decimals(min_value=D("0.01"), max_value=D("2.5"), places=4),
)
def test_price_band(ltp: Decimal, ratio: Decimal) -> None:
    """An entry whose price is more than 20% from the last price is rejected with
    `price_out_of_band`, and one within 20% (the edge included) is not; no live price is a
    `missing_input`. Money is Decimal, so the edge is exact."""
    entry = (ltp * ratio).quantize(D("0.01"))
    if entry < 1:
        return
    decision = decide(
        valid_ctx(entry_price=entry, ltp=ltp, trigger_level=entry, stop_price=entry * D("0.99"))
    )
    if abs(entry - ltp) * 100 > 20 * ltp:
        assert decision.reason == Reason.PRICE_OUT_OF_BAND
    else:
        assert decision.accepted, decision.reason
    assert decide(valid_ctx(ltp=D(0))).reason == Reason.MISSING_INPUT


@given(st.sampled_from([D("NaN"), D("Infinity"), D("-1"), D("-Infinity")]))
def test_price_band_width_fails_closed(band: Decimal) -> None:
    """A band width that is not a finite number of at least zero cannot be evaluated: the entry is
    `missing_input`, never an exception and never an unlimited band."""
    assert price_band_ok(D(100), D(100), band) is None
    assert evaluate_signal(valid_ctx(), band_pct=band).reason == Reason.MISSING_INPUT


# --- HAT-RISK-101/102: total, typed, fail closed ---

garbage_ctx = st.builds(SignalContext, **dict.fromkeys(FIELDS, junk))


def check_shape(decision: object) -> None:
    assert isinstance(decision, RiskDecision)
    if decision.accepted:
        assert isinstance(decision.quantity, int)
        assert decision.quantity >= 1
        assert decision.reason is None
        assert isinstance(decision.stop, Decimal)
        assert isinstance(decision.risk_amount, Decimal)
    else:
        assert decision.reason in REASON_CODES
        assert decision.quantity is None


@given(garbage_ctx)
def test_decision_total_on_garbage(ctx: SignalContext) -> None:
    """For any context at all the manager returns one typed decision and never raises."""
    check_shape(decide(ctx))


@given(st.sampled_from(FIELDS), junk)
def test_decision_total(field: str, value: object) -> None:
    """A valid signal with one field replaced by anything is still exactly ACCEPT or REJECT with a
    reason from the enumerated set, and never raises."""
    check_shape(decide(valid_ctx(**{field: value})))


DECIMAL_FIELDS = (
    "entry_price",
    "stop_price",
    "trigger_level",
    "ltp",
    "equity",
    "allocation_risk_pct",
    "free_cash",
    "adv20",
    "total_equity",
)
OTHER_REQUIRED = (
    "bot_id",
    "strategy_key",
    "at",
    "instrument_id",
    "industry",
    "is_virtual",
    "locked_upper_circuit",
    "policy_versions",
    "ladder",
    "open_positions",
    "entries_today",
    "stopouts_today",
    "gate_on",
    "feed_stale",
    "quality_failed",
    "kill",
    "calendar",
)
NOT_EXACT = (None, float("nan"), float("inf"), 1.5, D("NaN"), D("Infinity"))


@given(st.sampled_from(DECIMAL_FIELDS + OTHER_REQUIRED), st.sampled_from(NOT_EXACT))
def test_fail_closed(field: str, value: object) -> None:
    """A required field that is missing, a float, NaN or infinite makes the entry a REJECT
    with `missing_input`; no default of zero or infinity ever sizes an order."""
    if field not in DECIMAL_FIELDS and value is not None:
        return  # only a missing value is "not exact" for a non-numeric field
    decision = decide(valid_ctx(**{field: value}))
    assert not decision.accepted
    assert decision.reason == Reason.MISSING_INPUT


def test_fail_closed_naive_time_and_no_policy() -> None:
    """A naive instant and an empty policy history are missing inputs too."""
    assert decide(valid_ctx(at=datetime(2026, 10, 21, 10, 0))).reason == Reason.MISSING_INPUT
    assert decide(valid_ctx(policy_versions=[])).reason == Reason.MISSING_INPUT
    assert decide(valid_ctx(strategy_key="unknown")).reason == Reason.MISSING_INPUT


# --- HAT-GOV-090/093: increase or decrease ---

# field path -> (a more permissive value, a more restrictive value), written out literally from the
# 06 §1 table; a higher number is more permissive except for `up_after_wins`.
PATHS = {
    ("risk_pct",): ("0.003", "0.002"),
    ("p_max", S1): ("0.25", "0.15"),
    ("p_max", S2): ("0.30", "0.20"),
    ("adv_cap_pct",): ("0.02", "0.005"),
    ("global_open_risk_pct",): ("0.04", "0.02"),
    ("max_open_positions",): (7, 5),
    ("max_positions_per_industry",): (3, 1),
    ("max_entries_per_day",): (4, 2),
    ("max_stopouts_per_day",): (4, 2),
    ("exposure_ladder", "L1"): (None, None),
    ("exposure_ladder", "up_after_wins"): (1, 3),
    ("exposure_ladder", "down_after_losses"): (3, 1),
    ("kill", "account_drawdown_pct"): ("0.20", "0.10"),
    ("kill", "bot_drawdown_pct"): ("0.15", "0.05"),
    ("base_weight", "S1"): ("0.60", "0.40"),
    ("base_weight", "S2"): ("0.60", "0.40"),
    ("cluster_cap",): (None, "0.90"),
    ("multiplier_bounds", "lo"): ("0.30", "0.20"),
    ("multiplier_bounds", "hi"): ("1.2", "0.9"),
    ("health_bands", "half_dd"): ("0.09", "0.07"),
    ("health_bands", "zero_dd"): ("0.14", "0.10"),
    ("health_bands", "half_mult"): ("0.6", "0.4"),
}
LADDER_FIELDS = {
    ("exposure_ladder", "L1", "max_positions"): (3, 1),
    ("exposure_ladder", "L1", "risk_mult"): ("0.6", "0.4"),
    ("exposure_ladder", "L2", "max_positions"): (5, 3),
    ("exposure_ladder", "L3", "risk_mult"): (None, "0.9"),
}
ALL_PATHS = {
    **{p: v for p, v in PATHS.items() if v[0] is not None or v[1] is not None},
    **LADDER_FIELDS,
}


def with_path(doc: dict, path: tuple, value: object) -> None:
    node = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


@st.composite
def policy_pairs(draw: st.DrawFn) -> tuple:
    doc = seed_policy().to_json()
    permissive_changed = False
    for path, (more, less) in ALL_PATHS.items():
        choice = draw(st.sampled_from(("same", "more", "less")))
        value = {"more": more, "less": less}.get(choice)
        if value is None:
            continue
        with_path(doc, path, value)
        permissive_changed = permissive_changed or choice == "more"
    return doc, permissive_changed


@given(policy_pairs())
def test_policy_direction_classification(pair: tuple) -> None:
    """A proposed policy is a `decrease` exactly when no field is more permissive than the current
    one; raising a multiplier bound, a base weight or any cap is an `increase`."""
    doc, permissive_changed = pair
    try:
        proposed = parse_policy(doc)
    except Exception:  # noqa: BLE001 - a combination the schema refuses is not a proposal
        return
    direction = classify_direction(SEED, proposed)
    assert direction == ("increase" if permissive_changed else "decrease")
    assert classify_direction(proposed, proposed) == "decrease"


@given(st.sampled_from(("multiplier_bounds.hi", "base_weight.S1", "base_weight.S2")))
def test_multiplier_and_weight_raises_are_increases(name: str) -> None:
    """HAT-GOV-093: allowing a multiplier above 1 or a larger base weight is an increase."""
    doc = seed_policy().to_json()
    if name == "multiplier_bounds.hi":
        doc["multiplier_bounds"]["hi"] = "1.5"
    else:
        doc["base_weight"][name.split(".")[1]] = "0.75"
    assert classify_direction(SEED, parse_policy(doc)) == "increase"


# --- HAT-GOV-091: the evidence gate ---

trades = st.lists(
    st.tuples(st.decimals(min_value=D("-3"), max_value=D("6"), places=2), st.booleans()),
    max_size=140,
)


@given(trades, st.decimals(min_value=D("-1"), max_value=D("2"), places=2))
def test_increase_gate(raw: list, backtest: Decimal) -> None:
    """An increase passes only with at least 50 live (non-virtual) closed trades whose
    expectancy is not negative and whose 90% interval holds the backtest expectancy; shadow
    trades never count; a missing or malformed evidence document never passes."""
    outcomes = [TradeOutcome(r, virtual) for r, virtual in raw]
    live = [t for t in outcomes if not t.is_virtual]
    evidence = build_gate_evidence(outcomes, backtest)
    assert evidence["live_trades"] == len(live)
    assert increase_gate_ok(evidence) == increase_gate_ok(build_gate_evidence(live, backtest))
    if increase_gate_ok(evidence):
        assert len(live) >= MIN_LIVE_TRADES
        assert D(evidence["expectancy_r"]) >= 0
        assert D(evidence["ci90_low"]) <= backtest <= D(evidence["ci90_high"])
    if len(live) < MIN_LIVE_TRADES:
        assert not increase_gate_ok(evidence)
        with pytest.raises(GateError):
            require_increase_gate("increase", evidence)
    require_increase_gate("decrease", None)
    require_increase_gate("initial", None)


@given(
    st.lists(st.decimals(min_value=D("-1"), max_value=D("4"), places=2), min_size=50, max_size=90)
)
def test_increase_gate_passes_real_evidence(results: list) -> None:
    """With 50 or more live trades and a backtest expectancy equal to their mean, the gate passes
    exactly when that mean is not negative."""
    mean = sum(results, D(0)) / len(results)
    evidence = build_gate_evidence([TradeOutcome(r, False) for r in results], mean)
    assert increase_gate_ok(evidence) is (mean >= 0)


@given(
    st.one_of(
        st.none(),
        st.integers(),
        st.text(max_size=5),
        st.dictionaries(st.text(max_size=4), junk, max_size=5),
    )
)
def test_increase_gate_rejects_junk(evidence: object) -> None:
    assert increase_gate_ok(evidence) is False  # type: ignore[arg-type]


# --- HAT-GOV-094: changes are events ---

DB_ENGINE = memory_engine()
USER = load_settings().user_id
PASSING = {
    "live_trades": 60,
    "expectancy_r": "0.4",
    "ci90_low": "0.1",
    "ci90_high": "0.7",
    "backtest_expectancy_r": "0.3",
}
LONG_CALENDAR = calendar_around(TRADE_DAY, span=120)
EVENING = time(20, 0)

change_steps = st.lists(
    st.tuples(st.sampled_from(("raise", "lower", "apply")), st.integers(min_value=0, max_value=5)),
    min_size=1,
    max_size=10,
)


@settings(max_examples=60)
@given(change_steps)
def test_policy_change_emits_event(steps: list) -> None:
    """For every sequence of requests and session opens, each request appends
    `risk.change_requested`, a version takes effect only in a transaction that appends
    `risk.change_effective` (once) and supersedes its predecessor, and an increase is never
    effective sooner than 7 days after its request."""
    start = datetime.combine(TRADE_DAY, EVENING, tzinfo=IST).astimezone(UTC)
    with Session(DB_ENGINE) as session:
        try:
            seed_initial_policy(session, USER, start - timedelta(days=1))
            apply_due_versions(session, USER, start - timedelta(days=1))
            session.commit()
            clock, requests, raised = start, 0, 0
            for kind, advance in steps:
                clock += timedelta(days=advance)
                if kind == "apply":
                    apply_due_versions(session, USER, clock)
                    session.commit()
                    continue
                raised += kind == "raise"
                proposed = policy_with(
                    risk_pct=str(D("0.0025") + D("0.0001") * raised) if kind == "raise" else "0.002"
                )
                row = request_policy_change(
                    session,
                    user_id=USER,
                    proposed=proposed,
                    calendar=LONG_CALENDAR,
                    requested_at=datetime.combine(
                        clock.astimezone(IST).date(), EVENING, tzinfo=IST
                    ),
                    evidence=lambda _s, _u, _at: PASSING,
                    chat_id=1,
                )
                requests += 1
                session.commit()
                assert row.direction == ("increase" if kind == "raise" else "decrease")
            apply_due_versions(session, USER, clock + timedelta(days=30))
            session.commit()

            events = session.scalars(select(HatEvent)).all()
            versions = session.scalars(select(HatRiskPolicyVersion)).all()
            asked = [e for e in events if e.event_type == "risk.change_requested"]
            effective = [e for e in events if e.event_type == "risk.change_effective"]
            assert len(asked) == len(versions) == requests + 1
            assert sorted(e.aggregate_id for e in effective) == sorted(v.id for v in versions)
            in_force = effective_version(load_versions(session, USER), clock + timedelta(days=30))
            assert in_force is not None
            assert (
                max(versions, key=lambda v: (v.effective_from, v.version)).id == in_force.version_id
            )
            for row in versions:
                assert row.direction != "increase" or (
                    row.effective_from - row.requested_at >= timedelta(days=7)
                )
        finally:
            session.rollback()
            session.execute(delete(HatEvent))
            session.execute(delete(HatRiskPolicyVersion))
            session.commit()


@given(st.sampled_from(("raise", "lower")))
def test_policy_change_inside_session_or_without_evidence_is_refused(kind: str) -> None:
    """A change requested inside market hours, an increase without evidence (the default source,
    the backtest runs, has no completed run: HAT-GOV-091) and a request with no session open to
    take effect at are refused, and write nothing."""
    with Session(DB_ENGINE) as session:
        try:
            seed_initial_policy(session, USER, AT - timedelta(days=1))
            apply_due_versions(session, USER, AT)
            proposed = policy_with(risk_pct="0.003" if kind == "raise" else "0.002")
            evening = datetime.combine(TRADE_DAY, EVENING, tzinfo=IST)
            with pytest.raises(PolicyChangeError):
                request_policy_change(
                    session,
                    user_id=USER,
                    proposed=proposed,
                    calendar=LONG_CALENDAR,
                    requested_at=AT,
                )
            if kind == "raise":
                with pytest.raises(GateError):
                    request_policy_change(
                        session,
                        user_id=USER,
                        proposed=proposed,
                        calendar=LONG_CALENDAR,
                        requested_at=evening,
                        chat_id=1,
                    )
            short_calendar = calendar_around(TRADE_DAY, span=0)
            with pytest.raises(PolicyChangeError):
                request_policy_change(
                    session,
                    user_id=USER,
                    proposed=proposed,
                    calendar=short_calendar,
                    requested_at=evening,
                    evidence=lambda _s, _u, _at: PASSING,
                    chat_id=1,
                )
            asked = session.scalars(
                select(HatEvent).where(HatEvent.event_type == "risk.change_requested")
            ).all()
            assert len(asked) == 1  # the seed's only
            assert len(session.scalars(select(HatRiskPolicyVersion)).all()) == 1
        finally:
            session.rollback()
            session.execute(delete(HatEvent))
            session.execute(delete(HatRiskPolicyVersion))
            session.commit()
