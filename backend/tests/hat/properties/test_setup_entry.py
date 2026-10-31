"""The setup entry orders (decisions.md "Setup entry orders"): the buy stop-limit (`stop_order`)
and the next-session entry (`next_session`) as live fires them on a trade and the backtest
replays or estimates them, the entry day's lows after a gap over, and the audit of a volume rule
judged on the full day's volume."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal

import hat_trigger_support as support
import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.hat.backtest import run_audit
from app.hat.backtest.fillrules import (
    SKIP_GAP_NO_INTRADAY,
    entry_day_lows,
    pre_break_low,
    stop_order_fill,
)
from app.hat.backtest.fills import STOP_SLIPPAGE
from app.hat.backtest.intraday import bar_ticks, replay
from app.hat.data.bars import Bar, BarBuilder, Tick
from app.hat.engine.criteria.live import PriceBreaksLevelParams, price_breaks_level
from app.hat.engine.fill_0920 import gap_over_fill
from app.hat.engine.strategy.config import Playbook
from app.hat.engine.trigger import (
    ENTRY_BAND_PCT,
    DailyInputs,
    TriggerMonitor,
    chase_limit,
    daily_context,
    first_above,
    stop_limit,
)
from app.hat.execution.entry_timeout import break_failed
from app.hat.execution.router import past_limit
from app.hat.time import IST

D = Decimal
PRICE = st.integers(100, 100_000).map(lambda n: D(n) / 100)
TICKS = st.sampled_from([D("0.01"), D("0.05"), D("0.10")])
DAY = support.DAY
TICK = D("0.05")


def _grid_above(level: D, tick: D) -> D:
    """The smallest grid price above `level`, stated without the code's formula."""
    return next(p for p in (level - level % tick + n * tick for n in (0, 1, 2)) if p > level)


# --- prices ---------------------------------------------------------------


@given(PRICE, TICKS)
@example(D(12), D("0.05"))
@example(D("12.01"), D("0.05"))
def test_first_above(level: D, tick: D) -> None:
    """A buy stop's trigger is the first grid price strictly above the level. Harm: a stop at
    the level fills on a touch live never trades through."""
    assert first_above(level, tick) == _grid_above(level, tick)


@given(PRICE, st.sampled_from([D(1), D(2), D(5)]), TICKS)
def test_chase_limit(level: D, pct: D, tick: D) -> None:
    """A stop order's LIMIT is the highest grid price `max_chase` allows: within the chase, and
    one tick more is past it. Harm: the order fills past the chase the backtest refuses."""
    limit = chase_limit(level, pct, tick)
    assert limit % tick == 0
    assert limit * 100 <= level * (100 + pct)
    assert (limit + tick) * 100 > level * (100 + pct)


@given(PRICE, st.integers(0, 1000), TICKS)
@example(D(100), 20, D("0.05"))  # the chase binds below the band
def test_stop_limit(entry: D, chase_bp: int, tick: D) -> None:
    """A stop order's LIMIT is at least the triggering trade's price, at most that price plus
    `ENTRY_BAND_PCT` and at most the max-chase price (above the trade), on the grid when it is
    not the trade's price. So a fill, sized at the trade's price, costs at most the band more per
    share. Harm: a late placement fills up to the chase with the risk and cash sized below it."""
    entry = entry - entry % tick  # a traded price is on the grid
    if entry <= 0:
        return
    chase = entry * (10_000 + chase_bp) / 10_000 // tick * tick  # on the grid, as `chase_limit`
    limit = stop_limit(entry, chase, tick)
    assert entry <= limit <= max(entry, chase)
    assert limit * 100 <= entry * (100 + ENTRY_BAND_PCT)
    assert limit % tick == 0
    assert limit == max(entry, min(chase, entry * (100 + ENTRY_BAND_PCT) / 100) // tick * tick)


def test_past_limit() -> None:
    """A stop order whose live price is above its LIMIT is refused at placement and at a Take;
    another entry (no LIMIT of its own) or an unknown price is not judged here. Harm: a late
    Take places an order that fills above the size it was given, or never fills."""
    assert past_limit(D("100.60"), D("100.55"))
    assert not past_limit(D("100.55"), D("100.55"))
    assert not past_limit(D(200), None)
    assert not past_limit(None, D("100.55"))


@pytest.mark.parametrize(
    ("triggered", "closes", "failed"),
    [
        # a trade at 09:20:00: the 09:15 bar (final 09:20:02) is in progress, never counts
        ((9, 20, 0), [((9, 20, 2), "99")], False),
        ((9, 20, 0), [((9, 20, 2), "99"), ((9, 25, 2), "99")], True),  # the 09:20 bar does
        # a trade at 09:22: the 09:20 bar it fell in never counts; the 09:25 bar does
        ((9, 22, 0), [((9, 25, 2), "99")], False),
        ((9, 22, 0), [((9, 30, 2), "99")], True),
        # a 5-minute trigger (the 09:20 bar's final instant): its own bar never, the next does
        ((9, 25, 2), [((9, 25, 2), "99")], False),
        ((9, 25, 2), [((9, 30, 2), "99")], True),
        ((9, 25, 2), [((9, 30, 2), "100")], False),  # a close at the level holds
    ],
)
def test_break_failed_counts_bars_after_the_trigger(
    triggered: tuple[int, int, int], closes: list, failed: bool
) -> None:
    """Only a bar that started after the trigger fails the break: a trade-fired entry at 09:20
    is not refused for the 09:15 bar's close, which the backtest never reads. Harm: live refuses
    or cancels entries the backtest buys."""
    at = support.ist(DAY, *triggered)
    seen = [(support.ist(DAY, *t), D(c)) for t, c in closes]
    assert break_failed(D(100), at, seen) is failed


@given(PRICE, PRICE, PRICE, TICKS)
@example(D(10), D(12), D(12), D("0.05"))  # the high at the level: no fill
@example(D(10), D("12.05"), D(12), D("0.05"))  # reached exactly: the trigger, not past the high
@example(D(13), D(14), D(12), D("0.05"))  # a gap over: the 09:20 estimate
def test_stop_order_fill(open_: D, high: D, level: D, tick: D) -> None:
    """The daily estimate of a stop order: a session opening above the level buys at 09:20
    (`gap_over_fill`); else one whose high reaches the trigger (the first grid price above the
    level) buys at the trigger moved up by the stop slippage, up to the grid, never above the
    high nor below the trigger; else nothing. Harm: the backtest buys at the level a resting stop
    never fills at, or above anything that traded."""
    high = max(high, open_)
    fill = stop_order_fill(open_, high, level, tick)
    if open_ > level:
        assert fill == gap_over_fill(open_, high)
        return
    trigger = _grid_above(level, tick)
    if high < trigger:
        assert fill is None
        return
    moved = (trigger * (1 + STOP_SLIPPAGE) / tick).to_integral_value(ROUND_CEILING) * tick
    assert fill == min(high, moved)
    assert trigger <= fill <= high
    assert stop_order_fill(open_, high, level, None) is None  # no tick, no trigger


# --- the entry day's lows (L5) -----------------------------------------------


ADR = st.none() | st.integers(0, 2000).map(lambda n: D(n) / 100)


@given(PRICE, PRICE, PRICE, ADR, st.booleans())
@example(D(13), D(11), D(12), D(5), True)  # a gap over with a low-of-day stop
@example(D(13), D(11), D(12), D(5), False)  # a gap over: the day's low after the fill
def test_entry_day_lows(open_: D, low: D, level: D, adr20: D | None, low_of_day: bool) -> None:
    """A session that opened above the level gapped over it, though its 09:20 fill is not the
    open: a low-of-day stop is refused (`gap_over_no_intraday`) and any other stop sees the
    whole day's low after the fill. Otherwise the lows are `pre_break_low`'s at the entry price,
    a low-of-day stop without adr20 refused. Harm: a gap-over entry plans its low-of-day stop
    from a low it never knew, or its stop misses the day's low below it."""
    low = min(low, open_)
    if open_ > level:
        price = gap_over_fill(open_, open_ * 2)
        found = entry_day_lows((open_, low), level, price, adr20, low_of_day=low_of_day)
        if low_of_day:
            assert found == SKIP_GAP_NO_INTRADAY
        else:
            assert found == (pre_break_low(open_, low, level, adr20)[0], low)
        return
    price = level + TICK
    found = entry_day_lows((open_, low), level, price, adr20, low_of_day=low_of_day)
    if low_of_day and adr20 is None:
        assert found == "stop_input_missing"
    else:
        assert found == pre_break_low(open_, low, price, adr20)


# --- the level trigger ---------------------------------------------------------


LEVEL = D(100)


def _breaks(timing: str, *, high: D, close: D, prev_close: D | None) -> bool:
    params = PriceBreaksLevelParams(level="pivot", entry_timing=timing)
    daily = DailyInputs(tick=TICK, prev_close=prev_close)
    ctx = daily_context(support.ist(DAY, 10, 0), daily, {"pivot": LEVEL})
    ctx = ctx.__class__(**{**ctx.__dict__, "bar_high": high, "bar_close": close})
    return price_breaks_level(ctx, params).passed


def test_price_breaks_level_timings() -> None:
    """A stop order breaks on a trade above the level, whatever the close (it has no close); a
    next-session entry breaks on a previous close above the level, whatever trades now; the
    intraday break still needs its close. Harm: a stop order waits for a close live never
    waits for, or a next-session entry buys a setup that did not close above its level."""
    assert _breaks("stop_order", high=D("100.05"), close=D(99), prev_close=None)
    assert not _breaks("stop_order", high=D(100), close=D(100), prev_close=None)
    assert not _breaks("intraday_break", high=D("100.05"), close=D(99), prev_close=None)
    assert _breaks("next_session", high=D(90), close=D(90), prev_close=D("100.05"))
    assert not _breaks("next_session", high=D(110), close=D(110), prev_close=D(100))
    assert not _breaks("next_session", high=D(110), close=D(110), prev_close=None)


def test_tick_timings_refused_where_they_cannot_run() -> None:
    """A stop order or a next-session entry reads no level of the session's own bars (the
    opening range) and no breakout volume, which only a close entry knows. Harm: a config that
    can never fire live, or reads the full day's volume."""
    for timing in ("stop_order", "next_session"):
        with pytest.raises(ValidationError, match="tick_entry_needs_setup_level"):
            support.playbook(
                trigger=[
                    {
                        "type": "price_breaks_level",
                        "params": {"level": "opening_range_high", "entry_timing": timing},
                    }
                ]
            )
        with pytest.raises(ValidationError, match="breakout_volume_needs_daily_close"):
            PriceBreaksLevelParams(level="pivot", entry_timing=timing, volume_vs_avg=1.5)


# --- the monitor on trades -------------------------------------------------------


def _book(timing: str, chase: float = 2.0, name: str = "pb") -> Playbook:
    return support.playbook(
        trigger=[
            {"type": "price_breaks_level", "params": {"level": "pivot", "entry_timing": timing}}
        ],
        filters=[{"type": "max_chase", "params": {"pct": chase}}],
        name=name,
    )


def _tick(hh: int, mm: int, ss: int, price: str) -> Tick:
    return Tick("AAA", support.ist(DAY, hh, mm, ss), D(price), 1)


def test_stop_order_fires_on_the_first_trade_above_the_level() -> None:
    """A stop order fires on the first trade above its level from 09:20 to 15:05, at that
    trade's price, with its LIMIT at the max-chase price; never on a trade before 09:20, never
    on a final bar, and a 5-minute break candidate never on a trade. Harm: live waits for a bar
    close the backtest does not model, or buys a break before 09:20."""
    builder = BarBuilder()
    monitor = TriggerMonitor(builder)
    stop = support.candidate(_book("stop_order"), pivot=D(100))
    bar_break = support.candidate(_book("intraday_break", name="bars"), pivot=D(100))
    monitor.add(stop)
    monitor.add(bar_break)
    assert monitor.on_tick(_tick(9, 18, 0, "101")) == []  # before the window
    assert monitor.on_tick(_tick(9, 21, 0, "100")) == []  # at the level, not above it
    bar = Bar("AAA", support.ist(DAY, 9, 20), D(100), D(101), D(99), D("100.50"), 10)
    assert [t.candidate.key for t in monitor.on_bar(bar)] == [bar_break.key]
    (fired,) = monitor.on_tick(_tick(9, 25, 0, "100.05"))
    assert fired.candidate.key == stop.key
    assert fired.at == support.ist(DAY, 9, 25)
    assert fired.plan.entry == D("100.05")
    assert fired.plan.limit == D("100.55")  # 0.5% over the trade, on the grid, within the chase
    assert fired.plan.skip is None
    monitor.confirm(stop.key)
    assert monitor.on_tick(_tick(9, 26, 0, "100.10")) == []  # once a day


def test_next_session_fires_on_the_first_trade_of_the_window() -> None:
    """A next-session entry fires on the first trade from 09:20 after a previous close above
    its level, at that trade's price, and `max_chase` judges that price. Harm: the entry buys
    before 09:20, or past the chase the backtest refuses."""
    daily = DailyInputs(tick=TICK, band_known=True, band_pct=None, prev_close=D(101))
    monitor = TriggerMonitor(BarBuilder())
    monitor.add(support.candidate(_book("next_session"), pivot=D(100), daily=daily))
    assert monitor.on_tick(_tick(9, 19, 59, "101")) == []
    (fired,) = monitor.on_tick(_tick(9, 20, 0, "103"))
    assert fired.plan.entry == D(103)
    assert fired.plan.skip == "max_chase"  # 3% over the level at 2%


# --- the replay on 5-minute bars -------------------------------------------------


def _bars(rows: list[tuple[str, str, str, str]]) -> tuple[Bar, ...]:
    start = support.ist(DAY, 9, 15)
    return tuple(
        Bar("AAA", start + timedelta(minutes=5 * n), D(o), D(h), D(lo), D(c), 100)
        for n, (o, h, lo, c) in enumerate(rows)
    )


def test_bar_ticks() -> None:
    """The trades read from a bar: its open at its start, each stop trigger it rose to from the
    open just after (ascending), and the session's first bar's close at 09:20. Harm: the replay
    fills a stop above the price that first traded through it."""
    first, second = _bars([("99", "101", "98", "100.50"), ("100", "103", "99", "102")])
    assert bar_ticks(DAY, first, {D("100.05")}) == [
        Tick("AAA", first.start, D(99)),
        Tick("AAA", first.start + timedelta(microseconds=1), D("100.05")),
        Tick("AAA", support.ist(DAY, 9, 20), D("100.50")),
    ]
    assert bar_ticks(DAY, second, {D("102.05"), D("100.05"), D("104")}) == [
        Tick("AAA", second.start, D(100)),
        Tick("AAA", second.start + timedelta(microseconds=1), D("100.05")),
        Tick("AAA", second.start + timedelta(microseconds=2), D("102.05")),
    ]


@pytest.mark.parametrize(
    ("rows", "at", "entry"),
    [
        # the 09:15 bar closed above the level: bought at that close, at 09:20
        ([("99", "101", "98", "100.50"), ("100", "101", "99", "100")], (9, 20), "100.50"),
        # it broke and fell back: the stop rests and fills at its trigger in the 09:25 bar
        (
            [("99", "101", "98", "99.50"), ("99", "99.90", "98", "99"), ("99", "102", "99", "101")],
            (9, 25),
            "100.05",
        ),
        # a later bar opened above it: bought at that open
        ([("99", "99.50", "98", "99"), ("100.40", "101", "100", "100.60")], (9, 20), "100.40"),
    ],
)
def test_replay_stop_order(
    rows: list[tuple[str, str, str, str]], at: tuple[int, int], entry: str
) -> None:
    """The replay of a stop order on 5-minute bars buys at the 09:15 close when that is above
    the level, else at the stop's trigger or a later bar's open above it. Harm: the backtest
    measures a different order from the one live sends."""
    candidate = support.candidate(_book("stop_order"), pivot=D(100))
    (fired,) = replay(DAY, [candidate], {"AAA": _bars(rows)})
    assert fired.at.astimezone(IST).time().replace(microsecond=0) == time(*at)
    assert fired.plan.entry == D(entry)


def test_replay_next_session() -> None:
    """The replay of a next-session entry buys at the 09:15 bar's close, at 09:20. Harm: the
    backtest buys at the open, before live can."""
    daily = DailyInputs(tick=TICK, band_known=True, band_pct=None, prev_close=D(101))
    candidate = support.candidate(_book("next_session"), pivot=D(100), daily=daily)
    (fired,) = replay(DAY, [candidate], {"AAA": _bars([("102", "103", "101", "101.50")])})
    assert fired.at == datetime.combine(DAY, time(9, 20), tzinfo=IST)
    assert fired.plan.entry == D("101.50")


# --- the audit ----------------------------------------------------------------------


def test_full_day_volume_fails_the_audit() -> None:
    """A volume rule judged on the full day's volume fails the run audit (L1); another decision
    made in hindsight only warns. Harm: a run selected on volume no trigger knew passes."""
    failed = run_audit.check_lookahead({"volume_pace_full_day": 2})
    assert failed["status"] == "fail"
    assert "lookahead_full_day_volume" in [f["code"] for f in failed["findings"]]
    assert run_audit.check_lookahead({"ep_estimate_volume": 1})["status"] == "warn"


def test_late_print_fires_nothing() -> None:
    """A trade timed inside a bar already final is a late print: it fires no stop order, a
    trade in the next bar does. Harm: an entry at a price the market left minutes ago."""
    monitor = TriggerMonitor(BarBuilder())
    stop = support.candidate(_book("stop_order"), pivot=D(100))
    monitor.add(stop)
    monitor.on_bar(Bar("AAA", support.ist(DAY, 9, 20), D(99), D("99.50"), D(98), D(99), 10))
    assert monitor.on_tick(_tick(9, 24, 59, "101")) == []
    (fired,) = monitor.on_tick(_tick(9, 25, 0, "101"))
    assert fired.candidate.key == stop.key


def test_tick_entry_refuses_volume_pace() -> None:
    """A stop order or a next-session entry takes no `volume_pace`: live counts a bar's volume
    up to the trade, the replay cannot. Harm: live and the backtest judge the volume apart."""
    for timing in ("stop_order", "next_session"):
        with pytest.raises(ValidationError, match="tick_entry_no_volume_pace"):
            support.playbook(
                trigger=[
                    {
                        "type": "price_breaks_level",
                        "params": {"level": "pivot", "entry_timing": timing},
                    },
                    {"type": "volume_pace", "params": {"vs_avg50": 1.5}},
                ]
            )
