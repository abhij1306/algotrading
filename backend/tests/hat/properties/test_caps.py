"""PBT caps.* (..076, 100..104; ):
the caps, invariants, kill thresholds and policy-change rules, on the real
`app.hat.risk.service.evaluate_signal` and the pure rules it composes. Money is Decimal."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest
from hat_risk_support import (
    AT,
    S1,
    S2,
    TRADE_DAY,
    D,
    policy_with,
    valid_ctx,
)
from hypothesis import given
from hypothesis import strategies as st

from app.hat.risk import kill
from app.hat.risk.decision import Reason
from app.hat.risk.kill import EquityPoint
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
from app.hat.risk.service import evaluate_signal, price_band_ok
from app.hat.settings import load_settings
from app.hat.time import IST

SEED = seed_policy()
# --- global open risk ---


# --- per-bot caps ---


# --- per-instrument cooldown after a hard stop ---


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


# --- exits are never blocked ---

junk = st.one_of(
    st.none(),
    st.floats(),
    st.integers(),
    st.decimals(allow_nan=True, allow_infinity=True),
    st.text(max_size=4),
    st.booleans(),
    st.lists(st.integers(), max_size=2),
)
# --- the regime gate ---


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


def test_rearm_restarts_the_peak() -> None:
    """D232 (Q41): re-armed at 85 after a peak of 100, the kill measures from 85. A fall to 80
    (20% under the old peak, 5.9% under the base) does not trip; a rise to 90 lifts the peak,
    and only 81 (10% under 90) trips. A capital cut moves equity and peak together."""
    base = kill.SnapshotPoint(AT, D("85"), D("100"))

    def tripped(*equities: str) -> bool:
        points = [
            kill.SnapshotPoint(AT + timedelta(days=n + 1), D(e), D("100"))
            for n, e in enumerate(equities)
        ]
        return kill.tripped_after_rearm(base, points, SEED.bot_drawdown_pct)

    assert not tripped("80")
    assert not tripped("90", "81.01")
    assert tripped("90", "81")
    assert tripped("76.5")
    # the base itself, and a point at or before it, are never read
    old = kill.SnapshotPoint(AT - timedelta(days=1), D("10"), D("100"))
    assert not kill.tripped_after_rearm(base, [old, base], SEED.bot_drawdown_pct)
    # weight 0.5 -> 0.25 with no trade: equity 85 -> 35 is a reallocation, not a fall
    weighted = kill.SnapshotPoint(AT, D("85"), D("100"), D("0.5"))
    cut = kill.SnapshotPoint(AT + timedelta(days=1), D("35"), D("50"), D("0.25"))
    assert not kill.tripped_after_rearm(weighted, [cut], SEED.bot_drawdown_pct)


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


def test_restated_capital() -> None:
    """The previous capital at the new funds total: capital x previous weight / weight; a new
    weight of 0 keeps the previous capital; anything unknown is None."""
    assert kill.restated_capital(D("100000"), D("0.5"), D("45000"), D("0.25")) == D("90000")
    assert kill.restated_capital(D("100000"), D("0.5"), D("0"), D("0")) == D("100000")
    assert kill.restated_capital(D("0"), D("0"), D("50000"), D("0.5")) == D("0")
    assert kill.restated_capital(D("100000"), None, D("50000"), D("0.5")) is None
    assert kill.restated_capital(None, D("0.5"), D("50000"), D("0.5")) is None


# --- no adding to a position ---


# --- invariants 3 to 6 ---


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


# --- HAT's own kill switches are the halt ---


# --- the price band ---


@given(st.sampled_from([D("NaN"), D("Infinity"), D("-1"), D("-Infinity")]))
def test_price_band_width_fails_closed(band: Decimal) -> None:
    """A band width that is not a finite number of at least zero cannot be evaluated: the entry is
    `missing_input`, never an exception and never an unlimited band."""
    assert price_band_ok(D(100), D(100), band) is None
    assert evaluate_signal(valid_ctx(), band_pct=band).reason == Reason.MISSING_INPUT


# --- total, typed, fail closed ---

# --- increase or decrease ---

# field path -> (a more permissive value, a more restrictive value), written out literally from the
# table; a higher number is more permissive except for `up_after_wins`.
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


# --- changes are events ---

USER = load_settings().user_id
PASSING = {
    "live_trades": 60,
    "expectancy_r": "0.4",
    "ci90_low": "0.1",
    "ci90_high": "0.7",
    "backtest_expectancy_r": "0.3",
}
EVENING = time(20, 0)
