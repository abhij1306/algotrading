"""The risk authority: each cap of `risk/caps.py` at its boundary, one `evaluate_signal` refusal
per reason over `valid_ctx()`, and the kill thresholds, marks and policy-change rules. Money is
Decimal."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hat_risk_support import (
    AT,
    BOT,
    CALENDAR,
    S1,
    S2,
    TRADE_DAY,
    D,
    policy_with,
    position,
    valid_ctx,
    version_of,
)
from hypothesis import given
from hypothesis import strategies as st

from app.hat.engine.strategy.registry import STRATEGIES
from app.hat.risk import caps, kill
from app.hat.risk.caps import OpenPosition
from app.hat.risk.decision import Reason, SignalContext
from app.hat.risk.kill import EquityPoint, KillState
from app.hat.risk.policy import (
    MIN_LIVE_TRADES,
    GateError,
    PolicyError,
    TradeOutcome,
    build_gate_evidence,
    classify_direction,
    increase_gate_ok,
    parse_policy,
    require_increase_gate,
    seed_policy,
)
from app.hat.risk.service import evaluate_signal

SEED = seed_policy()


# --- the caps (HAT-RISK-030..036, 050, 070..076) ---


def test_global_open_risk_boundary() -> None:
    """Open risk plus the candidate's at exactly pct x total equity passes; a paisa over fails."""
    pct, total = D("0.015"), D(2_000_000)
    assert caps.global_open_risk_ok([D(20_000), D(7_500)], D(2_500), total, pct)
    assert not caps.global_open_risk_ok([D(20_000), D("7500.01")], D(2_500), total, pct)
    assert caps.global_open_risk_ok([], D(30_000), total, pct)
    assert not caps.global_open_risk_ok([], D("30000.01"), total, pct)


def test_open_risk_is_zero_once_locked() -> None:
    """A stop trailed to or above the entry carries no open risk; below it, (entry - stop) x qty."""
    assert position(qty=10, avg_entry=D(100), stop=D(95)).open_risk == D(50)
    assert position(qty=10, avg_entry=D(100), stop=D(100)).open_risk == 0
    assert position(qty=10, avg_entry=D(100), stop=D(104)).open_risk == 0


@pytest.mark.parametrize(("level", "limit"), [("L1", 2), ("L2", 4), ("L3", 6)])
def test_positions_ok_below_the_lesser_limit(level: str, limit: int) -> None:
    """Fewer open positions than min(policy maximum, the level's maximum); at the limit refuses."""
    assert caps.positions_ok(limit - 1, level, SEED)
    assert not caps.positions_ok(limit, level, SEED)
    tighter = policy_with(max_open_positions=3)
    assert caps.positions_ok(2, "L3", tighter)
    assert not caps.positions_ok(3, "L3", tighter)


@pytest.mark.parametrize(
    ("check", "limit"),
    [
        (caps.industry_ok, SEED.max_positions_per_industry),
        (caps.entries_ok, SEED.max_entries_per_day),
        (caps.stopouts_ok, SEED.max_stopouts_per_day),
    ],
)
def test_count_caps_refuse_at_the_limit(check: object, limit: int) -> None:
    assert check(limit - 1, SEED)  # type: ignore[operator]
    assert not check(limit, SEED)  # type: ignore[operator]


def test_sessions_since_counts_trading_days_after_the_stop() -> None:
    """Wednesday 21 Oct: 0 on the stop's own session; the weekend adds nothing."""
    assert caps.sessions_since(CALENDAR, TRADE_DAY, TRADE_DAY) == 0
    assert caps.sessions_since(CALENDAR, TRADE_DAY, TRADE_DAY + timedelta(days=1)) == 1
    assert caps.sessions_since(CALENDAR, TRADE_DAY, TRADE_DAY + timedelta(days=4)) == 2  # Sunday
    assert caps.sessions_since(CALENDAR, TRADE_DAY, TRADE_DAY + timedelta(days=5)) == 3  # Monday
    assert caps.sessions_since(CALENDAR, TRADE_DAY, TRADE_DAY - timedelta(days=1)) == 0


def test_cooldown_refuses_the_stop_session_and_the_n_after() -> None:
    """N = 2: refused on Wednesday (the stop), Thursday and Friday; open again on Monday."""
    policy = policy_with(cooldown_sessions=2)
    stop = TRADE_DAY
    refused = [TRADE_DAY + timedelta(days=n) for n in (0, 1, 2, 3, 4)]
    assert not any(caps.cooldown_ok(CALENDAR, stop, day, policy) for day in refused)
    assert caps.cooldown_ok(CALENDAR, stop, TRADE_DAY + timedelta(days=5), policy)
    # no hard stop, a stop after the day, or N = 0 refuses nothing
    assert caps.cooldown_ok(CALENDAR, None, TRADE_DAY, policy)
    assert caps.cooldown_ok(CALENDAR, stop + timedelta(days=1), TRADE_DAY, policy)
    assert caps.cooldown_ok(CALENDAR, stop, TRADE_DAY, SEED)


def test_regime_gate_is_only_true() -> None:
    assert caps.regime_gate_open(True)
    for value in (False, None, 1, "true"):
        assert not caps.regime_gate_open(value)  # type: ignore[arg-type]


def test_stop_valid_strictly_between_zero_and_entry() -> None:
    assert caps.stop_valid(D(100), D("99.99"))
    assert caps.stop_valid(D(100), D("0.01"))
    assert not caps.stop_valid(D(100), D(100))
    assert not caps.stop_valid(D(100), D("100.01"))
    assert not caps.stop_valid(D(100), D(0))


def test_effective_max_chase_pct() -> None:
    """The configured percent, else the strategy's seed value, else the stricter default of 2."""
    assert caps.effective_max_chase_pct(D(3), S1) == D(3)
    assert caps.effective_max_chase_pct(D(0), S1) == D(0)
    assert caps.effective_max_chase_pct(None, S1) == STRATEGIES[S1].max_chase_pct
    assert caps.effective_max_chase_pct(None, "no-such-strategy") == caps.MAX_CHASE_DEFAULT_PCT
    assert caps.MAX_CHASE_DEFAULT_PCT == D(2)


def test_chase_ok_boundary() -> None:
    """At most pct percent above the trigger: 102 on 100 at 2% passes, 102.01 fails."""
    assert caps.chase_ok(D(102), D(100), D(2), locked_upper=False)
    assert not caps.chase_ok(D("102.01"), D(100), D(2), locked_upper=False)
    assert caps.chase_ok(D(90), D(100), D(2), locked_upper=False)


def test_chase_ok_refuses_locked_upper() -> None:
    assert not caps.chase_ok(D(100), D(100), D(2), locked_upper=True)


def test_is_loser_below_entry_only() -> None:
    assert caps.is_loser(position(avg_entry=D(100), last_price=D("99.99")))
    assert not caps.is_loser(position(avg_entry=D(100), last_price=D(100)))


# --- evaluate_signal: one refusal per reason ---


def _mine(**changes: object) -> OpenPosition:
    return position(bot=BOT, **changes)  # type: ignore[arg-type]


SAME = uuid.UUID("00000000-0000-7000-8000-00000000a001")
COOLDOWN = policy_with(cooldown_sessions=2)
# reason -> the one change to valid_ctx() that makes evaluate_signal refuse with it
REFUSALS = {
    Reason.MISSING_INPUT: {"entry_price": None},
    Reason.INVALID_INPUT: {"equity": D(-1)},
    Reason.KILL_ACCOUNT: {"kill": KillState(account_tripped=True)},
    Reason.KILL_BOT: {"kill": KillState(bot_tripped=True)},
    Reason.KILL_HEALTH: {"kill": KillState(health_tripped=True)},
    Reason.BOT_PAUSED: {"kill": KillState(paused=True)},
    Reason.MARKET_CLOSED: {"at": AT.replace(hour=9, minute=14)},
    Reason.STALE_FEED: {"feed_stale": True},
    Reason.QUALITY_FAILED: {"quality_failed": True},
    Reason.REGIME_GATE: {"gate_on": False},
    Reason.STOP_NOT_BELOW_ENTRY: {"stop_price": D(100)},
    Reason.MAX_CHASE: {"configured_max_chase_pct": D(2), "entry_price": D("102.01")},
    Reason.ADD_TO_LOSER: {
        "instrument_id": SAME,
        "open_positions": [_mine(instrument=SAME, last_price=D(99))],
    },
    Reason.POSITION_EXISTS: {"instrument_id": SAME, "open_positions": [_mine(instrument=SAME)]},
    Reason.MAX_POSITIONS: {"open_positions": [_mine() for _ in range(4)]},
    Reason.INDUSTRY_CAP: {"open_positions": [_mine(industry="banks") for _ in range(2)]},
    Reason.ENTRY_CAP: {"entries_today": 3},
    Reason.STOPOUT_CAP: {"stopouts_today": 3},
    Reason.COOLDOWN: {"policy_versions": [version_of(COOLDOWN)], "hard_stop_days": [TRADE_DAY]},
    Reason.NO_ALLOCATION: {"allocation_risk_pct": D(0)},
    Reason.ALLOCATION_EXCEEDS_POLICY: {"allocation_risk_pct": D("0.0026")},
    Reason.SIZE_ZERO_RISK: {"equity": D(1000)},
    Reason.SIZE_ZERO_POSITION: {
        "entry_price": D(3_000_000),
        "stop_price": D(2_999_999),
        "trigger_level": D(3_000_000),
    },
    Reason.SIZE_ZERO_LIQUIDITY: {"adv20": D(50)},
    Reason.SIZE_ZERO_CASH: {"free_cash": D(50)},
    # 100 shares at 100 is 1% of the 1,000,000 equity, under the seed's 2% floor
    Reason.SIZE_BELOW_MIN: {"free_cash": D(10_000)},
    Reason.GLOBAL_OPEN_RISK: {"open_positions": [position(qty=5501)]},
}


def test_valid_ctx_is_accepted() -> None:
    """The base signal: E 1,000,000 at 0.25%, entry 100, stop 95 sizes to 500 by risk."""
    decision = evaluate_signal(valid_ctx())
    assert decision.accepted
    assert (decision.quantity, decision.risk_amount, decision.binding) == (500, D(2500), "risk")


def test_every_reason_has_a_refusal() -> None:
    assert set(REFUSALS) == set(Reason)


@pytest.mark.parametrize("reason", list(Reason))
def test_evaluate_signal_refuses_each_reason(reason: Reason) -> None:
    decision = evaluate_signal(valid_ctx(**REFUSALS[reason]))
    assert not decision.accepted
    assert decision.reason == reason.value
    assert decision.quantity is None


def test_global_open_risk_counts_same_mode_only() -> None:
    """27,500 of another bot's open risk plus the candidate's 2,500 is exactly 1.5% of 2,000,000
    and passes; a share more fails. A shadow position never counts against a real entry."""
    assert evaluate_signal(valid_ctx(open_positions=[position(qty=5500)])).accepted
    shadow = position(qty=100_000, virtual=True)
    assert evaluate_signal(valid_ctx(open_positions=[shadow])).accepted


def test_caps_count_only_this_bot_and_mode() -> None:
    """Another bot's positions, or this bot's shadow ones, fill none of this bot's real caps."""
    others = [position(industry="banks") for _ in range(4)]
    shadow = [_mine(industry="banks", virtual=True) for _ in range(4)]
    assert evaluate_signal(valid_ctx(open_positions=others + shadow)).accepted
    assert evaluate_signal(valid_ctx(open_positions=[_mine() for _ in range(3)])).accepted


def test_cooldown_reads_no_hard_stop_after_the_day() -> None:
    later = [TRADE_DAY + timedelta(days=1)]
    ctx = valid_ctx(policy_versions=[version_of(COOLDOWN)], hard_stop_days=later)
    assert evaluate_signal(ctx).accepted
    # with a cooldown in force the hard-stop days are a required input
    assert evaluate_signal(valid_ctx(policy_versions=[version_of(COOLDOWN)])).reason == (
        Reason.MISSING_INPUT.value
    )


def test_exits_are_accepted_in_every_state() -> None:
    """An exit or a stop move is accepted with every kill tripped, the market closed and the
    inputs missing (D118)."""
    tripped = KillState(account_tripped=True, bot_tripped=True, health_tripped=True, paused=True)
    for action in ("exit", "stop_move"):
        decision = evaluate_signal(
            SignalContext(action=action, kill=tripped, calendar=CALENDAR, at=AT.replace(hour=20))
        )
        assert decision.accepted
        assert set(decision.warnings) == {"manage_only_exit_allowed", "exit_outside_session"}


# --- the cooldown is governed ---


@given(st.integers(0, 250), st.integers(0, 250))
def test_cooldown_shortening_is_increase(current: int, proposed: int) -> None:
    """A shorter cooldown is a risk increase (the 7-day cooling-off and the evidence gate apply);
    a longer or equal one is a decrease. Values outside [0, 250], or not whole numbers, are
    refused. Harm: the cooldown is switched off at once, outside the governance gate."""
    direction = classify_direction(
        policy_with(cooldown_sessions=current), policy_with(cooldown_sessions=proposed)
    )
    assert direction == ("increase" if proposed < current else "decrease")
    for bad in (-1, 251, True, 1.0, "3", None):
        with pytest.raises(PolicyError):
            policy_with(cooldown_sessions=bad)


junk = st.one_of(
    st.none(),
    st.floats(),
    st.integers(),
    st.decimals(allow_nan=True, allow_infinity=True),
    st.text(max_size=4),
    st.booleans(),
    st.lists(st.integers(), max_size=2),
)
# --- kill thresholds ---

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


def test_the_peak_carries_across_a_manual_rearm() -> None:
    """QA10 (owner 2026-10-25): a re-arm never restarts the peak. 9% under a peak of 100, a
    re-arm, then another 9% (91 -> 82.81, 17.19% under 100) trips the 10% kill; the fall before
    the re-arm alone never does."""
    rearmed_at = AT + timedelta(days=1, hours=2)
    points = [
        kill.SnapshotPoint(AT, D("100"), D("100"), D("0.5")),
        kill.SnapshotPoint(AT + timedelta(days=1), D("91"), D("100"), D("0.5")),
        kill.SnapshotPoint(AT + timedelta(days=2), D("82.81"), D("100"), D("0.5")),
    ]
    series = kill.adjusted_series(points)
    assert [p.peak for p in series] == [D("100")] * 3
    assert not kill.tripped_since(series[:2], SEED.bot_drawdown_pct, rearmed_at)
    assert kill.tripped_since(series, SEED.bot_drawdown_pct, rearmed_at)
    assert kill.tripped_since(series, SEED.account_drawdown_pct, rearmed_at)


@given(st.decimals(min_value=D("1"), max_value=D("1000000"), places=2))
def test_kill_threshold_boundary(peak: Decimal) -> None:
    """Exactly 10% below the peak trips the account kill (D128); a paisa less of a drop does not."""
    at_limit = peak * D("0.90")
    assert kill.drawdown_breached(at_limit, peak, SEED.account_drawdown_pct)
    assert not kill.drawdown_breached(at_limit + D("0.01"), peak, SEED.account_drawdown_pct)


# --- mark-to-market equity (P5, X9) ---


def _held(
    price: str | None, fresh: bool, stop: str = "90", close: str | None = None
) -> kill.Holding:
    # 10 shares bought at 100 with 5 of fees
    return kill.Holding(
        10,
        D("-1005"),
        D(stop),
        None if price is None else D(price),
        fresh,
        None if close is None else D(close),
    )


def test_mark_fresh_trade_is_the_mark() -> None:
    assert kill.mark_of(_held("80", True, close="95")) == (D("80"), kill.MARK_TRADE)
    assert kill.open_pnl([_held("110", True)]) == (D("95"), {"close": 0, "stop": 0})


def test_mark_before_the_first_trade_is_the_previous_close() -> None:
    """D210: no trade yet this session marks at the previous close, above or below the stop; a
    trade gone stale keeps the conservative basis even with a close known; no close: the stop."""
    assert kill.mark_of(_held(None, False, close="104")) == (D("104"), kill.MARK_CLOSE)
    assert kill.mark_of(_held(None, False, close="85")) == (D("85"), kill.MARK_CLOSE)
    assert kill.mark_of(_held("120", False, close="104")) == (D("90"), kill.MARK_STOP)
    assert kill.mark_of(_held(None, False, close="NaN")) == (D("90"), kill.MARK_STOP)
    held = [_held(None, False, close="104"), _held(None, False), _held("110", True)]
    assert kill.open_pnl(held)[1] == {"close": 1, "stop": 1}


def test_mark_stale_or_missing_never_higher() -> None:
    """A stale trade above the stop marks at the stop; one below it marks at the trade; no trade
    marks at the stop; nothing known marks at zero. Never above what is known."""
    assert kill.mark_of(_held("120", False)) == (D("90"), kill.MARK_STOP)
    assert kill.mark_of(_held("70", False)) == (D("70"), kill.MARK_STOP)
    assert kill.mark_of(_held(None, False)) == (D("90"), kill.MARK_STOP)
    assert kill.mark_of(_held("NaN", True, stop="0")) == (D("0"), kill.MARK_STOP)
    assert kill.open_pnl([_held("110", True), _held(None, False)]) == (
        D("95") + D("-105"),
        {"close": 0, "stop": 1},
    )


def test_live_point_peak() -> None:
    """Before any snapshot the peak is the capital; after one, its peak restated for a change of
    weight, while a change of the funds total (a realised result, Q1) is measured; never below
    the equity."""
    at = datetime(2026, 10, 23, 6, 0, tzinfo=UTC)
    now = kill.SnapshotPoint(at, D("95000"), D("100000"), D("0.5"))
    assert kill.live_point(None, D(0), now).peak == D("100000")
    last = kill.SnapshotPoint(at, D("100000"), D("100000"), D("0.5"))
    same = kill.SnapshotPoint(at, D("99000"), D("100000"), D("0.5"))
    assert kill.live_point(last, D("110000"), same).peak == D("110000")
    halved = kill.live_point(
        last, D("110000"), kill.SnapshotPoint(at, D("49000"), D("50000"), D("0.25"))
    )
    assert halved.peak == D("55000")
    assert kill.drawdown_breached(halved.equity, halved.peak, D("0.10"))
    # the same weight on a funds total cut by a realised loss: the peak stays, the loss counts
    lost = kill.live_point(
        last, D("100000"), kill.SnapshotPoint(at, D("90000"), D("90000"), D("0.5"))
    )
    assert lost.peak == D("100000")
    assert kill.drawdown_breached(lost.equity, lost.peak, D("0.10"))
    risen = kill.SnapshotPoint(at, D("120000"), D("100000"), D("0.5"))
    assert kill.live_point(last, D("110000"), risen).peak == D("120000")


def test_peak_restarts_at_capital_after_zero_allocation() -> None:
    """A sleeve back from a zero allocation measures from its new capital, as a new sleeve does:
    a 10% loss on its first day trips, in the live point and in the restated series."""
    at = datetime(2026, 10, 23, 6, 0, tzinfo=UTC)
    benched = kill.SnapshotPoint(at, D("0"), D("0"), D("0"))
    back = kill.SnapshotPoint(at + timedelta(days=1), D("90000"), D("100000"), D("0.5"))
    live = kill.live_point(benched, D("0"), back)
    assert live.peak == D("100000")
    assert kill.drawdown_breached(live.equity, live.peak, D("0.10"))
    series = kill.adjusted_series([benched, back])
    assert [(p.equity, p.peak) for p in series] == [(D("90000"), D("100000"))]


def test_restated_capital() -> None:
    """The previous capital at the new funds total: capital x previous weight / weight; a new
    weight of 0 keeps the previous capital; anything unknown is None."""
    assert kill.restated_capital(D("100000"), D("0.5"), D("45000"), D("0.25")) == D("90000")
    assert kill.restated_capital(D("100000"), D("0.5"), D("0"), D("0")) == D("100000")
    assert kill.restated_capital(D("0"), D("0"), D("50000"), D("0.5")) == D("0")
    assert kill.restated_capital(D("100000"), None, D("50000"), D("0.5")) is None
    assert kill.restated_capital(None, D("0.5"), D("50000"), D("0.5")) is None


# --- increase or decrease ---

# field path -> (a more permissive value, a more restrictive value), written out literally from the
# table; a higher number is more permissive except for `up_after_wins`.
PATHS = {
    ("risk_pct",): ("0.003", "0.002"),
    ("p_max", S1): ("0.25", "0.15"),
    ("p_max", S2): ("0.30", "0.20"),
    ("adv_cap_pct",): ("0.02", "0.005"),
    ("global_open_risk_pct",): ("0.04", "0.01"),
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
    """Allowing a multiplier above 1 or a larger base weight is an increase."""
    doc = seed_policy().to_json()
    if name == "multiplier_bounds.hi":
        doc["multiplier_bounds"]["hi"] = "1.5"
    else:
        doc["base_weight"][name.split(".")[1]] = "0.75"
    assert classify_direction(SEED, parse_policy(doc)) == "increase"


# --- the evidence gate ---

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


# --- kill switches ---


@pytest.mark.parametrize(
    ("state", "withdraws"),
    [
        (kill.KillState(), False),
        (kill.KillState(account_tripped=True), True),  # drawdown, manual or a flatten
        (kill.KillState(bot_tripped=True), True),
        (kill.KillState(paused=True), True),
        (kill.KillState(health_tripped=True), False),  # clears itself; placement re-checks
    ],
)
def test_withdraws_entries(state: kill.KillState, withdraws: bool) -> None:
    """A risk-channel kill or a pause withdraws the bot's working entries; each is manage-only."""
    assert state.withdraws_entries is withdraws
    assert state.manage_only or not withdraws
