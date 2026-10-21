"""PBT features.* (HAT-DATA-071..077, HAT-DEC-T11): the polars features on adjusted bars.

No look-ahead, the formulas against a direct Fraction evaluation, the RS and momentum percentiles,
breadth, split invariance, incremental equals full rebuild, the job's idempotence, and the
equivalence with the pandas `compute_rs_map` and stage-2 averages. The real `compute_features`,
store, adjusted view and `run_features` run; no network. Frames are a few hundred rows; failures
compare digests and short summaries, never whole frames.
"""

import contextlib
import hashlib
import math
import tempfile
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import polars as pl
from hypothesis import given, settings
from hypothesis import strategies as st

from app.hat.data import features as features_module
from app.hat.data.features import (
    ABOVE_MARGIN,
    BAR_COLUMNS,
    compute_features,
    features_job,
    load_bars,
    run_features,
)
from app.hat.data.store import (
    DATASET_BARS,
    DATASET_BREADTH,
    DATASET_FEATURES,
    FEATURE_NAMES,
    ActionFactor,
    BarRow,
    DataStore,
)
from app.hat.ids import uuid7
from app.services.vcp.pattern import compute_rs_map, stage2_metrics

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
SCALAR = (
    "sma10 sma20 sma50 sma150 sma200 high_52w low_52w adr20 adtr20 adv20 turnover20 "
    "ret21 ret63 ret126 ret252"
).split()
LONG = 253  # the shortest history with every window full


# --- generated histories ---------------------------------------------------------------------


class Series:
    """One instrument's rows, oldest first: Decimal bars with the §7 inputs."""

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


def universes(draw_pick: int, world: list[Series]) -> set[str] | None:
    """None (every instrument) or a deterministic subset chosen by `draw_pick`."""
    if draw_pick == 0:
        return None
    return {s.iid for i, s in enumerate(world) if (i + draw_pick) % 2 == 0}


def feature_rows(world: list[Series], universe: set[str] | None = None) -> dict:
    features, _ = compute_features(frame(world), universe)
    return {(r["instrument_id"], r["date"]): r for r in features.iter_rows(named=True)}


def close_to(got: float | None, want: Fraction | None) -> bool:
    if got is None or want is None:
        return got is None and want is None
    return math.isclose(got, float(want), rel_tol=1e-9, abs_tol=1e-12)


# --- HAT-DATA-071 no look-ahead --------------------------------------------------------------


@given(
    world=st.one_of(histories(), histories(full=True)),
    cut=st.integers(0, 700),
    kind=st.sampled_from(["change", "append", "drop"]),
    rng=RNG,
    pick=st.integers(0, 3),
)
@settings(max_examples=100, deadline=None)
def test_no_lookahead(world: list[Series], cut: int, kind: str, rng: Lcg, pick: int) -> None:
    """Features (and breadth) of every date <= d are identical after any bar dated after d is
    changed, dropped or after more bars are appended."""
    day = EPOCH + timedelta(days=cut)
    universe = universes(pick, world)
    changed: list[Series] = []
    for s in world:
        before = [r for r in s.rows if r["date"] <= day]
        after = [r for r in s.rows if r["date"] > day]
        if kind == "drop":
            after = []
        elif kind == "change":
            fresh = _make_series(rng, s.iid, len(after), 0, narrow=False, gaps=False).rows
            after = [{**f, "date": r["date"]} for f, r in zip(fresh, after, strict=True)]
        else:
            last = max(s.rows[-1]["date"], day) if s.rows else day
            extra = _make_series(rng, s.iid, rng.randrange(1, 40), 0, narrow=False, gaps=False).rows
            after = after + [
                {**f, "date": last + timedelta(days=i + 1)} for i, f in enumerate(extra)
            ]
        changed.append(Series(s.iid, before + after))
    base_f, base_b = compute_features(frame(world), universe)
    new_f, new_b = compute_features(frame(changed), universe)
    keep = pl.col("date") <= day
    a, b = base_f.filter(keep), new_f.filter(keep)
    assert a.height == b.height, (a.height, b.height)
    assert digest(a) == digest(b), f"features of dates <= {day} changed by {kind}"
    assert digest(base_b.filter(keep)) == digest(new_b.filter(keep)), "breadth changed"


@given(world=histories(), cut=st.integers(0, 700), batch=st.integers(1, 3), pick=st.integers(0, 3))
@settings(max_examples=60, deadline=None)
def test_batching_changes_nothing(world: list[Series], cut: int, batch: int, pick: int) -> None:
    """Computing a few instruments at a time (to bound memory) and dropping the dates before
    `from_date` early give exactly the features and breadth of the one-shot computation."""
    day = EPOCH + timedelta(days=cut)
    universe = universes(pick, world)
    whole_f, whole_b = compute_features(frame(world), universe)
    part_f, part_b = compute_features(frame(world), universe, from_date=day, batch=batch)
    keep = pl.col("date") >= day
    assert digest(part_f) == digest(whole_f.filter(keep))
    assert digest(part_b) == digest(whole_b.filter(keep))


# --- HAT-DATA-072 formulas -------------------------------------------------------------------


def _mean(values: list[Fraction]) -> Fraction:
    return sum(values, Fraction(0)) / len(values)


def naive(s: Series) -> dict[date, dict[str, Fraction | None]]:
    """The §7 definitions evaluated directly, in exact arithmetic, row by row."""
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


@given(world=st.one_of(histories(), histories(full=True)))
@settings(max_examples=100, deadline=None)
def test_formulas(world: list[Series]) -> None:
    """Every scalar feature equals the direct evaluation within 1e-9 relative, and is null
    exactly when the window is not full (or its base is zero)."""
    got = feature_rows(world)
    assert len(got) == sum(len(s.rows) for s in world)
    for s in world:
        for day, expected in naive(s).items():
            row = got[(s.iid, day)]
            for name in SCALAR:
                assert close_to(row[name], expected[name]), (
                    s.iid,
                    day,
                    name,
                    row[name],
                    expected[name],
                )


@given(rng=RNG, kind=st.sampled_from(["none", "all", "mixed"]))
@settings(max_examples=60, deadline=None)
def test_turnover_input(rng: Lcg, kind: str) -> None:
    """A bar's turnover input is close x volume when its traded value is null and the traded
    value when it is not (checked on 20 bars, so turnover20 is that bar-by-bar mean)."""
    s = _make_series(rng, "t", 20, 0, narrow=False, gaps=False)
    for r in s.rows:
        has = {"none": False, "all": True, "mixed": rng.random() < 0.5}[kind]
        r["traded_value"] = Decimal(rng.randrange(0, 10**9)) / 100 if has else None
    row = feature_rows([s])[("t", s.rows[-1]["date"])]
    inputs = [
        Fraction(r["close"]) * r["volume"]
        if r["traded_value"] is None
        else Fraction(r["traded_value"])
        for r in s.rows
    ]
    assert close_to(row["turnover20"], _mean(inputs))
    if kind == "none":
        assert close_to(
            row["turnover20"], _mean([Fraction(r["close"]) * r["volume"] for r in s.rows])
        )


# --- HAT-DATA-074, 075 percentiles -----------------------------------------------------------


def _avg_rank_pct(values: dict[str, float]) -> dict[str, float]:
    n = len(values)
    out = {}
    for key, v in values.items():
        less = sum(1 for x in values.values() if x < v)
        equal = sum(1 for x in values.values() if x == v)
        out[key] = 100.0 * (less + (equal + 1) / 2) / n
    return out


def _closes(world: list[Series]) -> dict[str, list[tuple[date, float]]]:
    return {s.iid: [(r["date"], float(r["close"])) for r in s.rows] for s in world}


def _ret_float(series: list[tuple[date, float]], i: int, n: int) -> float | None:
    if i < n or series[i - n][1] <= 0:
        return None
    return series[i][1] / series[i - n][1] - 1.0


def _weighted(series: list[tuple[date, float]], i: int) -> float | None:
    parts = [_ret_float(series, i, n) for n in (63, 126, 189, 252)]
    if any(p is None for p in parts):
        return None
    return 0.4 * parts[0] + 0.2 * parts[1] + 0.2 * parts[2] + 0.2 * parts[3]


@given(world=st.one_of(histories(), histories(full=True)), pick=st.integers(0, 3))
@settings(max_examples=100, deadline=None)
def test_rs_pct(world: list[Series], pick: int) -> None:
    """On every date, rs_pct is null outside the universe or without 253 bars, else a value in
    [0, 100] equal to 100 x average rank / count of the day's universe values, and a strictly
    greater weighted return never has a smaller rs_pct."""
    universe = universes(pick, world)
    got = feature_rows(world, universe)
    closes = _closes(world)
    per_day: dict[date, dict[str, float]] = defaultdict(dict)
    for iid, series in closes.items():
        for i, (day, _) in enumerate(series):
            w = _weighted(series, i)
            if w is not None and (universe is None or iid in universe):
                per_day[day][iid] = w
    for iid, series in closes.items():
        for day, _ in series:
            value = got[(iid, day)]["rs_pct"]
            if iid not in per_day[day]:
                assert value is None, (iid, day, value)
    for day, weights in per_day.items():
        expected = _avg_rank_pct(weights)
        for iid, pct in expected.items():
            value = got[(iid, day)]["rs_pct"]
            assert value is not None and 0.0 <= value <= 100.0
            assert math.isclose(value, pct, rel_tol=1e-12, abs_tol=1e-12), (iid, day, value, pct)
        for a, wa in weights.items():
            for b, wb in weights.items():
                if wa > wb:
                    assert got[(a, day)]["rs_pct"] >= got[(b, day)]["rs_pct"], (a, b, day)


@given(world=st.one_of(histories(), histories(full=True)), pick=st.integers(0, 3))
@settings(max_examples=100, deadline=None)
def test_mom_rank(world: list[Series], pick: int) -> None:
    """mom_rank is the largest of the day's universe percentiles of ret21, ret63 and ret126,
    in [0, 100]; null only when none of the three exists or the instrument is outside."""
    universe = universes(pick, world)
    got = feature_rows(world, universe)
    closes = _closes(world)
    pcts: dict[tuple[str, date], list[float]] = defaultdict(list)
    for n in (21, 63, 126):
        per_day: dict[date, dict[str, float]] = defaultdict(dict)
        for iid, series in closes.items():
            if universe is not None and iid not in universe:
                continue
            for i, (day, _) in enumerate(series):
                r = _ret_float(series, i, n)
                if r is not None:
                    per_day[day][iid] = r
        for day, values in per_day.items():
            for iid, pct in _avg_rank_pct(values).items():
                pcts[(iid, day)].append(pct)
    for iid, series in closes.items():
        for day, _ in series:
            value = got[(iid, day)]["mom_rank"]
            want = pcts.get((iid, day))
            if not want:
                assert value is None, (iid, day, value)
            else:
                assert 0.0 <= value <= 100.0
                assert math.isclose(value, max(want), rel_tol=1e-12, abs_tol=1e-12), (iid, day)


# --- HAT-DATA-076 breadth --------------------------------------------------------------------


@given(world=st.one_of(histories(), histories(full=True)), pick=st.integers(0, 3))
@settings(max_examples=100, deadline=None)
def test_breadth(world: list[Series], pick: int) -> None:
    """Per date, breadth% = 100 x |close > sma50| / |sma50 not null| over the universe (exact
    arithmetic), in [0, 100]; null when no member has an SMA50 yet."""
    universe = universes(pick, world)
    _, breadth = compute_features(frame(world), universe)
    rows = {r["date"]: r for r in breadth.iter_rows(named=True)}
    counts: dict[date, list[int]] = defaultdict(lambda: [0, 0, 0])
    for s in world:
        if universe is not None and s.iid not in universe:
            continue
        for day, exp in naive(s).items():
            counts[day][0] += 1
            sma = exp["sma50"]
            if sma is not None:
                counts[day][1] += 1
                close = next(r["close"] for r in s.rows if r["date"] == day)
                counts[day][2] += Fraction(close) > sma
    dates = {r["date"] for s in world for r in s.rows}
    assert set(rows) == dates
    for day in dates:
        size, with_sma, above = counts[day]
        row = rows[day]
        assert (row["universe_size"], row["with_sma50"], row["above_sma50"]) == (
            size,
            with_sma,
            above,
        )
        if with_sma == 0:
            assert row["breadth_pct"] is None
        else:
            assert 0.0 <= row["breadth_pct"] <= 100.0
            assert math.isclose(row["breadth_pct"], 100 * above / with_sma, rel_tol=1e-12)


@given(world=histories(long=False))
@settings(max_examples=40, deadline=None)
def test_breadth_without_sma50(world: list[Series]) -> None:
    """A universe in which no stock has 50 bars (the first 49 sessions of history): breadth is
    null on every date, in the frame and in the stored partition (the regime engine then
    stores no state), never zero and never an error."""
    _, breadth = compute_features(frame(world))
    assert breadth["breadth_pct"].null_count() == breadth.height
    assert breadth["with_sma50"].sum() in (0, None)
    if not world or not any(s.rows for s in world):
        return
    with tempfile.TemporaryDirectory() as tmp:
        store = _write_store(Path(tmp), world, {})
        run_features(store, [])
        for day in store.days(DATASET_BARS):
            stored = store.read_partition(DATASET_BREADTH, day)
            assert stored.height == 1 and stored["breadth_pct"][0] is None


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
    assert last["above_sma50"] == 0 and last["with_sma50"] == 2
    assert ABOVE_MARGIN < 1e-9


# --- stores ----------------------------------------------------------------------------------


def _write_store(root: Path, world: list[Series], extra: dict) -> DataStore:
    """The world as raw bars, one partition per date (Decimal exact)."""
    store = DataStore(root)
    run = uuid7()
    by_day: dict[date, list[BarRow]] = defaultdict(list)
    for k, s in enumerate(world):
        for r in s.rows:
            by_day[r["date"]].append(
                BarRow(
                    s.iid,
                    f"INE{k:09d}",
                    f"S{k}",
                    "EQ",
                    r["close"],
                    r["high"],
                    r["low"],
                    r["close"],
                    r["prev_close"],
                    r["volume"],
                    r["traded_value"],
                    "bhavcopy",
                    run,
                )
            )
    for day, rows in by_day.items():
        store.write_bars(day, rows)
    return store


def read_features(store: DataStore) -> dict[date, pl.DataFrame]:
    return {
        day: store.read_partition(DATASET_FEATURES, day).sort("instrument_id")
        for day in store.days(DATASET_FEATURES)
    }


def same_features(a: pl.DataFrame, b: pl.DataFrame) -> bool:
    """Equal ids, equal null patterns, floats within 1e-9 relative (a rolling mean's last bits
    depend on where its frame starts); rank columns and counts exactly."""
    if a.height != b.height or a["instrument_id"].to_list() != b["instrument_id"].to_list():
        return False
    for name in FEATURE_NAMES:
        for x, y in zip(a[name].to_list(), b[name].to_list(), strict=True):
            if (x is None) != (y is None):
                return False
            if x is not None:
                exact = name in ("rs_pct", "mom_rank")
                if (x != y) if exact else not math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-12):
                    return False
    return True


@given(
    rng=RNG,
    n_days=st.integers(4, 40),
    first=st.integers(0, 39),
    rows=st.integers(1, 6),
    slack=st.integers(0, 4),
)
@settings(max_examples=40, deadline=None)
def test_load_lookback(rng: Lcg, n_days: int, first: int, rows: int, slack: int) -> None:
    """For every instrument (including ones that stopped trading for longer than the slice) the
    loader returns the rows of first..last and exactly the last `rows` rows before `first`,
    whether the first read is bounded by the store's days or not."""
    days = [EPOCH + timedelta(days=i) for i in range(n_days)]
    first = min(first, n_days - 1)
    world = []
    for k in range(4):
        present = [d for d in days if rng.random() < rng.choice([0.2, 0.6, 1.0])]
        series = _make_series(rng, f"i{k}", len(present), 0, narrow=False, gaps=False)
        for r, d in zip(series.rows, present, strict=True):
            r["date"] = d
        world.append(series)
    with tempfile.TemporaryDirectory() as tmp:
        store = _write_store(Path(tmp), world, {})
        # a later run rewrites some bars with other values: the loader must read what
        # `bars_daily` (latest run per instrument and date) holds
        later = uuid7()
        for s0 in world[:2]:
            for r in s0.rows[::3]:
                store.write_bars(
                    r["date"],
                    [
                        BarRow(
                            s0.iid,
                            "INE0",
                            "S",
                            "EQ",
                            *(r["close"] + 1,) * 4,
                            r["prev_close"],
                            5,
                            None,
                            "bhavcopy",
                            later,
                        )
                    ],
                )
        con = store.connect([])
        try:
            with (
                patch.object(features_module, "LOOKBACK_ROWS", rows),
                patch.object(features_module, "LOOKBACK_SLACK_DAYS", slack),
            ):
                bounded = load_bars(con, days[first], days[-1], store.days(DATASET_BARS))
                exact = load_bars(con, days[first], days[-1])
            view = {
                (i, d): (h, c)
                for i, d, h, c in con.execute(
                    "SELECT instrument_id, date, CAST(high AS DOUBLE), CAST(close AS DOUBLE) "
                    "FROM bars_daily"
                ).fetchall()
            }
        finally:
            con.close()
    for got in (bounded, exact):
        for i, d, h, c in got.select("instrument_id", "date", "high", "close").iter_rows():
            assert view[(i, d)] == (h, c), (i, d)
    want = set()
    for s in world:
        mine = [r["date"] for r in s.rows]
        before = [d for d in mine if d < days[first]]
        want |= {
            (s.iid, d)
            for d in before[max(0, len(before) - rows) :] + [d for d in mine if d >= days[first]]
        }
    for got in (bounded, exact):
        assert {(i, d) for i, d in got.select("instrument_id", "date").iter_rows()} == want
        assert got.height == len(want)


# --- HAT-DATA-077 incremental equals full ----------------------------------------------------


def _split_actions(world: list[Series], rng: Lcg) -> list[ActionFactor]:
    """A split (factor < 1) for one or two instruments at a date inside their bars."""
    out = []
    for s in world[:2]:
        if len(s.rows) > 20 and rng.random() < 0.7:
            ex = s.rows[rng.randrange(5, len(s.rows) - 5)]["date"]
            out.append(ActionFactor(s.iid, ex, Decimal(rng.choice(["0.5", "0.25", "0.2", "0.1"]))))
    return out


@given(
    world=histories(max_instruments=3),
    rng=RNG,
    chunk=st.integers(1, 40),
    steps=st.integers(1, 4),
)
@settings(max_examples=8, deadline=None)
def test_incremental_equals_full(world: list[Series], rng: Lcg, chunk: int, steps: int) -> None:
    """Runs through successive cut dates (with a corporate action arriving part-way) leave the
    same features and breadth for every date as one full rebuild over the same bars."""
    dates = sorted({r["date"] for s in world for r in s.rows})
    if not dates:
        return
    final_actions = _split_actions(world, rng)
    cuts = sorted(rng.sample(dates, min(steps, len(dates))))
    with tempfile.TemporaryDirectory() as tmp:
        incr = _write_store(Path(tmp) / "incr", world, {})
        full = _write_store(Path(tmp) / "full", world, {})
        for k, cut in enumerate(cuts):
            seen = final_actions[: len(final_actions) * (k + 1) // len(cuts)]
            run_features(incr, seen, through=cut, chunk_days=chunk)
        run_features(incr, final_actions, chunk_days=chunk)
        report = run_features(full, final_actions, full=True, chunk_days=126)
        assert report.full and len(report.dates) == len(dates)
        a, b = read_features(incr), read_features(full)
        assert list(a) == list(b) == dates
        for day in dates:
            assert same_features(a[day], b[day]), f"features of {day} differ"
            sa = incr.read_partition(DATASET_BREADTH, day)
            sb = full.read_partition(DATASET_BREADTH, day)
            assert digest(sa) == digest(sb), f"breadth of {day} differs"


# --- HAT-DATA-073 split invariance -----------------------------------------------------------


@given(
    rng=RNG,
    factor=st.sampled_from(["0.5", "0.25", "0.2", "0.1", "0.05"]),
    length=st.integers(LONG, LONG + 30),
    ex_at=st.integers(1, 280),
)
@settings(max_examples=8, deadline=None)
def test_split_invariant(rng: Lcg, factor: str, length: int, ex_at: int) -> None:
    """A split at an ex-date, stored with its factor, leaves every price feature, return and
    percentile of every date as the unsplit series has them (raw prices before the ex-date are
    the unsplit prices divided by the factor; the adjusted view multiplies them back)."""
    f = Decimal(factor)
    base = [_make_series(rng, f"i{k}", length, 0, narrow=False, gaps=False) for k in range(3)]
    for s in base:
        for r in s.rows:
            r["volume"] = (r["volume"] // 20) * 20  # so raw volume x factor is whole
            r["traded_value"] = None
    ex_index = min(ex_at, length - 1)
    split = []
    for k, s in enumerate(base):
        rows = []
        for i, r in enumerate(s.rows):
            if k == 0 and i < ex_index:  # only the first instrument splits
                r = {
                    **r,
                    "high": r["high"] / f,
                    "low": r["low"] / f,
                    "close": r["close"] / f,
                    "prev_close": r["prev_close"] / f,
                    "volume": int(r["volume"] * f),
                }
            elif k == 0 and i == ex_index:
                r = {**r, "prev_close": r["prev_close"] / f}
            rows.append(r)
        split.append(Series(s.iid, rows))
    ex_date = base[0].rows[ex_index]["date"]
    with tempfile.TemporaryDirectory() as tmp:
        plain = _write_store(Path(tmp) / "plain", base, {})
        raw = _write_store(Path(tmp) / "split", split, {})
        run_features(plain, [])
        run_features(raw, [ActionFactor(base[0].iid, ex_date, f)])
        a, b = read_features(plain), read_features(raw)
    assert list(a) == list(b)
    for day in a:
        assert same_features(a[day], b[day]), f"features of {day} jump at the split"


# --- idempotence and resume ------------------------------------------------------------------


def _hashes(root: Path) -> dict[str, str]:
    """sha256 of every derived file (features, breadth and the job's state), by relative path."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.relative_to(root).parts[0] != DATASET_BARS
    }


@given(world=histories(max_instruments=3), rng=RNG, crash_at=st.integers(1, 6))
@settings(max_examples=8, deadline=None)
def test_job_idempotent(world: list[Series], rng: Lcg, crash_at: int) -> None:
    """The `features` body run twice leaves byte-identical files and the second run writes
    nothing; a crash at any write (between a day's features and its breadth, or between days,
    in a first run or after an earlier one) followed by re-runs ends with the same files and
    values as an uninterrupted run (a re-run after the job record was lost is harmless)."""
    dates = sorted({r["date"] for s in world for r in s.rows})
    if len(dates) < 3:
        return
    actions = _split_actions(world, rng)
    cut = dates[len(dates) // 2]
    with tempfile.TemporaryDirectory() as tmp:
        clean = _write_store(Path(tmp) / "clean", world, {})
        crashed = _write_store(Path(tmp) / "crashed", world, {})
        features_job(clean, lambda: actions, through=cut)()
        features_job(clean, lambda: actions)()
        before = _hashes(clean.root)
        again = features_job(clean, lambda: actions)()
        assert again.rows == 0
        assert _hashes(clean.root) == before

        features_job(crashed, lambda: actions, through=cut)()
        real = DataStore.write_partition
        calls: list[int] = []

        def crash(self: DataStore, *args: object, **kwargs: object) -> Path:
            calls.append(1)
            if len(calls) == crash_at:
                raise RuntimeError("crash")
            return real(self, *args, **kwargs)  # type: ignore[arg-type]

        # the run crashes only if it has that many writes
        with patch.object(DataStore, "write_partition", crash), contextlib.suppress(RuntimeError):
            features_job(crashed, lambda: actions)()
        features_job(crashed, lambda: actions)()
        features_job(crashed, lambda: actions)()
        # The same files; the same values (a re-run computes the remaining dates in another
        # frame, and a rolling mean's last bits depend on where its frame starts).
        assert _hashes(crashed.root).keys() == before.keys()
        a, b = read_features(crashed), read_features(clean)
        assert list(a) == list(b)
        for day in a:
            assert same_features(a[day], b[day]), f"features of {day} differ after the crash"
            sa = crashed.read_partition(DATASET_BREADTH, day)
            assert digest(sa) == digest(clean.read_partition(DATASET_BREADTH, day))


# --- equivalence with the pandas implementation ----------------------------------------------


@given(
    rng=RNG,
    n_inst=st.integers(2, 6),
    length=st.integers(LONG, LONG + 20),
    narrow=st.booleans(),
)
@settings(max_examples=20, deadline=None)
def test_pandas_equivalence(rng: Lcg, n_inst: int, length: int, narrow: bool) -> None:
    """Where the definitions match the features equal the pandas code: SMA 50/150/200 equal
    `stage2_metrics`' averages, and round(0.99 x rs_pct / 100) equals `compute_rs_map`'s 0-99
    rating (same weights, same 63/126/189/252-row horizons, same average-rank percentile, same
    omission below 253 bars; at an exact .5 the integer may differ by one, see below).
    They differ in two documented ways: rs_pct is 0-100 (the pandas rating is that, scaled to
    integers 0-99), and high_52w is the highest *high* where pandas
    takes the highest close, so ours is never below it."""
    world = [
        _make_series(rng, f"i{k}", length, 0, narrow=narrow, gaps=False) for k in range(n_inst)
    ]
    got = feature_rows(world)
    frames = {
        s.iid: pd.DataFrame({"close_ref": [float(r["close"]) for r in s.rows]}) for s in world
    }
    ratings = compute_rs_map(frames)
    for s in world:
        last = got[(s.iid, s.rows[-1]["date"])]
        metrics = stage2_metrics(frames[s.iid])
        assert math.isclose(last["sma50"], metrics.ma_50, rel_tol=1e-9)
        assert math.isclose(last["sma150"], metrics.ma_150, rel_tol=1e-9)
        assert math.isclose(last["sma200"], metrics.ma_200, rel_tol=1e-9)
        assert last["high_52w"] >= metrics.high_52 - 1e-9
        want = ratings[s.iid]
        scaled = last["rs_pct"] * 99 / 100
        mine = min(99, max(0, round(scaled)))
        # At an exact half (a rating of k + 0.5) the two float evaluations of the same ratio can
        # land either side of it, so the two integer ratings may differ by one there and only there.
        on_half = abs(scaled - math.floor(scaled) - 0.5) < 1e-9
        assert mine == want or (on_half and abs(mine - want) == 1), (s.iid, mine, want)
