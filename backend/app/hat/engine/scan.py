"""The nightly scan (04 §1, HAT-CRIT-010, HAT-DEC-T12): per bot, universe -> regime gate -> setup.

`scan_bot` is a pure function of its inputs: the instruments in the universe on the scan date,
their adjusted daily bars up to that date, the date's features, the index closes for the regime
gate and the bot's configuration. It reads no clock, database or file, so the backtest and the
live job call the same function and get the same watchlist for the same inputs (T12).

A symbol reaches a later stage only after passing every earlier one, and the stages recorded in
`criteria_results` stop at the first one that failed. A symbol outside the universe on the date
is never evaluated, so a stock removed from the universe produces no watchlist row from its
effective date (HAT-PORT-023). The episodic-pivot playbook is not scanned here: its setup is
built live from the pre-open scan (HAT-BOT-052).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import polars as pl

from ..data.ticks import tick_for_date
from .criteria import EvalContext
from .regime_gate import regime_allows_longs
from .stops import StopInputs, plan_stop
from .strategy.config import Playbook, StopBelowFinalContractionLow, StrategyConfig
from .strategy.scoring import PlaybookEvaluation, StageOutcome, evaluate_playbook

__all__ = [
    "EP_PLAYBOOK",
    "ScanInputs",
    "ScanResult",
    "WatchRow",
    "criteria_results",
    "scan_bot",
]

EP_PLAYBOOK = "episodic_pivot"
# The setup levels stored with a watchlist row for the live trigger and stop (07 §12).
LEVEL_KEYS = ("pivot", "consolidation_high", "final_contraction_low")


@dataclass(frozen=True)
class ScanInputs:
    """Everything a scan reads. `members` are instrument ids (text) in the universe on `as_of`.

    `bars` holds the members' adjusted daily bars (`instrument_id`, `date`, `high`, `low`,
    `close`, `volume`, optional `traded_value`); `features` the date's feature rows
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


def _dec(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int | float):
        number = Decimal(repr(value))
        return number if number.is_finite() else None
    return None


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


def criteria_results(evaluation: PlaybookEvaluation) -> dict[str, Any]:
    """The stored `criteria_results`: the stages in stage order, stopping at the first that
    failed (HAT-CRIT-010), and the setup levels the live trigger reads (`levels`, text
    decimals, a level the setup did not expose left out)."""
    levels = {k: str(v) for k in LEVEL_KEYS if (v := _extra(evaluation, k)) is not None}
    return {"stages": [_stage_json(s) for s in evaluation.stages], "levels": levels}


def _extra(evaluation: PlaybookEvaluation, key: str) -> Decimal | None:
    for stage in evaluation.stages:
        if stage.stage != "setup":
            continue
        for outcome in stage.outcomes:
            if key in outcome.result.extras:
                return _dec(outcome.result.extras[key])
    return None


def _planned_stop(
    playbook: Playbook,
    evaluation: PlaybookEvaluation,
    pivot: Decimal | None,
    lows: Sequence[float | None],
    tick: Decimal | None,
) -> Decimal | None:
    """The stop the setup implies at the pivot, for the rules that can be priced overnight, on
    the instrument's tick grid (HAT-EXEC-053). A skip (too wide, no pivot, no tick) leaves it
    empty; the live entry recomputes it from the real entry price (HAT-BOT-062)."""
    if pivot is None or tick is None:
        return None
    rule = playbook.stop
    contraction_low = _extra(evaluation, "final_contraction_low")
    if isinstance(rule, StopBelowFinalContractionLow) and contraction_low is None:
        return None
    swing = [d for v in lows if v is not None and (d := _dec(v)) is not None]
    plan = plan_stop(
        rule,
        StopInputs(entry=pivot, tick=tick, final_contraction_low=contraction_low, swing_lows=swing),
    )
    return plan.stop


def next_session_tick(ctx: EvalContext) -> Decimal | None:
    """The tick of the session after the scan date: NSE sets it from the close of the last
    trading day of the month before (`data.ticks`). The day after `as_of` stands for the next
    session; across a month end with a holiday on its last weekday it reads one month early,
    which only the overnight planned stop uses (the live entry prices its own stop)."""
    history = ctx.history
    closes = [None if c is None else _dec(c) for c in history.close]
    return tick_for_date(history.dates, closes, ctx.as_of + timedelta(days=1))


def _lookback_lows(playbook: Playbook, ctx: EvalContext) -> list[float | None]:
    """The lows a `swing_low` stop reads: the last `lookback_days` bars, none for other rules and
    none when the history is shorter (a stop is never priced from a partial lookback)."""
    days = getattr(playbook.stop, "lookback_days", 0)
    lows = ctx.history.low
    return lows[-days:] if days and len(lows) >= days else []


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
        pivot = _extra(evaluation, "pivot") or _extra(evaluation, "consolidation_high")
        lows = _lookback_lows(playbook, ctx)
        rows.append(
            WatchRow(
                playbook=playbook.name,
                instrument_id=instrument_id,
                score=evaluation.stages[-1].score,
                criteria_results=criteria_results(evaluation),
                pivot=pivot,
                planned_stop=_planned_stop(
                    playbook, evaluation, pivot, lows, next_session_tick(ctx)
                ),
                adtr=_dec(adtr.get(instrument_id)),
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
        if playbook.name == EP_PLAYBOOK:
            continue
        rows.extend(_scan_playbook(config, playbook, inputs, features, per_instrument))
    rows.sort(key=lambda r: (-r.score, r.playbook, r.instrument_id))
    return ScanResult(tuple(rows), True, len(per_instrument))
