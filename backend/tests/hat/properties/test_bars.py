"""PBT bars.* (HAT-DATA-111..119, HAT-DEC-I4, HAT-EXEC-120): the tick hook, 5-minute bars, the
opening range, volume pace, staleness and its health kill, triggers on final bars, persistence,
subscriptions, gap capture and the optional minute-history loader, all on the real code.

Time is explicit: every property passes the instants it wants, so no test reads a clock."""

import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import hat_trigger_support as trigger_support
import pyarrow.parquet as pq
from hat_engine_support import open_trade, seed_engine_world
from hat_risk_support import CALENDAR, TRADE_DAY, valid_ctx, version_of
from hat_support import event_engine, memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.clock import FixedClock
from app.hat.daily.live import ExitViews, LivePrices, LiveRoute, PriceBook, band_bounds
from app.hat.data.bars import Bar, BarBuilder, Tick, TickOutcome, volume_pace
from app.hat.data.bars_store import SCHEMA, BarStore, row_from_bar
from app.hat.data.live import (
    PRICE_OUT_OF_BAND,
    PRICE_STALE,
    PRICE_UNKNOWN,
    EventFeedHealth,
    LiveData,
    Quote,
    tick_from_payload,
    usable_price,
)
from app.hat.data.minute_history import (
    CHUNK_DAYS,
    CallPacer,
    HistoryLimits,
    Progress,
    aggregate,
    candle_prints,
    load_minute_history,
    parse_candles,
)
from app.hat.data.preopen import GapSource, PreopenGaps
from app.hat.data.sessions import IST
from app.hat.data.staleness import STALE_AFTER, SilenceWatch, StalenessMonitor
from app.hat.data.subscriptions import (
    HAT_OWNER,
    HEARTBEAT_SYMBOL,
    LiveSubscriptions,
    SubscriptionInputs,
    SubscriptionRegistry,
    load_inputs,
)
from app.hat.engine.exits import ExitView
from app.hat.engine.live_entry import DataHealthKill, TriggerService
from app.hat.engine.stops import floor_to_tick
from app.hat.engine.trigger import (
    DailyInputs,
    TriggerMonitor,
    locked_lower_circuit,
    lower_circuit_price,
    upper_circuit_price,
)
from app.hat.execution.router import placement_refusal
from app.hat.models import HatEvent, HatInstrument, HatPosition, HatSignal, HatWatchlistEntry
from app.hat.risk import controls
from app.hat.risk.policy import seed_policy
from app.hat.settings import load_settings
from app.utils.env_loader import TRUE_VALUES

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
    trigger_support.watch_from_open(builder, ["AAA"], DAY)
    for offset, price, qty in first:
        builder.on_tick(Tick("AAA", at_us(offset), price, qty), at_us(offset))
    assert builder.opening_range("AAA", DAY) is None
    assert builder.advance(at_us((OPEN_SOD + 300) * US + 2 * US - 1)) == []
    assert builder.opening_range("AAA", DAY) is None
    first_end = at_us((OPEN_SOD + 300) * US)
    builder.advance(at_us((OPEN_SOD + 300) * US + 2 * US))
    assert builder.opening_range("AAA", DAY) is None  # the feed is not seen past the first bar
    builder.on_tick(Tick("ZZZ", first_end, Decimal(1)), at_us((OPEN_SOD + 300) * US + 2 * US))
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


@given(
    sequence=st.lists(
        st.tuples(
            st.sampled_from(SYMBOLS),
            st.integers(OPEN_SOD * US, CLOSE_SOD * US - 1),
            prices,
            quantities,
        ),
        max_size=40,
    ),
    cut=st.integers(OPEN_SOD * US, CLOSE_SOD * US),
)
def test_restore_matches_live(sequence: list[tuple[str, int, Decimal, int]], cut: int) -> None:
    """PR 17 (a restart mid-session): a builder restored from the final bars a live builder made
    before a restart at `cut` has the same opening range and day volume of every symbol, and a
    second restore changes nothing. Harm: after a restart past 09:20 no opening-range breakout
    can trigger for the rest of the day, and volume pace reads as a fraction of the real one."""
    live = BarBuilder()
    trigger_support.watch_from_open(live, SYMBOLS, DAY)
    for symbol, offset, price, qty in sorted(sequence, key=lambda row: row[1]):
        if offset < cut:
            live.on_tick(Tick(symbol, at_us(offset), price, qty), at_us(offset))
    finals = live.advance(at_us(cut))
    unfinished = {  # a symbol with a bar still building at the cut
        symbol
        for symbol, offset, price, qty in sequence
        if offset < cut
        and (start := expected_start(Tick(symbol, at_us(offset), price, qty))) is not None
        and start + timedelta(minutes=5, seconds=2) > at_us(cut)
    }
    restored = BarBuilder()
    from_open, gap, seen = live.coverage(DAY)
    assert restored.restore(finals, from_open, gap, seen) == len(finals)
    assert restored.restore(finals, from_open, gap, seen) == 0
    for builder in (live, restored):  # the rest of the feed, seen at the restart
        builder.on_tick(Tick("ZZZ", at_us(cut), Decimal(1)), at_us(cut))
    # the stopped process saw the feed past the first bar, else what it missed is unknown
    past_first = seen is not None and seen >= at_us((OPEN_SOD + 300) * US)
    for symbol in SYMBOLS:
        expected = live.opening_range(symbol, DAY) if past_first else None
        assert restored.opening_range(symbol, DAY) == expected
        assert restored.day_low(symbol, DAY) is None  # the restart is a gap (PR 22)
        if symbol not in unfinished:
            assert restored.day_volume(symbol, DAY) == live.day_volume(symbol, DAY)


# --- complete-session state (PR 22: HAT-DATA-114, HAT-EXEC-051, HAT-BOT-071) -------------------

PROBE = [(0, 11_000), (150, 10_100), (160, 10_300), (170, 10_000), (180, 10_400), (600, 10_500)]
"""The audit's probe (F4): a 09:15 spike to 110 HAT never saw, then prints from 09:17:30."""


def tape_bars(tape: Iterable[tuple[int, int]], now: datetime) -> list[Bar]:
    """The final bars at `now` of a tape of (seconds after 09:15, paise) prints for AAA, each
    arriving at its own instant: the session as it really traded."""
    opens = at_us(OPEN_SOD * US)
    ticks = [Tick("AAA", opens + timedelta(seconds=s), Decimal(p).scaleb(-2)) for s, p in tape]
    return [b for b in expected_bars(enumerate(ticks)).values() if b.final_at <= now]


def true_opening(tape: list[tuple[int, int]]) -> tuple[Decimal, Decimal] | None:
    opens = at_us(OPEN_SOD * US)
    first = [b for b in tape_bars(tape, at_us(CLOSE_SOD * US + 3600 * US)) if b.start == opens]
    return (first[0].high, first[0].low) if first else None


FIRST_FINAL = 302  # seconds after 09:15 when the first bar is final


class Downtime:
    """A window of seconds after 09:15 in which no tick reaches HAT."""

    def __init__(self, window: tuple[int, int] | None) -> None:
        self.window = window

    def hides(self, second: int) -> bool:
        return self.window is not None and self.window[0] <= second < sum(self.window)


@example(tape=PROBE, watch=150, dropout=None, restart=None, seeded=False)
@example(  # a 10:15 bar
    tape=[(3600, 9_000), (0, 9_001)], watch=0, dropout=None, restart=None, seeded=False
)
@example(  # a gap inside the first bar
    tape=PROBE, watch=-600, dropout=(100, 60), restart=None, seeded=False
)
@example(  # silent from 09:19:50 (the first bar's low unseen), restarted at 09:20:05
    tape=[(0, 10_000), (297, 9_000), (400, 10_500)],
    watch=-600,
    dropout=(290, 30),
    restart=(305, 5),
    seeded=False,
)
@example(  # a gap found after the first bar was stored, then a restart before the next bar
    tape=[(0, 10_000), (285, 9_000), (400, 10_500)],
    watch=-600,
    dropout=(281, 40),
    restart=(325, 5),
    seeded=False,
)
@example(  # the feed drops at 09:19:55; the low of the first bar prints, unseen, at 09:19:57
    tape=[(0, 10_000), (297, 9_000), (400, 10_500)],
    watch=-600,
    dropout=(295, 60),
    restart=None,
    seeded=False,
)
@example(  # a restart at 09:21:40; the day's low trades while HAT is down
    tape=[*PROBE, (410, 9_000)], watch=-600, dropout=None, restart=(400, 30), seeded=False
)
@example(tape=PROBE, watch=150, dropout=None, restart=None, seeded=True)  # a 09:17:30 subscriber
@example(  # seeded after a restart; the day's low traded while HAT was down
    tape=[*PROBE, (410, 9_000), (700, 10_200)],
    watch=-600,
    dropout=None,
    restart=(400, 30),
    seeded=True,
)
@example(  # seeded at 09:15:10, then the first bar's low trades unseen in a gap
    tape=[(10, 10_000), (250, 9_000)], watch=-600, dropout=(200, 100), restart=None, seeded=True
)
@example(  # seen from 09:17:30; a restart; the next tick (09:22:30) knows a high after 09:20
    tape=[*PROBE, (350, 11_500), (450, 10_200)],
    watch=150,
    dropout=None,
    restart=(400, 30),
    seeded=True,
)
@example(  # a gap inside the first bar, then a tick seeds again
    tape=[*PROBE, (250, 9_500)], watch=-600, dropout=(100, 60), restart=None, seeded=True
)
@settings(max_examples=120, deadline=None)
@given(
    tape=st.lists(
        st.tuples(st.integers(0, 2 * 3600), st.integers(9_000, 11_000)), min_size=1, max_size=50
    ),
    watch=st.integers(-900, 1800),  # seconds after 09:15 when HAT's first tick arrives
    dropout=st.one_of(st.none(), st.tuples(st.integers(0, 2 * 3600), st.integers(30, 900))),
    restart=st.one_of(st.none(), st.tuples(st.integers(0, 2 * 3600), st.integers(1, 600))),
    seeded=st.booleans(),
)
def test_session_state_never_partial(
    tape: list[tuple[int, int]],
    watch: int,
    dropout: tuple[int, int] | None,
    restart: tuple[int, int] | None,
    seeded: bool,
) -> None:
    """PR 22 (audit F4): whatever HAT missed of the session (a first tick after the open, a feed
    silence of 30 s or more, a restart with its downtime), the opening range it publishes is the
    one the session really had, and the day low it publishes is the real low of the day's final
    bars; otherwise it publishes none. Watched from the open with no gap it publishes both (the
    opening range also after a restart once the first bar was stored). PR 39: with the exchange
    range on (`seeded`, each tick carrying the session's high and low so far), the same holds,
    and a symbol first seen late, after a restart or after a feed gap, with no gap after, has
    both: the opening range when it ticked inside the first bar, the day low once that tick's
    bar is final. Harm:
    EP buys under its true opening-range high, with a stop above the day's real low that is too
    tight and oversizes the position; or, without the seed, EP never fires on a gap-scan name."""
    ids = {"AAA": uuid4(), "BBB": uuid4()}
    clock = FixedClock(at_us(8 * 3600 * US))
    opens = at_us(OPEN_SOD * US)
    hidden = Downtime(dropout)
    down = Downtime(restart)
    truth = true_opening(tape)
    watched = watch < 0 and dropout is None  # from the open, with no feed gap
    with tempfile.TemporaryDirectory() as tmp:
        store = BarStore(Path(tmp))

        def start() -> LiveData:
            return LiveData(
                health=QuietHealth(),
                is_open=is_open,
                clock=clock,
                store=store,
                instrument_ids=lambda: ids,
                exchange_range=seeded,
            )

        live = start()
        # the second of the current process's first AAA tick after any dropout
        seed_at: list[int] = []
        healed = 0 if dropout is None else sum(dropout)  # no gap after this second

        def check(now: datetime) -> None:
            live.step()
            live.persist()
            final = tape_bars(tape, now)
            opening = live.builder.opening_range("AAA", DAY)
            past = now >= opens + timedelta(seconds=healed)
            first = seed_at[0] if seeded and seed_at and seed_at[0] >= healed and past else None
            if opening is not None:
                assert opening == truth
            elif watched and (restart is None or restart[0] > FIRST_FINAL):
                # watched from the open, first bar stored before any restart: it is published
                assert truth is None or now < opens + timedelta(seconds=FIRST_FINAL)
            elif first is not None and first < 300:  # seeded inside the first bar, no gap after
                assert now < opens + timedelta(seconds=FIRST_FINAL)
            low = live.builder.day_low("AAA", DAY)
            if low is not None:
                assert low == min(b.low for b in final)
            elif watched and restart is None:
                assert not final
            elif first is not None:  # seeded, no gap after: published once the seed's bar is
                assert now < opens + timedelta(seconds=300 * (first // 300 + 1) + 2)

        exchange: dict[tuple[int, int], tuple[int, int]] = {}  # the session's (high, low) so far
        high, low = 0, 10**9
        for index, (second, paise) in sorted(enumerate(tape), key=lambda e: (e[1][0], e[0])):
            high, low = max(high, paise), min(low, paise)
            exchange[(second, index)] = (high, low)

        tick_at: dict[int, list[tuple[int, int]]] = {}
        for index, (second, paise) in enumerate(tape):
            tick_at.setdefault(second, []).append((index, paise))
        heartbeat = {
            (s, "BBB") for s in range(-890, 2 * 3600 + 10 * 60, 10)
        }  # the rest of the feed
        first = {(watch, "watch")} if watch < 0 else set()  # AAA's pre-open snapshot
        bar_ends = {(300 * k + 302, "step") for k in range(2 * 12 + 2)}
        back = restart is None
        for second, kind in sorted({(s, "AAA") for s in tick_at} | heartbeat | first | bar_ends):
            now = opens + timedelta(seconds=second)
            if down.hides(second):
                continue  # the process is down: what it had not stored is lost
            if not back and second >= sum(restart or (0, 0)):
                clock.set(now)
                live = start()
                live.restore(DAY)
                seed_at.clear()
                back = True
            clock.set(now)
            if hidden.hides(second) or (kind == "AAA" and second < watch):
                check(now)  # the feed is silent; the process steps on
                continue
            if kind == "watch":
                live.on_tick({"symbol": "AAA", "ltp": 100.0})
            elif kind == "BBB":
                live.on_tick({"symbol": "BBB", "ltp": 50.0, "exch_feed_time": now.timestamp()})
            elif kind == "AAA":
                for index, paise in tick_at[second]:
                    high, low = exchange[(second, index)]
                    live.on_tick(
                        {
                            "symbol": "AAA",
                            "ltp": float(Decimal(paise).scaleb(-2)),
                            "exch_feed_time": now.timestamp(),
                            "high_price": float(Decimal(high).scaleb(-2)),
                            "low_price": float(Decimal(low).scaleb(-2)),
                        }
                    )
                    if not seed_at or seed_at[0] < healed <= second:
                        seed_at[:] = [second]
            if kind != "BBB":
                check(now)


@example(steps=[(s, p, 1) for s, p in PROBE], watch=150, expr="opening_range_high", seeded=False)
@example(steps=[(s, p, 1) for s, p in PROBE], watch=150, expr="opening_range_high", seeded=True)
@given(
    steps=st.lists(
        st.tuples(st.integers(0, 2 * 3600), st.integers(9_000, 11_000), st.integers(0, 5)),
        max_size=40,
    ),
    watch=st.integers(-1, 1800),
    expr=st.sampled_from(["pivot", "opening_range_high"]),
    seeded=st.booleans(),
)
@settings(max_examples=120, deadline=None)
def test_trigger_never_uses_a_partial_session(
    steps: list[tuple[int, int, int]], watch: int, expr: str, seeded: bool
) -> None:
    """PR 22 (audit F4), through the real bar builder and trigger monitor: for a candidate HAT
    first saw at `watch` seconds after 09:15, an opening-range trigger fires only at the real
    opening-range high, and a `low_of_day` stop is planned from the day's real low (less a
    tick) or not at all (`stop_input_missing`). Watched from before the open, both are exactly
    the session's; so is the stop of a late candidate whose ticks carry the exchange's high and
    low (PR 39, `seeded`). Harm: a breakout bought under its own level, with an oversized
    position."""
    book = trigger_support.playbook(
        trigger=[{"type": "price_breaks_level", "params": {"level": expr}}],
        stop={"rule": "low_of_day", "buffer_ticks": 1},
    )
    builder = BarBuilder()
    monitor = TriggerMonitor(builder)
    monitor.add(trigger_support.candidate(book, pivot=trigger_support.paise(10_000)))
    first_seen = at_us((OPEN_SOD + watch) * US)
    tape_ticks = trigger_support.session_ticks("AAA", steps)
    seen = [t for t in tape_ticks if t.at >= first_seen]

    def so_far(tick: Tick) -> tuple[Decimal, Decimal]:
        prices = [t.price for t in tape_ticks if t.at <= tick.at]
        return max(prices), min(prices)

    _bars, fired = trigger_support.drive(
        monitor,
        builder,
        seen,
        at_us(16 * 3600 * US),
        from_open=watch < 0,
        seed=so_far if seeded else None,
    )
    tape = [(s, p) for s, p, _q in steps]
    tick = Decimal("0.05")
    for trigger in fired:
        if expr == "opening_range_high":
            truth = true_opening(tape)
            assert truth is not None
            assert trigger.evaluation.level == truth[0]
        real_low = min(b.low for b in tape_bars(tape, trigger.at))
        expected = floor_to_tick(real_low - tick, tick)
        stop = trigger.plan.stop
        if (watch < 0 or seeded) and expected < trigger.plan.entry:
            assert stop == expected
        else:
            assert stop is None or stop == expected
        if stop is None and watch >= 0:
            assert trigger.plan.skip == "stop_input_missing"


@example(seen=[(0, 540, False), (1, 540, False)])  # a 09:00 snapshot carries yesterday's open
@given(
    seen=st.lists(
        st.tuples(st.integers(0, 2), st.integers(8 * 60, 16 * 60), st.booleans()), max_size=30
    )
)
def test_gap_record_from_its_own_day(seen: list[tuple[int, int, bool]]) -> None:
    """PR 22 (audit F13): over a process that runs across days, every gap record of a trade date
    holds that date's open: its own indicative open in 09:00-09:08, or the feed's open from a
    tick at or after 09:15 (before it the feed's open is the previous session's). Harm: `gap_up`
    runs on yesterday's open, so EP buys a stock that did not gap or misses one that did."""
    gaps = PreopenGaps()
    session_open = [Decimal(1000 + 10 * i) for i in range(4)]  # session_open[i + 1] is day i's
    indicative = [Decimal(2000 + 10 * i) for i in range(3)]
    for index, minute, with_indicative in sorted(seen, key=lambda row: (row[0], row[1])):
        day = DAY + timedelta(days=index)
        now = datetime.combine(day, time(), tzinfo=IST) + timedelta(minutes=minute)
        payload: dict[str, Any] = {
            "symbol": "AAA",
            "prev_close_price": 900.0,
            "open_price": float(session_open[index + (1 if minute >= 9 * 60 + 15 else 0)]),
        }
        if with_indicative:
            payload["indicative_open_price"] = float(indicative[index])
        gaps.observe(payload, now)
    last = max((index for index, _m, _i in seen), default=0)
    for index in range(3):
        day = DAY + timedelta(days=index)
        for record in gaps.records(day).values():
            assert record.trade_date == day
            assert record.open in {session_open[index + 1], indicative[index]}
        if index < last and gaps.records(DAY + timedelta(days=last)):
            assert gaps.records(day) == {}  # a later day's record drops the earlier days


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


@example(price=10_000, high=9_999, low=9_000, second=150)  # the price above the high
@example(price=10_000, high=11_000, low=0, second=150)
@given(
    price=st.integers(1, 20_000),
    high=st.integers(-10, 20_000),
    low=st.integers(-10, 20_000),
    second=st.integers(-60, 600),
)
def test_seed_needs_ordered_prices(price: int, high: int, low: int, second: int) -> None:
    """PR 39: a seed is taken only from a tick inside the session whose day low and high bracket
    its own price above zero (`0 < low <= price <= high`); any other leaves the opening range
    and day low as they would be without it. Harm: a garbled or stale high/low field publishes
    an opening range or a day low the session never had."""
    opens = at_us(OPEN_SOD * US)
    at = opens + timedelta(seconds=second)
    tick = Tick("AAA", at, Decimal(price).scaleb(-2))
    plain, seeded = BarBuilder(), BarBuilder()
    for builder in (plain, seeded):
        builder.on_tick(tick, at)
    taken = seeded.seed(tick, Decimal(high).scaleb(-2), Decimal(low).scaleb(-2), at)
    ordered = 0 < low <= price <= high and 0 <= second < CLOSE_SOD - OPEN_SOD
    assert taken == ordered
    end = opens + timedelta(seconds=900)
    for builder in (plain, seeded):
        builder.on_tick(Tick("BBB", end, Decimal(1)), end)
        builder.advance(end)
    if not ordered:
        assert seeded.opening_range("AAA", DAY) == plain.opening_range("AAA", DAY)
        assert seeded.day_low("AAA", DAY) == plain.day_low("AAA", DAY)


@example(at=100, delay=60, gap=130, first=True)  # traded before the gap, arrived after it
@example(at=100, delay=60, gap=130, first=False)
@given(
    at=st.integers(0, 290), delay=st.integers(0, 120), gap=st.integers(0, 410), first=st.booleans()
)
def test_delayed_seed_spoiled_by_gap(at: int, delay: int, gap: int, first: bool) -> None:
    """PR 39 (CodeAnt): a seed covers prints only up to the earlier of its exchange time and its
    arrival, so a feed gap that began between the two (a delayed tick arriving after the gap)
    spoils it: neither the opening range nor the day low is published from it. Harm: prints
    the gap hid, missing from both the seed and the bars, leave a range or a low the session
    never had."""
    opens = at_us(OPEN_SOD * US)
    traded, arrived, since = (opens + timedelta(seconds=x) for x in (at, at + delay, gap))
    seeded, plain = BarBuilder(), BarBuilder()
    tick = Tick("AAA", traded, Decimal(100))
    end = opens + timedelta(seconds=900)
    for builder in (seeded, plain):
        if first:  # the gap is found before the delayed tick arrives, else after
            builder.mark_gap(min(since, arrived))
        builder.on_tick(tick, arrived)
        if builder is seeded:
            builder.seed(tick, Decimal(101), Decimal(99), arrived)
        if not first:
            builder.mark_gap(since)
        builder.on_tick(Tick("BBB", end, Decimal(1)), end)
        builder.advance(end)
    # AAA's one tick is in the first bar, so both the range and the low cover up to its end
    began = min(since, arrived) if first else since
    if min(traded, arrived) <= began < opens + timedelta(seconds=300):  # spoiled
        assert seeded.opening_range("AAA", DAY) == plain.opening_range("AAA", DAY)
        assert seeded.day_low("AAA", DAY) == plain.day_low("AAA", DAY)


@example(seed=None, silent=None, timeout=None)
@example(seed="1", silent="0", timeout="0")
@given(
    seed=st.none() | st.sampled_from(["1", "true", " On ", "yes", "0", "off", "", "no", "maybe"]),
    silent=st.none() | st.sampled_from(["1", "45", "300", "0", "-3", "x", ""]),
    timeout=st.none() | st.sampled_from(["1", "30", "90", "0", "-5", "60m", ""]),
)
def test_live_flags_parsed(seed: str | None, silent: str | None, timeout: str | None) -> None:
    """PR 39 (HAT-DATA-114, HAT-DATA-116), PR 49 (HAT-EXEC-046): `HAT_EXCHANGE_DAY_RANGE` turns
    the exchange-range seed on exactly for a true value (off unset or for anything else),
    `HAT_SYMBOL_SILENT_SECONDS` is a positive whole number, else 120, and
    `HAT_ENTRY_TIMEOUT_MINUTES` a positive whole number, else 60. Harm: unverified tick fields
    seed the opening range and the stop by default, a typo reports every symbol, or a typo
    cancels every entry at once (or never)."""
    env = {}
    if seed is not None:
        env["HAT_EXCHANGE_DAY_RANGE"] = seed
    if silent is not None:
        env["HAT_SYMBOL_SILENT_SECONDS"] = silent
    if timeout is not None:
        env["HAT_ENTRY_TIMEOUT_MINUTES"] = timeout
    loaded = load_settings(env)
    assert loaded.exchange_day_range == (seed is not None and seed.strip().lower() in TRUE_VALUES)
    number = int(silent) if silent is not None and silent.lstrip("-").isdigit() else 0
    assert loaded.symbol_silent_seconds == (number if number > 0 else 120)
    minutes = int(timeout) if timeout is not None and timeout.lstrip("-").isdigit() else 0
    assert loaded.entry_timeout_minutes == (minutes if minutes > 0 else 60)


# --- one symbol's silence (PR 39: HAT-DATA-116) -----------------------------------------------

SILENT_AFTER = 60  # seconds, the watch's threshold in the property
QUIET = ["AAA", "BBB", "CCC"]


@example(  # AAA ticks once and goes quiet; an outage restarts the count; the market closes a while
    closed=(400, 200), ticks=[(20, "AAA")], watches=[], outage=(100, 40), raises=False
)
@example(  # AAA trades every 100 s: asked again after each silence, alerted only once
    closed=None,
    ticks=[(s, "AAA") for s in range(0, 901, 100)],
    watches=[],
    outage=None,
    raises=False,
)
@settings(max_examples=80, deadline=None)
@given(
    closed=st.one_of(st.none(), st.tuples(st.integers(0, 900), st.integers(1, 400))),
    ticks=st.lists(st.tuples(st.integers(0, 900), st.sampled_from(QUIET)), max_size=40),
    watches=st.lists(
        st.tuples(st.integers(0, 900), st.frozensets(st.sampled_from(QUIET))), max_size=6
    ),
    outage=st.one_of(st.none(), st.tuples(st.integers(0, 900), st.integers(1, 300))),
    raises=st.booleans(),
)
def test_symbol_silence_resubscribes_and_alerts(
    closed: tuple[int, int] | None,
    ticks: list[tuple[int, str]],
    watches: list[tuple[int, frozenset[str]]],
    outage: tuple[int, int] | None,
    raises: bool,
) -> None:
    """PR 39: with the heartbeat ticking, a watched symbol is subscribed again exactly when it
    has been silent for the threshold, counted from its last tick, its last resubscribe, or the
    last check at which it was not yet watched, the market closed or the feed stale; the
    symbols found at one check go in one resubscribe, and the owner gets one alert per check
    naming those not alerted since the market last opened. Neither a quiet symbol nor a failing
    alert or resubscribe ever reaches the feed's health. Harm: an illiquid or band-locked name
    makes the account manage-only all day, a dead subscription goes unnoticed, or an alert per
    quiet name per interval floods the owner's chat."""
    start = 10 * 3600
    shut = Downtime(closed)  # the market is closed for a while (as overnight), then reopens

    def is_open(when: datetime) -> bool:
        return not shut.hides(round((when - at_us(start * US)).total_seconds()))

    clock = FixedClock(at_us(start * US))
    sink = Sink()
    alerts: list[tuple[datetime, list[str]]] = []
    resubscribed: list[tuple[datetime, list[str]]] = []
    watched: set[str] = {*QUIET, HEARTBEAT_SYMBOL}

    def alert(symbols: list[str], now: datetime) -> None:
        alerts.append((now, symbols))
        if raises:
            raise RuntimeError("alert sink down")

    def resubscribe(symbols: list[str]) -> None:
        resubscribed.append((clock.now(), symbols))
        if raises:
            raise RuntimeError("provider down")

    watch = SilenceWatch(is_open, alert, resubscribe, silent_after=timedelta(seconds=SILENT_AFTER))
    live = LiveData(
        health=sink, is_open=is_open, clock=clock, silence=watch, watched=lambda: watched
    )
    hidden = Downtime(outage)
    changes = dict(sorted(watches))
    at_second: dict[int, list[str]] = {}
    for second, symbol in ticks:
        at_second.setdefault(second, []).append(symbol)
    checks: list[tuple[datetime, frozenset[str], bool, bool]] = []  # (t, watched, open, stale)
    arrivals: dict[str, list[datetime]] = {s: [] for s in QUIET}
    first_tick: datetime | None = None
    for second in range(901):
        now = at_us((start + second) * US)
        clock.set(now)
        if second in changes:
            watched = {*changes[second], HEARTBEAT_SYMBOL}
        if not hidden.hides(second):
            names = at_second.get(second, []) + ([HEARTBEAT_SYMBOL] if second % 10 == 0 else [])
            for symbol in names:
                first_tick = first_tick or now
                live.on_tick({"symbol": symbol, "ltp": 100.0, "exch_feed_time": now.timestamp()})
                if symbol in arrivals:
                    arrivals[symbol].append(now)
        if second % 5 == 0:
            live.step()
            checks.append((now, frozenset(watched), is_open(now), live.staleness.stale))

    asked = dict(resubscribed)
    told = dict(alerts)
    assert len(asked) == len(resubscribed)  # one call per check
    assert len(told) == len(alerts)
    assert all(names == sorted(names) and names for names in [*asked.values(), *told.values()])
    found = {(s, t) for t, names in resubscribed for s in names}
    alerted = {(s, t) for t, names in alerts for s in names}
    limit = timedelta(seconds=SILENT_AFTER)
    for symbol in QUIET:
        for i, (t, names, open_, stale) in enumerate(checks):
            if symbol not in names:
                assert (symbol, t) not in found
                continue
            first = i
            while first > 0 and symbol in checks[first - 1][1]:
                first -= 1
            run = checks[first : i + 1]
            c0 = run[0][0]
            own = [a for a in arrivals[symbol] if c0 <= a <= t]
            asked_before = [c for c, *_ in run if c < t and (symbol, c) in found]
            paused = [c for c, _, o, st_ in run if not o or st_]
            quiet_from = max([c0, *own, *asked_before, *paused])
            expected = open_ and not stale and t - quiet_from >= limit
            assert ((symbol, t) in found) == expected, (symbol, t)
            opened = max([c for c, _, o, _ in checks[: i + 1] if not o], default=None)
            told_before = (
                not raises
                and any(  # a failed alert is sent again
                    (symbol, c) in found for c, *_ in checks[:i] if opened is None or c > opened
                )
            )
            assert ((symbol, t) in alerted) == (expected and not told_before), (symbol, t)
    opens = at_us(start * US)
    # until the first tick the feed has never ticked: stale by definition, not a symbol's doing
    later = [t for t in sink.stale if first_tick is not None and t > first_tick]
    if outage is None:
        assert later == []  # the heartbeat ticks: no quiet symbol is a stale feed
    for t in later:  # only the outage silences the feed
        assert outage is not None
        assert (
            opens + timedelta(seconds=outage[0]) < t <= opens + timedelta(seconds=sum(outage) + 5)
        )
    if outage is not None and outage[1] >= 40 and closed is None and 10 <= outage[0] <= 860:
        assert later  # and an outage of 40 s or more does


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
    of the first final bar in [09:20, 15:20] whose high is above the pivot and whose close is at
    or above it (`confirm: close`, the default, PR 49), never on a print
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
        (
            bar
            for _, bar in finals
            if bar.high > level and bar.close >= level and bar.final_at <= last_in_window
        ),
        None,
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
    assert trigger.context.bar_close == trigger.context.price == expected.close


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

# the heartbeat is in the pool, so inputs and other owners also hold it (the refcount)
symbol_sets = st.frozensets(
    st.sampled_from([*(f"S{i}" for i in range(12)), HEARTBEAT_SYMBOL]), max_size=8
)
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
    watchlist | positions | candidates | the heartbeat (PR 39: from the first sync until HAT
    releases its symbols), open positions are always in it, the provider is subscribed to
    exactly the symbols some owner holds, and releasing HAT's symbols never unsubscribes a
    symbol another owner holds. A set at or above the limit raises an alert."""
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
    active = False  # HAT holds its set: synced since the last release
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
        active = op[0] in ("hat", "candidates") or (active and op[0] != "release")
        wanted = {s.upper() for s in inputs.watchlist | inputs.positions | inputs.candidates}
        if active:
            wanted.add(HEARTBEAT_SYMBOL)
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
    inside 09:00-09:08, else the open of the first tick at or after the open."""
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
    elif opened is not None and when.time() >= time(9, 15):
        source, price = GapSource.FIRST_TICK, opened
    else:
        assert record is None
        return
    assert record is not None
    assert record.trade_date == DAY
    assert record.source is source
    sent = Decimal(str(float(price)))  # a float round trip, as a feed payload carries it
    assert record.open == sent
    exact = 100 * (Fraction(record.open) / Fraction(Decimal(str(float(prev_close)))) - 1)
    assert abs(Fraction(record.gap_pct) - exact) < Fraction(1, 10**20)
    # a later first tick never replaces a record already made
    later = {k: v for k, v in payload.items() if k != "indicative_open_price"}
    again = gaps.observe({**later, "open_price": float(prev_close * 2)}, when)
    assert again is None
    assert gaps.records(DAY)["AAA"] == record


# --- HAT-DEC-I4, HAT-DATA-119: the optional minute-history loader ---------------------------


@st.composite
def minute_candles(draw: st.DrawFn, days: int = 1) -> list[list[Any]]:
    """Raw history candles ([epoch s, open, high, low, close, volume], prices on the paise grid
    as the broker sends them) at distinct minutes from 09:00 to 15:44 IST, some outside the
    session."""
    minutes = draw(
        st.sets(
            st.tuples(st.integers(0, days - 1), st.integers(9 * 60, 15 * 60 + 44)),
            max_size=40,
        )
    )
    rows = []
    for day, minute in sorted(minutes):
        start = MIDNIGHT + timedelta(days=day, minutes=minute)
        a, b = sorted(draw(st.lists(st.integers(100, 100_000), min_size=2, max_size=2)))
        open_, close = (draw(st.integers(a, b)) for _ in range(2))
        volume = draw(st.integers(0, 10**6))
        rows.append([int(start.timestamp()), open_ / 100, b / 100, a / 100, close / 100, volume])
    return rows


def _builder_bars(symbol: str, candles: list) -> list[Bar]:
    """The bars the live builder makes of the candles' prints, each arriving on time."""
    builder = BarBuilder()
    for candle in candles:
        for tick in candle_prints(symbol, candle):
            builder.on_tick(tick, tick.at)
    return builder.advance(MIDNIGHT + timedelta(days=30))


@given(raw=minute_candles())
@example(raw=[])
def test_history_matches_builder(raw: list[list[Any]]) -> None:
    """Aggregating a minute series to 5-minute bars gives the bars the live builder makes of
    the same prints: same windows, open, high, low, close and volume; nothing outside the
    session."""
    candles = parse_candles(raw)
    assert aggregate("AAA", candles) == _builder_bars("AAA", candles)


class _Clock:
    """A clock that only moves when the pacer sleeps or a call takes time."""

    def __init__(self, start: datetime) -> None:
        self.at = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.slept.append(seconds)
        self.at += timedelta(seconds=seconds)


PLAN_REFUSAL = {
    "s": "error",
    "code": -50,
    "message": "Invalid input",
    "data": {"range_from": "range_from exceeds the historical data available for your plan"},
}


def _fake_history(
    raw: dict[str, list[list[Any]]],
    asked: list[dict[str, str]],
    *,
    floor: date | None = None,
    failing: frozenset[str] = frozenset(),
    clock: _Clock | None = None,
    took: timedelta = timedelta(0),
) -> Callable[[dict[str, str]], dict[str, Any]]:
    """A Fyers history endpoint over `raw` (broker symbol -> candles): refuses a range before
    `floor` for the plan's depth, answers -16 for the `failing` symbols, `no_data` when a range
    holds no candle; each call takes `took` on `clock`."""

    def history(request: dict[str, str]) -> dict[str, Any]:
        asked.append(request)
        if clock is not None:
            clock.at += took
        lo, hi = date.fromisoformat(request["range_from"]), date.fromisoformat(request["range_to"])
        if floor is not None and lo < floor:
            return PLAN_REFUSAL
        if request["symbol"] in failing:
            return {"s": "error", "code": -16, "message": "token expired for client XY1234"}
        rows = [
            r
            for r in raw.get(request["symbol"], [])
            if lo <= datetime.fromtimestamp(r[0], IST).date() <= hi
        ]
        return {"s": "ok", "candles": rows} if rows else {"s": "no_data", "candles": []}

    return history


def _stored(store: BarStore, start: date, end: date) -> list[Any]:
    rows = []
    day = start
    while day <= end:
        rows.extend(store.read(day))
        day += timedelta(days=1)
    return rows


def _want(symbol: str, raw: list[list[Any]], instrument: str, start: date, end: date) -> list[Any]:
    inside = [c for c in parse_candles(raw) if start <= c.at.astimezone(IST).date() <= end]
    return [row_from_bar(b, instrument) for b in _builder_bars(symbol, inside)]


@given(
    raw=minute_candles(days=3),
    first=st.integers(0, 2),
    span=st.integers(0, 250),
    chunk=st.integers(1, CHUNK_DAYS),
    failing=st.sampled_from(["NSE:AAA-EQ", "NSE:MMM-EQ", "NSE:ZZZ-EQ", None]),
    cap=st.integers(1, 60),
    garbage=st.sampled_from(
        [
            None,
            [None],
            [{}],
            ["x"],
            [[1, 2]],
            [[10**20, 1.0, 1.0, 1.0, 1.0, 0]],
            [[0, 1.0, 1.0]],
            [[0, 1e300, 1e300, 1e300, 1e300, 0]],
        ]
    ),
    envelope=st.sampled_from([None, [], "error", 0, {"s": "ok"}, {"s": "ok", "candles": None}]),
)
@settings(deadline=None)
def test_minute_history_aggregates_to_5m(
    raw: list[list[Any]],
    first: int,
    span: int,
    chunk: int,
    failing: str | None,
    cap: int,
    garbage: list[Any] | None,
    envelope: object,
) -> None:
    """The loader asks for 1-minute candles in consecutive chunks of at most `chunk_days`
    covering the period, and writes each symbol's builder bars as soon as it loads: a symbol
    whose call fails, or whose answer holds more candles than the cap, is reported with a fixed
    code (no broker text) and has nothing written, while every other symbol, before or after it,
    is stored. A garbage candle row (not a list, too short, an impossible epoch) fails its symbol
    (a price too large for a decimal included) with `history_bad_candle`, and an answer that is
    not a mapping, or an "ok" without candles, fails its symbol with `history_bad_answer`, never
    the run, however many in a row."""
    start = DAY + timedelta(days=first)
    end = start + timedelta(days=span)
    symbols = {s: str(uuid4()) for s in ("NSE:AAA-EQ", "NSE:MMM-EQ", "NSE:ZZZ-EQ")}
    asked: list[dict[str, str]] = []
    clean = _fake_history(dict.fromkeys(symbols, raw), asked, failing=frozenset({failing} - {None}))

    def history(request: dict[str, str]) -> dict[str, Any]:
        answer = clean(request)
        if garbage is not None and request["symbol"] == "NSE:MMM-EQ" and answer["s"] != "error":
            return {"s": "ok", "candles": garbage}
        return answer

    limits = HistoryLimits(chunk_days=chunk, max_candles=cap, per_minute=10**6)
    with tempfile.TemporaryDirectory() as root:
        store = BarStore(Path(root))
        report = load_minute_history(history, store, symbols, start, end, limits=limits)
        assert report.stopped is None
        for symbol in symbols:
            spans = [
                (date.fromisoformat(r["range_from"]), date.fromisoformat(r["range_to"]))
                for r in asked
                if r["symbol"] == symbol
            ]
            assert all(r["resolution"] == "1" for r in asked)
            assert spans[0][0] == start
            if symbol in report.failed:
                continue
            assert spans[-1][1] == end
            assert all((hi - lo).days < chunk for lo, hi in spans)
            assert all(
                b[0] == a[1] + timedelta(days=1) for a, b in zip(spans, spans[1:], strict=False)
            )
        too_many = any(
            len([r for r in raw if lo <= datetime.fromtimestamp(r[0], IST).date() <= hi]) > cap
            for lo, hi in (
                (date.fromisoformat(r["range_from"]), date.fromisoformat(r["range_to"]))
                for r in asked
            )
        )
        expected_failed = {}
        if failing is not None:
            expected_failed[failing] = "history_failed"
        if too_many:
            expected_failed |= {s: "history_too_many_candles" for s in symbols if s != failing}
        if garbage is not None and failing != "NSE:MMM-EQ":
            expected_failed["NSE:MMM-EQ"] = "history_bad_candle"
        assert report.failed == expected_failed
        assert all("XY1234" not in code for code in report.failed.values())
        want = sorted(
            (
                row
                for symbol, instrument in symbols.items()
                if symbol not in report.failed
                for row in _want(symbol, raw, instrument, start, end)
            ),
            key=lambda r: (r.bar_start, r.instrument_id),
        )
        assert _stored(store, start, end) == want

        # an answer that is not a mapping at all fails its symbol only, and a run of them is
        # not a dead session: every symbol is tried
        bare = BarStore(Path(root) / "bare")
        enveloped = load_minute_history(
            lambda _request: envelope, bare, symbols, start, end, limits=limits
        )
        assert enveloped.stopped is None
        assert enveloped.failed == dict.fromkeys(symbols, "history_bad_answer")


@given(
    raw=minute_candles(days=3),
    per_minute=st.integers(1, 5),
    per_day=st.integers(1, 12),
    spent=st.integers(0, 12),
    took=st.integers(0, 40),
    offset=st.integers(0, 24 * 3600 - 1),
)
@settings(deadline=None)
def test_minute_history_budget(
    raw: list[list[Any]], per_minute: int, per_day: int, spent: int, took: int, offset: int
) -> None:
    """However long calls take and whatever the day's earlier runs spent, no 60 seconds hold
    more than `per_minute` history calls and no IST day more than `per_day`, also across a
    second run started right after the first (a fresh pacer on the saved progress); a spent day
    stops a run with `history_daily_limit` (not a crash) and the day's count is kept."""
    start = DAY
    end = DAY + timedelta(days=2)
    symbols = {f"NSE:S{i}-EQ": str(uuid4()) for i in range(4)}
    clock = _Clock(MIDNIGHT + timedelta(days=10, seconds=offset))
    asked: list[dict[str, str]] = []
    instants: list[datetime] = []
    inner = _fake_history(
        dict.fromkeys(symbols, raw), asked, clock=clock, took=timedelta(seconds=took)
    )

    def history(request: dict[str, str]) -> dict[str, Any]:
        instants.append(clock.now())
        return inner(request)

    limits = HistoryLimits(chunk_days=1, per_minute=per_minute, per_day=per_day)
    with tempfile.TemporaryDirectory() as root:
        progress = Progress(Path(root) / "progress.json")
        progress.calls[clock.now().astimezone(IST).date().isoformat()] = spent
        progress.save()
        reports = []
        for part in (dict(list(symbols.items())[:2]), dict(list(symbols.items())[2:])):
            progress = Progress.load(Path(root) / "progress.json")
            reports.append(
                load_minute_history(
                    history,
                    BarStore(Path(root)),
                    part,
                    start,
                    end,
                    limits=limits,
                    progress=progress,
                    pacer=CallPacer(limits, progress, clock=clock.now, sleep=clock.sleep),
                )
            )
        for i, at in enumerate(instants):
            window = [t for t in instants[: i + 1] if t > at - timedelta(minutes=1)]
            assert len(window) <= per_minute
        per_ist_day: dict[date, int] = {}
        for at in instants:
            per_ist_day[at.astimezone(IST).date()] = (
                per_ist_day.get(at.astimezone(IST).date(), 0) + 1
            )
        first_day = (MIDNIGHT + timedelta(days=10, seconds=offset)).astimezone(IST).date()
        for day, n in per_ist_day.items():
            assert n + (spent if day == first_day else 0) <= per_day
        assert sum(r.calls for r in reports) == len(instants)
        if len(asked) < len(symbols) * 3:
            assert reports[-1].stopped == "history_daily_limit"
        else:
            assert all(r.stopped is None for r in reports)
        stored = Progress.load(Path(root) / "progress.json")
        assert stored.calls == progress.calls
        assert sum(stored.calls.values()) == spent + len(instants)


# The reply Fyers gives once the plan's daily requests are spent (the message the owner saw).
RATE_LIMITED = {"s": "error", "code": -360, "message": "Standard plan limit of 5,000 requests/day"}


@given(raw=minute_candles(days=3), limit=st.integers(0, 14))
@settings(deadline=None)
def test_minute_history_rate_limit_stops_run(raw: list[list[Any]], limit: int) -> None:
    """Once Fyers answers -360 (the plan's daily requests spent), the run stops at that call with
    `history_rate_limited`, not `history_failed`, and asks nothing more: the symbol it stopped on
    is neither failed nor done (a resume asks for it again), every symbol loaded before it is
    stored and done, and no broker text is kept."""
    start, end = DAY, DAY + timedelta(days=2)
    symbols = {f"NSE:S{i}-EQ": str(uuid4()) for i in range(4)}
    asked: list[dict[str, str]] = []
    inner = _fake_history(dict.fromkeys(symbols, raw), asked)

    def history(request: dict[str, str]) -> dict[str, Any]:
        return RATE_LIMITED if len(asked) >= limit else inner(request)

    limits = HistoryLimits(chunk_days=1, per_minute=10**6)
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "progress.json"
        progress = Progress.load(path)
        report = load_minute_history(
            history, BarStore(Path(root)), symbols, start, end, limits=limits, progress=progress
        )
        loaded = list(symbols)[: limit // 3]
        assert report.calls == min(limit + 1, 3 * len(symbols))
        assert report.stopped == ("history_rate_limited" if limit < 3 * len(symbols) else None)
        assert not report.failed
        assert set(Progress.load(path).done) == set(loaded)
        assert set(report.days) == set(loaded)
        assert "5,000" not in path.read_text()


@given(
    raw=minute_candles(days=3),
    budget=st.integers(0, 12),
    failing=st.sets(st.sampled_from([f"NSE:S{i}-EQ" for i in range(4)])),
)
@settings(deadline=None)
def test_minute_history_resume(raw: list[list[Any]], budget: int, failing: set[str]) -> None:
    """A run stopped anywhere (the day's budget spent, symbols that failed) and then resumed the
    next day leaves `bars_5m` as one uninterrupted run would (the resume continues the stopped
    run's period, not the shifted one the next day asks for), and never asks again for a symbol
    already loaded over it."""
    start, end = DAY, DAY + timedelta(days=2)
    symbols = {f"NSE:S{i}-EQ": str(uuid4()) for i in range(4)}
    limits = HistoryLimits(chunk_days=1, per_minute=10**6, per_day=budget or 1)
    with tempfile.TemporaryDirectory() as root:
        once = BarStore(Path(root) / "once")
        report = load_minute_history(
            _fake_history(dict.fromkeys(symbols, raw), []), once, symbols, start, end
        )
        assert report.stopped is None
        assert not report.failed

        store = BarStore(Path(root) / "resumed")
        path = Path(root) / "progress.json"
        clock = _Clock(MIDNIGHT + timedelta(days=10, hours=10))
        first_asked: list[dict[str, str]] = []
        progress = Progress.load(path)
        stopped = load_minute_history(
            _fake_history(dict.fromkeys(symbols, raw), first_asked, failing=frozenset(failing)),
            store,
            symbols,
            start,
            end,
            limits=HistoryLimits(
                chunk_days=1, per_minute=10**6, per_day=budget or 1, failures_in_row=4
            ),
            progress=progress,
            pacer=CallPacer(limits, progress, clock=clock.now, sleep=clock.sleep),
        )
        done = set(Progress.load(path).done)
        assert done == set(stopped.days)
        clock.at += timedelta(days=1)  # the next day: a fresh budget
        again: list[dict[str, str]] = []
        progress = Progress.load(path)
        resumed = load_minute_history(
            _fake_history(dict.fromkeys(symbols, raw), again),
            store,
            symbols,
            start + timedelta(days=1),  # the next day's command line asks for a shifted period
            end + timedelta(days=1),
            limits=HistoryLimits(chunk_days=1, per_minute=10**6),
            progress=progress,
            pacer=CallPacer(
                HistoryLimits(chunk_days=1, per_minute=10**6),
                progress,
                clock=clock.now,
                sleep=clock.sleep,
            ),
            resume=True,
        )
        assert resumed.stopped is None
        assert not resumed.failed
        assert resumed.skipped == len(done)
        assert not {r["symbol"] for r in again} & done
        assert _stored(store, start, end) == _stored(once, start, end)


@given(
    raw=minute_candles(days=3),
    late=st.integers(0, 2),
    back=st.integers(0, 3),
    other=st.booleans(),
)
@settings(deadline=None)
def test_minute_history_plan_clamp(raw: list[list[Any]], late: int, back: int, other: bool) -> None:
    """A -50 refusal for the plan's depth clamps the start to where the plan serves (the run
    goes on and every symbol is loaded from there to the end); any other -50 is a failure of
    the symbol, never a clamp."""
    today = DAY + timedelta(days=2)
    clock = _Clock(MIDNIGHT + timedelta(days=2, hours=10))
    plan_days = 2  # the plan serves from DAY + `late` days
    floor = today - timedelta(days=plan_days) + timedelta(days=late)
    start = floor - timedelta(days=back)
    end = today
    symbols = {f"NSE:S{i}-EQ": str(uuid4()) for i in range(3)}
    asked: list[dict[str, str]] = []
    refused = _fake_history(dict.fromkeys(symbols, raw), asked, floor=floor)
    bad = {"s": "error", "code": -50, "message": "Invalid input", "data": {"range_from": "bad"}}

    def history(request: dict[str, str]) -> dict[str, Any]:
        answer = refused(request)
        return bad if other and answer is PLAN_REFUSAL else answer

    limits = HistoryLimits(chunk_days=2, per_minute=10**6, plan_days=plan_days)
    with tempfile.TemporaryDirectory() as root:
        store = BarStore(Path(root))
        progress = Progress(Path(root) / "progress.json")
        report = load_minute_history(
            history,
            store,
            symbols,
            start,
            end,
            limits=limits,
            progress=progress,
            pacer=CallPacer(limits, progress, clock=clock.now, sleep=clock.sleep),
        )
        if other and back:
            assert report.available_from is None
            assert set(report.failed) == set(symbols)
            assert report.failed == dict.fromkeys(symbols, "history_failed")
            assert _stored(store, start, end) == []
            return
        assert report.stopped is None
        assert not report.failed
        assert report.available_from == (floor if back else None)
        assert Progress.load(Path(root) / "progress.json").available_from == report.available_from
        assert report.clamps <= limits.max_clamps
        assert (report.clamps > 0) == (back > 0)
        want = sorted(
            (r for s, i in symbols.items() for r in _want(s, raw, i, floor, end)),
            key=lambda r: (r.bar_start, r.instrument_id),
        )
        assert _stored(store, start, end) == want


# --- the dated live price (PR 51; ME-F6, ME-F7, MX-F11) ---------------------------------------

LIVE_AT = datetime.combine(DAY, time(11, 0), tzinfo=IST)
BAND_PCTS = st.sampled_from([None, *(Decimal(b) for b in (2, 5, 10, 20))])
TICKS = st.sampled_from([None, Decimal("0.01"), Decimal("0.05"), Decimal(1)])


def _paise(n: int) -> Decimal:
    return Decimal(n) / 100


def _instrument_rows(session: Session, ids: list[UUID]) -> dict[UUID, str]:
    """`HatInstrument` rows for the engine world's instrument ids (it seeds none)."""
    symbols = {}
    for n, iid in enumerate(ids):
        symbols[iid] = f"LIVE{n}"
        session.add(
            HatInstrument(
                id=iid,
                market_id=uuid4(),
                isin=f"INE9{n:08d}",
                series="EQ",
                symbol=symbols[iid],
                name="n",
                face_value=Decimal(10),
                active=True,
            )
        )
    session.flush()
    return symbols


@settings(max_examples=300)
@given(
    price=st.integers(1, 10**7),
    age_ms=st.integers(-120_000, 120_000),  # negative: stamped ahead of now
    lower=st.one_of(st.none(), st.integers(1, 10**7)),
    upper=st.one_of(st.none(), st.integers(1, 10**7)),
    known=st.booleans(),
)
@example(price=100, age_ms=30_000, lower=None, upper=None, known=True)  # exactly 30 s: stale
@example(price=100, age_ms=29_999, lower=None, upper=None, known=True)
@example(price=100, age_ms=-30_000, lower=None, upper=None, known=True)
@example(price=100, age_ms=0, lower=100, upper=100, known=True)  # at both bounds: usable
def test_usable_price(
    price: int, age_ms: int, lower: int | None, upper: int | None, known: bool
) -> None:
    """PR 51 (ME-F6, ME-F7, HAT-DATA-116): a Take, a placement or an exit acts only on a price
    whose tick is less than the stale threshold old (30 s; one exactly 30 s old, or stamped 30 s
    or more ahead, is stale), inside the day's band where a bound is known; otherwise no price
    and a fixed reason (`price_unknown` without a tick). Harm: a 13:40 print drives a 15:21
    MARKET SELL, or a confirm Taken after a failed breakout buys under its own stop."""
    quote = Quote(_paise(price), LIVE_AT - timedelta(milliseconds=age_ms)) if known else None
    low, high = (None if b is None else _paise(b) for b in (lower, upper))
    got = usable_price(quote, LIVE_AT, lower=low, upper=high)
    if not known:
        assert got == (None, PRICE_UNKNOWN)
    elif abs(age_ms) >= STALE_AFTER.total_seconds() * 1000:
        assert got == (None, PRICE_STALE)
    elif (lower is not None and price < lower) or (upper is not None and price > upper):
        assert got == (None, PRICE_OUT_OF_BAND)
    else:
        assert got == (_paise(price), None)


@settings(max_examples=100, deadline=None)
@given(
    ticks=st.lists(
        st.tuples(
            st.integers(0, 2),  # which instrument
            st.integers(1, 10**6),  # price in paise
            st.one_of(st.none(), st.integers(-5, 0)),  # exchange time, seconds before arrival
            st.integers(0, 3),  # seconds since the previous arrival
        ),
        max_size=25,
    )
)
@example(ticks=[(0, 100, 0, 1), (0, 200, -5, 1)])  # a late print, 4 s older than the kept one
def test_price_book_dates_each_tick(ticks: list[tuple[int, int, int | None, int]]) -> None:
    """PR 51: the price book keeps, per instrument, the latest tick's price and its exchange time
    (`exch_feed_time`), else the time it arrived, so every price HAT acts on carries the time it
    traded; a print that arrives after a newer one (by exchange time) does not replace it. Harm:
    a price that has not traded for an hour reads as current, or a late print regresses it."""
    with Session(memory_engine()) as session:
        world = seed_engine_world(session, instruments=3)
        symbols = _instrument_rows(session, world.instruments)
        book = PriceBook()
        book.track_session(session, set(world.instruments))
    clock = FixedClock(LIVE_AT)
    live = LiveData(health=QuietHealth(), is_open=lambda _t: True, clock=clock)
    live.add_tick_listener(book.on_tick)
    expected: dict[UUID, Quote] = {}
    now = LIVE_AT
    for index, paise, exch, gap in ticks:
        now += timedelta(seconds=gap)
        clock.set(now)
        iid = world.instruments[index]
        payload: dict[str, object] = {"symbol": symbols[iid], "ltp": str(_paise(paise))}
        if exch is not None:
            payload["exch_feed_time"] = int(now.timestamp()) + exch
        live.on_tick(payload)
        at = now if exch is None else datetime.fromtimestamp(int(now.timestamp()) + exch, UTC)
        kept = expected.get(iid)
        if kept is None or at >= kept.at:
            expected[iid] = Quote(_paise(paise), at)
    for iid in world.instruments:
        assert book.quote(iid) == expected.get(iid)
        assert book.price(iid) == (expected[iid].price if iid in expected else None)


@settings(max_examples=300)
@given(
    prev=st.integers(100, 10**7),
    band=BAND_PCTS,
    tick=TICKS,
    known=st.booleans(),
    offset=st.integers(-5, 5),  # ticks from a band price
)
@example(prev=10_000, band=Decimal(5), tick=Decimal("0.05"), known=True, offset=0)
def test_circuit_locks_read_the_band(
    prev: int, band: Decimal | None, tick: Decimal | None, known: bool, offset: int
) -> None:
    """PR 51 (MX-F11): the lower and upper circuit locks read the stored band of the
    day: the lower band price is the previous close less the band, up to the tick grid; a price
    at or below it is locked at the lower circuit; no band row tells nothing (None) and an F&O
    name (no band) is never locked. The prices a live price may take are the band's, one tick
    wider each way for the exchange's own rounding. Harm: a sale or an entry is judged against
    a placeholder band, or a real print at the circuit is refused as impossible."""
    daily = DailyInputs(
        prev_close=_paise(prev), tick=tick, band_known=known, band_pct=band if known else None
    )
    step = tick if tick is not None else Decimal("0.01")
    lower, upper = lower_circuit_price(daily), upper_circuit_price(daily)
    low_bound, high_bound = band_bounds(daily)
    if not known:
        assert locked_lower_circuit(_paise(prev), daily) is None
        assert (low_bound, high_bound) == (None, None)
        return
    if band is None:
        assert lower is None
        assert upper is None
        assert locked_lower_circuit(_paise(prev), daily) is False
        assert (low_bound, high_bound) == (None, None)
        return
    assert lower is not None
    assert upper is not None
    raw = _paise(prev) * (100 - band) / 100
    if tick is not None:
        assert lower % tick == 0
        assert raw <= lower < raw + tick
    else:
        assert lower == raw
    price = lower + offset * step
    if price > 0:
        assert locked_lower_circuit(price, daily) is (price <= lower)
    slack = tick if tick is not None else Decimal(0)
    assert (low_bound, high_bound) == (lower - slack, upper + slack)
    for bound in (lower, upper):  # a print at either band price is a real price
        assert usable_price(Quote(bound, LIVE_AT), LIVE_AT, lower=low_bound, upper=high_bound)[0]
    beyond = upper + slack + step
    assert usable_price(Quote(beyond, LIVE_AT), LIVE_AT, lower=low_bound, upper=high_bound) == (
        None,
        PRICE_OUT_OF_BAND,
    )


class _LevelsBook:
    """The day's levels: a 10% band around 100 and an exit view at any price."""

    DAILY = DailyInputs(
        prev_close=Decimal(100), tick=Decimal("0.05"), band_known=True, band_pct=Decimal(10)
    )

    def day(self, _day: date) -> "_LevelsBook":
        return self

    def daily(self, _session: Session, _iid: object) -> DailyInputs:
        return self.DAILY

    def exit_view(self, _iid: object, price: Decimal, _rules: object, held: int) -> ExitView:
        return ExitView(price=price, days_held=held, sma={10: price})


class _OpenGates:
    def quality_failed(self, *_args: object) -> bool:
        return False

    def gate_on(self, *_args: object) -> bool:
        return True


@settings(max_examples=60, deadline=None)
@given(
    price=st.one_of(st.none(), st.integers(8_000, 12_500)),  # paise x 100: 80.00 .. 125.00
    age_s=st.integers(-40, 90),
)
@example(price=11_000, age_s=29)  # the upper band price, 29 s old: acted on
@example(price=11_005, age_s=0)  # the upper band price plus its tick of slack: acted on
@example(price=8_995, age_s=0)  # the lower band price less its tick of slack: acted on
@example(price=11_006, age_s=0)  # beyond the band and its tick of slack
@example(price=10_000, age_s=30)
def test_exits_and_placement_read_the_checked_price(price: int | None, age_s: int) -> None:
    """PR 51 (ME-F6, ME-F7): the exits' view and the placement check (used at a Take, an auto
    entry and a resumed one) carry the live price only when it is fresh and inside the band;
    otherwise the exits get no view (no rule fires; the broker stop still rests) and the
    placement check carries no price and the refusal's code, so no entry is placed. Harm: a
    stale or impossible print sells a whole position or places an entry."""
    clock = FixedClock(datetime.combine(TRADE_DAY, time(11, 0), tzinfo=IST))
    now = clock.now()
    with Session(memory_engine()) as session:
        world = seed_engine_world(session, instruments=1)
        trade = open_trade(session, world)
        position = session.get(HatPosition, trade.outcome.position_id)
        assert position is not None
        iid = position.instrument_id
        symbol = _instrument_rows(session, [iid])[iid]
        quotes = PriceBook()
        quotes.track_session(session, {iid})
        if price is not None:
            quotes.on_tick(Tick(symbol, now - timedelta(seconds=age_s), _paise(price)))
        book = _LevelsBook()
        prices = LivePrices(quotes, book, clock)  # type: ignore[arg-type]
        # prev 100, a 10% band on a 0.05 tick: 90.00 .. 110.00, a tick wider each way
        lower, upper = Decimal("89.95"), Decimal("110.05")
        assert band_bounds(book.DAILY) == (lower, upper)
        quote = quotes.quote(iid)
        usable, why = usable_price(quote, now, lower=lower, upper=upper)
        views = ExitViews(book, prices.price, clock, lambda: CALENDAR)  # type: ignore[arg-type]
        view = views(session, position)
        assert (view is None) == (usable is None)
        if view is not None:
            assert view.price == usable
        route = LiveRoute(
            lambda _a: None,
            _OpenGates(),  # type: ignore[arg-type]
            lambda: False,
            clock,
            lambda: CALENDAR,
            prices,
        )
        check = route.check(session, world.bot_id, iid, now)
        assert (check.ltp, check.ltp_refusal) == (usable, why)
        refusal = placement_refusal(check, now)
        assert (refusal is None) == (usable is not None)
