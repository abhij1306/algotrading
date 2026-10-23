"""PBT features.*: the polars features on adjusted bars.

No look-ahead, the formulas against a direct Fraction evaluation, the RS and momentum percentiles,
breadth, split invariance, incremental equals full rebuild, the job's idempotence, and the
X The real `compute_features`,
store, adjusted view and `run_features` run; no network. Frames are a few hundred rows; failures
compare digests and short summaries, never whole frames.
"""

import hashlib
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import polars as pl
from hypothesis import strategies as st

from app.hat.data.features import (
    ABOVE_MARGIN,
    BAR_COLUMNS,
    compute_features,
)
from app.hat.data.store import (
    DATASET_BARS,
)

EPOCH = date(2020, 1, 6)


class Lcg:
    """A small deterministic generator (splitmix64) seeded by Hypothesis: a whole example is one
    integer, so a few hundred bars cost no entropy budget and replay exactly."""

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

    def random(self) -> float:
        return self._next() / 2**64

    def choice(self, items):  # noqa: ANN001, ANN201 - any sequence
        return items[self.randrange(len(items))]

    def sample(self, items, k: int) -> list:  # noqa: ANN001
        pool = list(items)
        return [pool.pop(self.randrange(len(pool))) for _ in range(k)]


RNG = st.integers(0, 2**32).map(Lcg)
LONG = 253  # the shortest history with every window full


# --- generated histories ---------------------------------------------------------------------


class Series:
    """One instrument's rows, oldest first: Decimal bars with the feature inputs."""

    def __init__(self, iid: str, rows: list[dict]) -> None:
        self.iid = iid
        self.rows = rows

    def __repr__(self) -> str:
        first = self.rows[0]["date"].isoformat() if self.rows else "-"
        return f"Series({self.iid}, {len(self.rows)} bars from {first})"


def _make_series(
    rng: Lcg, iid: str, length: int, start: int, *, narrow: bool, gaps: bool
) -> Series:
    pool = [Decimal(x) for x in ("0.05", "1.00", "1.05", "20.00", "20.05")]
    rows: list[dict] = []
    index = start
    prev = Decimal(0)
    for _ in range(length):
        if narrow:
            close = rng.choice(pool)
        else:
            close = Decimal(rng.randrange(5, 999995)) / 100
        high = close + Decimal(rng.randrange(0, 500)) / 100
        low = max(Decimal("0.01"), close - Decimal(rng.randrange(0, 500)) / 100)
        volume = rng.choice([0, rng.randrange(0, 10**7)])
        traded = rng.choice([None, Decimal(rng.randrange(0, 10**11)) / 100])
        rows.append(
            {
                "date": EPOCH + timedelta(days=index),
                "high": high,
                "low": low,
                "close": close,
                "prev_close": rng.choice([prev, prev, prev, Decimal(0)]),
                "volume": volume,
                "traded_value": traded,
            }
        )
        prev = close
        index += rng.choice([1, 1, 1, 2, 5]) if gaps else 1
    return Series(iid, rows)


@st.composite
def histories(
    draw: st.DrawFn, *, long: bool = True, full: bool = False, max_instruments: int = 4
) -> list[Series]:
    """1-4 instruments with different starts, lengths (short, around the 200/252 windows, or
    long), optional gaps (a day without a bar is not a row), a clone of the first instrument
    (identical returns: tied percentiles) and a narrow price pool (flat stretches, ties)."""
    rng = draw(RNG)
    narrow = draw(st.booleans())
    gaps = draw(st.booleans())
    lengths = st.one_of(
        st.integers(0, 60), st.integers(195, 205), st.integers(248, 262), st.integers(0, 300)
    )
    if not long:
        lengths = st.integers(0, 49)
    if full:  # every window of every instrument is full on the last dates
        lengths = st.integers(LONG, 300)
    out = []
    for k in range(draw(st.integers(1, max_instruments))):
        out.append(
            _make_series(
                rng,
                f"i{k:02d}",
                draw(lengths),
                draw(st.integers(0, 20)),
                narrow=narrow,
                gaps=gaps,
            )
        )
    if draw(st.booleans()) and out:
        out.append(Series("iclone", [dict(r) for r in out[0].rows]))
    return out


def frame(world: list[Series]) -> pl.DataFrame:
    cols: dict[str, list] = {name: [] for name in BAR_COLUMNS}
    for s in world:
        for r in s.rows:
            cols["instrument_id"].append(s.iid)
            cols["date"].append(r["date"])
            for name in ("high", "low", "close", "prev_close"):
                cols[name].append(float(r[name]))
            cols["volume"].append(float(r["volume"]))
            cols["traded_value"].append(
                None if r["traded_value"] is None else float(r["traded_value"])
            )
    schema = dict.fromkeys(BAR_COLUMNS, pl.Float64)
    schema.update(instrument_id=pl.Utf8, date=pl.Date)
    return pl.DataFrame(cols, schema=schema)


def digest(df: pl.DataFrame) -> str:
    """A short summary for assertion messages and equality: a sha256 over the sorted rows."""
    text = df.sort(df.columns).write_csv()
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --- no look-ahead --------------------------------------------------------------


# --- formulas -------------------------------------------------------------------


def _mean(values: list[Fraction]) -> Fraction:
    return sum(values, Fraction(0)) / len(values)


def naive(s: Series) -> dict[date, dict[str, Fraction | None]]:
    """The feature definitions evaluated directly, in exact arithmetic, row by row."""
    rows = s.rows
    out: dict[date, dict[str, Fraction | None]] = {}
    for i, row in enumerate(rows):
        res: dict[str, Fraction | None] = {}

        def window(n: int, i: int = i) -> list[dict] | None:
            return rows[i - n + 1 : i + 1] if i + 1 >= n else None

        for n in (10, 20, 50, 150, 200):
            w = window(n)
            res[f"sma{n}"] = None if w is None else _mean([Fraction(r["close"]) for r in w])
        w = window(252)
        res["high_52w"] = None if w is None else max(Fraction(r["high"]) for r in w)
        res["low_52w"] = None if w is None else min(Fraction(r["low"]) for r in w)
        w = window(20)
        if w is None:
            res.update(adr20=None, adtr20=None, adv20=None, turnover20=None)
        else:
            if any(r["low"] <= 0 for r in w):
                res["adr20"] = None
            else:
                res["adr20"] = 100 * _mean(
                    [Fraction(r["high"]) / Fraction(r["low"]) - 1 for r in w]
                )

            def true_range(r: dict) -> Fraction:
                high, low, prev = Fraction(r["high"]), Fraction(r["low"]), Fraction(r["prev_close"])
                if prev > 0:
                    return max(high, prev) - min(low, prev)
                return high - low  # no usable previous close: the bar's own range

            res["adtr20"] = _mean([true_range(r) for r in w])
            res["adv20"] = _mean([Fraction(r["volume"]) for r in w])
            res["turnover20"] = _mean(
                [
                    Fraction(r["close"]) * r["volume"]
                    if r["traded_value"] is None
                    else Fraction(r["traded_value"])
                    for r in w
                ]
            )
        for n in (21, 63, 126, 252):
            base = Fraction(rows[i - n]["close"]) if i >= n else None
            res[f"ret{n}"] = None if not base else Fraction(row["close"]) / base - 1
        out[row["date"]] = res
    return out


# --- percentiles -----------------------------------------------------------


# --- breadth --------------------------------------------------------------------


def test_flat_series_is_not_above_its_own_mean() -> None:
    """Float noise in a rolling mean must not make a flat price count as above its SMA50: the
    margin is far below any real difference and far above the noise (a close of exactly the
    mean is `not above`, as the spec's exact comparison says)."""
    rows = [
        {
            "date": EPOCH + timedelta(days=i),
            "high": Decimal("0.07"),
            "low": Decimal("0.03"),
            "close": Decimal("0.05"),
            "prev_close": Decimal("0.05"),
            "volume": 10,
            "traded_value": None,
        }
        for i in range(120)
    ]
    _, breadth = compute_features(
        frame([Series("flat", rows), Series("flat2", [dict(r) for r in rows])])
    )
    last = breadth.filter(pl.col("date") == rows[-1]["date"]).row(0, named=True)
    assert last["above_sma50"] == 0
    assert last["with_sma50"] == 2
    assert ABOVE_MARGIN < 1e-9


# --- stores ----------------------------------------------------------------------------------


# --- incremental equals full ----------------------------------------------------


# --- split invariance -----------------------------------------------------------


# --- idempotence and resume ------------------------------------------------------------------


def _hashes(root: Path) -> dict[str, str]:
    """Sha256 of every derived file (features, breadth and the job's state), by relative path."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.relative_to(root).parts[0] != DATASET_BARS
    }


# --- equivalence with the legacy pandas implementation (frozen oracle) -----------------------
