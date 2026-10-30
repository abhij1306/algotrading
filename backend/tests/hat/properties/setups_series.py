"""Small synthetic series for the S1 setups' tests (`test_s1_setups.py`)."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.hat.engine.criteria.base import EvalContext

START = date(2015, 1, 1)


def days(n: int) -> list[date]:
    return [START + timedelta(days=i) for i in range(n)]


def path(points: list[tuple[int, float]], start: float) -> list[float]:
    """Closes walking linearly from `start` to each (bars, level) in turn."""
    out, level = [], start
    for bars, target in points:
        step = (target - level) / bars
        out.extend(level + step * (i + 1) for i in range(bars))
        level = target
    return out


def frame(closes: list[float], volume: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": days(len(closes)),
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "close": closes,
            "volume": volume,
            "raw_close": closes,
        }
    )


def ctx(
    data: pl.DataFrame,
    benchmark: list[tuple[date, float]] = (),  # type: ignore[assignment]
    universe: pl.DataFrame | None = None,
    levels: dict[str, float] | None = None,
    calendar: list[date] | None = None,
) -> EvalContext:
    """`data`'s context; `calendar` the scan's sessions, by default the bars' dates after one
    session before them (a window that starts before the listing)."""
    dates = data["date"].to_list()
    found = EvalContext(
        as_of=dates[-1],
        instrument_id="i",
        bars=data,
        benchmark=benchmark,
        universe=universe,
        calendar=[dates[0] - timedelta(days=1), *dates] if calendar is None else calendar,
    )
    return found.with_levels(levels or {})


def quiet(n: int, total: int, low: float = 400.0, normal: float = 1000.0) -> list[float]:
    """`total` volumes, the last `n` at `low` (one of them at 300) and the rest `normal`."""
    out = [normal] * (total - n) + [low] * n
    out[-2] = 300.0
    return out


# Setup 2: a 300-bar advance to H0 ~100, a 30% cup, a rise to 82 and a 10-bar pause there.
CHEAT = path([(300, 100.0), (30, 70.0), (20, 82.0), (10, 82.0)], 40.0)
# Setup 3: the same cup, the right side back to 95, then an 11-bar handle down to 91.
HANDLE = path([(300, 100.0), (30, 70.0), (25, 95.0), (11, 91.0)], 40.0)
# Setup 4: 300 flat bars at 50, a thrust to 110, a flag of two contractions (to 95, to 100).
POWER = [50.0] * 300 + path([(30, 110.0), (6, 95.0), (5, 107.0), (4, 100.0), (4, 104.0)], 50.0)
# Setup 5: 200 bars of history: a rise to H0 100, contractions to 80 and to 87.
PRIMARY = path([(100, 100.0), (8, 80.0), (8, 95.0), (6, 87.0), (5, 92.0)], 50.0)
