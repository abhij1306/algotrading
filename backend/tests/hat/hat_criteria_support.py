"""Generators, exact oracles and the common contract of 04 §4 for the criteria properties.

Prices are integers in paise and volumes integers, so every oracle is a `Fraction` computed
without a float. A frame holds `paise / 100` as floats (the shortest text of such a float is the
paise decimal, which is what the library reads). One Hypothesis example is one integer seed
(`Lcg`), so a few hundred bars cost no entropy budget and replay exactly.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from fractions import Fraction

import polars as pl
from hypothesis import strategies as st

from app.hat.engine.criteria import CriterionResult, EvalContext, evaluate

EPOCH = date(2024, 1, 1)
REASON_NO_DATA = "no_data"


class Lcg:
    """A small deterministic generator (splitmix64) seeded by Hypothesis."""

    def __init__(self, seed: int) -> None:
        self.state = seed

    def _next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) % 2**64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) % 2**64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) % 2**64
        return z ^ (z >> 31)

    def randrange(self, start: int, stop: int | None = None) -> int:
        if stop is None:
            start, stop = 0, start
        return start + self._next() % (stop - start)

    def choice(self, items):  # noqa: ANN001, ANN201 - any sequence
        return items[self.randrange(len(items))]

    def chance(self, numerator: int, denominator: int) -> bool:
        return self.randrange(denominator) < numerator


RNG = st.integers(0, 2**32).map(Lcg)


def rupees(paise: int) -> Fraction:
    return Fraction(paise, 100)


@dataclasses.dataclass
class Row:
    """One bar in paise; `traded` is whole rupees (None: unknown) and `volume` shares."""

    day: date
    high: int
    low: int
    close: int
    volume: int
    traded: int | None = None


def make_rows(
    rng: Lcg, length: int, style: str = "walk", *, start: int = 0, base: int = 10000
) -> list[Row]:
    """`length` consecutive-day bars. Styles: `walk` (a random walk), `tie` (few price levels,
    so equal averages and equal ratios occur), `flat` (one price), `trend` (a walk drifting up or
    down, so large moves of either sign occur)."""
    rows: list[Row] = []
    price = base
    drift = rng.choice([-150, 150]) if style == "trend" else 0
    for i in range(length):
        if style == "flat":
            close, up, down = base, 0, 0
        elif style == "tie":
            close = rng.choice([base, base + 100, base + 200])
            up, down = rng.choice([0, 100]), rng.choice([0, 100])
        else:
            price = max(100, price + drift + rng.randrange(-300, 301))
            close, up, down = price, rng.randrange(0, 400), rng.randrange(0, 400)
        low = max(1, close - down)
        rows.append(
            Row(
                EPOCH + timedelta(days=start + i),
                close + up,
                low,
                close,
                rng.randrange(0, 5_000_000),
                rng.choice([None, rng.randrange(0, 10**9)]),
            )
        )
    return rows


def frame(rows: list[Row]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [r.day for r in rows],
            "high": [r.high / 100 for r in rows],
            "low": [r.low / 100 for r in rows],
            "close": [r.close / 100 for r in rows],
            "volume": [float(r.volume) for r in rows],
            "traded_value": [None if r.traded is None else float(r.traded) for r in rows],
        },
        schema={
            "date": pl.Date,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
            "traded_value": pl.Float64,
        },
    )


def context(rows: list[Row], **kwargs: object) -> EvalContext:
    """A context evaluated on the last row's date."""
    return EvalContext(rows[-1].day if rows else EPOCH, "x", frame(rows), **kwargs)  # type: ignore[arg-type]


def future_rows(last: date, rng: Lcg, count: int = 3) -> list[Row]:
    """Rows dated after `last`, with arbitrary values."""
    return make_rows(
        rng, count, "walk", start=(last - EPOCH).days + 1, base=rng.randrange(100, 90000)
    )


def contract(code: str, params: dict, ctx: EvalContext, rng: Lcg) -> CriterionResult:
    """The common properties of 04 §4 for one call: repeated calls agree, rows dated after the
    evaluation date change nothing (no look-ahead), and the score is in [0, 1] (the result type
    refuses it otherwise). Returns the result."""
    first = evaluate(code, params, ctx)
    assert evaluate(code, params, ctx) == first
    later = dataclasses.replace(ctx, bars=pl.concat([ctx.bars, frame(future_rows(ctx.as_of, rng))]))
    assert evaluate(code, params, later) == first, "a later row changed the result"
    assert 0.0 <= first.score <= 1.0
    assert first.passed == (first.reason == "ok")
    return first


def fails_closed(code: str, params: dict, ctx: EvalContext) -> bool:
    """With every bar removed the criterion fails closed with reason `no_data`."""
    empty = dataclasses.replace(ctx, bars=ctx.bars.head(0))
    result = evaluate(code, params, empty)
    return (not result.passed) and result.reason == REASON_NO_DATA


def universe_frame(rows: list[tuple[str, dict[str, float | None]]]) -> pl.DataFrame:
    names = sorted({k for _, cols in rows for k in cols})
    data: dict[str, list] = {"instrument_id": [i for i, _ in rows]}
    for name in names:
        data[name] = [cols.get(name) for _, cols in rows]
    schema = dict.fromkeys(names, pl.Float64)
    schema["instrument_id"] = pl.Utf8
    return pl.DataFrame(data, schema=schema)
