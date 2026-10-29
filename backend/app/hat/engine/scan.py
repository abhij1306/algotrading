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
(`pivot_date`), stored in `criteria_results` as `pivot_date`.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import polars as pl

from ..data.features import eligible_rs
from ..data.ticks import scaled_tick, tick_for_date
from ..money import exact
from .criteria import CONTRACTION_LOW, PIVOT, EvalContext
from .criteria import book as book_criterion
from .criteria.base import Bars
from .regime_gate import regime_allows_longs
from .stops import SeriesHistory, StopInputs, history_inputs, plan_stop_at_pivot
from .strategy.config import Playbook, StopBelowFinalContractionLow, StrategyConfig
from .strategy.scoring import PlaybookEvaluation, StageOutcome, evaluate_playbook, evaluate_stage
from .trigger import daily_level, needs_setup_level

__all__ = [
    "ScanInputs",
    "ScanResult",
    "WatchRow",
    "criteria_results",
    "pivot_date",
    "scan_bot",
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
    (`EvalContext`)."""

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


@dataclass(frozen=True)
class ScanResult:
    rows: tuple[WatchRow, ...]
    gate_on: bool
    evaluated: int  # members with a bar on `as_of`, which were evaluated
    # per playbook, the stock-days that passed each gate, in gate order (`funnel_gates`)
    funnel: Mapping[str, Mapping[str, int]] = field(default_factory=dict)


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
    playbook: Playbook, evaluation: PlaybookEvaluation, pivot_on: date | None = None
) -> dict[str, Any]:
    """The stored `criteria_results`: the stages in stage order, stopping at the first that
    failed (HAT-CRIT-010), the setup levels (`setup_levels`, text decimals, a level the setup
    did not expose left out) and, when known, the setup's `pivot_date` (ISO)."""
    levels = {k: str(v) for k, v in setup_levels(playbook, evaluation).items()}
    results: dict[str, Any] = {
        "stages": [_stage_json(s) for s in evaluation.stages],
        "levels": levels,
    }
    if pivot_on is not None:
        results["pivot_date"] = pivot_on.isoformat()
    return results


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
    evaluation: PlaybookEvaluation,
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
    contraction_low = _extra(evaluation, CONTRACTION_LOW)
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
    checks as one gate, `book_vcp` as its own gates (`book.GATES`), a repeated gate once."""
    gates: list[str] = []
    criteria = sorted((i for i in playbook.setup.criteria if i.enabled), key=lambda i: i.order)
    for instance in criteria:
        if instance.mode != "hard":
            continue
        if instance.type == "book_vcp":
            names = [gate for gate, _ in book_criterion.GATES]
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
        rows.append(
            WatchRow(
                playbook=playbook.name,
                instrument_id=instrument_id,
                score=evaluation.stages[-1].score,
                criteria_results=criteria_results(playbook, evaluation, pivot_on),
                pivot=pivot,
                planned_stop=_planned_stop(playbook, evaluation, pivot, ctx),
                adtr=exact(adtr.get(instrument_id), floats=True),
            )
        )
    return rows, funnel


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
    for instrument_id in sorted(per_instrument):
        ctx = EvalContext(
            as_of=inputs.as_of,
            instrument_id=instrument_id,
            bars=per_instrument[instrument_id],
            series=inputs.series.get(instrument_id),
            price_bands=inputs.price_bands.get(instrument_id, ()),
            benchmark=inputs.benchmark,
            memo=inputs.memo,
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
        return ScanResult((), False, 0)  # stage 2 fails for every symbol: nothing is evaluated
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
    for playbook in config.playbooks:
        if playbook.preopen:
            continue
        found, counted = _scan_playbook(config, playbook, inputs, contexts, adtr)
        rows.extend(found)
        funnel[playbook.name] = counted
    rows.sort(key=lambda r: (-r.score, r.playbook, r.instrument_id))
    return ScanResult(tuple(rows), True, len(per_instrument), funnel)
