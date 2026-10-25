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
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import polars as pl

from ..data.ticks import scaled_tick, tick_for_date
from ..money import exact
from .criteria import CONTRACTION_LOW, EvalContext
from .regime_gate import regime_allows_longs
from .stops import SeriesHistory, StopInputs, history_inputs, plan_stop_at_pivot
from .strategy.config import Playbook, StopBelowFinalContractionLow, StrategyConfig
from .strategy.scoring import PlaybookEvaluation, StageOutcome, evaluate_playbook
from .trigger import daily_level

__all__ = [
    "ScanInputs",
    "ScanResult",
    "WatchRow",
    "criteria_results",
    "scan_bot",
]


@dataclass(frozen=True)
class ScanInputs:
    """Everything a scan reads. `members` are instrument ids (text) in the universe on `as_of`.

    `bars` holds the members' adjusted daily bars (`instrument_id`, `date`, `high`, `low`,
    `close`, `volume`, optional `traded_value`) with `raw_close`, the unadjusted close the
    rules on a rupee price read (`criteria.base.EvalContext`); `features` the date's feature rows
    (`instrument_id`, `rs_pct`, `adtr20`, ...). `regime_closes` are the regime index's closes
    up to `as_of`, oldest first."""

    as_of: date
    members: Collection[str]
    bars: pl.DataFrame
    regime_closes: Sequence[Decimal]
    features: pl.DataFrame | None = None
    series: Mapping[str, str] = field(default_factory=dict)
    price_bands: Mapping[str, Sequence[tuple[date, float | None]]] = field(default_factory=dict)


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
            }
            for o in stage.outcomes
        ],
    }


def setup_levels(playbook: Playbook, evaluation: PlaybookEvaluation) -> dict[str, Decimal]:
    """Every level the playbook's setup criteria declare (`Playbook.setup_levels`) that they
    exposed on this evaluation: what the live trigger, the stop and the backtest read."""
    return {k: v for k in playbook.setup_levels if (v := _extra(evaluation, k)) is not None}


def criteria_results(playbook: Playbook, evaluation: PlaybookEvaluation) -> dict[str, Any]:
    """The stored `criteria_results`: the stages in stage order, stopping at the first that
    failed (HAT-CRIT-010), and the setup levels (`setup_levels`, text decimals, a level the
    setup did not expose left out)."""
    levels = {k: str(v) for k, v in setup_levels(playbook, evaluation).items()}
    return {"stages": [_stage_json(s) for s in evaluation.stages], "levels": levels}


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


def _scan_playbook(
    config: StrategyConfig,
    playbook: Playbook,
    inputs: ScanInputs,
    features: pl.DataFrame | None,
    per_instrument: dict[str, Any],
) -> list[WatchRow]:
    adtr = {}
    if features is not None and "adtr20" in features.columns:
        adtr = dict(
            zip(features["instrument_id"].to_list(), features["adtr20"].to_list(), strict=False)
        )
    rows: list[WatchRow] = []
    for instrument_id in sorted(per_instrument):
        bars = per_instrument[instrument_id]
        ctx = EvalContext(
            as_of=inputs.as_of,
            instrument_id=instrument_id,
            bars=bars,
            universe=features,
            series=inputs.series.get(instrument_id),
            price_bands=inputs.price_bands.get(instrument_id, ()),
        )
        evaluation = evaluate_playbook(config, playbook.name, ctx, regime_allows_longs=True)
        if not evaluation.passed:
            continue
        pivot = daily_level(playbook, dict(setup_levels(playbook, evaluation)))
        rows.append(
            WatchRow(
                playbook=playbook.name,
                instrument_id=instrument_id,
                score=evaluation.stages[-1].score,
                criteria_results=criteria_results(playbook, evaluation),
                pivot=pivot,
                planned_stop=_planned_stop(playbook, evaluation, pivot, ctx),
                adtr=exact(adtr.get(instrument_id), floats=True),
            )
        )
    return rows


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


def scan_bot(config: StrategyConfig, inputs: ScanInputs) -> ScanResult:
    """The watchlist rows of every nightly playbook of `config` for `inputs.as_of`, best score
    first (ties by instrument id), and whether the regime gate was on."""
    gate_on = regime_allows_longs(config.regime, inputs.regime_closes)
    if not gate_on:
        return ScanResult((), False, 0)  # stage 2 fails for every symbol: nothing is evaluated
    members = sorted({str(m) for m in inputs.members})
    per_instrument = _member_bars(inputs, members)
    features = inputs.features
    if features is not None:
        # Ranks are taken within the universe: a non-member's row never moves a cutoff.
        features = features.filter(pl.col("instrument_id").cast(pl.String).is_in(members))
    rows: list[WatchRow] = []
    for playbook in config.playbooks:
        if playbook.preopen:
            continue
        rows.extend(_scan_playbook(config, playbook, inputs, features, per_instrument))
    rows.sort(key=lambda r: (-r.score, r.playbook, r.instrument_id))
    return ScanResult(tuple(rows), True, len(per_instrument))
