"""The criterion contract: a pure function over daily bars returning `(passed, score, value,
reason)` (HAT-CRIT-021, 04 §4).

Every function reads its input only through `EvalContext`. The context exposes the instrument's
bars dated on or before the evaluation date and nothing later, so a result can never depend on
a later row (the common purity rule of 04 §4). A required input that is null or absent fails
closed: `passed = False`, reason `no_data`.

Threshold comparisons on prices and volumes are exact: floats are read as the decimal their
shortest text gives (`dec`), so a price of 1.15 against a 15% rule is a tie, not a rounding
accident. Nothing here reads the database, the clock or the network.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal, NamedTuple

import polars as pl
from pydantic import BaseModel, ConfigDict

Stage = Literal["universe", "setup", "trigger", "entry_filter"]
STAGES: tuple[str, ...] = ("universe", "setup", "trigger", "entry_filter")

REASON_OK = "ok"
REASON_NO_DATA = "no_data"


class ParamModel(BaseModel):
    """Base of every criterion parameter model: strict types, no unknown key, no NaN."""

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


def dec(value: float | int | Decimal) -> Decimal:
    """The decimal a number's shortest text names (exact for any price on a paise grid)."""
    return value if isinstance(value, Decimal) else Decimal(repr(value))


def finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


@dataclass(frozen=True)
class CriterionResult:
    """`passed`, a `score` in [0, 1], the measured `value`, a short `reason` code, and `extras`
    (named levels such as the pivot that a later stage reads)."""

    passed: bool
    score: float
    value: float | None
    reason: str
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score outside [0, 1]")


def no_data(reason: str = REASON_NO_DATA) -> CriterionResult:
    return CriterionResult(False, 0.0, None, reason)


def verdict(
    passed: bool, value: float | None, fail_reason: str, *, score: float | None = None
) -> CriterionResult:
    """A pass/fail result: score 1.0 or 0.0 unless `score` is given."""
    if score is None:
        score = 1.0 if passed else 0.0
    return CriterionResult(passed, score, value, REASON_OK if passed else fail_reason)


def clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


class Bars(NamedTuple):
    """One instrument's bars up to the evaluation date, oldest first, as plain lists."""

    dates: list[date]
    high: list[float | None]
    low: list[float | None]
    close: list[float | None]
    volume: list[float | None]
    traded_value: list[float | None]

    def __len__(self) -> int:
        return len(self.dates)


EMPTY_BARS = Bars([], [], [], [], [], [])
BAR_FIELDS = ("high", "low", "close", "volume", "traded_value")


def _clean(values: Sequence[float | None]) -> list[float | None]:
    return [float(v) if v is not None and math.isfinite(v) else None for v in values]


def _bars(frame: pl.DataFrame) -> Bars:
    columns = {
        name: _clean(frame[name].cast(pl.Float64).to_list())
        if name in frame.columns
        else [None] * frame.height
        for name in BAR_FIELDS
    }
    return Bars(frame["date"].to_list(), **columns)


@dataclass(eq=False)
class EvalContext:
    """What a criterion may read, for one instrument on one evaluation date.

    `bars`: the instrument's adjusted daily bars (`date`, `high`, `low`, `close`, `volume`,
    optional `traded_value`); rows after `as_of` are ignored. `universe`: the date's feature
    rows of every universe instrument (`instrument_id`, `rs_pct`, `ret21`, ...), for the rank
    criteria. `series`: the instrument's series (instrument master). `price_bands`: the
    instrument's `(effective_from, band_pct)` rows, `band_pct` None for no band.
    `fundamentals`: rows `filed_at`, `eps_yoy`, `sales_yoy` (none until Phase 2).
    `levels`: the numeric levels earlier criteria of the same stage exposed (such as `vcp`'s
    `pivot`), set by `with_levels`; empty outside a stage.
    """

    as_of: date
    instrument_id: str
    bars: pl.DataFrame
    universe: pl.DataFrame | None = None
    series: str | None = None
    price_bands: Sequence[tuple[date, float | None]] = ()
    fundamentals: pl.DataFrame | None = None
    levels: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    # The bars read so far, shared by the `with_levels` copies (not an init field, so
    # `dataclasses.replace` with other bars starts empty).
    _read: dict[str, Bars] = field(default_factory=dict, init=False, repr=False)

    def with_levels(self, levels: Mapping[str, float]) -> EvalContext:
        """A copy reading `levels` and sharing the bars read by either (this one is unchanged,
        so a level never leaks into another playbook's stage); itself when both have none."""
        if not levels and not self.levels:
            return self
        staged = copy.copy(self)
        staged.levels = MappingProxyType(dict(levels))
        return staged

    @property
    def history(self) -> Bars:
        """The bars dated on or before `as_of`, sorted by date; empty without a bar on `as_of`
        itself (an instrument that did not trade that day has no features for it either)."""
        if "history" not in self._read:
            frame = self.bars.filter(pl.col("date") <= self.as_of).sort("date")
            if frame.is_empty() or frame["date"][-1] != self.as_of:
                self._read["history"] = EMPTY_BARS
            else:
                self._read["history"] = _bars(frame)
        return self._read["history"]

    @property
    def previous_history(self) -> Bars:
        """The bars dated strictly before `as_of` (live criteria read the previous session)."""
        if "previous" not in self._read:
            self._read["previous"] = _bars(
                self.bars.filter(pl.col("date") < self.as_of).sort("date")
            )
        return self._read["previous"]


Evaluator = Callable[[EvalContext, Any], CriterionResult]
# A live criterion reads a `live.LiveContext` (typed as Any here: base must not import live).
LiveEvaluator = Callable[[Any, Any], CriterionResult]


@dataclass(frozen=True)
class CriterionType:
    """One catalogue entry: the parameter model (its JSON Schema is `hat_criterion_type`'s
    `param_schema`), the stage, the data it needs and its function: `evaluate` over daily bars,
    or `evaluate_live` over a `LiveContext` for a live trigger or entry filter."""

    code: str
    stage: Stage
    params: type[ParamModel]
    data_needs: tuple[str, ...]
    evaluate: Evaluator | None = None
    version: int = 1
    extra_stages: tuple[Stage, ...] = ()  # other sections that may hold this type
    evaluate_live: LiveEvaluator | None = None

    def param_schema(self) -> dict[str, Any]:
        return self.params.model_json_schema()
