"""The pure rules of the features job: SMA comparison under float noise, which ISINs are ranked
and counted, the stale days and chains a change touches, the rebuild chunks, and when a day's
percentiles may be spliced instead of recomputed."""

from datetime import date, timedelta
from decimal import Decimal

import polars as pl
from hypothesis import strategies as st

from app.hat.data.features import (
    ABOVE_MARGIN,
    BAR_COLUMNS,
    RANK_INPUTS,
    chunks,
    compute_features,
    splice_day,
)
from app.hat.data.features_state import LOOKBACK_ROWS, adjustment_of, affected_chains, stale_days

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
            cols["isin"].append(r.get("isin", "INE000A01011"))
            cols["date"].append(r["date"])
            for name in ("high", "low", "close", "prev_close"):
                cols[name].append(float(r[name]))
            cols["volume"].append(float(r["volume"]))
            cols["traded_value"].append(
                None if r["traded_value"] is None else float(r["traded_value"])
            )
    schema = dict.fromkeys(BAR_COLUMNS, pl.Float64)
    schema.update(instrument_id=pl.Utf8, isin=pl.Utf8, date=pl.Date)
    return pl.DataFrame(cols, schema=schema)


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


# --- the ranked universe (D257) --------------------------------------------------------------


def test_only_ordinary_equity_is_ranked_and_counted() -> None:
    """A fund row on the same day gets no percentile and is not in the breadth counts."""
    rows = [
        {
            "date": EPOCH + timedelta(days=i),
            "high": Decimal(11 + i),
            "low": Decimal(9 + i),
            "close": Decimal(10 + i),
            "prev_close": Decimal(10 + i),
            "volume": 10,
            "traded_value": None,
        }
        for i in range(LONG)
    ]
    world = [
        Series("eq", rows),
        Series("etf", [{**r, "isin": "INF204KB14I2"} for r in rows]),
    ]
    features, breadth = compute_features(frame(world))
    last = features.filter(pl.col("date") == rows[-1]["date"]).sort("instrument_id")
    assert last["instrument_id"].to_list() == ["eq", "etf"]
    assert last["rs_pct"].to_list() == [100.0, None]
    assert last["mom_rank"].to_list() == [100.0, None]
    assert breadth.filter(pl.col("date") == rows[-1]["date"])["universe_size"].to_list() == [1]


# --- what a run recomputes (DB5) -------------------------------------------------------------

DAYS = [EPOCH + timedelta(days=i) for i in range(600)]


def test_stale_days_cover_each_instruments_next_rows() -> None:
    """A changed day is stale with every later day on which an instrument it holds has one of
    its next LOOKBACK_ROWS rows (one that trades every other day reaches twice as far), and with
    at least the LOOKBACK_ROWS store days after it."""
    rare = set(DAYS[10::2])

    def presence(day: date) -> set[str]:
        return {"daily"} | ({"rare"} if day in rare else set())

    stale = stale_days(DAYS, {DAYS[10]}, presence)
    rare_end = sorted(rare)[LOOKBACK_ROWS]
    assert min(stale) == DAYS[10]
    assert max(stale) == rare_end
    assert DAYS[10 + LOOKBACK_ROWS] in stale
    # past the daily window only the rare instrument's days are stale
    assert DAYS[10 + LOOKBACK_ROWS + 1] not in stale
    assert DAYS[10 + LOOKBACK_ROWS + 2] in stale


def test_stale_days_floor_and_lost_days() -> None:
    """With nothing listed on a changed day the LOOKBACK_ROWS store days after it are still
    stale; a lost day (removed, or a recorded file gone) makes every day from it on stale, since
    the instruments it held are unknown; nothing dirty or lost, nothing stale."""
    assert stale_days(DAYS, set(), lambda _: {"x"}) == set()
    assert stale_days(DAYS, {DAYS[5]}, lambda _: set()) == set(DAYS[5 : 5 + LOOKBACK_ROWS + 1])
    removed = DAYS[0] - timedelta(days=1)
    assert stale_days(DAYS, set(), lambda _: set(), {removed}) == set(DAYS)
    assert stale_days(DAYS, {DAYS[3]}, lambda _: set(), {DAYS[400]}) == set(
        DAYS[3 : 3 + 253]
    ) | set(DAYS[400:])
    last = stale_days(DAYS, {DAYS[-1]}, lambda _: {"x"})
    assert last == {DAYS[-1]}


def test_affected_chains_and_their_adjustment() -> None:
    """A factor change touches its chain; a link change touches the old ISIN and both chains;
    the chains' part of the adjustment keeps every link into or out of them."""
    old = (["a|2024-01-05|0.5", "b|2024-02-01|0.2"], ["x>a"])
    assert affected_chains(old, old) == set()
    assert affected_chains(old, (["a|2024-01-05|0.5", "b|2024-02-02|0.2"], ["x>a"])) == {"b"}
    assert affected_chains(old, (old[0], ["x>c"])) == {"x", "a", "c"}
    assert affected_chains(old, (old[0], [])) == {"x", "a"}
    part = adjustment_of(old[0], ["x>a", "y>z"], {"a"})
    assert [(str(f.instrument_id), f.ex_date, f.factor) for f in part.factors] == [
        ("a", date(2024, 1, 5), Decimal("0.5"))
    ]
    assert dict(part.lineage) == {"x": "a"}


def test_chunks_bound_the_span_of_scattered_targets() -> None:
    targets = [DAYS[0], DAYS[1], DAYS[62], DAYS[63], DAYS[400]]
    assert chunks(targets, DAYS, 63) == [[DAYS[0], DAYS[1], DAYS[62]], [DAYS[63]], [DAYS[400]]]
    assert chunks([], DAYS, 63) == []


NO_OTHERS: dict[str, list[float]] = dict.fromkeys(RANK_INPUTS, [])


def _chain_row(
    iid: str, *, member: bool = True, ret: float | None = 0.1, sma: float = 10.0
) -> dict[str, object]:
    own = dict.fromkeys(("sma10", "sma20", "sma150", "sma200", "high_52w", "low_52w"), sma)
    own.update(adr20=1.0, adtr20=1.0, adv20=5.0, turnover20=50.0, sma50=sma)
    own.update(ret21=ret, ret63=ret, ret126=ret, ret252=ret)
    return {
        "instrument_id": iid,
        "_member": member,
        "_with": True,
        "_above": True,
        "rs_weighted": ret,
        **own,
    }


def test_splice_day_keeps_percentiles_when_returns_are_unchanged() -> None:
    """A whole-history scale (prices halve, returns equal within float noise) splices: the new
    row carries the new prices and the stored percentiles, and breadth does not move."""
    old = [_chain_row("a", sma=20.0)]
    new = [_chain_row("a", sma=10.0, ret=0.1 + 1e-16)]
    splice = splice_day(old, new, {"a": (55.0, 70.0)}, NO_OTHERS)
    assert splice is not None
    assert splice.rows[0]["sma50"] == 10.0
    assert (splice.rows[0]["rs_pct"], splice.rows[0]["mom_rank"]) == (55.0, 70.0)
    assert (splice.universe_size, splice.with_sma50, splice.above_sma50) == (0, 0, 0)


def test_splice_day_takes_the_percentiles_of_a_renamed_row() -> None:
    """A linked ISIN's row moves to its chain's id with the same returns: the chain row takes
    the old row's percentiles and the day's counts stay."""
    splice = splice_day(
        [_chain_row("old")], [_chain_row("chain")], {"old": (40.0, 41.0)}, NO_OTHERS
    )
    assert splice is not None
    assert [(r["instrument_id"], r["rs_pct"], r["mom_rank"]) for r in splice.rows] == [
        ("chain", 40.0, 41.0)
    ]


def test_splice_day_refuses_when_percentiles_move() -> None:
    """A changed return, a member leaving or joining, or a stored row missing: the day is
    recomputed whole. A non-member's change only moves the counts."""
    old = [_chain_row("a")]
    assert splice_day(old, [_chain_row("a", ret=0.2)], {"a": (1.0, 1.0)}, NO_OTHERS) is None
    assert splice_day(old, [], {"a": (1.0, 1.0)}, NO_OTHERS) is None
    assert splice_day(old, [_chain_row("a", member=False)], {"a": (1.0, 1.0)}, NO_OTHERS) is None
    assert splice_day(old, old, {}, NO_OTHERS) is None
    fund = splice_day([], [_chain_row("f", member=False, ret=0.3)], {}, NO_OTHERS)
    assert fund is not None
    assert fund.rows[0]["rs_pct"] is None
    assert (fund.universe_size, fund.with_sma50, fund.above_sma50) == (0, 0, 0)
    joined = splice_day([], [_chain_row("n", ret=None)], {}, NO_OTHERS)
    assert joined is not None
    assert (joined.universe_size, joined.with_sma50, joined.above_sma50) == (1, 1, 1)


def test_splice_day_refuses_a_value_that_moves_past_another() -> None:
    """A return within float noise of the old one still recomputes the day when another member's
    value (or a tie with it) lies between the old and the new value; one that moves past no other
    value splices."""
    old = [_chain_row("a", ret=0.1)]
    new = [_chain_row("a", ret=0.1 + 1e-15)]
    ranks = {"a": (50.0, 50.0)}
    between = {**NO_OTHERS, "rs_weighted": [0.1 + 5e-16]}
    assert splice_day(old, new, ranks, between) is None
    tie = {**NO_OTHERS, "ret63": [0.1]}
    assert splice_day(old, new, ranks, tie) is None
    clear = {c: [0.05, 0.2] for c in RANK_INPUTS}
    assert splice_day(old, new, ranks, clear) is not None
    # two chain rows trading places within the noise
    pair_old = [_chain_row("a", ret=0.1), _chain_row("b", ret=0.1 + 1e-15)]
    pair_new = [_chain_row("a", ret=0.1 + 2e-15), _chain_row("b", ret=0.1 + 1e-15)]
    assert splice_day(pair_old, pair_new, {"a": (1.0, 1.0), "b": (2.0, 2.0)}, NO_OTHERS) is None
