"""The nightly scan (HAT-CRIT-010, HAT-DEC-T12): per bot, universe -> regime gate -> setup.

`scan_bot` is a pure function of its inputs: the instruments in the universe on the scan date,
their adjusted daily bars up to that date, the date's features, the index closes for the regime
gate and the bot's configuration. It reads no clock, database or file, so the backtest and the
live job call the same function and get the same watchlist for the same inputs (T12).

A symbol reaches a later stage only after passing every earlier one, and the stages recorded in
`criteria_results` stop at the first one that failed. A symbol outside the universe on the date
is never evaluated, so a stock removed from the universe produces no watchlist row from its
effective date (HAT-PORT-023). A pre-open playbook (`Playbook.preopen`, such as the episodic
pivot) is not scanned here: its setup is built live from the gap scan (HAT-BOT-052).

Rank criteria read the date's feature rows of the members only, with `rs_eligible`: the RS
percentile among the members that pass the universe stage that night (`features.eligible_rs`).
`ScanResult.funnel` counts, per playbook, the stock-days that passed each gate (`funnel_gates`),
so a backtest reports its funnel without a replay.

A setup is entered once (decisions.md "A pivot is used once"): no row is published for an
instrument the bot holds (`ScanInputs.held`), nor for a setup whose pivot an earlier position of
the bot was opened on (`ScanInputs.used_pivots`). A setup is named by its pivot's bar
(`pivot_date`), stored in `criteria_results` as `pivot_date`. A playbook with a validity window
(`Playbook.valid_sessions`) reads its setups' windows (`SetupWindow`) and returns the ones the
night changed, which the caller stores (`watchlist`), so a setup is armed once.
"""

from __future__ import annotations

import copy
import math
from bisect import bisect_right
from collections.abc import Collection, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import polars as pl

from ..data.features import eligible_rs
from ..data.ticks import scaled_tick, tick_for_date
from ..money import exact
from .criteria import CONTRACTION_LOW, PIVOT, EvalContext, evaluate
from .criteria import book as book_criterion
from .criteria import setups as setups_criterion
from .criteria.base import Bars
from .criteria.setup import RsRankParams, rs_rank
from .regime_gate import regime_allows_longs
from .stops import SeriesHistory, StopInputs, history_inputs, plan_stop_at_pivot
from .strategy.config import (
    PIVOT_SETUPS,
    Playbook,
    StopBelowFinalContractionLow,
    StrategyConfig,
)
from .strategy.scoring import (
    PlaybookEvaluation,
    StageOutcome,
    evaluate_playbook,
    evaluate_stage,
    playbook_context,
    stop_buffer_pct,
)
from .trigger import daily_level, needs_setup_level, watchlist_levels

__all__ = [
    "SCAN_SESSIONS",
    "SESSIONS_AHEAD",
    "ScanInputs",
    "ScanResult",
    "WatchRow",
    "criteria_results",
    "pivot_date",
    "scan_bot",
    "scan_window",
    "setup_taken",
    "universe_passers",
]


@dataclass(frozen=True)
class ScanInputs:
    """Everything a scan reads. `members` are instrument ids (text) in the universe on `as_of`.

    `bars` holds the members' adjusted daily bars (`instrument_id`, `date`, `high`, `low`,
    `close`, `volume`, optional `traded_value`) with `raw_close`, the unadjusted close the
    rules on a rupee price read (`criteria.base.EvalContext`); `features` the date's feature rows
    (`instrument_id`, `rs_pct`, `adtr20`, ...). `regime_closes` are the regime index's closes
    up to `as_of`, oldest first, and `breadth` the `breadth_pct` of each stored session up to
    `as_of` (None where it has none; `regime_gate.breadth_above`). `held` are the instruments
    the bot has an open position in (any state) and `used_pivots` the (instrument, pivot date) of
    every setup a position of the bot was opened on (`setup_taken`). `benchmark` holds the NIFTY
    500's `(date, close)` up to `as_of` and `memo` a run's cache across its nights
    (`EvalContext`). `windows` are the bot's setup windows (`SetupWindow`) whose pivot is in the
    bars, `triggered` the (playbook, instrument, pivot date) of every windowed setup a row of
    which triggered (entered, skipped or declined), and `sessions_ahead` the trading sessions
    from the trade date on (the valid-until date; without them it is left empty)."""

    as_of: date
    members: Collection[str]
    bars: pl.DataFrame
    regime_closes: Sequence[Decimal]
    breadth: Sequence[Decimal | None] = ()
    features: pl.DataFrame | None = None
    series: Mapping[str, str] = field(default_factory=dict)
    price_bands: Mapping[str, Sequence[tuple[date, float | None]]] = field(default_factory=dict)
    held: Collection[str] = frozenset()
    used_pivots: Collection[tuple[str, date]] = frozenset()
    benchmark: Sequence[tuple[date, float]] = ()
    memo: MutableMapping[Any, Any] | None = None
    windows: Sequence[SetupWindow] = ()
    triggered: Collection[tuple[str, str, date]] = frozenset()
    sessions_ahead: Sequence[date] = ()


@dataclass(frozen=True)
class WatchRow:
    """One `hat_watchlist_entry` of the scan (without its bot, version and trade date)."""

    playbook: str
    instrument_id: str
    score: float
    criteria_results: dict[str, Any]
    pivot: Decimal | None = None
    planned_stop: Decimal | None = None
    adtr: Decimal | None = None
    setup_type: str | None = None
    valid_until: date | None = None


@dataclass(frozen=True)
class ScanResult:
    rows: tuple[WatchRow, ...]
    gate_on: bool
    evaluated: int  # members with a bar on `as_of`, which were evaluated
    # per playbook, the stock-days that passed each gate, in gate order (`funnel_gates`)
    funnel: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    # the setup windows the night opened or changed (`SetupWindow`), which the caller stores
    windows: tuple[SetupWindow, ...] = ()


def _json_extras(extras: Mapping[str, Any]) -> dict[str, Any]:
    """A criterion's extras as stored JSON: numbers, text, booleans and lists or mappings of
    them (a Decimal as its text); anything else, or a value holding a float that is not
    finite, is left out."""

    def plain(value: object) -> tuple[bool, object]:
        if isinstance(value, float):
            return math.isfinite(value), value
        if value is None or isinstance(value, (bool, int, str)):
            return True, value
        if isinstance(value, Decimal):
            return True, str(value)
        if isinstance(value, (list, tuple)):
            items = [plain(v) for v in value]
            return all(ok for ok, _ in items), [v for _, v in items]
        if isinstance(value, Mapping):
            pairs = {str(k): plain(v) for k, v in value.items()}
            return all(ok for ok, _ in pairs.values()), {k: v for k, (_, v) in pairs.items()}
        return False, None

    out: dict[str, Any] = {}
    for key, value in extras.items():
        ok, found = plain(value)
        if ok:
            out[str(key)] = found
    return out


def _stage_json(stage: StageOutcome) -> dict[str, Any]:
    return {
        "stage": stage.stage,
        "passed": stage.passed,
        "score": stage.score,
        "reason": stage.reason,
        "criteria": [
            {
                "type": o.type,
                "order": o.order,
                "mode": o.mode,
                "passed": o.result.passed,
                "score": o.result.score,
                "value": o.result.value,
                "reason": o.result.reason,
                # the measured shape (a VCP's contractions and base start), which a backtest's
                # result files report per signal (`backtest.folder`)
                **({"extras": _json_extras(o.result.extras)} if o.result.extras else {}),
            }
            for o in stage.outcomes
        ],
    }


def setup_levels(playbook: Playbook, evaluation: PlaybookEvaluation) -> dict[str, Decimal]:
    """Every level the playbook's setup criteria declare (`Playbook.setup_levels`) that they
    exposed on this evaluation: what the live trigger, the stop and the backtest read."""
    return {k: v for k in playbook.setup_levels if (v := _extra(evaluation, k)) is not None}


def criteria_results(
    playbook: Playbook,
    evaluation: PlaybookEvaluation,
    pivot_on: date | None = None,
    setup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The stored `criteria_results`: the stages in stage order, stopping at the first that
    failed (HAT-CRIT-010), the setup levels (`setup_levels`, text decimals, a level the setup
    did not expose left out), when known the setup's `pivot_date` (ISO) and its `setup`
    summary (`setup_summary`)."""
    levels = {k: str(v) for k, v in setup_levels(playbook, evaluation).items()}
    results: dict[str, Any] = {
        "stages": [_stage_json(s) for s in evaluation.stages],
        "levels": levels,
    }
    if pivot_on is not None:
        results["pivot_date"] = pivot_on.isoformat()
    if setup:
        results["setup"] = dict(setup)
    return results


# What a setup summary copies from its criteria's extras: the shape and dry-up (a detector's
# vr ratios or `book_vcp`'s), and the tags of `setup_tags`.
SUMMARY_KEYS = (
    "base_start",
    "footprint",
    "contraction_count",
    "base_number",
    "dryup_mean_vr",
    "dryup_min_vr",
    "dryup_mean_ratio",
    "dryup_min_ratio",
)
TAG_KEYS = ("shakeout", "shakeout_undercut_pct", "ath_pivot", "early_leader", "stage")
# The setup type a criterion's pass names when it does not say (`book_vcp`'s base is a VCP).
SETUP_NAMES = {"book_vcp": "vcp"}


def setup_type(evaluation: PlaybookEvaluation) -> str | None:
    """The setup a passing evaluation found: the `setup` its detector named, else the first
    passed pivot setup criterion's type (`SETUP_NAMES`); None without one."""
    for stage in evaluation.stages:
        if stage.stage != "setup":
            continue
        for outcome in stage.outcomes:
            if not outcome.result.passed:
                continue
            named = outcome.result.extras.get("setup")
            if isinstance(named, str):
                return named
            if outcome.type in PIVOT_SETUPS:
                return SETUP_NAMES.get(outcome.type, outcome.type)
    return None


def setup_summary(
    evaluation: PlaybookEvaluation,
    ctx: EvalContext,
    pivot: Decimal | None,
    stop: Decimal | None,
) -> dict[str, Any]:
    """The watchlist fields of a setup (plan "Daily scan"): its type, pivot, stop and stop %,
    the eligible RS rank, the shape and dry-up its criteria logged (`SUMMARY_KEYS`) and the tags
    (`TAG_KEYS`); a field without a value is left out."""
    out: dict[str, Any] = {}
    kind = setup_type(evaluation)
    if kind is not None:
        out["setup_type"] = kind
    if pivot is not None:
        out["pivot"] = str(pivot)
    if stop is not None:
        out["stop"] = str(stop)
        if pivot:
            out["stop_pct"] = float((pivot - stop) / pivot * 100)
    rs = rs_rank(ctx, RsRankParams(basis="eligible")).value
    if rs is not None:
        out["rs_rank"] = rs
    for outcome in _passed_setup_outcomes(evaluation):
        keys = TAG_KEYS if outcome.type == "setup_tags" else SUMMARY_KEYS
        extras = _json_extras(outcome.result.extras)
        out.update({k: extras[k] for k in keys if k in extras and k not in out})
    return out


def _passed_setup_outcomes(evaluation: PlaybookEvaluation) -> list[StageOutcome]:
    return [
        outcome
        for stage in evaluation.stages
        if stage.stage == "setup"
        for outcome in stage.outcomes
        if outcome.result.passed
    ]


def pivot_date(history: Bars, pivot: Decimal | None) -> date | None:
    """The date of the latest bar whose high is the setup's `pivot`: the bar that set it, which
    names the setup. It does not move when a later dividend rescales the history (the level
    does), and a new final contraction or box has a new one. None without a pivot or when no
    bar's high is it (a pivot that is not a bar's high, such as `rsi_below`'s close)."""
    if pivot is None:
        return None
    for day, high in zip(reversed(history.dates), reversed(history.high), strict=True):
        if high is not None and exact(high, floats=True) == pivot:
            return day
    return None


def setup_taken(
    instrument_id: str,
    pivot_on: date | None,
    held: Collection[str],
    used_pivots: Collection[tuple[str, date]],
) -> bool:
    """Whether a setup is kept off the watchlist: the bot holds the instrument, or a position of
    the bot was already opened on this pivot of it (a pivot is used once). A setup without a
    `pivot_date` is judged by the held rule only."""
    if instrument_id in held:
        return True
    return pivot_on is not None and (instrument_id, pivot_on) in used_pivots


def _extra(evaluation: PlaybookEvaluation, key: str) -> Decimal | None:
    """A setup level as the setup's own criteria saw it (`StageOutcome.levels`: the first
    passed criterion to report it), so the gate and the published level agree."""
    for stage in evaluation.stages:
        if stage.stage == "setup" and key in stage.levels:
            return exact(stage.levels[key], floats=True)
    return None


def _planned_stop(
    playbook: Playbook,
    contraction_low: Decimal | None,
    pivot: Decimal | None,
    ctx: EvalContext,
) -> Decimal | None:
    """The stop the setup implies at the pivot, for the rules that can be priced overnight, on
    the next session's tick grid (HAT-EXEC-053), from the bars through the scan date
    (`history_inputs`). A skip (too wide, no pivot, no tick) leaves it empty; the live entry
    recomputes it from the real entry price (HAT-BOT-062), and only there is a contraction
    stop's `min_pct` applied (`plan_stop_at_pivot`)."""
    tick = next_session_tick(ctx)
    if pivot is None or tick is None:
        return None
    rule = playbook.stop
    if isinstance(rule, StopBelowFinalContractionLow) and contraction_low is None:
        return None
    history = ctx.history
    atr, swing = history_inputs(
        rule, SeriesHistory.of_floats(history.high, history.low, history.close)
    )
    plan = plan_stop_at_pivot(
        rule,
        StopInputs(
            entry=pivot, tick=tick, final_contraction_low=contraction_low, atr=atr, swing_lows=swing
        ),
    )
    return plan.stop


def next_session_tick(ctx: EvalContext) -> Decimal | None:
    """The tick of the session after the scan date: NSE sets it from the close of the last
    trading day of the month before (`data.ticks`). The day after `as_of` stands for the next
    session; across a month end with a holiday on its last weekday it reads one month early,
    which only the overnight planned stop uses (the live entry prices its own stop). The slab
    is picked from the raw closes, as NSE sets it, and read in the units of the adjusted bars
    the stop is priced on (`ticks.scaled_tick`; the NSE tick itself on a live scan)."""
    history = ctx.history
    closes = [None if c is None else exact(c, floats=True) for c in history.raw_close]
    tick = tick_for_date(history.dates, closes, ctx.as_of + timedelta(days=1))
    if tick is None:
        return None
    adjusted, raw = history.close[-1], history.raw_close[-1]
    return scaled_tick(
        tick,
        None if adjusted is None else exact(adjusted, floats=True),
        None if raw is None else exact(raw, floats=True),
    )


TEMPLATE_TYPES = frozenset({"sma_above", "sma_rising", "pct_from_52w_low", "pct_from_52w_high"})
FUNNEL_HEAD = ("evaluated", "universe")
FUNNEL_TAIL = ("setup", "watchlist")


def _gate(criterion: str) -> str:
    if criterion in TEMPLATE_TYPES:
        return "trend_template"
    return criterion


def funnel_gates(playbook: Playbook) -> tuple[str, ...]:
    """The setup's gates in evaluation order: its enabled hard criteria, the trend template's
    checks as one gate, `book_vcp` and the S1 setups as their own gates (`book.GATES`,
    `setups.GATES`), a repeated gate once."""
    gates: list[str] = []
    criteria = sorted((i for i in playbook.setup.criteria if i.enabled), key=lambda i: i.order)
    for instance in criteria:
        if instance.mode != "hard":
            continue
        if instance.type == "book_vcp":
            names = [gate for gate, _ in book_criterion.GATES]
        elif instance.type in setups_criterion.GATES:
            names = [gate for gate, _ in setups_criterion.GATES[instance.type]]
        else:
            names = [_gate(instance.type)]
        gates.extend(n for n in names if n not in gates)
    return (*FUNNEL_HEAD, *gates, *FUNNEL_TAIL)


def _stopped_gate(evaluation: PlaybookEvaluation) -> str | None:
    """The gate a stock-day failed at: `universe`, a setup gate, `setup` (the score threshold);
    None when the setup passed."""
    last = evaluation.stages[-1]
    if last.passed:
        return None
    if last.stage != "setup":
        return "universe"
    failed = last.failed
    if failed is None:
        return "setup"
    if failed.type == "book_vcp":
        return book_criterion.gate_of(failed.result.reason)
    if failed.type in setups_criterion.GATES:
        return setups_criterion.gate_of(failed.type, failed.result.reason)
    return _gate(failed.type)


def _count(funnel: dict[str, int], gates: Sequence[str], stopped: str | None) -> None:
    """Add one stock-day that passed every gate before `stopped` (all when None)."""
    for gate in gates:
        if gate == stopped:
            return
        funnel[gate] += 1


def _scan_playbook(
    config: StrategyConfig,
    playbook: Playbook,
    inputs: ScanInputs,
    contexts: Mapping[str, tuple[EvalContext, StageOutcome]],
    adtr: Mapping[str, Any],
) -> tuple[list[WatchRow], dict[str, int]]:
    gates = funnel_gates(playbook)
    funnel = dict.fromkeys(gates, 0)
    rows: list[WatchRow] = []
    for instrument_id in sorted(contexts):
        ctx, universe = contexts[instrument_id]
        evaluation = evaluate_playbook(
            config, playbook.name, ctx, regime_allows_longs=True, universe=universe
        )
        if not evaluation.passed:
            _count(funnel, gates, _stopped_gate(evaluation))
            continue
        levels = setup_levels(playbook, evaluation)
        pivot = daily_level(playbook, dict(levels))
        pivot_on = pivot_date(ctx.history, levels.get(PIVOT))
        if (pivot is None and needs_setup_level(playbook)) or setup_taken(
            instrument_id, pivot_on, inputs.held, inputs.used_pivots
        ):
            # set up on score without the level its trigger breaks, or already taken
            _count(funnel, gates, "watchlist")
            continue
        _count(funnel, gates, None)
        stop = _planned_stop(playbook, _extra(evaluation, CONTRACTION_LOW), pivot, ctx)
        summary = setup_summary(evaluation, ctx, pivot, stop)
        rows.append(
            WatchRow(
                playbook=playbook.name,
                instrument_id=instrument_id,
                score=evaluation.stages[-1].score,
                criteria_results=criteria_results(playbook, evaluation, pivot_on, summary),
                pivot=pivot,
                planned_stop=stop,
                adtr=exact(adtr.get(instrument_id), floats=True),
                setup_type=summary.get("setup_type"),
            )
        )
    return rows, funnel


# --- the trigger validity window (S1 setups) --------------------------------------------------

# The sessions a scan reads (`scan_window`): enough for the 52-week and 200-day criteria and
# `book_vcp`'s earlier bases; an overhead supply 326-699 sessions back.
SCAN_SESSIONS = 700
WINDOW = "window"
# The sessions a scan's `sessions_ahead` needs: the longest validity window (`valid_sessions`).
SESSIONS_AHEAD = 20
_LEVEL_STEP = Decimal("0.000001")
_HUNDRED = Decimal(100)

# A setup window's status (`SetupWindow`): only an open one gives a row. Every other status
# is final for its setup (it is never armed again), except `regime_off`.
OPEN = "open"
EXPIRED = "expired"  # its last valid session passed
TRIGGERED = "triggered"  # a row of it triggered (entered, skipped or declined) or it is held
CANCELLED = "cancelled"  # a close below its stop, a swing high above its pivot, or no bar
REPLACED = "replaced"  # the playbook found the instrument's next setup (a newer pivot)
REGIME_OFF = "regime_off"  # a regime-off night ended it; a setup found later starts anew
WINDOW_STATUSES = (OPEN, EXPIRED, TRIGGERED, CANCELLED, REPLACED, REGIME_OFF)

WindowKey = tuple[str, str, date]  # (playbook, instrument id, pivot date)


@dataclass(frozen=True)
class SetupWindow:
    """A setup of a windowed playbook (`Playbook.valid_sessions`), named by its playbook,
    instrument and `pivot_date`, and its validity window: found on the night `found_on`, it is
    valid the next `valid_sessions` sessions while `status` is open; re-detection never
    extends it. `score` and `criteria_results` are its last row, which the scan carries."""

    playbook: str
    instrument_id: str
    pivot_date: date
    found_on: date
    status: str
    score: float
    criteria_results: Mapping[str, Any]

    @property
    def key(self) -> WindowKey:
        return (self.playbook, self.instrument_id, self.pivot_date)


def scan_window(days: Sequence[date], as_of: date) -> list[date]:
    """The sessions a scan reads: the last `SCAN_SESSIONS` of the sorted `days` through `as_of`,
    live (`daily.market.scan_inputs`) and in the backtest (`MarketView.window`)."""
    end = bisect_right(days, as_of)
    return list(days[max(0, end - SCAN_SESSIONS) : end])


def calendar(inputs: ScanInputs) -> list[date]:
    """The sessions of the scan's bars through `as_of`, sorted: what a window counts."""
    if not inputs.bars.height:
        return []
    dates = inputs.bars.filter(pl.col("date") <= inputs.as_of)["date"].unique().sort()
    return dates.to_list()


def window_session(dates: Sequence[date], first: date) -> int:
    """Which session of its window the next session is for a setup found on `first`: 1 + the
    sessions after `first` through the scan date (`dates`, the scan's `calendar`). The same
    sessions `valid_until` counts on (`_valid_until`)."""
    return 1 + len(dates) - bisect_right(dates, first)


def found_on(criteria_results: Mapping[str, Any]) -> date | None:
    """The night a windowed row's setup was first found."""
    window = criteria_results.get(WINDOW)
    value = window.get("found_on") if isinstance(window, Mapping) else None
    return date.fromisoformat(value) if isinstance(value, str) else None


def _pivot_on(criteria_results: Mapping[str, Any]) -> date | None:
    value = criteria_results.get("pivot_date")
    return date.fromisoformat(value) if isinstance(value, str) else None


def window_key(
    playbook: str, instrument_id: str, criteria_results: Mapping[str, Any]
) -> WindowKey | None:
    """The setup a watchlist row is of (`SetupWindow.key`), None without a `pivot_date`."""
    pivot_on = _pivot_on(criteria_results)
    return None if pivot_on is None else (playbook, instrument_id, pivot_on)


def _in_window(row: WatchRow, first: date, session: int, total: int, *, carried: bool) -> WatchRow:
    results = dict(row.criteria_results)
    results[WINDOW] = {"found_on": first.isoformat(), "session": session, "sessions": total}
    if carried:
        results["carried"] = True
    return copy.replace(row, criteria_results=results)


def _window_of(row: WatchRow, first: date, pivot_on: date) -> SetupWindow:
    return SetupWindow(
        row.playbook, row.instrument_id, pivot_on, first, OPEN, row.score, row.criteria_results
    )


def regime_off(windows: Iterable[SetupWindow]) -> tuple[SetupWindow, ...]:
    """A regime-off night ends every open window (live and in the backtest alike)."""
    return tuple(copy.replace(w, status=REGIME_OFF) for w in windows if w.status == OPEN)


@dataclass
class _Windows:
    """The night's window book: the stored windows by key and the ones the night changed."""

    stored: Mapping[WindowKey, SetupWindow]
    triggered: Collection[WindowKey]
    changed: dict[WindowKey, SetupWindow] = field(default_factory=dict)

    def close(self, window: SetupWindow, status: str) -> None:
        self.changed[window.key] = copy.replace(window, status=status)

    def put(self, window: SetupWindow) -> None:
        self.changed[window.key] = window


def windowed(
    playbook: Playbook,
    inputs: ScanInputs,
    days: Sequence[date],
    contexts: Mapping[str, tuple[EvalContext, StageOutcome]],
    fresh: Sequence[WatchRow],
    book: _Windows,
) -> list[WatchRow]:
    """The playbook's rows under its validity window: a setup first found on night D is valid
    for the sessions D+1..D+`valid_sessions`. A setup found again keeps the night it was first
    found; a closed one (expired, triggered, cancelled, replaced) is never armed again. An open
    setup the scan does not find again is carried on (`carry`) unless closed; one whose
    instrument has a newer setup tonight is replaced. Each row's window goes into `book`."""
    if playbook.valid_sessions is None:
        return list(fresh)
    out: list[WatchRow] = []
    seen: set[WindowKey] = set()
    for row in fresh:
        pivot_on = _pivot_on(row.criteria_results)
        if pivot_on is None:
            continue  # a setup without a pivot bar has no window to keep
        key = (playbook.name, row.instrument_id, pivot_on)
        seen.add(key)
        found = _found_again(playbook, inputs.as_of, days, row, key, book)
        if found is not None:
            out.append(found)
    opened = {r.instrument_id for r in out}
    for key, stored in sorted(book.stored.items()):
        if key[0] == playbook.name and stored.status == OPEN and key not in seen:
            carried = _carry_on(playbook, inputs, days, contexts, stored, opened, book)
            if carried is not None:
                out.append(carried)
    return out


def _found_again(
    playbook: Playbook,
    as_of: date,
    days: Sequence[date],
    row: WatchRow,
    key: WindowKey,
    book: _Windows,
) -> WatchRow | None:
    """Tonight's row of a setup the scan found, in its window (opened tonight unless it is
    open already), or None when its window is closed or over (closing it in `book`)."""
    total = playbook.valid_sessions or 0
    stored = book.stored.get(key)
    armed = stored is not None and stored.status == OPEN
    if stored is not None and not armed and stored.status != REGIME_OFF:
        return None  # closed for good
    if stored is not None and armed and key in book.triggered:
        book.close(stored, TRIGGERED)
        return None
    first = stored.found_on if stored is not None and armed else as_of
    session = window_session(days, first)
    if session > total:
        if stored is not None:
            book.close(stored, EXPIRED)
        return None
    found = _in_window(row, first, session, total, carried=False)
    book.put(_window_of(found, first, key[2]))
    return found


def _carry_on(
    playbook: Playbook,
    inputs: ScanInputs,
    days: Sequence[date],
    contexts: Mapping[str, tuple[EvalContext, StageOutcome]],
    stored: SetupWindow,
    opened: Collection[str],
    book: _Windows,
) -> WatchRow | None:
    """The carried row of an open setup the scan did not find again, or None when it closes:
    replaced (its instrument has a newer setup tonight), triggered, or as `carry` says."""
    if stored.instrument_id in opened:
        book.close(stored, REPLACED)
        return None
    if stored.key in book.triggered:
        book.close(stored, TRIGGERED)
        return None
    carried = carry(playbook, inputs, days, contexts.get(stored.instrument_id), stored)
    if isinstance(carried, str):
        book.close(stored, carried)
        return None
    book.put(copy.replace(stored, criteria_results=carried.criteria_results))
    return carried


def cancel_level(playbook: Playbook, contraction_low: Decimal) -> Decimal:
    """The close under which a carried setup is cancelled: the final contraction's low less
    the playbook's stop buffer (`stop_buffer_pct`), whatever stop the entry would plan."""
    return contraction_low * (1 - exact(stop_buffer_pct(playbook), floats=True) / _HUNDRED)


def carry(
    playbook: Playbook,
    inputs: ScanInputs,
    days: Sequence[date],
    found: tuple[EvalContext, StageOutcome] | None,
    window: SetupWindow,
) -> WatchRow | str:
    """The open setup `window` the scan did not find again, as tonight's row for the next
    session; or the status that closes it: `expired` after its last valid session;
    `triggered` when the bot holds the instrument or used the pivot (`setup_taken`);
    `cancelled` when the instrument has no bar tonight or fails the playbook's universe, its
    pivot bar left the window, tonight's close is below `cancel_level`, or a swing high above
    its pivot was confirmed since it was found (`setups.swing_high_above`). Its levels are read
    again on tonight's scale, through the high of its `pivot_date` (a dividend rescales the
    history), and its tags and RS rank are evaluated again tonight (`_retagged`)."""
    total = playbook.valid_sessions
    session = window_session(days, window.found_on)
    if total is None or session > total:
        return EXPIRED
    if setup_taken(window.instrument_id, window.pivot_date, inputs.held, inputs.used_pivots):
        return TRIGGERED
    if found is None:
        return CANCELLED
    ctx, universe = found
    if playbook.universe is not None:  # the playbook's own universe, as `evaluate_playbook`
        universe = evaluate_stage("universe", playbook.universe, ctx)
    history = ctx.history
    if not universe.passed or window.pivot_date not in history.dates:
        return CANCELLED
    results = window.criteria_results
    new_pivot = exact(history.high[history.dates.index(window.pivot_date)], floats=True)
    scaled = _rescaled(watchlist_levels(results.get("levels", {})), new_pivot)
    low = None if scaled is None else scaled.get(CONTRACTION_LOW)
    close = exact(history.close[-1], floats=True)
    if scaled is None or new_pivot is None or low is None or close is None:
        return CANCELLED
    if close < cancel_level(playbook, low):
        return CANCELLED
    series = book_criterion.series_of(ctx)
    start = bisect_right(history.dates, window.found_on)
    if series is None or setups_criterion.swing_high_above(series, start, new_pivot):
        return CANCELLED
    pivot = daily_level(playbook, scaled)
    stop = _planned_stop(playbook, low, pivot, ctx)
    out = dict(results)
    out["levels"] = {k: str(v) for k, v in scaled.items() if v is not None}
    setup = _carried_setup(results.get("setup") or {}, pivot, stop)
    if setup:
        out["setup"] = _retagged(playbook, ctx, setup, new_pivot)
    row = WatchRow(
        playbook=playbook.name,
        instrument_id=window.instrument_id,
        score=window.score,
        criteria_results=out,
        pivot=pivot,
        planned_stop=stop,
        setup_type=setup.get("setup_type"),
    )
    return _in_window(row, window.found_on, session, total, carried=True)


def _rescaled(
    levels: Mapping[str, Decimal | None], new_pivot: Decimal | None
) -> dict[str, Decimal | None] | None:
    """The levels on tonight's scale: each times the pivot's change (`new_pivot`, the high of
    the pivot's bar tonight, over the stored pivot); None without either pivot."""
    old_pivot = levels.get(PIVOT)
    if old_pivot is None or new_pivot is None:
        return None
    ratio = new_pivot / old_pivot
    scaled = {
        k: v if v is None or ratio == 1 else (v * ratio).quantize(_LEVEL_STEP)
        for k, v in levels.items()
    }
    scaled[PIVOT] = new_pivot
    return scaled


def _carried_setup(
    setup: Mapping[str, Any], pivot: Decimal | None, stop: Decimal | None
) -> dict[str, Any]:
    """A carried row's setup summary with tonight's pivot, stop and stop %."""
    out = {k: v for k, v in setup.items() if k not in ("stop", "stop_pct")}
    if out and pivot is not None:
        out["pivot"] = str(pivot)
    if out and stop is not None and pivot:
        out.update(stop=str(stop), stop_pct=float((pivot - stop) / pivot * 100))
    return out


def _retagged(
    playbook: Playbook, ctx: EvalContext, setup: Mapping[str, Any], pivot: Decimal
) -> dict[str, Any]:
    """`setup` with tonight's RS rank and, when the playbook has `setup_tags`, tonight's tags,
    on the carried pivot and the base from its `base_start` (an early leader orders the
    backtest's `leader_rs` slots)."""
    out = {k: v for k, v in setup.items() if k not in TAG_KEYS and k != "rs_rank"}
    rs = rs_rank(ctx, RsRankParams(basis="eligible")).value
    if rs is not None:
        out["rs_rank"] = rs
    tags = next((i for i in playbook.setup.criteria if i.type == "setup_tags" and i.enabled), None)
    if tags is None:
        return out
    levels: dict[str, float] = {PIVOT: float(pivot)}
    dates = ctx.history.dates
    start = setup.get("base_start")
    if isinstance(start, str) and date.fromisoformat(start) in dates:
        levels["base_length_sessions"] = len(dates) - dates.index(date.fromisoformat(start))
    result = evaluate("setup_tags", tags.params, playbook_context(playbook, ctx, levels))
    extras = _json_extras(result.extras)
    out.update({k: extras[k] for k in TAG_KEYS if k in extras})
    return out


def _valid_until(row: WatchRow, ahead: Sequence[date]) -> WatchRow:
    """The row with its last valid session: the trade date, or under a validity window the
    window's last session (None when `ahead` does not reach it). `ahead` are the calendar's
    sessions from the trade date on, the sessions `window_session` counts."""
    window = row.criteria_results.get(WINDOW)
    left = 0
    if isinstance(window, Mapping):
        left = int(window["sessions"]) - int(window["session"])
    return copy.replace(row, valid_until=ahead[left] if left < len(ahead) else None)


def with_setup_tags(config: StrategyConfig, rows: Sequence[WatchRow]) -> list[WatchRow]:
    """Each row's setup summary with `setup_tags`, every setup the night found for its
    instrument; under `one_setup_per_instrument` only each instrument's first row is kept, by
    playbook order in the config and a setup found tonight before a carried one."""
    tags: dict[str, set[str]] = {}
    for row in rows:
        tags.setdefault(row.instrument_id, set()).add(row.setup_type or row.playbook)
    order = {p.name: n for n, p in enumerate(config.playbooks)}
    ranked = sorted(
        rows,
        key=lambda r: (
            r.instrument_id,
            order.get(r.playbook, len(order)),
            bool(r.criteria_results.get("carried")),
        ),
    )
    out: list[WatchRow] = []
    kept: set[str] = set()
    for row in ranked:
        if config.one_setup_per_instrument and row.instrument_id in kept:
            continue
        kept.add(row.instrument_id)
        setup = row.criteria_results.get("setup")
        if isinstance(setup, Mapping):
            results = dict(row.criteria_results)
            results["setup"] = {**setup, "setup_tags": sorted(tags[row.instrument_id])}
            row = copy.replace(row, criteria_results=results)
        out.append(row)
    return out


def _member_bars(inputs: ScanInputs, members: Sequence[str]) -> dict[str, Any]:
    """Each member's bars up to `as_of`, for the members that have a bar on `as_of` itself: a
    member that did not trade on the scan date (suspended, no data) is not evaluated on stale
    bars."""
    per_instrument: dict[str, Any] = {}
    if not members or not inputs.bars.height:
        return per_instrument
    frame = inputs.bars.filter(
        pl.col("instrument_id").cast(pl.String).is_in(members) & (pl.col("date") <= inputs.as_of)
    )
    for key, part in frame.partition_by("instrument_id", as_dict=True).items():
        if part["date"].max() != inputs.as_of:
            continue
        per_instrument[str(key[0] if isinstance(key, tuple) else key)] = part
    return per_instrument


def _contexts(
    config: StrategyConfig, inputs: ScanInputs, per_instrument: Mapping[str, Any]
) -> dict[str, tuple[EvalContext, StageOutcome]]:
    """Each member's context (shared by the playbooks) and its universe stage."""
    out: dict[str, tuple[EvalContext, StageOutcome]] = {}
    days = calendar(inputs)
    for instrument_id in sorted(per_instrument):
        ctx = EvalContext(
            as_of=inputs.as_of,
            instrument_id=instrument_id,
            bars=per_instrument[instrument_id],
            series=inputs.series.get(instrument_id),
            price_bands=inputs.price_bands.get(instrument_id, ()),
            benchmark=inputs.benchmark,
            memo=inputs.memo,
            calendar=days,
        )
        out[instrument_id] = (ctx, evaluate_stage("universe", config.universe, ctx))
    return out


def universe_passers(config: StrategyConfig, inputs: ScanInputs) -> list[str]:
    """The members (text ids, sorted) with a bar on `inputs.as_of` that pass `config`'s universe
    stage that night, evaluated as the scan evaluates it: what a rotation ranks."""
    members = sorted({str(m) for m in inputs.members})
    contexts = _contexts(config, inputs, _member_bars(inputs, members))
    return [i for i, (_, outcome) in contexts.items() if outcome.passed]


def scan_bot(config: StrategyConfig, inputs: ScanInputs) -> ScanResult:
    """The watchlist rows of every nightly playbook of `config` for `inputs.as_of`, best score
    first (ties by instrument id), and whether the regime gate was on."""
    gate_on = regime_allows_longs(config.regime, inputs.regime_closes, inputs.breadth)
    if not gate_on:
        # stage 2 fails for every symbol: nothing is evaluated, and every window ends
        return ScanResult((), False, 0, windows=regime_off(inputs.windows))
    members = sorted({str(m) for m in inputs.members})
    per_instrument = _member_bars(inputs, members)
    contexts = _contexts(config, inputs, per_instrument)
    features = inputs.features
    adtr: dict[str, Any] = {}
    if features is not None:
        # Ranks are taken within the universe: a non-member's row never moves a cutoff.
        features = features.filter(pl.col("instrument_id").cast(pl.String).is_in(members))
        eligible = [i for i, (_, universe) in contexts.items() if universe.passed]
        features = eligible_rs(features, eligible)
        if "adtr20" in features.columns:
            adtr = dict(
                zip(
                    features["instrument_id"].cast(pl.String).to_list(),
                    features["adtr20"].to_list(),
                    strict=False,
                )
            )
    for ctx, _ in contexts.values():
        ctx.universe = features
    rows: list[WatchRow] = []
    funnel: dict[str, Mapping[str, int]] = {}
    days = calendar(inputs)
    book = _Windows({w.key: w for w in inputs.windows}, inputs.triggered)
    for playbook in config.playbooks:
        if playbook.preopen:
            continue
        found, counted = _scan_playbook(config, playbook, inputs, contexts, adtr)
        if playbook.valid_sessions is not None:
            found = [
                copy.replace(r, adtr=exact(adtr.get(r.instrument_id), floats=True))
                for r in windowed(playbook, inputs, days, contexts, found, book)
            ]
        rows.extend(found)
        funnel[playbook.name] = counted
    rows = [_valid_until(row, inputs.sessions_ahead) for row in rows]
    rows = with_setup_tags(config, rows)
    rows.sort(key=lambda r: (-r.score, r.playbook, r.instrument_id))
    windows = tuple(book.changed[k] for k in sorted(book.changed))
    return ScanResult(tuple(rows), True, len(per_instrument), funnel, windows)
