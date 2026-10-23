"""PBT backtest.* (09, HAT-BT-010..061): the day loop of `app.hat.engine.backtest` on generated
markets (random walks on a paise grid, `hat_backtest_support`), against rules written out
independently: the live scan run directly, the fill proxies, R and money identities, and the
version and run rules of `publish_version`.

A run takes about a second, so the run-level properties use fewer examples than the profile."""

import dataclasses
import json
import math
import tempfile
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import hat_backtest_support as s
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hat_support import memory_engine
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from app.hat.backtest.costs import DEFAULT_SLIPPAGE_BPS, BacktestFills, load_cost_model
from app.hat.backtest.fills import (
    AMBIGUOUS_OPTIMISTIC,
    AMBIGUOUS_PESSIMISTIC,
    AMBIGUOUS_RULES,
    ENTRY_FILL_RULES,
    FILL_PATH,
    GAP_THROUGH_RULE,
    PENETRATION,
    SUSPENSION_SESSIONS,
    TOUCH,
    TRIGGER_PROXY,
    entry_fill,
    stop_fill,
)
from app.hat.backtest.market import load_market
from app.hat.backtest.slots import SLOT_ORDER_SCORE, SLOT_ORDER_TIME, order_slots, trigger_rank
from app.hat.confighash import config_hash
from app.hat.data.store import (
    DATASET_FEATURES,
    DATASET_SCHEMAS,
    BarRow,
    DataStore,
    IndexRow,
)
from app.hat.data.ticks import tick_for_date
from app.hat.engine.backtest import (
    KNOWN_GAPS,
    NSE_BANDS,
    TOUCH_GAP,
    BacktestError,
    BacktestResult,
    MarketData,
    TradeRow,
    WatchRow,
    check_conventions,
    compute_metrics,
    config_digest,
    industry_on,
    lower_circuit_locked,
    pre_break_low,
    run_backtest,
    trades_parquet,
    upper_circuit_locked,
    with_cooldown,
)
from app.hat.engine.portfolio.universe import Interval
from app.hat.engine.stops import SKIP_BAD_INPUT, SKIP_TOO_WIDE, floor_to_tick, low_of_day
from app.hat.engine.strategy.versions import VersionError, publish_version, seed_default_strategies
from app.hat.models import (
    HatBacktestRun,
    HatIndustry,
    HatInstrument,
    HatStrategyVersion,
    HatUniverseMember,
)
from app.hat.risk.policy import parse_policy
from app.hat.schema import seed_hat_reference

D = Decimal
SESSIONS = 34  # 21 warm-up sessions, then 13 simulated ones
RUNS = settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
CLOSE_EXITS = {"close_below_sma", "time_stop", "partial_after_days", "trailing_swing_low"}
PRICE = st.integers(100, 100_000).map(lambda n: D(n) / 100)  # 1.00 .. 1000.00 on the paise grid
DAYS = s.sessions(SESSIONS)


def _run(data: MarketData, **kw: object) -> BacktestResult:
    return run_backtest(s.spec(s.config(), data.days, **kw), data)  # type: ignore[arg-type]


def _bar(data: MarketData, instrument_id: str, day: date) -> tuple[D, D, D, D]:
    row = data.bars.filter(
        (data.bars["instrument_id"] == instrument_id) & (data.bars["date"] == day)
    ).row(0, named=True)
    return tuple(D(repr(row[k])).quantize(D("0.0001")) for k in ("open", "high", "low", "close"))  # type: ignore[return-value]


def _watch_key(row: object) -> tuple:
    return (row.playbook, str(row.instrument_id), row.pivot, row.planned_stop)  # type: ignore[attr-defined]


def _assert_parity(result: BacktestResult, data: MarketData, day: date) -> None:
    """The run's watchlist for `day` equals the live scan of the session before it."""
    prev = data.days[data.days.index(day) - 1]
    live = sorted(s.scan_watch(s.config(), data, prev), key=_watch_key)
    ran = sorted((w for w in result.watchlists if w.trade_date == day), key=_watch_key)
    assert [_watch_key(w) for w in ran] == [_watch_key(w) for w in live]
    for got, want in zip(ran, live, strict=True):
        assert math.isclose(float(got.score), want.score, abs_tol=1e-6)


# --- HAT-BT-010 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS))
@RUNS
def test_scan_parity(paths: dict) -> None:
    """The watchlist the loop publishes for each simulated date equals the live nightly scan
    (`scan_bot`) of the previous session for the same version, universe and inputs."""
    data = s.market(paths, DAYS)
    result = _run(data)
    for day in DAYS[s.WARM_UP :]:
        _assert_parity(result, data, day)


# --- HAT-BT-011 -------------------------------------------------------------------------------


def _upto(result: BacktestResult, day: date) -> dict[str, list]:
    return {
        "watch": [w for w in result.watchlists if w.trade_date <= day],
        "signals": [x for x in result.signals if x.trade_date <= day],
        "fills": [f for f in result.fills if f.day <= day],
        "trades": [t for t in result.trades if t.exit_date <= day],
        "equity": [e for e in result.equity if e[0] <= day],
    }


@given(
    s.paths(SESSIONS),
    s.paths(SESSIONS),
    st.integers(s.WARM_UP, SESSIONS - 2),
    st.lists(st.integers(900, 1100), min_size=SESSIONS, max_size=SESSIONS),
)
@RUNS
def test_no_lookahead(base: dict, other: dict, cut: int, index: list[int]) -> None:
    """Changing every bar and index close dated after d leaves every decision on or before d
    (watchlists, signals, fills, closed trades, equity) unchanged."""
    day = DAYS[cut]
    mutated = {i: path[: cut + 1] + other.get(i, path)[cut + 1 :] for i, path in base.items()}
    closes = [1000.0 + n for n in range(SESSIONS)]
    changed = closes[: cut + 1] + [float(c) for c in index[cut + 1 :]]
    first = _run(s.market(base, DAYS, index=closes))
    second = _run(s.market(mutated, DAYS, index=changed))
    assert _upto(first, day) == _upto(second, day)


# --- HAT-BT-012 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS), st.integers(s.WARM_UP + 1, SESSIONS - 1), st.booleans())
@RUNS
def test_survivorship_free(paths: dict, gone: int, removed: bool) -> None:
    """An instrument that stops trading at session x is a member, and scanned, before x; no
    fill of it is dated after its last traded bar, and every position in it is closed no later
    than that bar."""
    victim = s.instrument(0)
    cut = {i: (p[:gone] if i == victim else p) for i, p in paths.items()}
    last = DAYS[gone - 1]
    members = [
        Interval(uuid.UUID(i), DAYS[0], DAYS[gone] if (i == victim and removed) else None)
        for i in sorted(cut)
    ]
    data = s.market(cut, DAYS, members=members, last_traded={victim: last})
    result = _run(data)
    assert all(f.day <= last for f in result.fills if f.instrument_id == victim)
    assert all(t.exit_date <= last for t in result.trades if t.instrument_id == victim)
    for n in range(s.WARM_UP, gone):
        _assert_parity(result, data, DAYS[n])


# --- HAT-BT-013 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS))
@RUNS
def test_version_pinned(paths: dict) -> None:
    """Every simulated signal and trade carries the run's strategy and risk-policy versions."""
    result = _run(s.market(paths, DAYS))
    pinned = (s.VERSION, s.policy_version().version_id)
    assert all((x.strategy_version_id, x.risk_policy_version_id) == pinned for x in result.signals)
    assert all((t.strategy_version_id, t.risk_policy_version_id) == pinned for t in result.trades)
    assert result.metrics["strategy_version_id"] == str(s.VERSION)
    assert result.metrics["risk_policy_version_id"] == str(pinned[1])


# --- HAT-BT-020 / 021 -------------------------------------------------------------------------


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


@given(
    s.paths(SESSIONS), st.sampled_from([D("0.5"), D(1), D(5)]), st.sampled_from(ENTRY_FILL_RULES)
)
@RUNS
def test_entry_fill_proxy_chase(paths: dict, chase: D, rule: str) -> None:
    """In a run, every opened entry records its watchlist level as the trigger price
    (HAT-CRIT-119) and filled at the proxy price of that level, within max chase of it: a
    gap-over beyond level x (1 + pct/100) is never bought, under either entry fill rule, and
    every watchlist row the rule fills on its day becomes a signal or a counted skip. The
    generated opens gap up by at most 3%, so the chase is drawn below that too."""
    data = s.market(paths, DAYS)
    cfg = s.config(chase=float(chase))
    spec = dataclasses.replace(s.spec(cfg, DAYS, fills=s.zero_fills()), entry_fill_rule=rule)
    result = run_backtest(spec, data)
    levels = {(w.trade_date, w.instrument_id): w.pivot for w in result.watchlists}
    bought = {(f.day, f.instrument_id): f.price for f in result.fills if f.purpose == "entry"}
    for signal in result.signals:
        if signal.state != "Open":
            continue
        level = levels[(signal.trade_date, signal.instrument_id)]
        assert signal.trigger_price == level  # the signal records the level it broke
        open_, high, _, _ = _bar(data, signal.instrument_id, signal.trade_date)
        price = bought[(signal.trade_date, signal.instrument_id)]
        tick = _tick(data, signal.instrument_id, signal.trade_date)
        assert price == entry_fill(open_, high, level, tick, rule)
        assert price <= level * (1 + chase / 100)
    # Every row the rule fills on its day became a signal or a counted skip: none was dropped.
    triggered = sum(
        row.pivot is not None
        and entry_fill(
            *_bar(data, row.instrument_id, row.trade_date)[:2],
            row.pivot,
            _tick(data, row.instrument_id, row.trade_date),
            rule,
        )
        is not None
        for row in result.watchlists
        if row.trade_date in DAYS[s.WARM_UP :]
    )
    skipped = sum(n for reason, n in result.skips.items() if reason != "no_trigger_level")
    assert triggered == len(result.signals) + skipped


@given(s.paths(SESSIONS), st.sampled_from([1, 30]))
@RUNS
def test_stop_on_instrument_tick(paths: dict, scale: int) -> None:
    """Every opened entry's stop is the live plan on the instrument's NSE tick of the trade date
    (HAT-EXEC-053): entry x 0.95 rounded down to the tick set by the closes before that date.
    Prices are scaled into the 0.01 and the 0.10 slab. Harm: backtest stops sit on a grid the
    live trigger never uses, so backtest and live risk differ."""
    scaled = {
        i: [tuple(round(v * scale, 2) for v in bar) for bar in path] for i, path in paths.items()
    }
    data = s.market(scaled, DAYS)
    result = run_backtest(s.spec(s.config(), DAYS, fills=s.zero_fills()), data)
    for signal in result.signals:
        if signal.state != "Open":
            continue
        prior = data.bars.filter(
            (data.bars["instrument_id"] == signal.instrument_id)
            & (data.bars["date"] < signal.trade_date)
        ).sort("date")
        closes = [D(repr(c)).quantize(D("0.0001")) for c in prior["close"].to_list()]
        tick = tick_for_date(prior["date"].to_list(), closes, signal.trade_date)
        assert tick is not None
        open_, high, _, _ = _bar(data, signal.instrument_id, signal.trade_date)
        entry = entry_fill(
            open_, high, signal.trigger_price, tick
        )  # the trigger price is the level
        assert entry is not None
        assert signal.stop_price == floor_to_tick(entry * D("0.95"), tick)


# --- HAT-BT-020: the entry day (PR 60) ---------------------------------------------------------

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


def _intraday(draw: st.DrawFn, data: MarketData) -> dict[tuple[str, date], list[tuple[D, D]]]:
    """For some simulated sessions, 5-minute (high, low) bars of a path open, low and high in a
    drawn order, close, with prices between: the bars span exactly the day's range."""
    found: dict[tuple[str, date], list[tuple[D, D]]] = {}
    for row in data.bars.filter(data.bars["date"] >= DAYS[s.WARM_UP]).iter_rows(named=True):
        if not draw(st.booleans()):
            continue
        o, h, low, c = (D(repr(row[k])) for k in ("open", "high", "low", "close"))
        turns = [low, h] if draw(st.booleans()) else [h, low]
        points = [o]
        for target in [*turns, c]:
            cents = st.integers(int(low * 100), int(h * 100)).map(lambda n: D(n) / 100)
            points += [*draw(st.lists(cents, max_size=2)), target]
        found[(row["instrument_id"], row["date"])] = [
            (max(a, b), min(a, b)) for a, b in zip(points, points[1:], strict=False)
        ]
    return found


def _tick(data: MarketData, instrument_id: str, day: date) -> D:
    prior = data.bars.filter(
        (data.bars["instrument_id"] == instrument_id) & (data.bars["date"] < day)
    ).sort("date")
    closes = [D(repr(c)).quantize(D("0.0001")) for c in prior["close"].to_list()]
    tick = tick_for_date(prior["date"].to_list(), closes, day)
    assert tick is not None
    return tick


def _adr20(data: MarketData, instrument_id: str, day: date, name: str = "adr20") -> D | None:
    """A feature (adr20 unless `name`) at the close of the session before `day`, as the loop
    reads it."""
    prev = data.days[data.days.index(day) - 1]
    rows = data.features.filter(
        (data.features["instrument_id"] == instrument_id) & (data.features["date"] == prev)
    )[name].to_list()
    return None if not rows or rows[0] is None else D(repr(rows[0])).quantize(D("0.0001"))


@given(s.paths(SESSIONS), st.booleans(), st.data())
@RUNS
def test_entry_day_stop(paths: dict, with_intraday: bool, draw: st.DataObject) -> None:
    """Under a low-of-day stop, every entry's stop is the live plan from the day's low before the
    break (`pre_break_low`); a gap over without 5-minute bars is never entered and counts as a
    `gap_over_no_intraday` skip, and an entry without them needs adr20; a day whose low after
    the trigger bar reaches the stop exits that day at the stop for -1 R (MAE -1), and a higher
    one bounds the MAE.
    Harm: an S2 entry's stop sits at a low the market made after the entry, and the entry day's
    loss is never seen."""
    data = s.market(paths, DAYS)
    if with_intraday:
        data = dataclasses.replace(data, intraday=draw.draw(st.composite(_intraday)(data)))
    blank = draw.draw(st.sets(st.sampled_from(DAYS[s.WARM_UP - 1 :]), max_size=6))
    if blank:  # adr20 missing at some closes (a short history)
        adr20 = pl.when(pl.col("date").is_in(sorted(blank))).then(None).otherwise(pl.col("adr20"))
        data = dataclasses.replace(data, features=data.features.with_columns(adr20.alias("adr20")))
    cfg = s.config(stop={"rule": "low_of_day", "buffer_ticks": 1})
    result = run_backtest(s.spec(cfg, DAYS, fills=s.zero_fills()), data)
    gaps = 0
    for row in result.watchlists:
        if row.pivot is None or row.trade_date not in DAYS[s.WARM_UP :]:
            continue
        open_, high, _, _ = _bar(data, row.instrument_id, row.trade_date)
        no_bars = (row.instrument_id, row.trade_date) not in data.intraday
        tick = _tick(data, row.instrument_id, row.trade_date)
        gaps += entry_fill(open_, high, row.pivot, tick) == open_ and no_bars
    assert result.skips.get("gap_over_no_intraday", 0) == gaps
    trades = {(t.instrument_id, t.entry_date): t for t in result.trades}
    for signal in result.signals:
        day, iid, level = signal.trade_date, signal.instrument_id, signal.trigger_price
        open_, high, low, _ = _bar(data, iid, day)
        intraday = data.intraday.get((iid, day), ())
        entry = entry_fill(open_, high, level, _tick(data, iid, day))
        assert entry is not None
        assert entry != open_ or intraday  # no gap over without 5-minute bars
        adr20 = _adr20(data, iid, day)
        assert intraday or adr20 is not None
        broke = level if entry == open_ else entry  # the break is at the entry price
        before, dip = pre_break_low(open_, low, broke, adr20, intraday)
        assert signal.stop_price == low_of_day(entry, [before], 1, _tick(data, iid, day)).stop
        trade = trades.get((iid, day))
        if trade is None or dip is None:
            continue
        if dip <= trade.initial_stop:  # the MAE stops at the stop's fill
            outcome = (trade.exit_date, trade.exit_reason, trade.r_multiple, trade.mae_r)
            assert outcome == (day, "hard_stop", -1, -1)
        else:
            risk = trade.entry_price - trade.initial_stop
            assert trade.mae_r <= ((dip - trade.entry_price) / risk).quantize(D("0.0001")) + D(
                "1e-4"
            )


@given(s.paths(SESSIONS), st.sampled_from([1.0, 8.0, 50.0]))
@RUNS
def test_contraction_stop_too_wide(paths: dict, max_pct: float) -> None:
    """A contraction stop the scan left empty (the stop at the pivot is too wide, or there is no
    contraction low) skips its trigger as `stop_too_wide`, never `stop_input_missing`: here the
    setup reports no contraction low, so every trigger skips and no signal is made. Harm: the
    run's skip counts blame missing data for a stop the plan refused as too wide."""
    data = s.market(paths, DAYS)
    stop = {"rule": "below_final_contraction_low", "max_pct": max_pct}
    result = run_backtest(s.spec(s.config(stop=stop), DAYS, fills=s.zero_fills()), data)
    triggers = sum(
        row.pivot is not None
        and entry_fill(
            *_bar(data, row.instrument_id, row.trade_date)[:2],
            row.pivot,
            _tick(data, row.instrument_id, row.trade_date),
        )
        is not None
        for row in result.watchlists
        if row.trade_date in DAYS[s.WARM_UP :]
    )
    assert result.signals == ()
    assert result.skips.get(SKIP_BAD_INPUT, 0) == 0
    assert result.skips.get(SKIP_TOO_WIDE, 0) == triggers


# --- HAT-BT-025: entry filters at the trigger (PR 61) -------------------------------------------


@given(
    s.paths(SESSIONS),
    st.sampled_from([0.25, 0.5, 1.0, 2.0]),
    st.sets(st.sampled_from(DAYS[s.WARM_UP - 1 :]), max_size=6),
)
@RUNS
def test_adtr_filter_applied(paths: dict, multiple: float, blank: set[date]) -> None:
    """With an `adtr_filter`, a trigger whose stop is wider than max_multiple x ADTR(20) as of
    the session before (or that has no ADTR) is recorded Rejected with reason `adtr_filter`, as
    live records it, and buys nothing; one within it is never refused for that reason. ADTR is
    blanked at some closes, so a read of the wrong session shows. Harm: the backtest takes
    wide-stop entries live refuses, so its evidence is not the live strategy's."""
    data = s.market(paths, DAYS)
    if blank:
        adtr20 = pl.when(pl.col("date").is_in(sorted(blank))).then(None).otherwise(pl.col("adtr20"))
        data = dataclasses.replace(
            data, features=data.features.with_columns(adtr20.alias("adtr20"))
        )
    cfg = s.config(adtr=multiple, chase=30.0)
    result = run_backtest(s.spec(cfg, DAYS, fills=s.zero_fills()), data)
    bought = {(f.day, f.instrument_id) for f in result.fills if f.purpose == "entry"}
    for signal in result.signals:
        day, iid = signal.trade_date, signal.instrument_id
        open_, high, _, _ = _bar(data, iid, day)
        entry = entry_fill(open_, high, signal.trigger_price, _tick(data, iid, day))
        assert entry is not None
        adtr = _adr20(data, iid, day, "adtr20")
        wide = adtr is None or adtr <= 0 or entry - signal.stop_price > D(repr(multiple)) * adtr
        assert (signal.reason == "adtr_filter") == wide
        if wide:
            assert (signal.state, signal.qty) == ("Rejected", 0)
            assert (day, iid) not in bought


# --- HAT-RISK-036: the per-instrument cooldown (PR 58) -----------------------------------------


@given(s.paths(SESSIONS), st.integers(0, 4), st.sampled_from([1, 2]))
@RUNS
def test_cooldown_applied(paths: dict, n: int, pct: int) -> None:
    """With `cooldown_sessions` N, a trigger of an instrument the bot closed by `hard_stop` on
    that session or one of the N before it is Rejected `cooldown` and buys nothing, and no entry
    opens within N sessions after a hard stop of its instrument; N = 0 refuses nothing for this
    reason, and no signal is refused for a missing stop history. Tight stops make hard stops and
    re-triggers common. Harm: the knob the cooldown sweep turns does nothing, or the backtest
    refuses entries live takes."""
    data = s.market(paths, DAYS)
    cfg = s.config(stop={"rule": "pct_below_entry", "pct": pct}, chase=30.0)
    policy = with_cooldown(s.policy_version(), n)
    result = run_backtest(s.spec(cfg, DAYS, fills=s.zero_fills(), policy=policy), data)
    assert result.metrics["cooldown_sessions"] == n
    stops: dict[str, list[date]] = {}
    for trade in result.trades:
        if trade.exit_reason == "hard_stop":
            stops.setdefault(trade.instrument_id, []).append(trade.exit_date)
    bought = {(f.day, f.instrument_id) for f in result.fills if f.purpose == "entry"}

    def cooling(iid: str, day: date, *, same_day: bool) -> bool:
        """A hard stop of `iid` within N sessions before `day` (on `day` too if `same_day`)."""
        gap = [DAYS.index(day) - DAYS.index(d) for d in stops.get(iid, [])]
        return n > 0 and any((0 if same_day else 1) <= g <= n for g in gap)

    for signal in result.signals:
        day, iid = signal.trade_date, signal.instrument_id
        assert signal.reason not in ("missing_input", "invalid_input")  # the history is passed
        if signal.reason == "cooldown":
            assert cooling(iid, day, same_day=True)
            assert (signal.state, signal.qty) == ("Rejected", 0)
            assert (day, iid) not in bought
        if signal.state != "Rejected":
            assert not cooling(iid, day, same_day=False)


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


def _lock(paths: dict, sessions: set[int], band: float, fade: bool) -> dict:
    """`paths` with each session in `sessions` opening at the upper band over the previous close
    and never trading above it: pinned all day, or (`fade`) falling back 3% by the close; the
    sessions after it move by the same ratio."""
    locked = {}
    for iid, path in paths.items():
        path = [tuple(bar) for bar in path]
        for n in sorted(sessions):
            prev = path[n - 1][3]
            upper = math.floor(prev * (100 + band) + 1e-6) / 100
            low = round(upper * 0.97, 2) if fade else upper
            close = round(upper * 0.98, 2) if fade else upper
            ratio = close / path[n][3]
            path[n] = (upper, upper, low, close)
            path[n + 1 :] = [tuple(round(v * ratio, 2) for v in bar) for bar in path[n + 1 :]]
        locked[iid] = path
    return locked


@given(
    s.paths(SESSIONS),
    st.sets(st.integers(s.WARM_UP, SESSIONS - 1), min_size=1, max_size=4),
    st.sampled_from([5.0, 10.0, 20.0]),
    st.booleans(),
)
@RUNS
def test_upper_circuit_skip(paths: dict, sessions: set[int], band: float, fade: bool) -> None:
    """A trigger whose entry is at the upper band (the day's high, within a tick of a band over
    the previous close), whether the stock stays pinned or falls back later in the day, is
    recorded Rejected by `max_chase` (locked upper circuit), as live refuses it at that price,
    and buys nothing; with a 30% chase no other trigger is refused by `max_chase`. Harm: the
    backtest buys locked gap-ups live never gets, the best days of a momentum strategy."""
    data = s.market(_lock(paths, sessions, band, fade), DAYS)
    result = run_backtest(s.spec(s.config(chase=30.0), DAYS, fills=s.zero_fills()), data)
    bought = {(f.day, f.instrument_id) for f in result.fills if f.purpose == "entry"}
    for signal in result.signals:
        day, iid = signal.trade_date, signal.instrument_id
        open_, high, _, _ = _bar(data, iid, day)
        entry = entry_fill(open_, high, signal.trigger_price, _tick(data, iid, day))
        assert entry is not None
        prev_close = _bar(data, iid, DAYS[DAYS.index(day) - 1])[3]
        locked = _at_band(entry, high, prev_close, _tick(data, iid, day))
        assert (signal.reason == "max_chase") == locked
        if locked:
            assert (signal.state, signal.qty) == ("Rejected", 0)
            assert (day, iid) not in bought


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


@given(s.paths(SESSIONS))
@RUNS
def test_exit_fill_proxy_close(paths: dict) -> None:
    """In a run without slippage, a trade closed by a close-based rule sold its last shares at
    that day's official close, and the 15:25 difference is recorded as a known gap."""
    data = s.market(paths, DAYS)
    result = _run(data, fills=s.zero_fills())
    assert "close_exits_at_official_close" in result.metrics["known_gaps"]
    for trade in result.trades:
        if trade.exit_reason not in CLOSE_EXITS:
            continue
        sells = [
            f
            for f in result.fills
            if f.instrument_id == trade.instrument_id
            and f.side == "sell"
            and f.day == trade.exit_date
        ]
        close = _bar(data, trade.instrument_id, trade.exit_date)[3]
        assert any(f.price == close for f in sells)


# --- HAT-BT-024 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS), st.integers(0, 50).map(D))
@RUNS
def test_decimal_money(paths: dict, slippage: D) -> None:
    """Every trade has a whole quantity of at least 1 and Decimal money; the trades' net P&L sums
    exactly to the change in equity (every position is closed at the end of the period)."""
    result = _run(s.market(paths, DAYS), slippage=slippage)
    for trade in result.trades:
        assert isinstance(trade.qty, int)
        assert trade.qty >= 1
        for value in (trade.entry_price, trade.initial_stop, trade.realized_pnl, trade.fees):
            assert isinstance(value, Decimal)
    assert all(isinstance(v, Decimal) for _, v in result.equity)
    net = sum((t.realized_pnl for t in result.trades), D(0))
    assert result.equity[-1][1] - s.spec(s.config(), DAYS).initial_equity == net


# --- HAT-BT-032 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS))
@RUNS
def test_trades_match_metrics(paths: dict) -> None:
    """The trade count and sum of R in `trades.parquet` equal the run's metrics, and each trade
    carries the score of the watchlist row it entered from (PR 65, N10), in the record and in
    the file. Harm: the confidence buckets (D132) group trades by a score they did not have."""
    result = _run(s.market(paths, DAYS))
    table = pq.read_table(pa.BufferReader(trades_parquet(result.trades)))
    rs = table.column("r_multiple").to_pylist()
    assert table.num_rows == result.metrics["trades"]
    assert str(sum(rs, D(0)).quantize(D("0.000001"))) == result.metrics["sum_r"]
    listed = {(w.trade_date, w.playbook, w.instrument_id): w.score for w in result.watchlists}
    scores = [listed[(t.entry_date, t.playbook, t.instrument_id)] for t in result.trades]
    assert [t.score for t in result.trades] == scores
    assert table.column("score").to_pylist() == [float(x) for x in scores]


# --- HAT-BT-040 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS))
@RUNS
def test_r_and_expectancy(paths: dict) -> None:
    """R = realised P&L / ((entry - initial stop) x quantity); a trade that left whole at its
    initial stop without costs is -1 R; expectancy is the mean R."""
    result = _run(s.market(paths, DAYS), fills=s.zero_fills())
    for t in result.trades:
        risk = (t.entry_price - t.initial_stop) * t.qty
        assert risk > 0
        assert t.r_multiple == (t.realized_pnl / risk).quantize(D("0.000001"))
        if t.realized_pnl == (t.initial_stop - t.entry_price) * t.qty:
            assert t.r_multiple == -1
    if result.trades:
        mean = sum((t.r_multiple for t in result.trades), D(0)) / len(result.trades)
        assert result.metrics["expectancy_r"] == str(mean.quantize(D("0.000001")))
    else:
        assert result.metrics["expectancy_r"] is None


# --- HAT-BT-041 -------------------------------------------------------------------------------


_NO_FEES = D(0)


def _trade(day: int, r: D, n: int, entry: int = 0, fees: D = _NO_FEES) -> TradeRow:
    return TradeRow(
        entry_date=DAYS[entry],
        exit_date=DAYS[day],
        playbook="p",
        instrument_id=s.instrument(n),
        qty=1,
        entry_price=D(100),
        initial_stop=D(90),
        exit_reason="hard_stop",
        realized_pnl=r * 10,
        fees=fees,
        r_multiple=r,
        mfe_r=max(r, D(0)),
        mae_r=min(r, D(0)),
        holding_days=1,
        strategy_version_id=s.VERSION,
        risk_policy_version_id=s.policy_version().version_id,
    )


R_VALUES = st.integers(-300, 500).map(lambda n: D(n) / 100)


@given(
    st.lists(st.tuples(st.integers(0, 9), R_VALUES), max_size=30),
    st.lists(st.integers(1, 3_000_000).map(D), min_size=1, max_size=10),
    st.randoms(use_true_random=False),
)
def test_metrics_bounds(rows: list[tuple[int, D]], curve: list[D], rnd: object) -> None:
    """Max drawdown and win rate are in [0, 1], and the metrics do not depend on the order in
    which trades closing on the same day are listed."""
    trades = sorted(
        (_trade(day, r, n) for n, (day, r) in enumerate(rows)), key=lambda t: t.exit_date
    )
    equity = [(DAYS[n], v) for n, v in enumerate(curve)]
    spec = s.spec(s.config(), DAYS)
    metrics = compute_metrics(trades, equity, spec)
    assert 0 <= metrics["max_drawdown"] <= 1
    assert metrics["win_rate"] is None or 0 <= metrics["win_rate"] <= 1
    shuffled = list(trades)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    shuffled.sort(key=lambda t: t.exit_date)  # stable: same-day trades keep the shuffled order
    assert compute_metrics(shuffled, equity, spec) == metrics


@given(
    st.lists(
        st.tuples(st.integers(0, 9), st.integers(0, 9), R_VALUES, st.integers(0, 500)),
        max_size=30,
    ),
    st.lists(st.integers(1, 3_000_000).map(D), min_size=1, max_size=10),
)
@example([(0, 2, D(-1), 5), (5, 6, D(4), 0)], [D(100), D(90), D(95), D(80), D(120)])
@example([], [D(10) ** 12])  # one session far above the start: the CAGR overflows, so no MAR
def test_robustness_metrics(rows: list[tuple[int, int, D, int]], curve: list[D]) -> None:
    """A run's robustness figures (PR 65) restate its trades and equity: `exposure` is the share
    of its sessions inside some trade's entry..exit span, `longest_drawdown_sessions` the
    longest run of sessions below the running peak of the curve (the initial equity first),
    `mar` the CAGR over the drawdown's depth (both null when the CAGR overflows, never a crash),
    `tail_trimmed_r` the mean R without the best ceil(1, 5, 10 % of the trades), and
    `cost_share` the fees over net P&L + fees. Harm: the tuning report shows a strategy that is idle most of the time, under water for months,
    resting on one outlier trade or eaten by costs as robust."""
    trades = sorted(
        (
            _trade(max(a, b), r, n, entry=min(a, b), fees=D(fee))
            for n, (a, b, r, fee) in enumerate(rows)
        ),
        key=lambda t: t.exit_date,
    )
    equity = [(DAYS[n], v) for n, v in enumerate(curve)]
    spec = s.spec(s.config(), DAYS)
    metrics = compute_metrics(trades, equity, spec)
    days = [d for d, _ in equity]
    inside = [d for d in days if any(t.entry_date <= d <= t.exit_date for t in trades)]
    assert metrics["exposure"] == len(inside) / len(days)
    points = [spec.initial_equity, *curve]
    peaks = [max(points[: n + 1]) for n in range(len(points))]
    under = "".join("1" if v < p else "0" for v, p in zip(points, peaks, strict=True))
    assert metrics["longest_drawdown_sessions"] == max(len(run) for run in under.split("0"))
    depth = metrics["max_drawdown"]
    if depth == 0 or metrics["cagr"] is None:
        assert metrics["mar"] is None
    elif math.isfinite(metrics["cagr"] / depth):
        assert math.isclose(metrics["mar"] * depth, metrics["cagr"], rel_tol=1e-9)
    else:  # the quotient overflows
        assert metrics["mar"] is None
    best_first = sorted((t.r_multiple for t in trades), reverse=True)
    for pct in (1, 5, 10):
        rest = best_first[math.ceil(len(best_first) * pct / 100) :]
        want = str((sum(rest, D(0)) / len(rest)).quantize(D("0.000001"))) if rest else None
        assert metrics["tail_trimmed_r"][f"top_{pct}pct"] == want
    fees = sum((t.fees for t in trades), D(0))
    gross = sum((t.realized_pnl for t in trades), D(0)) + fees
    share = str((fees / gross).quantize(D("0.000001"))) if gross > 0 else None
    assert metrics["cost_share"] == share


# --- HAT-BT-050 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS), st.randoms(use_true_random=False))
@RUNS
def test_deterministic(paths: dict, rnd: object) -> None:
    """Two runs of the same market and config give byte-identical trade files and equal metrics,
    whatever the order of the input rows and mappings."""
    data = s.market(paths, DAYS)
    order = list(range(data.bars.height))
    rnd.shuffle(order)  # type: ignore[attr-defined]
    members = list(data.members)
    rnd.shuffle(members)  # type: ignore[attr-defined]
    shuffled = MarketData(
        days=data.days,
        bars=data.bars[order],
        features=data.features.sample(fraction=1.0, shuffle=True, seed=rnd.randint(0, 999)),  # type: ignore[attr-defined]
        index_closes=data.index_closes,
        members=tuple(members),
        industries=dict(reversed(list(data.industries.items()))),
        series=dict(reversed(list(data.series.items()))),
        last_traded=data.last_traded,
    )
    first, second = _run(data), _run(shuffled)
    assert trades_parquet(first.trades) == trades_parquet(second.trades)
    assert json.dumps(first.metrics, sort_keys=True) == json.dumps(second.metrics, sort_keys=True)
    assert first.metrics["known_gaps"] == list(KNOWN_GAPS)


# --- HAT-BT-020..022: recorded fill conventions (PR 62) -----------------------------------------

SLIPPAGES = st.sampled_from([D(0), D("-0"), D(5), DEFAULT_SLIPPAGE_BPS, D("20.0"), D(50)])


@given(s.paths(SESSIONS), st.sampled_from(ENTRY_FILL_RULES), SLIPPAGES, st.booleans())
@RUNS
@example({s.instrument(0): [(100.0, 101.0, 99.0, 100.0)] * SESSIONS}, PENETRATION, D(0), True)
@example({s.instrument(0): [(100.0, 101.0, 99.0, 100.0)] * SESSIONS}, TOUCH, D(20), True)
def test_fill_conventions_recorded(paths: dict, rule: str, bps: D, promote: bool) -> None:
    """Every run's metrics record its fill path (open, low, high, close), entry fill rule,
    gap-through rule (min(open, stop)), trigger proxy, slippage, cost model, slot order
    (`time` by default, PR 63) and path rules (PR 64: the ambiguous-day rule, `optimistic` by
    default, the lower-circuit carry and the suspension markdown), both as flat keys and as one
    `conventions` block (PR 65), whether it is flagged for promotion, and `config_digest`, the
    hash of exactly those conventions; a
    `touch` run also reports the touch gap. A run flagged for promotion at a slippage of 0, or
    under `touch`, is refused. Harm: runs under different fill conventions read as comparable
    evidence, or a promotion rides on perfect or optimistic fills."""
    fills = BacktestFills(load_cost_model(), bps)
    spec = dataclasses.replace(
        s.spec(s.config(), DAYS, fills=fills), entry_fill_rule=rule, for_promotion=promote
    )
    market = s.market(paths, DAYS)
    if promote and (bps == 0 or rule == TOUCH):
        with pytest.raises(BacktestError, match="backtest_promotion_(without_slippage|on_touch)"):
            run_backtest(spec, market)
        return
    metrics = run_backtest(spec, market).metrics
    recorded = {
        "fill_path": FILL_PATH,
        "entry_fill_rule": rule,
        "gap_through_rule": GAP_THROUGH_RULE,
        "trigger_proxy": TRIGGER_PROXY,
        "slippage_bps": "0" if bps == 0 else format(bps.normalize(), "f"),  # -0 too
        "cost_model": fills.model.to_json(),
        "slot_order": SLOT_ORDER_TIME,  # the default (PR 63)
        "ambiguous": AMBIGUOUS_OPTIMISTIC,  # the defaults of PR 64
        "lower_circuit": "carry_to_next_unlocked_open",
        "suspension_markdown": {"after_sessions": SUSPENSION_SESSIONS, "band_pct": "5"},
    }
    assert {k: metrics[k] for k in recorded} == recorded
    assert metrics["conventions"] == recorded  # the block of PR 65, exactly what is hashed
    assert metrics["for_promotion"] is promote
    assert metrics["config_digest"] == config_hash(recorded)
    assert metrics["known_gaps"] == [*KNOWN_GAPS, *([TOUCH_GAP] if rule == TOUCH else [])]


SLOT_ORDERS = st.sampled_from([SLOT_ORDER_TIME, SLOT_ORDER_SCORE, "random:0", "random:7"])
CONVENTIONS = st.tuples(
    st.sampled_from(ENTRY_FILL_RULES),
    SLIPPAGES,
    st.sampled_from(["zero", "default"]),
    SLOT_ORDERS,
    st.sampled_from(AMBIGUOUS_RULES),
    st.sampled_from([0, SUSPENSION_SESSIONS, 20]),
)


@given(CONVENTIONS, CONVENTIONS)
def test_fill_conventions_recorded_digest(a: tuple, b: tuple) -> None:
    """Two runs' `config_digest`s are equal exactly when their entry fill rule, slippage (20
    and 20.0 bps alike, -0 and 0 alike), cost model, slot order (PR 63), ambiguous-day rule and
    bar-less sessions before the markdown (PR 64) are. Harm: a run under other conventions
    passes for a rerun, or a rerun reads as another convention."""

    def spec(rule: str, bps: D, model: str, slots: str, ambiguous: str, after: int) -> object:
        fills = s.zero_fills() if model == "zero" else BacktestFills(load_cost_model(), D(0))
        fills = BacktestFills(fills.model, bps)
        base = s.spec(s.config(), DAYS, fills=fills)
        return dataclasses.replace(
            base,
            entry_fill_rule=rule,
            slot_order=slots,
            ambiguous=ambiguous,
            suspension_sessions=after,
        )

    same = tuple(a) == tuple(b)  # Decimal(20) == Decimal("20.0")
    assert (config_digest(spec(*a)) == config_digest(spec(*b))) is same  # type: ignore[arg-type]


# --- HAT-BT-026: slot order on a capped day (PR 63) ---------------------------------------------


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


@given(
    st.sampled_from([SLOT_ORDER_TIME, SLOT_ORDER_SCORE, "random:0", "random:123456789"])
    | st.from_regex(r"random:-?[0-9]{0,11}", fullmatch=True)
    | st.text(max_size=12)
)
@example("random:07")  # a second spelling of seed 7
@example("random:\u0663")  # a non-ASCII digit
@example("Time")
def test_slot_order_refused(rule: str) -> None:
    """A run's slot order is `time`, `score` or `random:K` with K a whole number below 10^9
    written in ASCII digits without leading zeros; anything else is refused
    (`backtest_bad_slot_order`) before the run, and a run flagged for promotion is refused under
    any order but `time` (`backtest_promotion_off_time_order`). Harm: two spellings of one rule
    hash apart, a run under an unknown order reads as the default, or a version is promoted on
    a slot allocation live cannot get."""
    seed = rule.removeprefix("random:")
    ok = rule in ("time", "score") or (
        rule.startswith("random:")
        and 0 < len(seed) <= 9
        and all(c in "0123456789" for c in seed)
        and (seed == "0" or not seed.startswith("0"))
    )
    if ok:
        check_conventions(PENETRATION, DEFAULT_SLIPPAGE_BPS, False, rule)
        if rule == SLOT_ORDER_TIME:
            check_conventions(PENETRATION, DEFAULT_SLIPPAGE_BPS, True, rule)
        else:  # a promotion run takes the slots in trigger order only
            with pytest.raises(BacktestError, match="backtest_promotion_off_time_order"):
                check_conventions(PENETRATION, DEFAULT_SLIPPAGE_BPS, True, rule)
    else:
        with pytest.raises(BacktestError, match="backtest_bad_slot_order"):
            check_conventions(PENETRATION, DEFAULT_SLIPPAGE_BPS, False, rule)


def _one_slot() -> object:
    """The seed policy with one open position at most, so most days with a trigger are capped."""
    base = s.policy_version()
    policy = parse_policy({**base.policy.to_json(), "max_open_positions": 1})
    return dataclasses.replace(base, policy=policy)


@given(s.paths(SESSIONS, instruments=6), RULES, st.sampled_from(ENTRY_FILL_RULES))
@RUNS
def test_slot_order_applied(paths: dict, rule: str, fill: str) -> None:
    """In a run with one slot, each day's triggers reach `evaluate_signal` in the run's slot
    order, computed here from the day's watchlist and bars: under `time` by estimated trigger
    time then score, under `score` by score, under `random:K` in the seeded permutation of the
    score order; so no trigger takes the slot after an earlier one of that day was refused
    `max_positions`. The run records the rule. Harm: the backtest fills a capped day's slot
    with a later break than live would take."""
    data = s.market(paths, DAYS)
    cfg = s.config(chase=30.0)
    spec = dataclasses.replace(
        s.spec(cfg, DAYS, fills=s.zero_fills(), policy=_one_slot()),  # type: ignore[arg-type]
        slot_order=rule,
        entry_fill_rule=fill,
    )
    result = run_backtest(spec, data)
    assert result.metrics["slot_order"] == rule
    for day in sorted({sig.trade_date for sig in result.signals}):
        watch = {(w.playbook, w.instrument_id): w for w in result.watchlists if w.trade_date == day}
        signals = [sig for sig in result.signals if sig.trade_date == day]
        rows = [watch[(sig.playbook, sig.instrument_id)] for sig in signals]

        def score_key(w: WatchRow) -> tuple:
            return (-w.score, w.playbook, w.instrument_id)

        def at(w: WatchRow, day: date = day) -> tuple[D | None, D | None]:
            return _bar(data, w.instrument_id, day)[0], w.pivot

        if rule == SLOT_ORDER_TIME:
            for x, y in zip(rows, rows[1:], strict=False):
                order = _before(at(x), at(y), fill)
                assert order < 0 or (order == 0 and score_key(x) < score_key(y))
        elif rule == SLOT_ORDER_SCORE:
            assert rows == sorted(rows, key=score_key)
        else:
            ranked = order_slots(
                sorted(watch.values(), key=score_key), rule, day, lambda _: (0, D(0))
            )
            assert rows == [w for w in ranked if w in rows]
        refused = False
        for sig in signals:
            assert not (refused and sig.state == "Open")
            refused = refused or sig.reason == "max_positions"


# --- HAT-BT-021 / 022: path pessimism (PR 64) ---------------------------------------------------

# A market of one instrument: a flat consolidation at 100 (pivot 101) for the warm-up, then a
# break that enters at 101.01 under penetration, with a 5% stop at 95.95 (1R = 5.06).
_FLAT = [(100.0, 101.0, 99.0, 100.0)] * s.WARM_UP
_BREAK = (100.0, 104.0, 99.5, 103.0)
_ENTRY, _STOP = D("101.01"), D("95.95")
_ONE_R = _ENTRY + (_ENTRY - _STOP)  # the breakeven rule moves the stop to the entry here
_HOLD = (105.0, 106.0, 104.0, 105.0)  # a day no exit rule acts on


def _path_run(path: list[tuple[float, ...]], **kw: object) -> tuple[BacktestResult, MarketData]:
    data = s.market({s.instrument(0): path}, DAYS)
    spec = dataclasses.replace(s.spec(s.config(), DAYS, fills=s.zero_fills()), **kw)
    return run_backtest(spec, data), data


def _sells(result: BacktestResult) -> list:
    return [f for f in result.fills if f.side == "sell"]


@given(
    st.integers(10_400, 11_000),  # the high, in paise: around 1R (106.07), below 2R (111.13)
    st.integers(9_600, 10_400),  # the low, above the initial stop
    st.integers(0, 600),  # the open above the low (kept below 1R)
    st.integers(10_076, 11_000),  # the close, above the 5-session SMA
    st.sampled_from(AMBIGUOUS_RULES),
)
@settings(max_examples=25, deadline=None)
@example(10_750, 10_101, 300, 10_500, AMBIGUOUS_PESSIMISTIC)  # the low at the raised stop
@example(10_750, 10_101, 300, 10_500, AMBIGUOUS_OPTIMISTIC)
@example(10_750, 10_050, 300, 10_090, AMBIGUOUS_OPTIMISTIC)  # the close below it: exits either way
@example(10_500, 10_050, 300, 10_200, AMBIGUOUS_PESSIMISTIC)  # no raise: not ambiguous
def test_raised_stop_dip(high: int, low: int, up: int, close: int, rule: str) -> None:
    """The day after an entry, the breakeven rule raises the stop to the entry on the way up
    when the high reaches 1R. The day is ambiguous exactly when the stop was raised, the low
    reaches the raised stop and the close is above it; every run counts it (`ambiguous_days`).
    The raised stop fills at its price that day when the close is at or below it, or, under
    `pessimistic`, when the low reaches it; otherwise the position is held. Harm: a breakeven
    rule reads as never losing a winner the daily bar cannot show was kept, or a stop the close
    went through is not taken."""
    h, l_, c = D(high) / 100, D(low) / 100, D(close) / 100
    o = min(l_ + D(up) / 100, h, _ONE_R - D("0.01"))
    c = max(min(c, h), l_)
    day = (float(o), float(h), float(l_), float(c))
    result, _ = _path_run([*_FLAT, _BREAK, day, *[_HOLD] * 11], ambiguous=rule)
    trade = result.trades[0]
    assert (trade.entry_price, trade.initial_stop) == (_ENTRY, _STOP)
    raised = h >= _ONE_R
    ambiguous = raised and l_ <= _ENTRY < c
    assert result.metrics["ambiguous_days"] == int(ambiguous)
    assert result.metrics["ambiguous"] == rule
    exits_at_stop = raised and (c <= _ENTRY or (ambiguous and rule == AMBIGUOUS_PESSIMISTIC))
    sold = [f for f in _sells(result) if f.day == DAYS[s.WARM_UP + 1]]
    assert [f.price for f in sold] == ([_ENTRY] if exits_at_stop else [])


@given(s.paths(SESSIONS))
@RUNS
def test_raised_stop_dip_rules_agree(paths: dict) -> None:
    """On a market without an ambiguous day the two ambiguous-day rules make the same trades,
    and each run counts the ambiguous days it saw. Harm: the rule changes a run it has no day
    to act on."""
    data = s.market(paths, DAYS)
    runs = {
        rule: run_backtest(dataclasses.replace(s.spec(s.config(), DAYS), ambiguous=rule), data)
        for rule in AMBIGUOUS_RULES
    }
    counts = {rule: r.metrics["ambiguous_days"] for rule, r in runs.items()}
    assert (counts[AMBIGUOUS_OPTIMISTIC] == 0) is (counts[AMBIGUOUS_PESSIMISTIC] == 0)
    if counts[AMBIGUOUS_OPTIMISTIC] == 0:
        assert runs[AMBIGUOUS_OPTIMISTIC].trades == runs[AMBIGUOUS_PESSIMISTIC].trades


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
    st.sampled_from([D(2), D(5), D(10), D(20)]),
    st.integers(1, 3),
    st.integers(7_000, 10_400),  # the next open, in paise
)
@settings(max_examples=25, deadline=None)
@example(D(10), 1, 9_000)  # pinned below the stop, then an open below it
@example(D(5), 2, 9_650)  # pinned below the stop, then a low through it
@example(D(5), 1, 10_000)  # pinned above the stop
def test_lower_circuit_carry(band: D, locked: int, reopen: int) -> None:
    """The day after an entry the stock is pinned at its lower band (one price all day, a band
    under the previous close) for `locked` sessions. Nothing is sold on a pinned session, and
    each counts in `lower_circuit_days`; the stop waits for the next session not pinned and
    fills there at min(open, stop) when its open or low reaches it. Harm: the backtest
    sells at a locked lower band, where live finds no buyer, so a gap-down crash costs it less
    than it costs live."""
    path: list[tuple[float, ...]] = [*_FLAT, _BREAK]
    prev = D(repr(_BREAK[3]))
    pinned: list[D] = []
    for _ in range(locked):
        lower = _band_lower(prev, band, D("0.01"))
        path.append((float(lower),) * 4)
        pinned.append(lower)
        prev = lower
    o = D(reopen) / 100
    path.append((float(o), float(o + 2), float(o - 1), float(o + 1)))
    path += [_HOLD] * (SESSIONS - len(path))
    result, _ = _path_run(path)
    locked_days = DAYS[s.WARM_UP + 1 : s.WARM_UP + 1 + locked]
    assert result.metrics["lower_circuit_days"] == locked
    assert not [f for f in _sells(result) if f.day in locked_days]
    fill = stop_fill(o, o - 1, _STOP)  # the next day's way down, its low a rupee under the open
    if fill is not None:
        first = [f for f in _sells(result) if f.day == DAYS[s.WARM_UP + 1 + locked]]
        assert [f.price for f in first] == [fill]


@given(st.integers(0, 10), st.integers(0, 12))
@settings(max_examples=25, deadline=None)
@example(10, 0)  # bar-less from the second session after the entry, marked from the next
@example(0, 12)  # bar-less for 12 sessions: carried at the last close
def test_suspension_markdown(traded: int, after: int) -> None:
    """A held stock with no bars after `traded` sessions past its entry, and not delisted, is
    carried at its last close for `after` sessions without a bar, then marked 5% lower per
    further session (floored to the tick, never below it nor above the last close): in the
    equity curve each session and at the period end, where the position closes at that mark
    and its MAE reaches it.
    Harm: a suspended position is carried at a stale price for months, so a stock that never
    trades again reads as worth its last close."""
    path = [*_FLAT, _BREAK, *[_HOLD] * traded]
    result, _ = _path_run(path, suspension_sessions=after)
    last_close = D(repr(path[-1][3]))
    (trade,) = result.trades
    buy = next(f for f in result.fills if f.side == "buy")
    cash = D(result.metrics["initial_equity"]) - buy.qty * buy.price
    seen = len(path) - 1  # the index of the last session with a bar

    def mark(n: int) -> D:
        excess = n - seen - after
        if excess <= 0:
            return last_close
        raw = last_close * D("0.95") ** excess
        return min(last_close, max(floor_to_tick(raw, D("0.01")), D("0.01")))

    for day, value in result.equity:
        n = DAYS.index(day)
        if n >= seen:
            assert value == cash + buy.qty * mark(n)
    assert trade.exit_reason == "end_of_period"
    assert _sells(result)[-1].price == mark(SESSIONS - 1)
    # the excursion reaches the mark it closes at (the MAE is never above it)
    risk = trade.entry_price - trade.initial_stop
    assert trade.mae_r <= ((mark(SESSIONS - 1) - trade.entry_price) / risk).quantize(D("1e-6"))


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
        check_conventions(
            PENETRATION, DEFAULT_SLIPPAGE_BPS, False, ambiguous=rule, suspension_sessions=after
        )
        if after <= SUSPENSION_SESSIONS:
            check_conventions(
                PENETRATION, DEFAULT_SLIPPAGE_BPS, True, ambiguous=rule, suspension_sessions=after
            )
        else:  # a promotion run marks a bar-less position down no later than the default
            with pytest.raises(BacktestError, match="backtest_promotion_late_suspension"):
                check_conventions(
                    PENETRATION,
                    DEFAULT_SLIPPAGE_BPS,
                    True,
                    ambiguous=rule,
                    suspension_sessions=after,
                )
        return
    code = "backtest_bad_ambiguous_rule" if not good_rule else "backtest_bad_suspension"
    with pytest.raises(BacktestError, match=code):
        check_conventions(
            PENETRATION, DEFAULT_SLIPPAGE_BPS, False, ambiguous=rule, suspension_sessions=after
        )


# --- HAT-BT-061 -------------------------------------------------------------------------------

ENGINE = memory_engine()
seed_hat_reference(ENGINE)


@given(
    st.sampled_from(["running", "completed", "failed"]),
    st.booleans(),
    st.booleans(),
    st.sampled_from([SLOT_ORDER_TIME, SLOT_ORDER_SCORE, "random:3", None]),
)
@settings(max_examples=40, deadline=None)
@example("completed", True, False, SLOT_ORDER_TIME)  # a run of the version, not flagged
@example("completed", True, True, None)  # a promotion run recorded before PR 63
@example("completed", True, True, SLOT_ORDER_SCORE)
def test_version_status_requires_run(
    status: str, same_version: bool, promoted: bool, slots: str | None
) -> None:
    """A draft becomes `backtested` only with a completed run of that same version flagged for
    promotion (PR 62) whose slots went in trigger order (`slot_order` `time`, PR 63); a run that
    is still running, failed, of another version, not flagged, in another slot order or recorded
    before PR 63 is refused and leaves the draft a draft."""
    with Session(ENGINE) as session:
        strategy = seed_default_strategies(session, uuid.uuid4())[0]
        draft = HatStrategyVersion(
            strategy_id=strategy.id,
            version=99,
            status="draft",
            config={},
            config_hash="h",
        )
        session.add(draft)
        session.flush()
        run = HatBacktestRun(
            strategy_version_id=draft.id if same_version else uuid.uuid4(),
            risk_policy_version_id=uuid.uuid4(),
            family_key="f",
            period_start=DAYS[0],
            period_end=DAYS[-1],
            data_snapshot={},
            status=status,
            metrics={"for_promotion": promoted, **({} if slots is None else {"slot_order": slots})},
        )
        session.add(run)
        session.flush()
        if status == "completed" and same_version and promoted and slots == SLOT_ORDER_TIME:
            assert publish_version(session, draft.id, backtest_run_id=run.id).status == "backtested"
            assert draft.backtest_run_id == run.id
        else:
            with pytest.raises(VersionError):
                publish_version(session, draft.id, backtest_run_id=run.id)
            assert draft.status == "draft"
            assert draft.backtest_run_id is None
        session.rollback()


# --- HAT-BT-011 / 012: the market loader --------------------------------------------------------

LOAD_DAYS = s.sessions(10)
KINDS = st.sampled_from(["listed", "gone_before", "gone_after"])


def _store_bar(iid: str, n: int, price: D) -> BarRow:
    return BarRow(
        iid,
        f"INE{n:09d}",
        f"S{n}",
        "EQ",
        price,
        price,
        price,
        price,
        None,
        1000,
        None,
        "bhavcopy",
        uuid.UUID(int=7),
    )


@given(
    st.lists(st.tuples(KINDS, st.integers(0, 9), st.booleans()), min_size=1, max_size=3),
    st.integers(2, 7),
)
@settings(max_examples=25, deadline=None)
def test_market_point_in_time(rows: list[tuple[str, int, bool]], end_n: int) -> None:
    """`load_market` reads bars and index closes dated up to the period's end only; an
    instrument delisted by the end has its last bar on or before its delisting as its last
    traded session, and one delisted after the end has none; every industry classification
    comes with its effective date, so the industry in force on a day is never a later
    reclassification (HAT-BT-011, HAT-BT-012)."""
    start, end = LOAD_DAYS[1], LOAD_DAYS[end_n]
    engine = memory_engine()
    universe = uuid.uuid4()
    expected_last: dict[str, date] = {}
    classes: dict[str, list[tuple[date, str]]] = {}
    with tempfile.TemporaryDirectory() as tmp, Session(engine) as session:
        store = DataStore(Path(tmp))
        listed: dict[str, int] = {}  # instrument -> index of its last session
        for n, (kind, k, reclass) in enumerate(rows):
            iid = s.instrument(n)
            last = {
                "listed": len(LOAD_DAYS) - 1,
                "gone_before": max(1, min(k, end_n)),
                "gone_after": end_n + 1 + k % (len(LOAD_DAYS) - 1 - end_n),
            }[kind]
            listed[iid] = last
            session.add(
                HatInstrument(
                    id=uuid.UUID(iid),
                    market_id=uuid.uuid4(),
                    isin=f"INE{n:09d}",
                    series="EQ",
                    symbol=f"S{n}",
                    name=f"S{n}",
                    face_value=D(1),
                    delisted_on=None if kind == "listed" else LOAD_DAYS[last],
                    active=kind == "listed",
                )
            )
            session.add(
                HatUniverseMember(
                    universe_id=universe,
                    instrument_id=uuid.UUID(iid),
                    effective_from=LOAD_DAYS[0],
                    reason="constituent",
                )
            )
            classes[iid] = [(LOAD_DAYS[0], "ind-a")]
            if reclass:
                classes[iid].append((LOAD_DAYS[max(1, k)], "ind-b"))
            for effective_from, industry in classes[iid]:
                session.add(
                    HatIndustry(
                        instrument_id=uuid.UUID(iid),
                        sector="s",
                        industry=industry,
                        effective_from=effective_from,
                    )
                )
            if kind == "gone_before":
                expected_last[iid] = LOAD_DAYS[last]
        session.flush()
        schema = DATASET_SCHEMAS[DATASET_FEATURES]
        for d, day in enumerate(LOAD_DAYS):
            alive = [i for i, last in listed.items() if d <= last]
            if alive:
                store.write_bars(day, [_store_bar(i, n, D(100 + d)) for n, i in enumerate(alive)])
            store.upsert_index(day, [IndexRow(s.INDEX, D(1000), D(1000), D(1000), D(1000 + d))])
            table = pa.Table.from_pylist(
                [{"instrument_id": i} for i in sorted(listed)], schema=schema
            )
            store.write_partition(DATASET_FEATURES, day, "features", table)
        data, _ = load_market(
            store, session, universe_id=universe, index=s.INDEX, start=start, end=end
        )
    assert data.bars["date"].max() <= end
    assert all(d <= end for d, _ in data.index_closes[s.INDEX])
    assert dict(data.last_traded) == expected_last
    for iid, history in classes.items():
        assert sorted(data.industries[iid]) == sorted(history)
        for day in LOAD_DAYS:
            want = [name for since, name in sorted(history) if since <= day][-1]
            assert industry_on(data, iid, day) == want
