"""PBT backtest.* (09): the pure rules of the backtest day loop, against rules written out
independently: the fill proxies, the entry-day and circuit inferences, slot order, and the
recorded fill conventions and their refusals."""

import dataclasses
import uuid
from datetime import date, timedelta
from decimal import Decimal

import hat_backtest_support as s
import polars as pl
import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from app.hat.backtest.data import (
    Bar,
    MarketData,
    MarketView,
    jump_skips,
    membership_cover_refusal,
    point_in_time_members,
    real_shares,
    scan_night_members,
    stopped_trading,
)
from app.hat.backtest.fillrules import (
    NSE_BANDS,
    lower_circuit_locked,
    pre_break_low,
    upper_circuit_locked,
)
from app.hat.backtest.fills import (
    AMBIGUOUS_OPTIMISTIC,
    AMBIGUOUS_PESSIMISTIC,
    AMBIGUOUS_RULES,
    ENTRY_FILL_RULES,
    PENETRATION,
    SUSPENSION_SESSIONS,
    TOUCH,
    entry_fill,
    stop_fill,
)
from app.hat.backtest.slots import SLOT_ORDER_SCORE, SLOT_ORDER_TIME, order_slots, trigger_rank
from app.hat.backtest.spec import (
    REGIME_MULT_POLICY,
    REGIME_MULT_RULES,
    RESEARCH_SLEEVE_WEIGHT,
    BacktestError,
    RunConventions,
    sleeve_weight,
)
from app.hat.confighash import config_hash
from app.hat.costs import DEFAULT_SLIPPAGE_BPS, BacktestFills, load_cost_schedule
from app.hat.data.ticks import scaled_tick, tick_for_date
from app.hat.engine.portfolio.allocator import sleeve_key
from app.hat.engine.portfolio.universe import Interval, members_on
from app.hat.engine.stops import floor_to_tick

D = Decimal
SESSIONS = 34  # 21 warm-up sessions, then 13 simulated ones
PRICE = st.integers(100, 100_000).map(lambda n: D(n) / 100)  # 1.00 .. 1000.00 on the paise grid
DAYS = s.sessions(SESSIONS)


# --- -------------------------------------------------------------------------


FILL_TICKS = st.sampled_from([D("0.01"), D("0.05"), D("0.10")])


@given(PRICE, PRICE, PRICE, FILL_TICKS)
@example(D(10), D(12), D(10), D("0.05"))
@example(D(10), D(12), D(12), D("0.05"))  # touch fills at 12; penetration needs 12.05
@example(D(10), D("12.05"), D(12), D("0.05"))  # penetrated by exactly one tick
@example(D(12), D(13), D(12), D("0.05"))  # an open at the level does not fill at the open
@example(D(10), D("12.05"), D("12.01"), D("0.05"))  # an off-grid level: cleared at 12.05
def test_entry_fill_proxy(open_: D, high: D, level: D, tick: D) -> None:
    """A day opening above the level enters at the open (a gap over) under either rule.
    Otherwise the default `penetration_1_tick` enters at the first tick-grid price above the
    level (level + tick on the grid; the next grid price for an off-grid adjusted level) when
    the high reaches it, as live's first trade above the level, and `touch` at the level when
    the high reaches the
    level; no entry when the high falls short. Penetration never fills where touch does not, nor
    below touch's price. Harm: the backtest buys breakouts live never sees trade, at a better
    price than live pays."""
    high = max(high, open_)
    pen = entry_fill(open_, high, level, tick)
    touch = entry_fill(open_, high, level, tick, TOUCH)
    if open_ > level:
        assert pen == touch == open_
        return
    # the smallest grid price above the level, stated without the code's formula
    above = next(p for p in (level - level % tick + n * tick for n in (0, 1, 2)) if p > level)
    assert above % tick == 0
    assert level < above <= level + tick
    assert pen == (above if high >= above else None)
    assert touch == (level if high >= level else None)
    assert pen is None or (touch is not None and pen >= touch)
    assert entry_fill(open_, high, level, None) is None  # no tick, no penetration


# --- the entry day ---------------------------------------------------------

LOWS = st.lists(st.tuples(PRICE, PRICE), min_size=1, max_size=8)


ADR = st.none() | st.integers(0, 2000).map(lambda n: D(n) / 100)


@given(LOWS, PRICE, PRICE, ADR, st.booleans())
@example([(D(12), D(9)), (D(10), D(8))], D(10), D(9), None, False)
@example([(D(12), D(9))], D(10), D(9), D(5), False)
@example([(D(12), D(9))], D(10), D(9), None, True)
def test_pre_break_low(
    bars: list[tuple[D, D]], open_: D, level: D, adr20: D | None, missed: bool
) -> None:
    """With the session's 5-minute bars, the low before the break is the lowest low of the bars
    read in order until one reaches the level (the last bar when none does), and the low after
    it is the lowest low of the bars after that one, or the day low when the bars miss it (None
    when neither). Without them it is max(day low, min(open, level x
    (1 - adr20/100))), and the day low counts as after the break only when it is lower; with no
    adr20 the whole day's low is before. Either way the lower of the two is the day low. Harm: a
    low-of-day stop reads the day's low after the entry, which live never knows."""
    intraday = [(max(a, b), min(a, b)) for a, b in bars]
    seen: list[D] = []
    for n, (high, low) in enumerate(intraday):
        seen.append(low)
        if high >= level or n == len(intraday) - 1:
            break
    floor = min(low for _, low in intraday)
    bar_low = floor - D("0.01") if missed and floor > D("0.01") else floor
    before, after = pre_break_low(open_, bar_low, level, adr20, intraday)
    rest = [low for _, low in intraday[len(seen) :]] + ([bar_low] if bar_low < floor else [])
    assert before == min(seen)
    assert after == (min(rest) if rest else None)
    assert min(before, before if after is None else after) == bar_low
    day_low = min(open_, *(low for _, low in intraday))
    before, after = pre_break_low(open_, day_low, level, adr20)
    if adr20 is None:
        assert (before, after) == (day_low, None)
        return
    assert before == max(day_low, min(open_, level * (1 - adr20 / 100)))
    assert day_low <= before <= open_
    assert after == (day_low if day_low < before else None)


# --- the per-instrument cooldown -----------------------------------------


TICKS = st.sampled_from([D("0.01"), D("0.05"), D("0.10")])


def _band_upper(prev_close: D, band: D, tick: D) -> D:
    return floor_to_tick(prev_close * (100 + band) / 100, tick)


def _at_band(price: D, high: D, prev_close: D, tick: D) -> bool:
    """The entry is the day's high, above the previous close and within a tick of an NSE band's
    upper price (one above the previous close)."""
    uppers = [_band_upper(prev_close, b, tick) for b in NSE_BANDS]
    near = any(u > prev_close and abs(price - u) <= tick for u in uppers)
    return price == high and price > prev_close and near


@given(PRICE, st.sampled_from(NSE_BANDS), TICKS)
@example(D(100), D(5), D("0.05"))
@example(D(2), D(2), D("0.05"))
def test_upper_circuit_inferred(prev_close: D, band: D, tick: D) -> None:
    """An entry is at the upper circuit when it is the day's high (nothing traded above it),
    above the previous close and within one tick of an NSE band's upper price over the previous
    close (2, 5, 10 or 20%, a band whose floored upper price is above the previous close): an
    entry below the day's high, at or below the previous close, or between bands is not; without
    a previous close or a tick the state is unknown (None). Harm: the backtest buys a stock at its
    upper band, which live cannot buy, or refuses a flat low-priced one live would buy."""
    upper = _band_upper(prev_close, band, tick)
    for price in (upper - tick, upper, upper + tick):
        if price > prev_close and upper > prev_close:
            assert upper_circuit_locked(price, price, prev_close, tick) is True
        assert upper_circuit_locked(price, price + tick, prev_close, tick) is False
    flat = floor_to_tick(prev_close, tick)
    assert upper_circuit_locked(flat, flat, prev_close, tick) is False
    between = _band_upper(prev_close, band - D("0.5"), tick)
    expected = _at_band(between, between, prev_close, tick)
    assert upper_circuit_locked(between, between, prev_close, tick) is expected
    assert upper_circuit_locked(upper, upper, None, tick) is None
    assert upper_circuit_locked(upper, upper, prev_close, None) is None


@given(PRICE, PRICE, PRICE)
@example(D(10), D(9), D(9))
@example(D(8), D(7), D(9))
def test_exit_fill_proxy(open_: D, low: D, stop: D) -> None:
    """A stop fills at min(open, stop) when the day reaches it (a gap through fills at the open)
    and is never better than the stop; a day that stays above it does not fill."""
    low = min(low, open_)
    filled = stop_fill(open_, low, stop)
    if low <= stop:
        assert filled == min(open_, stop)
        assert filled <= stop
    else:
        assert filled is None


# --- recorded fill conventions -----------------------------------------

SLIPPAGES = st.sampled_from([D(0), D("-0"), D(5), DEFAULT_SLIPPAGE_BPS, D("20.0"), D(50)])


SLOT_ORDERS = st.sampled_from([SLOT_ORDER_TIME, SLOT_ORDER_SCORE, "random:0", "random:7"])
CONVENTIONS = st.tuples(
    st.sampled_from(ENTRY_FILL_RULES),
    SLIPPAGES,
    st.sampled_from(["zero", "default"]),
    SLOT_ORDERS,
    st.sampled_from(AMBIGUOUS_RULES),
    st.sampled_from([0, SUSPENSION_SESSIONS, 20]),
    st.sampled_from([None, D("0.5"), D("0.50"), D("0.3")]),
    st.sampled_from(REGIME_MULT_RULES),
)


@given(CONVENTIONS, CONVENTIONS)
def test_fill_conventions_recorded_digest(a: tuple, b: tuple) -> None:
    """Two runs' `config_digest`s are equal exactly when their entry fill rule, slippage (20
    and 20.0 bps alike, -0 and 0 alike), cost model, slot order, ambiguous-day rule,
    bar-less sessions before the markdown, sleeve weight (the policy's 0.50 when none
    is given, 0.5 and 0.50 alike) and regime rule are. Harm: a run under other
    conventions passes for a rerun, or a rerun reads as another convention."""

    def spec(
        rule: str,
        bps: D,
        model: str,
        slots: str,
        ambiguous: str,
        after: int,
        weight: D | None,
        regime_rule: str,
    ) -> str:
        fills = s.zero_fills() if model == "zero" else BacktestFills(load_cost_schedule(), D(0))
        fills = BacktestFills(fills.schedule, bps)
        base = s.spec(s.config(), DAYS, fills=fills)
        run = dataclasses.replace(
            base,
            conventions=RunConventions(
                entry_fill_rule=rule,
                slot_order=slots,
                ambiguous=ambiguous,
                suspension_sessions=after,
                sleeve_weight=weight,
                regime_mult=regime_rule,
            ),
        )
        return config_hash(run.recorded_conventions())  # the run's `config_digest`

    def key(c: tuple) -> tuple:
        return (*c[:6], D("0.5") if c[6] is None else c[6], c[7])

    same = key(a) == key(b)  # Decimal(20) == Decimal("20.0")
    assert (spec(*a) == spec(*b)) is same


# --- slot order on a capped day ---------------------------------------------


def _before(x: tuple[D | None, D | None], y: tuple[D | None, D | None], rule: str) -> int:
    """-1, 0 or 1 as a break of level x[1] from open x[0] is estimated to come before, with or
    after y's under entry rule `rule`, written without the code's key: a gap over (an entry at
    the open: open above the level, or at it under `touch`) first, then the smaller
    (level - open) / open (compared by cross-multiplying), then no trigger."""

    def kind(c: tuple[D | None, D | None]) -> int:
        open_, level = c
        if open_ is None or level is None or open_ <= 0:
            return 2
        return 0 if open_ > level or (open_ == level and rule == TOUCH) else 1

    if kind(x) != kind(y):
        return -1 if kind(x) < kind(y) else 1
    if kind(x) != 1:
        return 0
    left = (x[1] - x[0]) * y[0]  # type: ignore[operator]
    right = (y[1] - y[0]) * x[0]  # type: ignore[operator]
    return (left > right) - (left < right)


CANDIDATE = st.tuples(st.none() | PRICE, st.none() | PRICE)
RULES = st.sampled_from([SLOT_ORDER_TIME, SLOT_ORDER_SCORE]) | st.integers(0, 9).map(
    lambda k: f"random:{k}"
)


@given(st.lists(CANDIDATE, max_size=8), RULES, st.dates(), st.sampled_from(ENTRY_FILL_RULES))
@example(
    [(D(110), D(100)), (D(100), D(101)), (D(105), D(100))],
    SLOT_ORDER_TIME,
    date(2025, 1, 6),
    PENETRATION,
)
@example([(D(100), D(102)), (D(50), D(51))], SLOT_ORDER_TIME, date(2025, 1, 6), PENETRATION)
# under touch both enter at the open, so the score order stands
@example([(D(100), D(100)), (D(101), D(100))], SLOT_ORDER_TIME, date(2025, 1, 6), TOUCH)
def test_slot_order_rank(cands: list[tuple], rule: str, day: date, fill: str) -> None:
    """Under `time` the candidates take the slots in estimated trigger order: every gap over
    (an entry at the open: open above the level, or at it under `touch`) before every break
    during the day, those in ascending (level - open)
    / open, a candidate with no level or open last, and ties in the score order they came in;
    `score` keeps the score order; `random:K` is a permutation fixed by K and the day. Harm: on a
    capped day the backtest gives the slots to the best-scored setups, which live gets only when
    they also break first, so the evidence is better than live can trade."""
    rows = list(enumerate(cands))  # the index is the score order

    def rank(row: tuple[int, tuple]) -> tuple[int, D]:
        return trigger_rank(*row[1], fill)

    got = order_slots(rows, rule, day, rank)
    assert sorted(got) == rows  # a permutation
    if rule == SLOT_ORDER_SCORE:
        assert got == rows
    elif rule == SLOT_ORDER_TIME:
        for (i, x), (j, y) in zip(got, got[1:], strict=False):
            assert _before(x, y, fill) < 0 or (_before(x, y, fill) == 0 and i < j)
    else:
        assert order_slots(rows, rule, day, rank) == got  # reproducible


# --- path pessimism ---------------------------------------------------


def _band_lower(prev_close: D, band: D, tick: D) -> D:
    raw = prev_close * (100 - band) / 100
    return -floor_to_tick(-raw, tick)  # up to the tick grid


@given(PRICE, st.sampled_from(NSE_BANDS), TICKS)
@example(D(100), D(5), D("0.05"))
@example(D("1.01"), D(2), D("0.05"))
def test_lower_circuit_inferred(prev_close: D, band: D, tick: D) -> None:
    """A session is pinned at its lower circuit when it traded at one price all day, below the
    previous close and within one tick of an NSE band's lower price under it (2, 5, 10 or 20%,
    rounded up to the tick): a session that moved, closed at or above the previous close or sat
    between bands is not; without a previous close or a tick the state is unknown (None). Harm:
    the backtest sells at a locked lower band, where live finds no buyer, or carries a stop on
    a day that traded."""
    lower = _band_lower(prev_close, band, tick)

    def at(price: D) -> tuple[D, D, D, D]:
        return (price, price, price, price)

    for price in (lower - tick, lower, lower + tick):
        if 0 < price < prev_close and lower < prev_close:
            assert lower_circuit_locked(at(price), prev_close, tick) is True
        moved = (price + tick, price + tick, price, price)
        assert lower_circuit_locked(moved, prev_close, tick) is False
    assert lower_circuit_locked(at(prev_close), prev_close, tick) is False
    between = _band_lower(prev_close, band - D("0.5"), tick)
    near = any(
        (w := _band_lower(prev_close, b, tick)) < prev_close and abs(between - w) <= tick
        for b in NSE_BANDS
    )
    expected = 0 < between < prev_close and near
    assert lower_circuit_locked(at(between), prev_close, tick) is expected
    assert lower_circuit_locked(at(lower), None, tick) is None
    assert lower_circuit_locked(at(lower), prev_close, None) is None


@given(
    st.text(max_size=12) | st.sampled_from(AMBIGUOUS_RULES), st.integers(-3, 300) | st.booleans()
)
@example("Pessimistic", 5)
@example(AMBIGUOUS_PESSIMISTIC, True)
@example(AMBIGUOUS_OPTIMISTIC, 251)
@example(AMBIGUOUS_OPTIMISTIC, 6)  # past the default: refused for promotion only
def test_path_rules_refused(rule: str, after: int) -> None:
    """A run's ambiguous-day rule is `optimistic` or `pessimistic` and its bar-less sessions
    before the markdown a whole number from 0 to 250; anything else is refused before the run
    (`backtest_bad_ambiguous_rule`, `backtest_bad_suspension`), and a run flagged for promotion
    with more than the default 5 (`backtest_promotion_late_suspension`). Harm: a run under an
    unknown rule reads as the default, a negative count marks a trading stock down, or a
    promotion carries a suspended position at its stale close."""
    good_rule = rule in AMBIGUOUS_RULES
    good_after = not isinstance(after, bool) and 0 <= after <= 250
    if good_rule and good_after:
        RunConventions(ambiguous=rule, suspension_sessions=after)
        promoted = RunConventions(  # a promotion run sizes under `policy`
            ambiguous=rule, suspension_sessions=after, regime_mult=REGIME_MULT_POLICY
        )
        if after <= SUSPENSION_SESSIONS:
            promoted.check_promotion(DEFAULT_SLIPPAGE_BPS)
        else:  # a promotion run marks a bar-less position down no later than the default
            with pytest.raises(BacktestError, match="backtest_promotion_late_suspension"):
                promoted.check_promotion(DEFAULT_SLIPPAGE_BPS)
        return
    code = "backtest_bad_ambiguous_rule" if not good_rule else "backtest_bad_suspension"
    with pytest.raises(BacktestError, match=code):
        RunConventions(ambiguous=rule, suspension_sessions=after)


PROMOTED = RunConventions(regime_mult=REGIME_MULT_POLICY)  # the conventions a promotion uses


def test_regime_table_is_recorded_and_checked() -> None:
    """A run sizes under the regime table it was given (the stored one, C23 E3), records it in
    the hashed conventions, and refuses a table without an exact multiplier for every state."""
    cfg = s.config()
    days = s.sessions(3)
    table = {
        "bull_quiet": D("1.0"),
        "bull_volatile": D("0.6"),
        "correction": D("0.4"),
        "bear": D("0.25"),
    }
    run = dataclasses.replace(s.spec(cfg, days, conventions=PROMOTED), regime_table=table)
    assert run.recorded_conventions()["regime_table"] == {k: str(v) for k, v in table.items()}
    for bad in ({**table, "bear": 0.25}, {k: v for k, v in table.items() if k != "bear"}):
        with pytest.raises(BacktestError, match="backtest_bad_regime_table"):
            dataclasses.replace(run, regime_table=bad)


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"entry_fill_rule": "midpoint"}, "backtest_bad_fill_rule"),
        ({"slot_order": "random:x"}, "backtest_bad_slot_order"),
        ({"ambiguous": "Pessimistic"}, "backtest_bad_ambiguous_rule"),
        ({"suspension_sessions": -1}, "backtest_bad_suspension"),
        ({"suspension_sessions": 5.0}, "backtest_bad_suspension"),
        ({"sleeve_weight": D("1.01")}, "backtest_bad_sleeve_weight"),
        ({"sleeve_weight": 0.5}, "backtest_bad_sleeve_weight"),
        ({"sleeve_weight": D("NaN")}, "backtest_bad_sleeve_weight"),
        ({"regime_mult": "2"}, "backtest_bad_regime_mult"),
    ],
)
def test_conventions_refused_at_construction(fields: dict, code: str) -> None:
    """A convention outside its rule never makes a `RunConventions`."""
    with pytest.raises(BacktestError, match=code):
        RunConventions(**fields)


@pytest.mark.parametrize(
    ("fields", "slippage", "code"),
    [
        ({}, D(0), "backtest_promotion_without_slippage"),
        ({"entry_fill_rule": TOUCH}, DEFAULT_SLIPPAGE_BPS, "backtest_promotion_on_touch"),
        (
            {"slot_order": SLOT_ORDER_SCORE},
            DEFAULT_SLIPPAGE_BPS,
            "backtest_promotion_off_time_order",
        ),
        ({"suspension_sessions": 6}, DEFAULT_SLIPPAGE_BPS, "backtest_promotion_late_suspension"),
        ({"sleeve_weight": D("0.5")}, DEFAULT_SLIPPAGE_BPS, "backtest_promotion_off_base_weight"),
        ({"regime_mult": "1"}, DEFAULT_SLIPPAGE_BPS, "backtest_promotion_regime_variant"),
    ],
)
def test_promotion_conventions_refused(fields: dict, slippage: D, code: str) -> None:
    """A run flagged for promotion is refused at construction under any convention live does
    not trade by; the same conventions make a sensitivity run."""
    conventions = dataclasses.replace(PROMOTED, **fields)
    fills = BacktestFills(load_cost_schedule(), slippage)
    config = s.config()
    with pytest.raises(BacktestError, match=code):
        s.spec(config, DAYS, fills=fills, conventions=conventions, for_promotion=True)
    s.spec(config, DAYS, fills=fills, conventions=conventions)
    PROMOTED.check_promotion(DEFAULT_SLIPPAGE_BPS)


def _sessions(n: int) -> list[date]:
    return [date(2024, 1, 1) + timedelta(days=k) for k in range(n)]


def test_stopped_trading_needs_no_later_bar_and_more_than_the_slack() -> None:
    days = _sessions(31)  # the store's last day is days[30]
    last_bar = {"gone": days[5], "edge": days[10], "past": days[9], "back": days[5]}
    # "edge" is 20 sessions before the end (a suspension within the slack), "past" 21
    assert stopped_trading(last_bar, {"back"}, days, 20) == {"gone": days[5], "past": days[9]}
    assert stopped_trading(last_bar, set(), [], 20) == {}


def test_jump_skips_cover_the_sessions_either_side_of_the_jump() -> None:
    days = _sessions(10)
    assert jump_skips([("a", days[0])], days, 2) == {("a", d) for d in days[:3]}
    assert jump_skips([("a", days[5]), ("b", days[9])], days, 1) == {
        ("a", days[4]),
        ("a", days[5]),
        ("a", days[6]),
        ("b", days[8]),
        ("b", days[9]),
    }


def test_market_view_builds_exact_bars_only_for_tradeable_days() -> None:
    days = _sessions(40)
    rows = [
        # (date, open, high, low, close) in 1/10,000; the 10th bar has no positive low; the
        # closes fall below 250 in February, whose tick is still read from January's last close
        (d, 1_000_000 + n, 1_010_000 + n, 0 if n == 10 else 990_000, 3_205_123 - n * 20_000)
        for n, d in enumerate(days)
    ]
    bars = pl.DataFrame(
        [("x", d, *p, p[-1] / 10_000, p[-1] / 20_000) for d, *p in rows],
        schema={
            "instrument_id": pl.String,
            "date": pl.Date,
            "open_e4": pl.Int64,
            "high_e4": pl.Int64,
            "low_e4": pl.Int64,
            "close_e4": pl.Int64,
            "raw_close": pl.Float64,
            "close": pl.Float64,  # unrounded: a 2:1 split after the period halves it
        },
        orient="row",
    )
    features = pl.DataFrame({"date": [days[0]], "instrument_id": ["x"]})
    market = MarketData(tuple(days), bars, features, {}, (), skipped=frozenset({("x", days[20])}))
    view = MarketView(market, "NIFTY 500")
    assert view.bar("x", days[0]) == Bar(D("100.0000"), D("101.0000"), D("99.0000"), D("320.5123"))
    assert str(view.bar("x", days[1]).open) == "100.0001"  # type: ignore[union-attr]
    assert view.bar("x", days[10]) is None  # not four positive prices
    assert view.bar("x", days[20]) is None  # skipped near a jump
    assert view.bar("y", days[0]) is None
    assert view.real_shares("x", days[5], 11) == 5  # 5.5 real shares, whole 5
    assert view.real_shares("y", days[5], 11) is None
    closes = {d: D(close).scaleb(-4) for d, _, _, _, close in rows}
    assert [b.close for b in view.bars_through("x", days[22], 3)] == [
        closes[days[20]],
        closes[days[21]],
        closes[days[22]],
    ]
    assert view.bars_through("x", days[22], 3, inclusive=False)[-1].close == closes[days[21]]
    assert view.prev_close("x", days[0]) is None
    assert view.last_close("x", days[20]) == (days[19], closes[days[19]])
    assert view.last_close("x", days[21]) == (days[21], closes[days[21]])
    assert view.prev_close("x", days[21]) == closes[days[20]]
    assert view.last_close("x", days[0] - timedelta(days=1)) is None
    assert view.skipped_after("x", days[19], days[21]) == 1
    assert view.skipped_after("x", days[20], days[30]) == 0
    assert view.prev_close("x", days[11]) == closes[days[9]]
    assert view.tick("x", days[38]) == D("0.05")  # January's last close, 260.5123
    for day in days:
        # every raw close sets the tick, the 10th bar's too (live reads every raw close)
        before = [d for d in days if d < day]
        assert view.tick("x", day) == tick_for_date(before, [closes[d] for d in before], day)
    # the same bars adjusted for a later 1:10 split: the slab is still picked from the raw
    # closes (₹260.51 -> 0.05), and the tick is read in the bars' adjusted units
    split = bars.with_columns(pl.col(c) // 10 for c in ("open_e4", "high_e4", "low_e4", "close_e4"))
    adjusted = MarketView(dataclasses.replace(market, bars=split), "NIFTY 500")
    assert adjusted.tick("x", days[38]) == D("0.0050")


def test_scaled_tick_reads_the_tick_in_adjusted_units() -> None:
    assert scaled_tick(D("0.05"), D("123.45"), D("123.45")) == D("0.05")  # no adjustment
    assert scaled_tick(D("0.10"), D("120"), D("1200")) == D("0.0100")  # 1:10 split
    assert scaled_tick(D("0.05"), D("100"), D("300")) == D("0.0167")  # 2:1 bonus, rounded
    assert scaled_tick(D("0.01"), D("0.01"), D("1000")) == D("0.0001")  # never below 0.0001
    assert scaled_tick(D("0.05"), D("100"), D("0")) is None  # no raw close
    assert scaled_tick(D("0.05"), D("100"), None) is None


# --- research mode: the universe, the sleeve weight and real shares ---------------------------


def test_point_in_time_members_enter_and_leave_on_their_dates() -> None:
    """A stock is a member only inside its index interval: one that left is not a candidate
    after its exclusion, one that joined is not before it; a linked older ISIN's interval
    joins its chain's into one membership; an exclude drops a stock, an include makes one a
    member throughout; intervals outside the run are not read."""
    a, b, old_c, c, gone, owner = (uuid.UUID(int=n) for n in (1, 2, 3, 4, 5, 6))
    rows = [
        Interval(a, date(2016, 1, 1)),
        Interval(b, date(2016, 1, 1), date(2019, 3, 29)),
        Interval(old_c, date(2018, 4, 2), date(2021, 6, 1)),
        Interval(c, date(2021, 6, 1)),
        Interval(gone, date(2016, 1, 1), date(2016, 6, 1)),
    ]
    members = point_in_time_members(
        rows,
        include=[owner],
        exclude=[a],
        lineage={str(old_c): str(c)},
        since=date(2017, 1, 2),
        until=date(2024, 12, 31),
    )
    assert members == (
        Interval(b, date(2016, 1, 1), date(2019, 3, 29)),
        Interval(c, date(2018, 4, 2)),
        Interval(owner, date(1900, 1, 1)),
    )
    assert members_on(members, date(2019, 3, 28)) == {b, c, owner}
    assert members_on(members, date(2019, 3, 29)) == {c, owner}
    assert members_on(members, date(2018, 3, 29)) == {b, owner}


def test_history_first_day_members_are_members_the_night_before() -> None:
    """A row starting on the history's first day is a member from before it, so a run starting
    that day scans it on the night before; a row of a later change still starts on its date."""
    a, b = uuid.UUID(int=1), uuid.UUID(int=2)
    members = point_in_time_members(
        [Interval(a, date(2016, 1, 1)), Interval(b, date(2016, 4, 1))],
        include=[],
        exclude=[],
        lineage={},
        since=date(2015, 12, 31),
        until=date(2016, 12, 30),
        history_from=date(2016, 1, 1),
    )
    assert members_on(members, date(2015, 12, 31)) == {a}
    assert members_on(members, date(2016, 4, 1)) == {a, b}


def test_scan_night_members_read_the_trade_dates_members() -> None:
    """The scan for trade date D runs on the session before D: stored as of that night, an
    entrant effective D is a member when D's watchlist is scanned and a stock excluded on D is
    not; an effective date off the calendar moves to the session before it; a member from
    before the history keeps its start; an interval that moves to nothing is dropped."""
    days = [date(2024, 1, d) for d in (1, 2, 3, 4, 5, 8, 9)]
    joins, leaves, held, weekend, before = (uuid.UUID(int=n) for n in (1, 2, 3, 4, 5))
    stored = scan_night_members(
        [
            Interval(joins, date(2024, 1, 4)),
            Interval(leaves, date(1900, 1, 1), date(2024, 1, 4)),
            Interval(held, date(2024, 1, 2), date(2024, 1, 9)),
            Interval(weekend, date(2024, 1, 6), date(2024, 1, 7)),
            Interval(before, date(2023, 12, 1)),
        ],
        days,
    )
    assert stored == (
        Interval(joins, date(2024, 1, 3)),
        Interval(leaves, date(1900, 1, 1), date(2024, 1, 3)),
        Interval(held, date(2024, 1, 1), date(2024, 1, 8)),
        Interval(before, date(2023, 12, 1)),
    )
    # the night of Jan 3 scans the watchlist for Jan 4
    assert members_on(stored, date(2024, 1, 3)) == {joins, held, before}
    assert members_on(stored, date(2024, 1, 2)) == {leaves, held, before}
    # the night of Jan 8 scans Jan 9, the day `held` has left
    assert members_on(stored, date(2024, 1, 8)) == {joins, before}


@pytest.mark.parametrize(
    ("start", "until", "refusal"),
    [
        (date(2016, 1, 1), date(2026, 10, 23), None),
        (date(2015, 12, 31), date(2026, 10, 23), "backtest_starts_before_membership@2016-01-01"),
        (date(2016, 1, 1), date(2026, 10, 26), "backtest_ends_after_membership@2026-10-25"),
    ],
)
def test_membership_cover_refusal(start: date, until: date, refusal: str | None) -> None:
    """A run reads the history only inside its cover, naming the edge it crosses."""
    cover = (date(2016, 1, 1), date(2026, 10, 25))
    assert membership_cover_refusal(cover, start, until) == refusal
    assert membership_cover_refusal(None, start, until) == "backtest_no_index_membership"


@pytest.mark.parametrize(
    ("qty", "factor", "real"),
    [
        (858, 0.2, 171),  # a 5:1 split after the day: 171.6 real shares, whole 171
        (300, 1 / 3, 100),  # a double a hair under the whole number
        (100, 1.0, 100),
        (7, 2.0, 14),  # a consolidation after the day
        (10, float("nan"), None),
        (10, 0.0, None),
    ],
)
def test_real_shares(qty: int, factor: float, real: int | None) -> None:
    assert real_shares(qty, factor) == real


def test_research_run_sizes_an_unweighted_sleeve_and_promotion_refuses_it() -> None:
    """A sleeve the policy gives weight 0 sizes at `RESEARCH_SLEEVE_WEIGHT` in a research run,
    recorded like an explicit weight; a promotion run of it is still refused."""
    version = s.policy_version()
    key = sleeve_key(s.BOT)
    policy = dataclasses.replace(
        version.policy, base_weight={**version.policy.base_weight, key: D(0)}
    )
    unweighted = dataclasses.replace(version, policy=policy)
    run = s.spec(s.config(), DAYS, policy=unweighted)
    assert sleeve_weight(run) == RESEARCH_SLEEVE_WEIGHT
    assert run.recorded_conventions()["sleeve_weight"] == "0.5"
    fills = BacktestFills(load_cost_schedule(), DEFAULT_SLIPPAGE_BPS)
    config = s.config()
    with pytest.raises(BacktestError, match="backtest_no_base_weight"):
        s.spec(
            config,
            DAYS,
            fills=fills,
            policy=unweighted,
            conventions=PROMOTED,
            for_promotion=True,
        )
