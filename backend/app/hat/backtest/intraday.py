"""A backtest session on its stored 5-minute bars: the live trigger replayed (HAT-BT-020,
HAT-EXEC-120..122, HAT-BOT-052).

Where `bars_5m` holds a session of an instrument the loop has a candidate for, the candidate is
decided as live decides it: `session_bars` reads only that session's bars of those instruments
(one day file, never the whole store) and puts them on the run's price basis (prices times the
day's adjusted-to-raw close ratio, volumes divided by it, as `bars_daily_cap_adj` adjusts the
daily bar), and `replay` feeds them in time order to the live `TriggerMonitor` through a
`trigger.BarView` of the bars seen so far. The opening range is the session's 09:15 bar (none
when the bars do not start at the open: such a session fails closed, as live does without the
first bar), the day volume is the volume of the bars up to the one being judged and the day low
the lowest low through it. So the level, the close confirmation, `volume_pace`, `gap_up`,
`early_volume`, the entry LIMIT, the low-of-day stop and the entry filters are live's own.

A candidate live fires on a trade (`entry_timing: stop_order` or `next_session`,
`TriggerMonitor.on_tick`) is replayed on trades read from each bar (`bar_ticks`), before the bar
is final: the 09:15 bar's close at 09:20 (the price at 09:20), then each bar's open at its
start and, where the bar rose to it, each such candidate's stop trigger (`trigger.first_above`
the level) just after. So a stop order fills at the 09:15 close when that is above the level,
else at the stop trigger or a later bar's open above it; a next-session entry at the 09:15
close. Their other trigger criteria read the volume of the final bars before the trade, as live
does.

`catalyst_attached` cannot be judged (no announcements are stored for the past): the replay
evaluates under `NO_CATALYST_MODE`, a mode no `required_in` lists, so it is recorded and never
blocks (the known gap `catalyst_not_evaluated`).

A pre-open playbook's session without bars is estimated from its daily bar with `EP_ESTIMATE`,
calibrated on the 5-minute years (2022-01..2026-10, the 279 neglected gaps of at least 10% with
bars from 09:15): live fired on 24% of them, at a median entry 2.48% over the open and 0.58% over
the opening-range high, with the low before the trigger a median 4.02% under the open. The rule
"high at least 1% over the open, close in the upper half of the day's range, day volume at
least the early-volume multiple of ADV(20)" fired on 25% and agreed with live on 84% of the
days; a naive high-over-the-open rule fired on 94%.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from app.paths import BARS5M_GAPS

from ..data.audit import CLOSE_5M_PCT
from ..data.bars import BAR_FINAL_LAG, BAR_WIDTH, Bar, Tick
from ..data.bars5m_gaps import gap_spans, in_gap, read_gaps
from ..data.bars_store import BarStore, RejectRow
from ..data.sessions import SESSION_CLOSE, SESSION_OPEN
from ..engine.trigger import (
    Candidate,
    CriterionResult,
    Trigger,
    TriggerMonitor,
    daily_level,
    evaluate_trigger,
    first_above,
    in_window,
)
from ..money import MONEY_SCALE, require_exact
from ..time import IST
from .ep_log import trigger_depth

__all__ = [
    "EP_ESTIMATE",
    "Admission",
    "NO_CATALYST_MODE",
    "EpEstimate",
    "EpTrace",
    "after_trigger",
    "bar_ticks",
    "traded_low_after",
    "REPLAY_RULE",
    "on_basis",
    "lacks_refused",
    "screened_buckets",
    "session_admitted",
    "replay",
    "session_bars",
    "trace",
]

# A mode no `catalyst_attached.required_in` can list: the catalyst never blocks a replay.
NO_CATALYST_MODE = "backtest"
_STEP = Decimal(1).scaleb(-MONEY_SCALE)
_CLOSE_OFF_PCT = require_exact(
    CLOSE_5M_PCT, "close_5m_pct", floats=True
)  # the data audit's, exactly


@dataclass(frozen=True)
class EpEstimate:
    """A pre-open playbook's trigger on a daily bar (percentages of the open): it fires when the
    high is at least `fire_pct` over the open and the close in the upper half of the range; the
    entry is `entry_pct` over the open (at most the high), breaking a level `chase_pct` under
    the entry, after a low `low_pct` under the open (at least the day's low)."""

    fire_pct: Decimal
    entry_pct: Decimal
    chase_pct: Decimal
    low_pct: Decimal

    def fires(self, open_: Decimal, high: Decimal, low: Decimal, close: Decimal) -> bool:
        return high * 100 >= open_ * (100 + self.fire_pct) and close * 2 >= high + low

    def entry(self, open_: Decimal, high: Decimal) -> Decimal:
        return min(high, open_ * (100 + self.entry_pct) / 100)

    def level(self, entry: Decimal) -> Decimal:
        return entry * 100 / (100 + self.chase_pct)

    def low_before(self, open_: Decimal, low: Decimal) -> Decimal:
        return max(low, open_ * (100 - self.low_pct) / 100)


EP_ESTIMATE = EpEstimate(Decimal(1), Decimal("2.48"), Decimal("0.58"), Decimal("4.02"))


def session_bars(
    store: BarStore, day: date, scales: Mapping[str, Decimal]
) -> dict[str, tuple[Bar, ...]]:
    """The stored 5-minute bars of `day` of each instrument in `scales`, on the run's price basis
    (`scales`: the day's adjusted close over its raw close), in time order; an instrument
    without bars that day is absent. Each bar's `symbol` is its instrument id."""
    if not scales or not store.path(day).is_file():
        return {}
    out: dict[str, list[Bar]] = {}
    for row in store.read(day, scales):
        scale = scales[row.instrument_id]

        def price(value: Decimal, scale: Decimal = scale) -> Decimal:
            return (value * scale).quantize(_STEP, ROUND_HALF_EVEN)

        volume = int((row.volume / scale).to_integral_value(ROUND_HALF_EVEN))
        out.setdefault(row.instrument_id, []).append(
            Bar(
                row.instrument_id,
                row.bar_start.astimezone(IST),
                price(row.open),
                price(row.high),
                price(row.low),
                price(row.close),
                volume,
            )
        )
    return {k: tuple(v) for k, v in out.items()}


REPLAY_RULE = "first_bar_clean_grid_open_5pct_no_refused_bucket_no_coverage_gap"


def on_basis(day: date, bars: Sequence[Bar], daily_open: Decimal) -> bool:
    """Whether a session's 5-minute bars fit its daily bar, by what is known at the open only:
    the regular session's first (09:15) bar present, every bar on the IST 5-minute grid of
    `day` and each once, and the 09:15 open within `CLOSE_5M_PCT` of the daily open (both on
    one price basis). A missing bucket, interior or trailing, is a window without a trade: live's
    bar builder makes no bar for one (`data.bars`), so the replay sees what live saw and trades
    nothing there. Nothing after the open decides it (no last bar, no close), so a session is
    never chosen by how it ended; a truncated copy is refused by its coverage
    (`bars5m_gaps`), a session lacking a bucket the import refused by `lacks_refused`. A
    session that does not fit is decided on the daily estimate, so no run reads bars off their
    day (`REPLAY_RULE`, in the run's conventions)."""
    if not bars or daily_open <= 0:
        return False
    opens = datetime.combine(day, SESSION_OPEN, tzinfo=IST)
    closes = datetime.combine(day, SESSION_CLOSE, tzinfo=IST)
    starts: dict[datetime, Bar] = {}
    for bar in bars:
        start = bar.start.astimezone(IST)
        if not opens <= start < closes or (start - opens) % BAR_WIDTH or start in starts:
            return False
        starts[start] = bar
    first = starts.get(opens)
    if first is None:
        return False
    return abs(first.open / daily_open - 1) * 100 <= _CLOSE_OFF_PCT


def session_admitted(
    day: date,
    bars: Sequence[Bar],
    daily_open: Decimal,
    refused: Collection[datetime],
    gaps: Mapping[str, tuple[tuple[date, date], ...]],
    isin: str | None,
) -> bool:
    """Whether a session's five-minute bars may be read: it fits its daily bar (`on_basis`),
    lacks no bucket the import refused (`lacks_refused`, `refused` the session's) and lies in no
    known coverage gap (`bars5m_gaps.in_gap` of its `isin`). The one admission rule of the
    backtest's replay, its 09:20 prices, the shadow-day replay and the 09:20 measurement."""
    return (
        not lacks_refused(bars, refused)
        and not in_gap(gaps, isin, day)
        and on_basis(day, bars, daily_open)
    )


@dataclass(frozen=True)
class Admission:
    """What admits a session's five-minute bars outside a run (`session_admitted`): the buckets
    the import refused, by (instrument, day), and the known coverage gaps by ISIN, read from the
    store as a run reads them (`backtest.data`)."""

    refused: Mapping[tuple[str, date], frozenset[datetime]]
    gaps: Mapping[str, tuple[tuple[date, date], ...]]

    @classmethod
    def read(cls, store: BarStore) -> Admission:
        return cls(screened_buckets(store.read_rejects()), gap_spans(read_gaps(BARS5M_GAPS)))

    def admits(
        self,
        instrument_id: str,
        day: date,
        bars: Sequence[Bar],
        daily_open: Decimal,
        isin: str | None,
    ) -> bool:
        return session_admitted(
            day, bars, daily_open, self.refused.get((instrument_id, day), ()), self.gaps, isin
        )


def screened_buckets(rejects: Iterable[RejectRow]) -> dict[tuple[str, date], frozenset[datetime]]:
    """The 5-minute buckets the import refused (ledger rows with a `bar_start`), by (instrument,
    day). Such a bucket traded; a session whose stored bars lack one is not quiet there."""
    found: dict[tuple[str, date], set[datetime]] = {}
    for r in rejects:
        if r.bar_start is not None and r.day is not None:
            found.setdefault((r.instrument_id, r.day), set()).add(r.bar_start)
    return {k: frozenset(v) for k, v in found.items()}


def lacks_refused(bars: Sequence[Bar], refused: Collection[datetime]) -> bool:
    """Whether a session's bars lack a bucket the import refused: its gap traded, so the session
    is decided on the daily estimate (a refused bucket stored from another copy is no gap)."""
    return bool(set(refused) - {bar.start for bar in bars})


class _ReplayView:
    """`trigger.BarView` over one regular session's bars as they are replayed: what live's bar
    builder knows when it hands the monitor a final bar."""

    def __init__(self, day: date, bars: Mapping[str, Sequence[Bar]]) -> None:
        self._start = datetime.combine(day, SESSION_OPEN, tzinfo=IST)
        self._end = datetime.combine(day, SESSION_CLOSE, tzinfo=IST)
        self._opening = {
            symbol: (series[0].high, series[0].low)
            for symbol, series in bars.items()
            if series and series[0].start == self._start
        }
        self._volume: dict[str, int] = {}
        self._lows: dict[str, list[tuple[datetime, Decimal]]] = {}

    def seen(self, bar: Bar) -> None:
        self._volume[bar.symbol] = self._volume.get(bar.symbol, 0) + bar.volume
        self._lows.setdefault(bar.symbol, []).append((bar.start, bar.low))

    def opening_range(self, symbol: str, day: date) -> tuple[Decimal, Decimal] | None:
        return self._opening.get(symbol)

    def session_start(self, day: date) -> datetime:
        return self._start

    def session_end(self, day: date) -> datetime:
        return self._end

    def day_volume(self, symbol: str, day: date) -> int:
        return self._volume.get(symbol, 0)

    def day_low(self, symbol: str, day: date, through: datetime | None = None) -> Decimal | None:
        lows = [
            low for start, low in self._lows.get(symbol, ()) if through is None or start <= through
        ]
        return min(lows, default=None)


def bar_ticks(day: date, bar: Bar, stops: Collection[Decimal]) -> list[Tick]:
    """The trades a stop order or a next-session entry is replayed on from one 5-minute bar (in
    time order): the session's first bar's close at its end (the price at 09:20, when the
    trigger window opens), each bar's open at its start, and each of `stops` (stop triggers) the
    bar rose to from its open, ascending, just after the open. Nothing else of the bar is a
    trade the replay reads: the first trade at or above a stop's trigger is one of these."""
    ticks = [Tick(bar.symbol, bar.start, bar.open)]
    above = sorted(t for t in stops if bar.open < t <= bar.high)
    ticks += [
        Tick(bar.symbol, bar.start + timedelta(microseconds=n + 1), t) for n, t in enumerate(above)
    ]
    if bar.start == datetime.combine(day, SESSION_OPEN, tzinfo=IST):
        ticks.append(Tick(bar.symbol, bar.start + BAR_WIDTH, bar.close))
    return ticks


def _stop_trigger(candidate: Candidate) -> Decimal | None:
    """A stop order's trigger: the first tick above its level (None without them)."""
    level = daily_level(candidate.playbook, candidate.levels)
    tick = candidate.daily.tick
    if level is None or tick is None or tick <= 0:
        return None
    return first_above(level, tick)


def replay(
    day: date, candidates: Iterable[Candidate], bars: Mapping[str, Sequence[Bar]]
) -> list[Trigger]:
    """The triggers live would fire on `day` for `candidates` (whose `symbol` is the instrument
    id `bars` is keyed by), in the order they fire: each fires at most once, as a written
    signal closes its key for the day. A candidate that fires on a trade is fed the trades of
    each bar (`bar_ticks`) before the bar is final."""
    view = _ReplayView(day, bars)
    monitor = TriggerMonitor(view)
    stops: dict[str, set[Decimal]] = {}
    for candidate in candidates:
        monitor.add(candidate)
        if candidate.playbook.on_ticks:
            found = stops.setdefault(candidate.symbol, set())
            if (
                stop := _stop_trigger(candidate)
            ) is not None and not candidate.playbook.next_session:
                found.add(stop)
    fired: list[Trigger] = []
    for bar in sorted((b for series in bars.values() for b in series), key=_order):
        if bar.symbol in stops:
            for tick in bar_ticks(day, bar, stops[bar.symbol]):
                for found in monitor.on_tick(tick):
                    monitor.confirm(found.candidate.key)
                    fired.append(found)
        view.seen(bar)
        for found in monitor.on_bar(bar):
            monitor.confirm(found.candidate.key)
            fired.append(found)
    return fired


@dataclass(frozen=True)
class EpTrace:
    """How far a pre-open candidate got on its session's bars (`trace`): the deepest prefix of
    the trigger gates that passed together on one bar, and that bar's instant, day volume and
    the trigger outcomes (the last bar judged when no prefix passed)."""

    depth: int
    at: datetime | None
    cum_volume: int | None
    outcomes: Mapping[str, CriterionResult]


def trace(day: date, candidate: Candidate, bars: Sequence[Bar], order: Sequence[str]) -> EpTrace:
    """The candidate's `EpTrace` over `bars` (its session, in time order) for the trigger gates
    `order`, judged as the monitor judges each final bar in the trigger window (the first bar
    that passes them all ends it: the replay fires there)."""
    view = _ReplayView(day, {candidate.symbol: bars})
    monitor = TriggerMonitor(view)
    best = EpTrace(-1, None, None, {})
    for bar in bars:
        view.seen(bar)
        if not in_window(bar.final_at, view.session_start(day), view.session_end(day)):
            continue
        ctx = monitor.context(candidate, bar)
        evaluation = evaluate_trigger(
            candidate.playbook, ctx, candidate.mode, candidate.daily_results
        )
        outcomes = {o.type: o.result for o in evaluation.outcomes}
        depth = trigger_depth(order, {k: v.passed for k, v in outcomes.items()})
        if depth > best.depth or depth == best.depth == 0:
            best = EpTrace(depth, bar.final_at, ctx.cum_volume, outcomes)
        if depth == len(order):
            break
    return best if best.depth >= 0 else EpTrace(0, None, None, {})


def _order(bar: Bar) -> tuple[datetime, str]:
    return bar.start, bar.symbol


def traded_low_after(bars: Sequence[Bar], at: datetime) -> Decimal | None:
    """The lowest low the bars after the trigger bar that finished at `at` show (None: no bar
    follows): the prices that traded after a LIMIT went in, for whether it filled. A day low the
    bars miss is not counted: it may have come before the trigger."""
    return min((b.low for b in bars if b.start >= at - BAR_FINAL_LAG), default=None)


def after_trigger(
    bars: Sequence[Bar], at: datetime, day_low: Decimal
) -> tuple[Decimal | None, Decimal | None]:
    """(the lowest low, the highest high) of the session after the trigger bar that finished at
    `at`, None where no bar follows; a day low the bars miss counts as after it (the adverse
    order, as `fillrules.pre_break_low` takes it)."""
    later = [b for b in bars if b.start >= at - BAR_FINAL_LAG]
    lows = [b.low for b in later]
    if day_low < min((b.low for b in bars), default=day_low):
        lows.append(day_low)
    return min(lows, default=None), max((b.high for b in later), default=None)
