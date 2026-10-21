"""PBT bars.* (HAT-DATA-111..118, HAT-EXEC-120): the tick hook, 5-minute bars, the opening range,
volume pace, staleness and its health kill, triggers on final bars, persistence, subscriptions and
gap capture, all on the real code.

Time is explicit: every property passes the instants it wants, so no test reads a clock."""

import tempfile
import threading
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import hat_trigger_support as trigger_support
import pyarrow.parquet as pq
from hat_engine_support import seed_engine_world
from hat_risk_support import valid_ctx, version_of
from hat_support import event_engine, memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.clock import FixedClock
from app.hat.data.bars import Bar, BarBuilder, Tick, TickOutcome, volume_pace
from app.hat.data.bars_store import SCHEMA, BarStore
from app.hat.data.live import EventFeedHealth, LiveData, tick_from_payload
from app.hat.data.preopen import GapSource, PreopenGaps
from app.hat.data.sessions import IST
from app.hat.data.staleness import STALE_AFTER, StalenessMonitor
from app.hat.data.subscriptions import (
    HAT_OWNER,
    LiveSubscriptions,
    SubscriptionInputs,
    SubscriptionRegistry,
    load_inputs,
)
from app.hat.engine.live_entry import DataHealthKill, TriggerService
from app.hat.engine.trigger import DailyInputs, TriggerMonitor
from app.hat.models import HatEvent, HatInstrument, HatPosition, HatSignal, HatWatchlistEntry
from app.hat.risk import controls
from app.hat.risk.policy import seed_policy

DAY = date(2026, 10, 21)
MIDNIGHT = datetime(2026, 10, 21, tzinfo=IST)
US = 1_000_000
OPEN_SOD, CLOSE_SOD = 9 * 3600 + 15 * 60, 15 * 3600 + 30 * 60  # seconds of the IST day
SYMBOLS = ["AAA", "BBB", "CCC"]


def at_us(offset_us: int) -> datetime:
    """The IST instant `offset_us` microseconds after midnight of DAY."""
    return MIDNIGHT + timedelta(microseconds=offset_us)


prices = st.integers(1, 10**7).map(lambda cents: Decimal(cents).scaleb(-4))
quantities = st.integers(0, 10**6)
EDGES = [
    OPEN_SOD * US - 1,
    OPEN_SOD * US,
    (OPEN_SOD + 300) * US - 1,
    (OPEN_SOD + 300) * US,
    CLOSE_SOD * US - 1,
    CLOSE_SOD * US,
    CLOSE_SOD * US + 1,
]
tick_offsets = st.one_of(
    st.integers(9 * 3600 * US, 15 * 3600 * US + 45 * 60 * US), st.sampled_from(EDGES)
)
ticks = st.builds(
    lambda s, o, p, q: Tick(s, at_us(o), p, q),
    st.sampled_from(SYMBOLS),
    tick_offsets,
    prices,
    quantities,
)


def expected_start(tick: Tick) -> datetime | None:
    """The spec's bar rule, by integer arithmetic on seconds of the IST day."""
    local = tick.at.astimezone(IST)
    seconds = local.hour * 3600 + local.minute * 60 + local.second
    if not OPEN_SOD <= seconds < CLOSE_SOD:
        return None
    return MIDNIGHT + timedelta(seconds=OPEN_SOD + 300 * ((seconds - OPEN_SOD) // 300))


def expected_bars(arrivals: Iterable[tuple[int, Tick]]) -> dict[tuple[str, datetime], Bar]:
    """Bars of the given (arrival index, tick) pairs: open and close by exchange time, with the
    arrival index breaking ties; high and low by price; volume by sum."""
    groups: dict[tuple[str, datetime], list[tuple[int, Tick]]] = {}
    for index, tick in arrivals:
        start = expected_start(tick)
        if start is not None:
            groups.setdefault((tick.symbol, start), []).append((index, tick))
    out = {}
    for (symbol, start), members in groups.items():
        ordered = sorted(members, key=lambda m: (m[1].at, m[0]))
        out[(symbol, start)] = Bar(
            symbol,
            start,
            ordered[0][1].price,
            max(t.price for _, t in members),
            min(t.price for _, t in members),
            ordered[-1][1].price,
            sum(t.qty for _, t in members),
        )
    return out


def by_key(bars: Iterable[Bar]) -> dict[tuple[str, datetime], Bar]:
    out: dict[tuple[str, datetime], Bar] = {}
    for bar in bars:
        assert (bar.symbol, bar.start) not in out, "a bar was reported twice"
        out[(bar.symbol, bar.start)] = bar
    return out


# --- alignment and aggregation (HAT-DATA-113) --------------------------------------------------


@given(data=st.data(), sequence=st.lists(ticks, max_size=60))
def test_alignment(data: st.DataObject, sequence: list[Tick]) -> None:
    """For every tick sequence, in any arrival order, each in-session tick is in exactly the bar
    that starts at 09:15 plus 5 minutes times floor((t - 09:15) / 5 min), IST, and each bar's
    OHLCV is the open, max, min, last price and summed volume of its ticks (open and close by
    exchange time). A tick outside [09:15, 15:30) belongs to no bar."""
    ordered = data.draw(st.permutations(sequence))
    arrival_order = list(enumerate(ordered))  # (arrival index, tick)
    builder = BarBuilder()
    for _index, tick in arrival_order:
        outcome = builder.on_tick(tick, tick.at)  # arrives on time: never late
        in_session = expected_start(tick) is not None
        assert (outcome is TickOutcome.ACCEPTED) == in_session
    finals = by_key(builder.advance(MIDNIGHT + timedelta(days=2)))
    assert finals == expected_bars(arrival_order)
    for (_, start), bar in finals.items():
        assert (start - MIDNIGHT - timedelta(seconds=OPEN_SOD)) % timedelta(minutes=5) == timedelta(
            0
        )
        assert bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
    assert builder.open_bar_count() == 0


@st.composite
def arrival_scripts(draw: st.DrawFn) -> list[tuple[str, int, Tick | None, int]]:
    """Events in non-decreasing arrival time: ticks (with a delay between their exchange time
    and their arrival, so some are late) and advance calls. (kind, now_us, tick, index)."""
    now = 9 * 3600 * US + 10 * 60 * US
    script: list[tuple[str, int, Tick | None, int]] = []
    for index in range(draw(st.integers(0, 50))):
        now += draw(st.integers(0, 150 * US))
        if draw(st.integers(0, 3)) == 0:
            script.append(("advance", now, None, index))
            continue
        delay = draw(st.one_of(st.integers(0, 3 * US), st.integers(0, 900 * US)))
        tick = Tick(
            draw(st.sampled_from(SYMBOLS)),
            at_us(max(now - delay, 0)),
            draw(prices),
            draw(quantities),
        )
        script.append(("tick", now, tick, index))
    return script


@given(script=arrival_scripts())
def test_alignment_finality(script: list[tuple[str, int, Tick | None, int]]) -> None:
    """No bar is reported before its end plus 2 seconds, each bar is reported once, and a final
    bar never changes: a tick that arrives at or after the bar's final instant is dropped, so
    the reported bars equal the bars of the on-time ticks only."""
    builder = BarBuilder()
    on_time: list[tuple[int, Tick]] = []
    late = 0
    reported: list[Bar] = []
    for kind, now_us, tick, index in script:
        now = at_us(now_us)
        if kind == "advance":
            for bar in builder.advance(now):
                assert now >= bar.end + timedelta(seconds=2)  # not final one instant earlier
                reported.append(bar)
            continue
        assert tick is not None
        start = expected_start(tick)
        outcome = builder.on_tick(tick, now)
        if start is None:
            assert outcome is TickOutcome.OUTSIDE_SESSION
        elif now >= start + timedelta(minutes=5, seconds=2):
            assert outcome is TickOutcome.LATE
            late += 1
        else:
            assert outcome is TickOutcome.ACCEPTED
            on_time.append((index, tick))
    end = MIDNIGHT + timedelta(days=2)
    reported.extend(builder.advance(end))
    assert by_key(reported) == expected_bars(on_time)
    assert builder.late_ticks == late
    assert builder.advance(end) == []  # nothing is reported twice


@given(
    seconds=st.integers(OPEN_SOD, CLOSE_SOD - 1),
    millis=st.integers(0, 999),
    before=st.integers(0, 10**6 * 400),
)
def test_alignment_boundary_instants(seconds: int, millis: int, before: int) -> None:
    """A bar is not final one microsecond before its end plus 2 s and is final at that instant."""
    builder = BarBuilder()
    tick = Tick("AAA", at_us(seconds * US + millis * 1000), Decimal("10"), 1)
    builder.on_tick(tick, tick.at)
    start = expected_start(tick)
    assert start is not None
    final_at = start + timedelta(minutes=5, seconds=2)
    assert builder.advance(final_at - timedelta(microseconds=1 + before % 1000)) == []
    assert len(builder.advance(final_at)) == 1


# --- opening range (HAT-DATA-114) --------------------------------------------------------------


@given(
    first=st.lists(
        st.tuples(st.integers(OPEN_SOD * US, (OPEN_SOD + 300) * US - 1), prices, quantities),
        max_size=12,
    ),
    later=st.lists(
        st.tuples(st.integers((OPEN_SOD + 300) * US, CLOSE_SOD * US - 1), prices, quantities),
        max_size=12,
    ),
    stragglers=st.lists(
        st.tuples(st.integers(OPEN_SOD * US, (OPEN_SOD + 300) * US - 1), prices, quantities),
        max_size=6,
    ),
)
def test_opening_range_fixed(
    first: list[tuple[int, Decimal, int]],
    later: list[tuple[int, Decimal, int]],
    stragglers: list[tuple[int, Decimal, int]],
) -> None:
    """For every tick sequence, from 09:20:02 on the opening range equals the high and low of the
    first bar and never changes for the rest of the day, even when ticks of the first bar arrive
    afterwards. Before 09:20:02 there is no opening range."""
    builder = BarBuilder()
    for offset, price, qty in first:
        builder.on_tick(Tick("AAA", at_us(offset), price, qty), at_us(offset))
    assert builder.opening_range("AAA", DAY) is None
    assert builder.advance(at_us((OPEN_SOD + 300) * US + 2 * US - 1)) == []
    assert builder.opening_range("AAA", DAY) is None
    builder.advance(at_us((OPEN_SOD + 300) * US + 2 * US))
    fixed = builder.opening_range("AAA", DAY)
    if first:
        assert fixed == (max(p for _, p, _ in first), min(p for _, p, _ in first))
    else:
        assert fixed is None
    now = (OPEN_SOD + 300) * US + 3 * US
    for offset, price, qty in sorted(later, key=lambda row: row[0]):
        now = max(now, offset)
        builder.on_tick(Tick("AAA", at_us(offset), price, qty), at_us(now))
        builder.advance(at_us(now))
        assert builder.opening_range("AAA", DAY) == fixed
    for offset, price, qty in stragglers:  # late ticks of the first bar
        outcome = builder.on_tick(Tick("AAA", at_us(offset), price, qty), at_us(now))
        assert outcome is TickOutcome.LATE
        builder.advance(at_us(now + 400 * US))
        assert builder.opening_range("AAA", DAY) == fixed


# --- volume pace (HAT-DATA-115) ----------------------------------------------------------------


@given(volume=st.integers(0, 10**9), offset=st.integers(0, 24 * 3600 * US))
def test_volume_pace(volume: int, offset: int) -> None:
    """For every instant t in (09:15, 15:30] and cumulative volume V, pace(t) projects V to the
    whole session: pace * (elapsed / 375 min) == V exactly. Outside (09:15, 15:30] there is no
    pace."""
    when = at_us(offset)
    pace = volume_pace(volume, when)
    inside = OPEN_SOD * US < offset <= CLOSE_SOD * US
    assert (pace is not None) == inside
    if pace is not None:
        assert pace * Fraction(offset - OPEN_SOD * US, 375 * 60 * US) == volume
        assert volume_pace(volume + 1, when) > pace  # more volume, more pace


@given(
    sequence=st.lists(
        st.tuples(st.integers(OPEN_SOD * US, CLOSE_SOD * US - 1), quantities), max_size=30
    ),
    probe=st.integers(0, 24 * 3600 * US),
)
def test_volume_pace_builder(sequence: list[tuple[int, int]], probe: int) -> None:
    """The builder never gives a pace before the symbol's first tick of the session, and after it
    the pace is that of the volume of every in-session tick so far (late ticks included)."""
    builder = BarBuilder()
    assert builder.volume_pace("AAA", at_us(probe)) is None
    total = 0
    # only the ticks that have arrived by the probe time: later ones are not yet traded
    for offset, qty in sorted(s for s in sequence if s[0] <= probe):
        builder.on_tick(Tick("AAA", at_us(offset), Decimal(1), qty), at_us(offset))
        total += qty
    assert builder.day_volume("AAA", DAY) == total
    if total or any(s[0] <= probe for s in sequence):
        assert builder.volume_pace("AAA", at_us(probe)) == volume_pace(total, at_us(probe))
    else:
        assert builder.volume_pace("AAA", at_us(probe)) is None


@given(
    start=st.integers(0, 10**6),
    steps=st.lists(st.integers(0, 10**5), min_size=1, max_size=25),
    first_qty=st.integers(0, 5000),
)
def test_tick_volume_delta(start: int, steps: list[int], first_qty: int) -> None:
    """A tick's volume is the change in the cumulative day volume, never negative: over a run of
    ticks the volumes after the first sum to the rise in cumulative volume, and the first tick
    contributes only its own print, not the volume traded before HAT subscribed."""
    last: dict[str, tuple[date, int]] = {}
    arrival = at_us(OPEN_SOD * US)
    cumulative = [start]
    for step in steps:
        cumulative.append(cumulative[-1] + step)
    quantities_out = []
    for index, total in enumerate(cumulative):
        payload = {"symbol": "AAA", "ltp": 10.5, "volume": total}
        if index == 0:
            payload["last_traded_qty"] = first_qty
        tick = tick_from_payload(payload, arrival, last)
        assert tick is not None
        assert tick.qty >= 0
        quantities_out.append(tick.qty)
    assert quantities_out[0] == first_qty
    assert sum(quantities_out[1:]) == cumulative[-1] - cumulative[0]


@given(offsets=st.lists(st.integers(OPEN_SOD * US, CLOSE_SOD * US - 1), max_size=40))
def test_no_store_keeps_no_bars(offsets: list[int]) -> None:
    """Without a bar store, final bars are reported to listeners and then forgotten: nothing
    accumulates for a writer that does not exist. Harm: the long-running loop grows memory
    for the whole session."""
    clock = FixedClock(at_us(OPEN_SOD * US))
    live = LiveData(health=QuietHealth(), is_open=lambda _t: False, clock=clock)
    seen: list[Bar] = []
    live.add_bar_listener(seen.append)
    for offset in sorted(offsets):
        clock.set(at_us(offset))
        live.on_tick({"symbol": "AAA", "ltp": 10.0, "exch_feed_time": clock.now().timestamp()})
        live.step()
    clock.set(at_us(CLOSE_SOD * US + 3600 * US))
    live.step()
    assert live.pending_bars == 0
    assert len(seen) == len({o // (300 * US) for o in offsets})


@given(
    ltq=st.integers(1, 5000),
    previous=st.integers(0, 10**6),
    rise=st.integers(0, 10**6),
    bad_volume=st.sampled_from([None, "", "x", -5]),
)
def test_tick_volume_fallbacks(ltq: int, previous: int, rise: int, bad_volume: object) -> None:
    """A tick with no usable cumulative volume contributes its own last traded quantity, and the
    first tick of a new trade date contributes its own print instead of its total minus the
    previous day's total. Harm: a print is lost from bar and day volume, or a new session's
    opening bar is charged yesterday's volume."""
    last: dict[str, tuple[date, int]] = {}
    day_one = at_us(OPEN_SOD * US)
    day_two = day_one + timedelta(days=1)
    first = tick_from_payload({"symbol": "AAA", "ltp": 10, "volume": previous}, day_one, last)
    assert first is not None
    broken = {"symbol": "AAA", "ltp": 10, "volume": bad_volume, "last_traded_qty": ltq}
    tick = tick_from_payload(broken, day_one, last)
    assert tick is not None
    assert tick.qty == ltq
    new_day = {"symbol": "AAA", "ltp": 10, "volume": previous + rise, "last_traded_qty": ltq}
    tick = tick_from_payload(new_day, day_two, last)
    assert tick is not None
    assert tick.qty == ltq


# --- staleness (HAT-DATA-116) ------------------------------------------------------------------


def is_open(when: datetime) -> bool:
    return OPEN_SOD * US <= (when - MIDNIGHT) // timedelta(microseconds=1) <= CLOSE_SOD * US


class Sink:
    def __init__(self) -> None:
        self.stale: list[datetime] = []
        self.recovered: list[datetime] = []

    def feed_stale(self, now: datetime, silent_for: timedelta | None) -> None:
        self.stale.append(now)

    def feed_recovered(self, now: datetime) -> None:
        self.recovered.append(now)


@given(
    steps=st.lists(
        st.tuples(st.sampled_from(["tick", "check"]), st.integers(0, 45 * US)),
        max_size=40,
    ),
    start=st.integers(8 * 3600 * US, 15 * 3600 * US + 29 * 60 * US),
)
def test_staleness_trip(steps: list[tuple[str, int]], start: int) -> None:
    """For every timeline, the feed is stale at t exactly when t is inside market hours and no
    tick arrived in (t - 30 s, t]; each transition into stale reaches the health sink once and
    each recovery once, and the stale flag always equals that rule at the last instant seen."""
    sink = Sink()
    monitor = StalenessMonitor(sink, is_open)
    now = start
    tick_times: list[int] = []
    samples: list[bool] = []
    for kind, gap in steps:
        now += gap
        when = at_us(now)
        if kind == "tick":
            samples.append(rule(now, tick_times))  # the check made before the tick is applied
            monitor.on_tick(when)
            tick_times.append(now)
            samples.append(rule(now, tick_times))
        else:
            monitor.check(when)
            samples.append(rule(now, tick_times))
        assert monitor.stale == samples[-1]
        assert monitor.is_stale(when) == rule(now, tick_times)
    transitions = list(zip([False, *samples], samples, strict=False))
    assert len(sink.stale) == sum(1 for a, b in transitions if not a and b)
    assert len(sink.recovered) == sum(1 for a, b in transitions if a and not b)


def rule(now_us: int, tick_times: list[int]) -> bool:
    """The spec's rule: inside market hours, no tick in (now - 30 s, now]."""
    if not is_open(at_us(now_us)):
        return False
    return not any(now_us - 30 * US < t <= now_us for t in tick_times)


@example(silence=30 * US)
@example(silence=30 * US - 1)
@example(silence=30 * US + 1)
@given(silence=st.integers(0, 120 * US))
def test_staleness_boundary(silence: int) -> None:
    """A tick followed by `silence` microseconds of nothing is stale once the silence reaches
    exactly 30 seconds and not one microsecond sooner."""
    sink = Sink()
    monitor = StalenessMonitor(sink, is_open)
    first = at_us(10 * 3600 * US)
    monitor.on_tick(first)
    assert monitor.is_stale(first + timedelta(microseconds=silence)) == (silence >= 30 * US)
    assert STALE_AFTER == timedelta(seconds=30)


@settings(max_examples=60)
@given(
    steps=st.lists(
        st.tuples(st.sampled_from(["tick", "check"]), st.integers(0, 45 * US)), max_size=25
    ),
)
def test_staleness_events_recorded(steps: list[tuple[str, int]]) -> None:
    """Each transition into stale records exactly one `feed.stale` event in the event log and
    calls the health kill once; a recovery clears the kill and records nothing."""
    engine = event_engine()
    changes: list[bool] = []
    health = EventFeedHealth(lambda: Session(engine), changes.append)
    monitor = StalenessMonitor(health, is_open)
    now = 9 * 3600 * US + 15 * 60 * US
    samples: list[bool] = []
    tick_times: list[int] = []
    for kind, gap in steps:
        now += gap
        if kind == "tick":
            samples.append(rule(now, tick_times))
            monitor.on_tick(at_us(now))
            tick_times.append(now)
            samples.append(rule(now, tick_times))
        else:
            monitor.check(at_us(now))
            samples.append(rule(now, tick_times))
    transitions = list(zip([False, *samples], samples, strict=False))
    trips = sum(1 for a, b in transitions if not a and b)
    with Session(engine) as session:
        stored = session.scalar(
            select(func.count()).select_from(HatEvent).where(HatEvent.event_type == "feed.stale")
        )
    assert stored == trips
    assert changes.count(True) == trips
    assert changes.count(False) == sum(1 for a, b in transitions if a and not b)


class Contexts:
    """The market-side entry inputs: a context every risk check accepts (the feed state is the
    trigger service's own)."""

    def __init__(self, policy_version_id: object) -> None:
        self.stored = replace(version_of(seed_policy()), version_id=policy_version_id)

    def entry_context(self, session: Session, bot_id: object, at: datetime) -> object:
        return valid_ctx(policy_versions=[self.stored])


@settings(max_examples=40, deadline=None)
@given(
    steps=st.lists(
        st.tuples(st.sampled_from(["tick", "check"]), st.integers(0, 45 * US)), max_size=20
    ),
)
def test_stale_feed_trips_health_kill(steps: list[tuple[str, int]]) -> None:
    """HAT-DATA-116, HAT-RISK-063: wired as in production (staleness monitor -> EventFeedHealth
    -> DataHealthKill), the account's data-health kill is tripped exactly while the feed is
    stale, so every bot is manage-only, and clears by itself when ticks resume; a trigger fired
    while the feed is stale is stored Rejected with `stale_feed` and opens nothing, one fired
    on a live feed opens its virtual position. Harm: entries keep being taken on prices that
    stopped updating."""
    engine = memory_engine()
    with Session(engine) as session:
        world = seed_engine_world(session, instruments=1)
        session.commit()
    now = [(10 * 3600) * US]
    health = EventFeedHealth(
        lambda: Session(engine), DataHealthKill(lambda: Session(engine), lambda: at_us(now[0]))
    )
    monitor = StalenessMonitor(health, is_open)
    monitor.on_tick(at_us(now[0]))
    for kind, gap in steps:
        now[0] += gap
        if kind == "tick":
            monitor.on_tick(at_us(now[0]))
        else:
            monitor.check(at_us(now[0]))
        with Session(engine) as session:
            assert controls.kill_state(session, world.bot_id).health_tripped == monitor.stale
    book = trigger_support.playbook(
        trigger=[{"type": "price_breaks_level", "params": {"level": "pivot"}}]
    )
    cand = trigger_support.candidate(
        book,
        bot_id=world.bot_id,
        instrument_id=world.instruments[0],
        pivot=Decimal(99),
        daily=DailyInputs(
            tick=Decimal("0.05"), band_known=True, band_pct=None, adv20=Decimal(1_000_000)
        ),
    )
    trigger_monitor = TriggerMonitor(BarBuilder())
    trigger_monitor.add(cand)
    service = TriggerService(
        trigger_monitor,
        lambda: Session(engine),
        Contexts(world.policy_version_id),
        lambda: monitor.stale,
    )
    bar = Bar("AAA", at_us(14 * 3600 * US), Decimal(99), Decimal(101), Decimal(98), Decimal(100), 9)
    (fired,) = service.on_bar(bar)
    with Session(engine) as session:
        signal = session.scalar(select(HatSignal).where(HatSignal.bot_id == world.bot_id))
        opened = session.scalar(select(func.count()).select_from(HatPosition))
    assert fired.at == bar.final_at
    if monitor.stale:
        assert signal.state == "Rejected"
        assert signal.risk_decision["reason"] == "stale_feed"
        assert opened == 0
    else:
        assert signal.risk_decision["decision"] == "ACCEPT"
        assert opened == 1


@given(changes=st.lists(st.booleans(), min_size=1, max_size=8), data=st.data())
@settings(max_examples=60, deadline=None)
def test_health_kill_writes_out_of_order(changes: list[bool], data: st.DataObject) -> None:
    """HAT-DATA-116, HAT-RISK-063 (part of `stale_feed_trips_health_kill`): the kill writes run
    on worker threads and may run in any order; whatever the order, the account's health kill
    ends in the state of the latest change. Harm: a trip that reaches the database after its
    recovery leaves every bot manage-only on a healthy feed, or a recovery written after a
    newer trip lets entries through on a stale feed."""
    engine = memory_engine()
    jobs: list[object] = []
    clock = [at_us(10 * 3600 * US)]
    hook = DataHealthKill(lambda: Session(engine), lambda: clock[0], submit=jobs.append)
    for stale in changes:
        clock[0] += timedelta(seconds=1)
        hook(stale)
    for job in data.draw(st.permutations(jobs)):
        job()
    with Session(engine) as session:
        assert controls.kill_state(session, uuid4()).health_tripped == changes[-1]


# --- triggers on final bars (HAT-EXEC-120) -----------------------------------------------------


@example(
    script=[(300 - 1, 0, 10_100), (300 + 1, 0, 9_900)],
    pivot=10_000,
)  # a print above the pivot inside a bar that ends below it: the bar's high still breaks
@example(script=[(299, 3, 10_100), (300, 0, 9_900)], pivot=10_000)  # the break print is late
@given(
    script=st.lists(
        st.tuples(
            st.integers(0, 6 * 3600 + 14 * 60),  # exchange second after 09:15
            st.integers(0, 6),  # arrival delay, seconds (2 s or more after the bar end is late)
            st.integers(9_800, 10_200),  # paise
        ),
        max_size=40,
    ),
    pivot=st.integers(9_900, 10_100),
)
@settings(max_examples=150, deadline=None)
def test_trigger_only_on_final_bars(script: list[tuple[int, int, int]], pivot: int) -> None:
    """HAT-EXEC-120, HAT-DATA-113: the trigger monitor sees only final bars. With prints
    arriving late or out of order, a pivot breakout fires once, at the final instant (end + 2 s)
    of the first final bar in [09:20, 15:20] whose high is above the pivot, never on a print
    inside a bar that is still open, never from a late print the bar never took, and never at
    an instant before the bar was handed over. Harm: an entry fires on a print the 5-minute bar
    later disowns, or before the bar that justifies it exists."""
    opens = datetime.combine(DAY, time(9, 15), tzinfo=IST)
    events = sorted(
        (
            (
                opens + timedelta(seconds=s + delay),
                n,
                Tick("AAA", opens + timedelta(seconds=s), Decimal(p).scaleb(-2), 1),
            )
            for n, (s, delay, p) in enumerate(script)
        ),
        key=lambda e: (e[0], e[1]),
    )
    book = trigger_support.playbook(
        trigger=[{"type": "price_breaks_level", "params": {"level": "pivot"}}]
    )
    builder = BarBuilder()
    monitor = TriggerMonitor(builder)
    monitor.add(trigger_support.candidate(book, pivot=Decimal(pivot).scaleb(-2)))
    finals: list[tuple[datetime, Bar]] = []
    fired: list[tuple[datetime, object]] = []
    for arrival, _n, tick in events:
        for bar in builder.advance(arrival):
            finals.append((arrival, bar))
            fired.extend((arrival, t) for t in monitor.on_bar(bar))
        builder.on_tick(tick, arrival)
    end = datetime.combine(DAY, time(16, 0), tzinfo=IST)
    for bar in builder.advance(end):
        finals.append((end, bar))
        fired.extend((end, t) for t in monitor.on_bar(bar))
    level = Decimal(pivot).scaleb(-2)
    last_in_window = datetime.combine(DAY, time(15, 20), tzinfo=IST)
    expected = next(
        (bar for _, bar in finals if bar.high > level and bar.final_at <= last_in_window), None
    )
    if expected is None:
        assert fired == []
        return
    ((handed_at, trigger),) = fired
    assert trigger.at == expected.final_at <= handed_at
    # volume pace reads the day volume at the trigger instant, so the elapsed share is taken there
    micro = timedelta(microseconds=1)
    session_length = timedelta(hours=6, minutes=15)
    assert trigger.context.elapsed == Fraction(
        (expected.final_at - opens) // micro, session_length // micro
    )
    assert trigger.context.bar_high == expected.high
    assert trigger.context.price == expected.close


@settings(max_examples=25, deadline=None)
@given(failures=st.integers(0, 4), bars=st.integers(1, 4))
def test_failed_signal_write_retries(failures: int, bars: int) -> None:
    """HAT-EXEC-120 (part of `trigger_only_on_final_bars`): a breakout holds on `bars`
    consecutive final bars and the first `failures` signal writes fail. Each failed write
    releases the key, so the next final bar fires again; the first write that succeeds stores
    the day's one signal, at its own bar's final instant, and no later bar fires. Harm: a
    database blip at the breakout loses the day's signal for good."""
    engine = memory_engine()
    with Session(engine) as session:
        world = seed_engine_world(session, instruments=1)
        session.commit()
    attempts = [0]

    def sessions() -> Session:
        attempts[0] += 1
        if attempts[0] <= failures:
            raise RuntimeError("database unavailable")
        return Session(engine)

    book = trigger_support.playbook(
        trigger=[{"type": "price_breaks_level", "params": {"level": "pivot"}}]
    )
    cand = trigger_support.candidate(
        book,
        bot_id=world.bot_id,
        instrument_id=world.instruments[0],
        pivot=Decimal(99),
        daily=DailyInputs(
            tick=Decimal("0.05"), band_known=True, band_pct=None, adv20=Decimal(1_000_000)
        ),
    )
    monitor = TriggerMonitor(BarBuilder())
    monitor.add(cand)
    service = TriggerService(monitor, sessions, Contexts(world.policy_version_id), lambda: False)
    finals = [
        Bar(
            "AAA",
            at_us((11 * 3600 + 300 * n) * US),
            Decimal(99),
            Decimal(101),
            Decimal(98),
            Decimal(100),
            9,
        )
        for n in range(bars)
    ]
    fired = [len(service.on_bar(bar)) for bar in finals]
    assert fired == [1 if n <= failures else 0 for n in range(bars)]
    with Session(engine) as session:
        stored = list(session.scalars(select(HatSignal)))
    if failures >= bars:
        assert stored == []
        return
    (signal,) = stored
    # SQLite keeps the instant's IST wall time without its offset
    assert signal.triggered_at.replace(tzinfo=None) == finals[failures].final_at.replace(
        tzinfo=None
    )


# --- listener hook (HAT-DATA-111) --------------------------------------------------------------


class FlakyListener:
    def __init__(self, raises_on: set[int]) -> None:
        self.raises_on = raises_on
        self.received: list[dict[str, Any]] = []

    def __call__(self, tick: dict[str, Any]) -> None:
        index = len(self.received)
        self.received.append(tick)
        if index in self.raises_on:
            raise RuntimeError("listener bug")


@settings(max_examples=25)
@given(
    symbols=st.lists(st.sampled_from(SYMBOLS), min_size=1, max_size=15),
    raising=st.lists(st.sets(st.integers(0, 14), max_size=15), min_size=1, max_size=4),
    always_raise=st.booleans(),
)
def test_listener_isolation(
    symbols: list[str], raising: list[set[int]], always_raise: bool
) -> None:
    """For every tick sequence and every set of listeners, any subset of which raises, every tick
    still reaches every listener that did not raise for it, in order, and the feed keeps
    delivering after a listener fails. A listener removed while ticks flow receives no more.
    Harm: a HAT bug freezes the live feed, or one listener starves another of ticks."""
    from hat.fakes.data_socket import make_feed

    feed, _ = make_feed()
    listeners = [
        FlakyListener(set(range(15)) if always_raise and i == 0 else raises)
        for i, raises in enumerate(raising)
    ]
    removed = FlakyListener(set())
    for listener in listeners:
        feed.add_tick_listener(listener)
    remove = feed.add_tick_listener(removed)
    sent = [{"symbol": s, "ltp": float(i + 1), "seq": i} for i, s in enumerate(symbols)]
    for i, tick in enumerate(sent):
        if i == len(sent) // 2:
            remove()
        feed._deliver(dict(tick))  # noqa: SLF001 - the loop-thread entry point
    for listener in listeners:
        assert [t["seq"] for t in listener.received] == [t["seq"] for t in sent]
    assert [t["seq"] for t in removed.received] == [t["seq"] for t in sent[: len(sent) // 2]]


# --- subscriptions (HAT-DATA-112) --------------------------------------------------------------

symbol_sets = st.frozensets(st.sampled_from([f"S{i}" for i in range(12)]), max_size=8)
sub_inputs = st.builds(SubscriptionInputs, symbol_sets, symbol_sets, symbol_sets)


class _SlowInputs(SubscriptionInputs):
    """Inputs whose construction pauses once, so a refresh can arrive between a candidate add
    reading the current inputs and writing the new ones."""

    pause: Any = None

    def __init__(
        self,
        watchlist: frozenset[str] = frozenset(),
        positions: frozenset[str] = frozenset(),
        candidates: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(watchlist, positions, candidates)
        pause = type(self).pause
        if pause is not None:
            type(self).pause = None
            pause()


@example(second=9 * 3600 - 1, weekend=False)
@example(second=9 * 3600, weekend=False)
@example(second=9 * 3600 + 8 * 60, weekend=False)
@example(second=15 * 3600 + 1800, weekend=False)
@example(second=15 * 3600 + 1801, weekend=False)
@given(second=st.integers(0, 24 * 3600 - 1), weekend=st.booleans())
def test_feed_connection_window(second: int, weekend: bool) -> None:
    """The live feed may connect on a trading day from 09:00 IST (the pre-open session, so the
    opening gaps can be captured) to the 15:30 close, and never on a weekend. Harm: the 09:00 to
    09:08 gap capture receives no ticks, or the feed stays up all night."""
    from hat.fakes.data_socket import make_feed

    day = date(2026, 10, 24 if weekend else 21)  # a Saturday or a Wednesday
    when = datetime.combine(day, time(), IST) + timedelta(seconds=second)
    feed, _ = make_feed(clock=FixedClock(when.astimezone(UTC)))
    expected = not weekend and 9 * 3600 <= second <= 15 * 3600 + 1800
    assert feed.connection_allowed() == expected


@settings(max_examples=8, deadline=None)
@given(
    old=symbol_sets,
    fresh=symbol_sets.filter(bool),
    candidates=symbol_sets.filter(bool),
)
def test_candidates_never_overwrite_refresh(
    old: frozenset[str], fresh: frozenset[str], candidates: frozenset[str]
) -> None:
    """A watchlist refresh that arrives while a candidate add is in progress is not lost: after
    both finish the `"hat"` set holds the refreshed watchlist. Harm: a
    refreshed watchlist symbol or an open position stops receiving prices."""
    registry = SubscriptionRegistry(str.upper)
    subs = LiveSubscriptions(registry, lambda _s: None, lambda _s: None, limit=1000)
    subs.sync(SubscriptionInputs(old))
    read_done, release = threading.Event(), threading.Event()
    refresh = threading.Thread(
        target=lambda: (read_done.wait(5), subs.sync(SubscriptionInputs(fresh)))
    )

    def pause() -> None:
        read_done.set()
        release.wait(0.2)  # the refresh runs now if nothing stops it

    refresh.start()
    with (
        patch.object(_SlowInputs, "pause", pause),
        patch("app.hat.data.subscriptions.SubscriptionInputs", _SlowInputs),
    ):
        subs.add_candidates(candidates)
    release.set()
    refresh.join(5)
    held = set(registry.symbols_of(HAT_OWNER))
    assert {x.upper() for x in fresh} <= held


@given(
    ops=st.lists(
        st.one_of(
            st.tuples(st.just("hat"), sub_inputs),
            st.tuples(st.just("add"), st.sampled_from(["client-1", "api"]), symbol_sets),
            st.tuples(st.just("remove"), st.sampled_from(["client-1", "api"]), symbol_sets),
            st.tuples(st.just("release")),
            st.tuples(st.just("candidates"), symbol_sets),
        ),
        max_size=30,
    )
)
def test_subscription_set(ops: list[tuple[Any, ...]]) -> None:
    """For every state of watchlists, positions and candidates, the `"hat"` owner's set equals
    watchlist | positions | candidates, open positions are always in it, the provider is
    subscribed to exactly the symbols some owner holds, and releasing HAT's symbols never
    unsubscribes a symbol another owner holds. A set at or above the limit raises an alert."""
    registry = SubscriptionRegistry(str.upper)
    provider: set[str] = set()
    alerts: list[str] = []
    wrong: list[str] = []

    def subscribe(symbols: list[str]) -> None:
        provider.update(symbols)

    def unsubscribe(symbols: list[str]) -> None:
        for symbol in symbols:
            if registry.is_held(symbol):
                wrong.append(symbol)
        provider.difference_update(symbols)

    subs = LiveSubscriptions(registry, subscribe, unsubscribe, alerts.append, limit=7)
    inputs = SubscriptionInputs()
    for op in ops:
        alerts_before = len(alerts)
        if op[0] == "hat":
            inputs = op[1]
            subs.sync(inputs)
        elif op[0] == "candidates":
            inputs = SubscriptionInputs(
                inputs.watchlist, inputs.positions, inputs.candidates | op[1]
            )
            subs.add_candidates(op[1])
        elif op[0] == "add":
            subscribe(sorted(registry.add(op[1], op[2])))
        elif op[0] == "remove":
            unsubscribe(sorted(registry.remove(op[1], op[2])))
        else:
            inputs = SubscriptionInputs()
            subs.release_all()
        wanted = {s.upper() for s in inputs.watchlist | inputs.positions | inputs.candidates}
        assert set(registry.symbols_of(HAT_OWNER)) == wanted
        assert {s.upper() for s in inputs.positions} <= set(registry.symbols_of(HAT_OWNER))
        assert provider == registry.all_symbols()
        if op[0] in ("hat", "candidates"):
            assert (len(alerts) > alerts_before) == (len(wanted) >= 7)
        else:
            assert len(alerts) == alerts_before
    assert wrong == []


@settings(max_examples=40)
@given(
    watch=st.sets(st.integers(0, 9), max_size=10),
    held=st.sets(st.integers(0, 9), max_size=10),
    closed=st.sets(st.integers(0, 9), max_size=10),
    other_day=st.sets(st.integers(0, 9), max_size=10),
    candidates=symbol_sets,
)
def test_subscription_inputs_from_database(
    watch: set[int],
    held: set[int],
    closed: set[int],
    other_day: set[int],
    candidates: frozenset[str],
) -> None:
    """The inputs are today's watchlist symbols, every open position's symbol (closed positions
    and other days' watchlists excluded) and the candidates given, in DB symbol format."""
    from hat_support import memory_engine

    from app.hat.models import HatPosition as Position

    engine = memory_engine(HatInstrument, HatWatchlistEntry, Position)
    session = Session(engine)
    instruments: list[HatInstrument] = []
    for i in range(10):
        row = HatInstrument(
            id=uuid4(),
            market_id=uuid4(),
            isin=f"INE{i:09d}",
            series="EQ",
            symbol=f"SYM{i}",
            name="n",
            face_value=Decimal(10),
            active=True,
        )
        session.add(row)
        instruments.append(row)
    session.flush()
    now = datetime(2026, 10, 21, 4, tzinfo=UTC)
    for i in watch | other_day:
        session.add(
            HatWatchlistEntry(
                id=uuid4(),
                bot_id=uuid4(),
                strategy_version_id=uuid4(),
                playbook="default",
                instrument_id=instruments[i].id,
                trade_date=DAY if i in watch else DAY - timedelta(days=1),
                score=Decimal("0.5"),
                criteria_results={},
                status="listed",
            )
        )
    for i in held | closed:
        session.add(
            HatPosition(
                id=uuid4(),
                bot_id=uuid4(),
                signal_id=uuid4(),
                instrument_id=instruments[i].id,
                is_virtual=False,
                qty_open=1,
                avg_entry=Decimal(1),
                initial_stop=Decimal(1),
                current_stop=Decimal(1),
                currency="INR",
                opened_at=now,
                closed_at=None if i in held else now,
                state="Open",
                rules_fired={},
            )
        )
    session.flush()
    loaded = load_inputs(session, DAY, candidates)
    session.close()
    assert loaded.watchlist == {f"SYM{i}" for i in watch}
    assert loaded.positions == {f"SYM{i}" for i in held}
    assert loaded.candidates == candidates


# --- persistence (HAT-DATA-117) ----------------------------------------------------------------


class FlakyStore(BarStore):
    """A store whose write fails on chosen calls, as a full disk would."""

    def __init__(self, root: Path, failures: set[int]) -> None:
        super().__init__(root)
        self.calls = 0
        self.failures = failures

    def write(self, bars: Iterable[Bar], instrument_ids: Any) -> int:  # noqa: ANN401
        self.calls += 1
        if self.calls in self.failures:
            raise OSError("disk full")
        return super().write(bars, instrument_ids)


class QuietHealth:
    def feed_stale(self, now: datetime, silent_for: timedelta | None) -> None:
        pass

    def feed_recovered(self, now: datetime) -> None:
        pass


@settings(max_examples=60)
@given(
    sequence=st.lists(
        st.tuples(
            st.sampled_from(SYMBOLS),
            st.integers(OPEN_SOD * US, CLOSE_SOD * US + 30 * 60 * US),
            prices,
            st.integers(0, 10**6),
        ),
        max_size=50,
    ),
    failures=st.sets(st.integers(1, 6), max_size=3),
    flush_every=st.integers(1, 10),
)
def test_persisted_equals_live(
    sequence: list[tuple[str, int, Decimal, int]], failures: set[int], flush_every: int
) -> None:
    """For every session, the bars in `bars_5m` equal the final bars the bar listener saw: one row
    per (instrument, bar_start), exactly the spec's columns, whatever writes failed on the way
    and however often the writer re-ran."""
    ids = {s: uuid4() for s in SYMBOLS}
    clock = FixedClock(at_us(9 * 3600 * US))
    with tempfile.TemporaryDirectory() as tmp:
        store = FlakyStore(Path(tmp), failures)
        live = LiveData(
            health=QuietHealth(),
            is_open=lambda _t: False,
            clock=clock,
            store=store,
            instrument_ids=lambda: ids,
        )
        seen: list[Bar] = []
        live.add_bar_listener(seen.append)
        cumulative = dict.fromkeys(SYMBOLS, 0)
        for index, (symbol, offset, price, qty) in enumerate(sorted(sequence, key=lambda r: r[1])):
            clock.set(at_us(offset))
            cumulative[symbol] += qty
            live.on_tick(
                {
                    "symbol": symbol,
                    "ltp": float(price),
                    "volume": cumulative[symbol],
                    "exch_feed_time": clock.now().timestamp(),
                }
            )
            if index % flush_every == 0:
                live.step()
                live.persist()
        clock.set(at_us(23 * 3600 * US))
        live.step()
        for _ in range(8):
            live.persist()
        stored = store.read(DAY)
        assert len({(r.instrument_id, r.bar_start) for r in stored}) == len(stored)
        expected = {
            (str(ids[b.symbol]), b.start.astimezone(UTC)): (
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
            )
            for b in seen
        }
        assert {
            (r.instrument_id, r.bar_start): (r.open, r.high, r.low, r.close, r.volume)
            for r in stored
        } == expected
        assert len(seen) == len(expected)
        if stored:
            assert pq.read_schema(store.path(DAY)).names == SCHEMA.names
            assert not list(Path(tmp).rglob("*.tmp"))
        else:
            assert not store.path(DAY).exists()


# --- gap capture (HAT-DATA-118) ----------------------------------------------------------------


@given(
    prev_close=prices,
    indicative=st.one_of(st.none(), prices),
    opened=st.one_of(st.none(), prices),
    minute=st.integers(0, 24 * 60 - 1),
)
def test_gap_pct(
    prev_close: Decimal, indicative: Decimal | None, opened: Decimal | None, minute: int
) -> None:
    """For every symbol with previous close c and indicative (or first-tick) open o, the recorded
    gap is 100 * (o / c - 1) and the record says which source supplied o: the indicative price
    inside 09:00-09:08, else the open of the first tick."""
    gaps = PreopenGaps()
    when = at_us(minute * 60 * US)
    payload: dict[str, Any] = {"symbol": "AAA", "prev_close_price": float(prev_close)}
    if indicative is not None:
        payload["indicative_open_price"] = float(indicative)
    if opened is not None:
        payload["open_price"] = float(opened)
    record = gaps.observe(payload, when)
    in_window = time(9, 0) <= when.time() <= time(9, 8)
    if indicative is not None and in_window:
        source, price = GapSource.PREOPEN, indicative
    elif opened is not None:
        source, price = GapSource.FIRST_TICK, opened
    else:
        assert record is None
        return
    assert record is not None
    assert record.source is source
    sent = Decimal(str(float(price)))  # a float round trip, as a feed payload carries it
    assert record.open == sent
    exact = 100 * (Fraction(record.open) / Fraction(Decimal(str(float(prev_close)))) - 1)
    assert abs(Fraction(record.gap_pct) - exact) < Fraction(1, 10**20)
    # a later first tick never replaces a record already made
    later = {k: v for k, v in payload.items() if k != "indicative_open_price"}
    again = gaps.observe({**later, "open_price": float(prev_close * 2)}, when)
    assert again is None
    assert gaps.records()["AAA"] == record
