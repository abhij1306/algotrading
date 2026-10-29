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
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from ..data.audit import CLOSE_5M_PCT
from ..data.bars import BAR_FINAL_LAG, BAR_WIDTH, Bar
from ..data.bars_store import BarStore, RejectRow
from ..data.sessions import SESSION_CLOSE, SESSION_OPEN
from ..engine.trigger import (
    Candidate,
    CriterionResult,
    Trigger,
    TriggerMonitor,
    evaluate_trigger,
    in_window,
)
from ..money import MONEY_SCALE, require_exact
from ..time import IST
from .ep_log import trigger_depth

__all__ = [
    "EP_ESTIMATE",
    "NO_CATALYST_MODE",
    "EpEstimate",
    "EpTrace",
    "after_trigger",
    "traded_low_after",
    "on_basis",
    "lacks_refused",
    "screened_buckets",
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


def on_basis(day: date, bars: Sequence[Bar], daily_open: Decimal) -> bool:
    """Whether a session's 5-minute bars fit its daily bar: the regular session's first (09:15)
    and last (15:25) bars present, every bar on the IST 5-minute grid of `day` and each once,
    and the 09:15 open within `CLOSE_5M_PCT` of the daily open (both on one price basis). An
    interior bucket may be empty: live's bar builder makes no bar for a window without a trade
    (`data.bars`), so the replay sees what live saw; the 15:25 bar keeps a truncated day from
    being replayed to miss its later lows. The data audit judges the last close
    (`audit.basis_of`); a run decides before the close, so it reads the open. A session that
    does not fit is decided on the daily estimate, so no run reads bars off their day."""
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
    if first is None or closes - BAR_WIDTH not in starts:
        return False
    return abs(first.open / daily_open - 1) * 100 <= _CLOSE_OFF_PCT


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


def replay(
    day: date, candidates: Iterable[Candidate], bars: Mapping[str, Sequence[Bar]]
) -> list[Trigger]:
    """The triggers live would fire on `day` for `candidates` (whose `symbol` is the instrument
    id `bars` is keyed by), in the order they fire: each fires at most once, as a written
    signal closes its key for the day."""
    view = _ReplayView(day, bars)
    monitor = TriggerMonitor(view)
    for candidate in candidates:
        monitor.add(candidate)
    fired: list[Trigger] = []
    for bar in sorted((b for series in bars.values() for b in series), key=_order):
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
