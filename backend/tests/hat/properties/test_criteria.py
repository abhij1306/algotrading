"""PBT criteria.* (HAT-CRIT-040, 100..114, 120): the library's pure criteria against exact oracles.

Every test checks the common contract of 04 §4 (repeated calls agree, a row dated after the
evaluation date changes nothing, the score is in [0, 1], missing data fails closed) and then the
criterion's own rule against an independent computation in `Fraction`s over integer paise. The
live trigger and entry-filter types (`gap_up` .. `max_chase`, HAT-CRIT-115..119, 121, 122) read a
`LiveContext`; their contracts, the trigger conjunction, the entry-filter skip and the EP candidate
rule are checked at the end, the breakout through the real bar builder and trigger monitor.
"""

import dataclasses
import json
import math
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import polars as pl
import pytest
from hat.hat_criteria_support import (
    EPOCH,
    RNG,
    Lcg,
    Row,
    context,
    contract,
    fails_closed,
    frame,
    make_rows,
    rupees,
    universe_frame,
)
from hat.hat_trigger_support import (
    AT,
    DAY,
    MODES,
    S1,
    S2,
    candidate,
    drive,
    ist,
    live_contract,
    paise,
    playbook,
    seed_playbook,
    session_ticks,
)
from hat_engine_support import seed_engine_world
from hat_risk_support import valid_ctx, version_of
from hat_support import memory_engine
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from app.hat.data.bars import BarBuilder
from app.hat.data.features import BAR_COLUMNS, compute_features
from app.hat.data.gapscan import Gapper, GapRow
from app.hat.engine.criteria import CriterionResult, EvalContext, evaluate, evaluate_live
from app.hat.engine.criteria import vcp as vcp_module
from app.hat.engine.criteria.live import LiveContext, resolve_level
from app.hat.engine.live_entry import ep_candidates, submit_trigger
from app.hat.engine.trades import evaluate_signal
from app.hat.engine.trigger import (
    DailyInputs,
    Trigger,
    TriggerEvaluation,
    TriggerMonitor,
    entry_filter_instances,
    evaluate_trigger,
    plan_entry,
)
from app.hat.models import HatSignal
from app.hat.risk.policy import seed_policy

STYLES = st.sampled_from(["walk", "tie", "flat"])
SLOW = settings(
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large], deadline=None
)


def mean(values: list[Fraction]) -> Fraction:
    return sum(values, Fraction(0)) / len(values)


def sma_of(rows: list[Row], n: int, back: int = 0) -> Fraction | None:
    end = len(rows) - back
    if end < n:
        return None
    return mean([rupees(r.close) for r in rows[end - n : end]])


# --- HAT-CRIT-100 series_in ------------------------------------------------------------------

SERIES = ["EQ", "BE", "SM", "N1", "BZ"]


@given(
    rng=RNG,
    series=st.one_of(st.none(), st.sampled_from(SERIES)),
    allowed=st.lists(st.sampled_from(SERIES), min_size=1, max_size=4, unique=True),
)
def test_series_in_contract(rng: Lcg, series: str | None, allowed: list[str]) -> None:
    ctx = context(make_rows(rng, 3), series=series)
    result = contract("series_in", {"series": allowed}, ctx, rng)
    if series is None:
        assert not result.passed
        assert result.reason == "no_data"
    else:
        assert result.passed == (series in allowed)


# --- HAT-CRIT-101 min_turnover ---------------------------------------------------------------


@given(
    rng=RNG,
    length=st.integers(0, 30),
    days=st.integers(1, 25),
    inr_cr=st.sampled_from([0.5, 1.0, 10.0, 12.5, 100.0, 1000.0]),
    tie=st.booleans(),
)
@example(rng=Lcg(1), length=20, days=20, inr_cr=10.0, tie=True)
def test_min_turnover_contract(rng: Lcg, length: int, days: int, inr_cr: float, tie: bool) -> None:
    rows = make_rows(rng, length)
    if tie:  # every session trades exactly the threshold: the rule is "at least"
        for r in rows:
            r.traded = int(inr_cr * 10**7)
    ctx = context(rows)
    result = contract("min_turnover", {"days": days, "inr_cr": inr_cr}, ctx, rng)
    window = rows[length - days :] if length >= days else None
    if window is None:
        assert not result.passed
        assert result.reason == "no_data"
        return
    traded = [
        Fraction(r.traded) if r.traded is not None else rupees(r.close) * r.volume for r in window
    ]
    assert result.passed == (mean(traded) >= Fraction(inr_cr) * 10**7)
    assert fails_closed("min_turnover", {"days": days, "inr_cr": inr_cr}, ctx)


# --- HAT-CRIT-102 min_price ------------------------------------------------------------------


@given(
    rng=RNG,
    inr=st.sampled_from([0.0, 49.99, 50.0, 50.01, 100.0, 250.5]),
    close=st.integers(1, 40000),
)
@example(rng=Lcg(1), inr=50.0, close=5000)
def test_min_price_contract(rng: Lcg, inr: float, close: int) -> None:
    rows = make_rows(rng, 3)
    rows[-1].close, rows[-1].high, rows[-1].low = close, close + 5, max(1, close - 5)
    ctx = context(rows)
    result = contract("min_price", {"inr": inr}, ctx, rng)
    assert result.passed == (rupees(close) >= Fraction(str(inr)))
    assert fails_closed("min_price", {"inr": inr}, ctx)


# --- HAT-CRIT-103 price_band_allowed ---------------------------------------------------------

BANDS = [2, 5, 10, 20, "none"]


@given(
    rng=RNG,
    history=st.lists(
        st.tuples(st.integers(-30, 30), st.sampled_from([None, 2.0, 5.0, 10.0, 20.0])), max_size=5
    ),
    allowed=st.one_of(
        st.none(), st.lists(st.sampled_from(BANDS), min_size=1, max_size=5, unique=True)
    ),
)
def test_price_band_allowed_contract(rng: Lcg, history: list, allowed: list | None) -> None:
    rows = make_rows(rng, 3)
    as_of = rows[-1].day
    bands = [(as_of + timedelta(days=d), pct) for d, pct in history]
    ctx = context(rows, price_bands=bands)
    params = {} if allowed is None else {"allowed": allowed}
    result = contract("price_band_allowed", params, ctx, rng)
    effective = sorted((d, i) for i, (d, _) in enumerate(bands) if d <= as_of)
    if not effective:
        assert not result.passed
        assert result.reason == "no_data"
        return
    pct = bands[effective[-1][1]][1]
    name = "none" if pct is None else int(pct)
    assert result.passed == (name in (BANDS if allowed is None else allowed))


# --- HAT-CRIT-104 adr_min --------------------------------------------------------------------


@given(
    rng=RNG,
    style=STYLES,
    length=st.integers(0, 30),
    days=st.sampled_from([5, 20]),
    pct=st.sampled_from([0.0, 1.0, 4.0, 5.0, 10.0]),
)
@SLOW
def test_adr_min_contract(rng: Lcg, style: str, length: int, days: int, pct: float) -> None:
    rows = make_rows(rng, length, style)
    ctx = context(rows)
    result = contract("adr_min", {"days": days, "pct": pct}, ctx, rng)
    if length < days:
        assert not result.passed
        assert result.reason == "no_data"
        return
    adr = mean([100 * (rupees(r.high) / rupees(r.low) - 1) for r in rows[length - days :]])
    assert result.passed == (adr >= Fraction(str(pct)))
    if days == 20 and abs(float(adr) - pct) > 1e-9:  # the `adr20` column of 02 section 7
        feats, _ = compute_features(_feature_bars(rows))
        assert result.passed == (feats["adr20"][-1] >= pct)


def _feature_bars(rows: list[Row]) -> pl.DataFrame:
    bars = frame(rows).with_columns(
        pl.lit("x").alias("instrument_id"),
        pl.col("close").shift(1).alias("prev_close"),
    )
    return bars.select(BAR_COLUMNS)


# --- HAT-CRIT-105 sma_above ------------------------------------------------------------------

SIDES = st.sampled_from(["close", 10, 50, 150, 200])


@given(rng=RNG, style=STYLES, length=st.sampled_from([0, 5, 60, 199, 200, 230]), a=SIDES, b=SIDES)
@SLOW
def test_sma_above_contract(rng: Lcg, style: str, length: int, a: object, b: object) -> None:
    assume(a != b)
    rows = make_rows(rng, length, style)
    ctx = context(rows)
    params = {"a": a, "b": b}
    result = contract("sma_above", params, ctx, rng)

    def value(side: object) -> Fraction | None:
        if not rows:
            return None
        return rupees(rows[-1].close) if side == "close" else sma_of(rows, int(side))  # type: ignore[arg-type]

    left, right = value(a), value(b)
    if left is None or right is None:
        assert not result.passed
        assert result.reason == "no_data"
    else:
        assert result.passed == (left > right)
    assert fails_closed("sma_above", params, ctx)


# --- HAT-CRIT-106 sma_rising -----------------------------------------------------------------


@given(
    rng=RNG,
    style=STYLES,
    length=st.sampled_from([0, 30, 70, 100, 230]),
    sma=st.sampled_from([10, 50, 200]),
    lookback=st.sampled_from([1, 5, 21, 40]),
)
@SLOW
def test_sma_rising_contract(rng: Lcg, style: str, length: int, sma: int, lookback: int) -> None:
    rows = make_rows(rng, length, style)
    ctx = context(rows)
    params = {"sma": sma, "lookback_days": lookback}
    result = contract("sma_rising", params, ctx, rng)
    now, before = sma_of(rows, sma), sma_of(rows, sma, lookback)
    if now is None or before is None:
        assert not result.passed
        assert result.reason == "no_data"
    else:
        assert result.passed == (now > before)
    assert fails_closed("sma_rising", params, ctx)


# --- HAT-CRIT-107 / 108 52-week position -----------------------------------------------------

MIN_PCT = st.sampled_from([0.0, 1.0, 10.0, 30.0, 100.0])


@given(rng=RNG, style=STYLES, length=st.sampled_from([0, 100, 251, 252, 253, 280]), min_pct=MIN_PCT)
@SLOW
def test_pct_from_52w_low_contract(rng: Lcg, style: str, length: int, min_pct: float) -> None:
    rows = make_rows(rng, length, style)
    ctx = context(rows)
    params = {"min_pct": min_pct}
    result = contract("pct_from_52w_low", params, ctx, rng)
    if length < 252:
        assert not result.passed
        assert result.reason == "no_data"
        return
    low = rupees(min(r.low for r in rows[-252:]))
    assert result.passed == ((rupees(rows[-1].close) / low - 1) * 100 >= Fraction(str(min_pct)))
    assert fails_closed("pct_from_52w_low", params, ctx)


@given(
    rng=RNG,
    style=STYLES,
    length=st.sampled_from([0, 100, 251, 252, 253, 280]),
    max_below=st.sampled_from([0.0, 1.0, 10.0, 25.0, 60.0]),
)
@SLOW
def test_pct_from_52w_high_contract(rng: Lcg, style: str, length: int, max_below: float) -> None:
    rows = make_rows(rng, length, style)
    ctx = context(rows)
    params = {"max_pct_below": max_below}
    result = contract("pct_from_52w_high", params, ctx, rng)
    if length < 252:
        assert not result.passed
        assert result.reason == "no_data"
        return
    high = rupees(max(r.high for r in rows[-252:]))
    assert result.passed == ((1 - rupees(rows[-1].close) / high) * 100 <= Fraction(str(max_below)))
    assert fails_closed("pct_from_52w_high", params, ctx)


# --- HAT-CRIT-109 rs_rank --------------------------------------------------------------------

PCT = st.sampled_from([0.0, 10.0, 69.99, 70.0, 70.01, 95.0, 99.5, 100.0])


@given(
    rng=RNG,
    rs=st.one_of(st.none(), st.floats(0, 100, allow_nan=False)),
    others=st.lists(st.floats(0, 100), max_size=4),
    min_percentile=PCT,
    score_from=st.sampled_from([0.0, 50.0, 70.0]),
    score_full=st.sampled_from([80.0, 95.0, 100.0]),
    present=st.booleans(),
)
def test_rs_rank_contract(rng, rs, others, min_percentile, score_from, score_full, present) -> None:  # noqa: ANN001
    rows = [(f"o{i}", {"rs_pct": v}) for i, v in enumerate(others)]
    if present:
        rows.append(("x", {"rs_pct": rs}))
    ctx = context(make_rows(rng, 3), universe=universe_frame(rows) if rows else None)
    params = {"min_percentile": min_percentile, "score_from": score_from, "score_full": score_full}
    result = contract("rs_rank", params, ctx, rng)
    if not present or rs is None:
        assert not result.passed
        assert result.reason == "no_data"
        return
    assert result.passed == (rs >= min_percentile)
    on_the_edge = context(
        make_rows(rng, 3), universe=universe_frame([("x", {"rs_pct": min_percentile})])
    )
    assert evaluate("rs_rank", params, on_the_edge).passed  # exactly at the bar passes
    if rs <= score_from:
        assert result.score == 0.0
    elif rs >= score_full:
        assert result.score == 1.0
    else:
        assert result.score == pytest.approx((rs - score_from) / (score_full - score_from))
    # monotone: a higher rs_pct never scores lower
    higher = min(100.0, rs + 1.0)
    more = context(
        make_rows(rng, 3), universe=universe_frame([*rows[:-1], ("x", {"rs_pct": higher})])
    )
    assert evaluate("rs_rank", params, more).score >= result.score
    assert evaluate("rs_rank", params, more).passed >= result.passed


@given(rng=RNG, count=st.integers(2, 5), shuffle=RNG)
@settings(max_examples=25, deadline=None)
def test_rs_pct_is_a_universe_percentile(rng: Lcg, count: int, shuffle: Lcg) -> None:
    """`rs_pct` is in [0, 100] and does not depend on the order of the instruments."""
    world = []
    for k in range(count):
        for r in make_rows(rng, 260, "walk", base=5000 + 1000 * k):
            world.append((f"i{k}", r))
    bars = pl.concat(
        [
            _feature_bars([r for iid, r in world if iid == f"i{k}"]).with_columns(
                pl.lit(f"i{k}").alias("instrument_id")
            )
            for k in range(count)
        ]
    )
    base, _ = compute_features(bars)
    order = sorted(range(count), key=lambda _: shuffle.randrange(1000))
    permuted = pl.concat([bars.filter(pl.col("instrument_id") == f"i{k}") for k in order])
    again, _ = compute_features(permuted)
    key = ["date", "instrument_id"]
    assert base.sort(key).select("rs_pct").equals(again.sort(key).select("rs_pct"))
    values = base["rs_pct"].drop_nulls()
    assert all(0.0 <= v <= 100.0 for v in values)


# --- HAT-CRIT-110 momentum_rank --------------------------------------------------------------


@given(
    values=st.lists(
        st.lists(
            st.one_of(st.none(), st.sampled_from([-0.1, 0.0, 0.1, 0.2, 0.2, 0.5, 1.0])),
            min_size=3,
            max_size=3,
        ),
        min_size=1,
        max_size=12,
    ),
    top_pct=st.sampled_from([2.0, 10.0, 25.0, 50.0, 100.0]),
    windows=st.lists(st.sampled_from([21, 63, 126]), min_size=1, max_size=3, unique=True),
    match=st.sampled_from(["any", "all"]),
    rng=RNG,
)
def test_momentum_rank_contract(
    values: list, top_pct: float, windows: list, match: str, rng: Lcg
) -> None:
    cols = {21: 0, 63: 1, 126: 2}
    rows = [(f"i{k}", {f"ret{w}": v[cols[w]] for w in (21, 63, 126)}) for k, v in enumerate(values)]
    universe = universe_frame(rows)

    def passes(iid: str, ws: list[int], how: str) -> bool | None:
        params = {"windows": ws, "top_pct": top_pct, "match": how}
        ctx = EvalContext(date(2024, 1, 2), iid, frame(make_rows(rng, 2)), universe=universe)
        result = evaluate("momentum_rank", params, ctx)
        assert 0.0 <= result.score <= 1.0
        return None if result.reason == "no_data" else result.passed

    def in_top(iid: str, w: int) -> bool:
        column = [v[cols[w]] for v in values if v[cols[w]] is not None]
        mine = values[int(iid[1:])][cols[w]]
        if mine is None:
            return False
        seats = math.ceil(Fraction(str(top_pct)) * len(column) / 100)
        threshold = sorted(column, reverse=True)[seats - 1]  # ties at the edge all pass
        return mine >= threshold

    for k in range(len(values)):
        iid = f"i{k}"
        per_window = [in_top(iid, w) for w in windows]
        got = passes(iid, windows, match)
        if all(values[k][cols[w]] is None for w in windows):
            assert got is None
        else:
            assert got == (all(per_window) if match == "all" else any(per_window))
    for w in windows:  # at most ceil(top/100 x N) pass, plus the ties at the edge
        column = [v[cols[w]] for v in values if v[cols[w]] is not None]
        if column:
            seats = math.ceil(Fraction(str(top_pct)) * len(column) / 100)
            edge = sorted(column, reverse=True)[seats - 1]
            winners = sum(1 for k in range(len(values)) if passes(f"i{k}", [w], "any"))
            assert winners == sum(1 for c in column if c >= edge)
            assert winners <= seats + column.count(edge) - 1
    assert passes("absent", windows, match) is None


# --- HAT-CRIT-111 consolidation --------------------------------------------------------------


@given(
    rng=RNG,
    style=STYLES,
    length=st.integers(0, 24),
    days=st.sampled_from([[5, 15], [5, 5], [3, 8]]),
    max_range=st.sampled_from([1.0, 5.0, 15.0, 40.0]),
    higher=st.booleans(),
)
@SLOW
def test_consolidation_contract(rng, style, length, days, max_range, higher) -> None:  # noqa: ANN001
    rows = make_rows(rng, length, style)
    ctx = context(rows)
    params = {"days": days, "max_range_pct": max_range, "higher_lows": higher}
    result = contract("consolidation", params, ctx, rng)
    if length < days[0]:
        assert not result.passed
        assert result.reason == "no_data"
        return
    best = None
    for size in range(days[0], min(days[1], length) + 1):
        window = rows[length - size :]
        lows = [r.low for r in window]
        if higher and min(lows[size // 2 :]) < min(lows[: size // 2]):
            continue
        rng_pct = (rupees(max(r.high for r in window)) / rupees(min(lows)) - 1) * 100
        if rng_pct <= Fraction(str(max_range)) and (best is None or (rng_pct, -size) < best[:2]):
            best = (rng_pct, -size, rupees(max(r.high for r in window)))
    assert result.passed == (best is not None)
    if best is not None:
        assert result.extras["consolidation_high"] == float(best[2])
        assert result.value == pytest.approx(float(best[0]))
        assert result.score == pytest.approx(max(0.0, 1 - float(best[0]) / max_range))
    assert fails_closed("consolidation", params, ctx)


# --- HAT-CRIT-112 ma_surf --------------------------------------------------------------------


@given(
    rng=RNG,
    style=STYLES,
    length=st.sampled_from([0, 15, 19, 20, 40]),
    mas=st.sampled_from([[10, 20], [10], [20]]),
    max_dist=st.sampled_from([0.5, 1.0, 5.0, 20.0]),
)
@SLOW
def test_ma_surf_contract(rng, style, length, mas, max_dist) -> None:  # noqa: ANN001
    rows = make_rows(rng, length, style)
    ctx = context(rows)
    params = {"mas": mas, "max_distance_pct": max_dist}
    result = contract("ma_surf", params, ctx, rng)
    averages = [sma_of(rows, m) for m in mas]
    if not rows or any(a is None for a in averages):
        assert not result.passed
        assert result.reason == "no_data"
        return
    worst = max(abs(rupees(rows[-1].close) / a - 1) * 100 for a in averages if a is not None)  # type: ignore[operator]
    assert result.passed == (worst <= Fraction(str(max_dist)))
    if result.passed:
        assert result.score == pytest.approx(max(0.0, 1 - float(worst) / max_dist))
    assert fails_closed("ma_surf", params, ctx)


# --- HAT-CRIT-113 prior_move -----------------------------------------------------------------


@given(
    rng=RNG,
    style=STYLES,
    length=st.sampled_from([0, 10, 21, 30, 70]),
    min_pct=st.sampled_from([5.0, 30.0, 100.0]),
    within=st.sampled_from([[21, 63], [5, 10], [2, 3]]),
)
@SLOW
def test_prior_move_contract(rng, style, length, min_pct, within) -> None:  # noqa: ANN001
    rows = make_rows(rng, length, style)
    ctx = context(rows)
    params = {"min_pct": min_pct, "within_days": within}
    result = contract("prior_move", params, ctx, rng)
    if length < within[0]:
        assert not result.passed
        assert result.reason == "no_data"
        return
    window = rows[length - min(within[1], length) :]
    hit = any(
        rupees(window[j].high) / rupees(window[i].low) - 1 >= Fraction(str(min_pct)) / 100
        for i in range(len(window))
        for j in range(i + 1, len(window))
    )
    assert result.passed == hit
    higher = evaluate("prior_move", {**params, "min_pct": min_pct * 2}, ctx)
    assert not higher.passed or result.passed  # raising min_pct never turns a fail into a pass
    assert fails_closed("prior_move", params, ctx)


# --- HAT-CRIT-114 earnings_growth ------------------------------------------------------------


@given(
    rng=RNG,
    filed=st.lists(
        st.tuples(st.integers(-40, 40), st.integers(-50, 80), st.integers(-50, 80)), max_size=4
    ),
    accelerating=st.booleans(),
)
def test_earnings_growth_contract(rng: Lcg, filed: list, accelerating: bool) -> None:
    rows = make_rows(rng, 3)
    as_of = rows[-1].day
    params = {"eps_yoy": 20.0, "sales_yoy": 20.0, "accelerating": accelerating}
    none = context(rows)
    assert evaluate("earnings_growth", params, none).reason == "no_data"  # no data source yet
    data = pl.DataFrame(
        {
            "filed_at": [as_of + timedelta(days=d) for d, _, _ in filed],
            "eps_yoy": [float(e) for _, e, _ in filed],
            "sales_yoy": [float(s) for _, _, s in filed],
        },
        schema={"filed_at": pl.Date, "eps_yoy": pl.Float64, "sales_yoy": pl.Float64},
    )
    result = contract("earnings_growth", params, context(rows, fundamentals=data), rng)
    known = [
        (d, e, s)
        for _, (d, e, s) in sorted(enumerate(filed), key=lambda t: (t[1][0], t[0]))
        if d <= 0
    ]
    if not known:
        assert not result.passed
        assert result.reason == "no_data"  # nothing filed by the date
        return
    _, eps, sales = known[-1]
    ok = eps >= 20 and sales >= 20
    if ok and accelerating:
        if len(known) < 2:
            assert result.reason == "no_data"
            return
        ok = eps > known[-2][1]
    assert result.passed == ok


# --- HAT-CRIT-120 neglect --------------------------------------------------------------------


@given(
    rng=RNG,
    style=STYLES,
    length=st.sampled_from([0, 40, 64, 65, 80]),
    max_ret=st.sampled_from([0.0, 10.0, 30.0, 100.0]),
)
@SLOW
def test_neglect_contract(rng: Lcg, style: str, length: int, max_ret: float) -> None:
    rows = make_rows(rng, length, style)
    ctx = context(rows)
    params = {"max_return_63d_pct": max_ret}
    result = contract("neglect", params, ctx, rng)
    previous = rows[:-1]  # bars up to the previous session
    if len(previous) < 64:
        assert not result.passed
        assert result.reason == "no_data"
        return
    window = previous[-64:]
    ret = (rupees(window[-1].close) / rupees(window[0].close) - 1) * 100
    assert result.passed == (ret <= Fraction(str(max_ret)))
    changed = [*rows[:-1], dataclasses.replace(rows[-1], close=rows[-1].close + 1000)]
    assert evaluate("neglect", params, context(changed)) == result  # today's bar is not read


# --- HAT-CRIT-040 vcp ------------------------------------------------------------------------

ORACLE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "vcp_oracle.json").read_text("utf-8")
)


def vcp_rows(rng: Lcg, depths: list[int], quiet: int) -> list[Row]:
    """A rising lead-in, a peak, then one down-and-up run per depth (percent, each recovery stops
    short of the last high), with the final run's volume at `quiet` percent of the others."""
    closes = [6000 + (3900 * i) // 69 for i in range(70)] + [10000]
    runs = []
    high = 10000
    for depth in depths:
        low = high * (100 - depth) // 100
        start = len(closes)
        down, up = rng.randrange(5, 9), rng.randrange(4, 7)
        closes += [high - (high - low) * s // down for s in range(1, down + 1)]
        recovered = low + (high - low) * rng.randrange(40, 70) // 100
        closes += [low + (recovered - low) * s // up for s in range(1, up + 1)]
        runs.append((start, len(closes)))
        high = recovered
    closes += [high + 100 * s for s in range(1, 5)]
    rows = []
    for i, close in enumerate(closes):
        volume = 1_000_000 + rng.randrange(0, 50_000)
        if runs and runs[-1][0] <= i < runs[-1][1]:
            volume = volume * quiet // 100
        rows.append(
            Row(
                EPOCH + timedelta(days=i),
                close + rng.randrange(0, 20),
                close - rng.randrange(0, 20),
                close,
                volume,
            )
        )
    return rows


VCP_PARAMS = st.fixed_dictionaries(
    {
        "contractions": st.sampled_from([[2, 6], [2, 6], [2, 4], [3, 3], [2, 2]]),
        "depth_ratio_max": st.sampled_from([0.5, 0.75, 0.9, 1.0, 1.0]),
        "final_depth_max_pct": st.sampled_from([5.0, 10.0, 20.0, 20.0]),
        "lookback_days": st.sampled_from([60, 90, 120, 120]),
        "dryup_vs_avg50": st.sampled_from([0.5, 0.7, 1.0, 1.0]),
    }
)


def segment_stats(rows: list[Row], start: str, end: str) -> tuple[int, int, Fraction, Fraction]:
    first = next(i for i, r in enumerate(rows) if r.day.isoformat() == start)
    last = next(i for i, r in enumerate(rows) if r.day.isoformat() == end)
    window = rows[first : last + 1]
    return first, last, rupees(max(r.high for r in window)), rupees(min(r.low for r in window))


def shrinking_depths(first: int, ratio: int, count: int) -> list[int]:
    """`count` depths (percent) from `first`, each `ratio` percent of the one before."""
    return [max(2, first * ratio**k // 100**k) for k in range(count)]


@given(
    rng=RNG,
    depths=st.builds(shrinking_depths, st.integers(8, 30), st.integers(40, 85), st.integers(2, 6)),
    quiet=st.sampled_from([20, 40, 70, 100]),
    params=VCP_PARAMS,
)
@SLOW
def test_vcp_contract(rng: Lcg, depths: list[int], quiet: int, params: dict) -> None:
    rows = vcp_rows(rng, depths, quiet)
    ctx = context(rows)
    result = contract("vcp", params, ctx, rng)
    assert fails_closed("vcp", params, ctx)
    if not result.passed:
        assert result.score == 0.0
        return
    lo, hi = params["contractions"]
    parts = result.extras["contractions"]
    assert lo <= len(parts) == result.extras["contraction_count"] <= hi
    stats = [segment_stats(rows, p["start"], p["end"]) for p in parts]
    depth = [(h - low) / h * 100 for _, _, h, low in stats]
    assert all(
        b <= Fraction(str(params["depth_ratio_max"])) * a
        for a, b in zip(depth, depth[1:], strict=False)
    )
    assert depth[-1] <= Fraction(str(params["final_depth_max_pct"]))
    first, last, top, bottom = stats[-1]
    final_volume = mean([Fraction(r.volume) for r in rows[first : last + 1]])
    average = mean([Fraction(r.volume) for r in rows[-50:]])
    assert final_volume <= Fraction(str(params["dryup_vs_avg50"])) * average
    assert stats[0][0] >= len(rows) - params["lookback_days"]  # inside the lookback
    assert result.extras["pivot"] == float(top)
    assert result.extras["final_contraction_low"] == float(bottom)
    assert bottom < top  # the stop rule always has a level below entry


@given(
    final_depth=st.floats(0, 10),
    wider=st.floats(0, 5),
    dryup=st.floats(0, 0.7),
    drier=st.floats(0, 0.5),
    ratio=st.floats(0, 0.75),
    count=st.integers(2, 6),
)
def test_vcp_score_monotone(final_depth, wider, dryup, drier, ratio, count) -> None:  # noqa: ANN001
    """The score never rises as the final depth or the dry-up ratio rises, all else equal."""
    params = vcp_module.VcpParams()
    base = vcp_module.vcp_score(count, ratio, final_depth, dryup, params)
    assert 0.0 <= base <= 1.0
    assert vcp_module.vcp_score(count, ratio, final_depth + wider, dryup, params) <= base
    assert vcp_module.vcp_score(count, ratio, final_depth, dryup + drier, params) <= base


def legacy_chain(
    high: list[float], low: list[float], close: list[float]
) -> list[tuple[int, int, float]]:
    """The legacy detector's structure search (`services/vcp/pattern.py` at 56b9a08: `base_window`
    from the first highest close of the last 60 bars, `pivot_points`, `find_contractions` capped at
    four), transcribed with plain lists. Returns (high_idx, low_idx, pivot-low price) per pullback
    and the base start."""
    window = len(close) - 60 if len(close) > 60 else 0
    top = max(close[window:])
    start = window + close[window:].index(top)
    h, lo = high[start:], low[start:]
    if len(h) < 15:
        return []
    pivot_highs = [
        i
        for i in range(3, len(h) - 3)
        if all(h[i] > h[j] for j in (*range(i - 3, i), *range(i + 1, i + 4)))
    ]
    pivot_lows = [
        i
        for i in range(3, len(lo) - 3)
        if all(lo[i] < lo[j] for j in (*range(i - 3, i), *range(i + 1, i + 4)))
    ]
    out: list[tuple[int, int, float]] = []
    current = 0
    while True:
        candidates = [i for i in pivot_lows if i > current]
        if not candidates:
            break
        out.append((current, candidates[0], lo[candidates[0]]))
        later = [i for i in pivot_highs if i > candidates[0]]
        if not later or len(out) >= 4:
            break
        current = later[0]
    return out


@given(
    rng=RNG,
    depths=st.lists(st.integers(3, 30), min_size=1, max_size=5),
    quiet=st.sampled_from([40, 100]),
    walk=st.booleans(),
    length=st.integers(15, 130),
)
@SLOW
def test_vcp_parity(rng: Lcg, depths: list[int], quiet: int, walk: bool, length: int) -> None:
    """Where the port and the legacy detector share a definition (the base start, the pivots and
    the pullback chain at 60 bars and at most four pullbacks) both find the same pullbacks, hence
    the same count; the final contraction low is the same unless a lower low sits inside the final
    pullback before its pivot low, where the spec's "minimum low of the final contraction" is
    lower (HAT-CRIT-040)."""
    rows = make_rows(rng, length) if walk else vcp_rows(rng, sorted(depths, reverse=True), quiet)
    high, low, close = (
        [float(rupees(getattr(r, k))) for r in rows] for k in ("high", "low", "close")
    )
    reference = legacy_chain(high, low, close)
    start = vcp_module.base_start(close, 60)
    assert start is not None
    chain = (
        vcp_module.contraction_chain(high[start:], low[start:], 4)
        if len(rows[start:]) >= 15
        else []
    )
    assert [(a, b) for a, b, _ in reference] == chain
    if chain:
        port = vcp_module.build_contractions(high[start:], low[start:], chain)
        legacy_low = reference[-1][2]
        assert float(port[-1].low) <= legacy_low
        segment = low[start:][chain[-1][0] : chain[-1][1] + 1]
        assert (float(port[-1].low) == legacy_low) == (min(segment) == legacy_low)


@pytest.mark.parametrize(
    "case", [c for c in ORACLE["cases"] if c["expected"]], ids=lambda c: c["name"]
)
def test_vcp_parity_frozen_oracle(case: dict) -> None:
    """On the frozen outputs of the legacy detector the port finds the same number of contractions
    and the same final contraction low (the legacy stage-2, RS and grade gates are other
    criteria's work, so the thresholds are opened wide here)."""
    bars = case["bars"]
    rows = [Row(EPOCH + timedelta(days=i), 0, 0, 0, 0) for i in range(len(bars["close"]))]
    data = pl.DataFrame(
        {
            "date": [r.day for r in rows],
            "high": bars["high"],
            "low": bars["low"],
            "close": bars["close_ref"],
            "volume": [float(v) for v in bars["volume"]],
        }
    )
    ctx = EvalContext(rows[-1].day, "x", data)
    wide = {
        "contractions": [2, 4],
        "depth_ratio_max": 1.0,
        "final_depth_max_pct": 100.0,
        "lookback_days": 60,
        "dryup_vs_avg50": 10.0,
    }
    result = evaluate("vcp", wide, ctx)
    expected = case["expected"]
    assert result.passed
    assert result.extras["contraction_count"] == len(expected["contractions"])
    assert result.extras["final_contraction_low"] == expected["final_contraction_low"]


# --- the exact edge of every inclusive threshold ----------------------------------------------


def flat_rows(
    closes: list[int], lows: list[int] | None = None, highs: list[int] | None = None
) -> list[Row]:
    """Bars on consecutive days with given paise values (default: high = low = close)."""
    return [
        Row(EPOCH + timedelta(days=i), (highs or closes)[i], (lows or closes)[i], c, 1000)
        for i, c in enumerate(closes)
    ]


def passes(code: str, key: str, value: float, rows: list[Row]) -> bool:
    return evaluate(code, {**BASE_PARAMS[code], key: value}, context(rows)).passed


BASE_PARAMS: dict[str, dict] = {
    "pct_from_52w_low": {},
    "pct_from_52w_high": {},
    "consolidation": {"days": [5, 5], "higher_lows": True},
    "ma_surf": {"mas": [2]},
    "prior_move": {"within_days": [2, 2]},
}


@given(k=st.integers(1, 40), pct=st.sampled_from([1, 5, 10, 25, 30, 100]))
@SLOW
def test_inclusive_edges_contract(k: int, pct: int) -> None:
    """A value exactly on its threshold passes and one hair beyond it fails (HAT-CRIT-107, 111,
    112, 113): the two sides of each rule are built so the percentage is exact in paise."""
    unit = 100 * k  # paise; `unit * (100 + pct) / 100` is a whole number of paise
    up = unit * (100 + pct) // 100
    # pct_from_52w_low: low 252 sessions back, last close `pct` above it
    rows = flat_rows([unit] * 251 + [up])
    assert passes("pct_from_52w_low", "min_pct", float(pct), rows)
    assert not passes("pct_from_52w_low", "min_pct", pct + 0.01, rows)
    # pct_from_52w_high: high at `up`, last close `pct` below it
    down = up * (100 - pct) // 100
    assume(down * 100 == up * (100 - pct))
    rows = flat_rows([up] + [down] * 251)
    assert passes("pct_from_52w_high", "max_pct_below", float(pct), rows)
    assert not passes("pct_from_52w_high", "max_pct_below", pct - 0.01, rows)
    # consolidation: five bars, low `unit`, high `up`
    rows = flat_rows([unit] * 5, highs=[up] * 5)
    assert passes("consolidation", "max_range_pct", float(pct), rows)
    assert not passes("consolidation", "max_range_pct", pct - 0.01, rows)
    # prior_move: the second bar's high is `pct` above the first bar's low
    rows = flat_rows([unit, unit], highs=[unit, up])
    assert passes("prior_move", "min_pct", float(pct), rows)
    assert not passes("prior_move", "min_pct", pct + 0.01, rows)
    # ma_surf: closes a, b with |b - a| / (a + b) = pct / 100 (percent distance of b from the mean)
    a, b = 100 * k * (100 - pct), 100 * k * (100 + pct)
    mean = Fraction(a + b, 2)
    distance = abs(Fraction(b) / mean - 1) * 100
    assert distance == pct
    rows = flat_rows([a, b])
    assert passes("ma_surf", "max_distance_pct", float(pct), rows)
    assert not passes("ma_surf", "max_distance_pct", pct - 0.01, rows)


# --- live trigger and entry-filter criteria (HAT-CRIT-115..119, 121, 122; 07 §12) -------------

PAISE = st.integers(1, 5_000_000)  # ₹0.01 .. ₹50,000.00
MAYBE_PAISE = st.one_of(st.none(), st.integers(-100, 0), PAISE)
SESSION_BARS = 75  # 09:15 .. 15:30 in five-minute bars
TRIGGER_TYPES = (
    "gap_up",
    "volume_pace",
    "early_volume",
    "catalyst_attached",
    "price_breaks_level",
    "neglect",
)


def maybe(value: int | None) -> Decimal | None:
    return None if value is None else paise(value)


def oracle_pct(value: float) -> Fraction:
    return Fraction(str(value))


def assert_no_data(result: CriterionResult) -> None:
    assert not result.passed
    assert result.score == 0.0


@example(open_=11_000, prev=10_000, min_pct=10.0)  # exactly a 10% gap passes
@example(open_=10_999, prev=10_000, min_pct=10.0)
@given(open_=MAYBE_PAISE, prev=MAYBE_PAISE, min_pct=st.sampled_from([0.5, 5.0, 10.0, 12.5, 20.0]))
def test_gap_up_contract(open_: int | None, prev: int | None, min_pct: float) -> None:
    """HAT-CRIT-115: passes iff (open / previous close - 1) x 100 >= min_pct, exactly; a missing
    or non-positive open or close fails closed. Harm: a 9.99% gap (or a float rounding of 10%)
    starts an EP trade the rule excludes, or a real 10% gap is missed."""
    ctx = LiveContext(at=AT, open_price=maybe(open_), prev_close=maybe(prev))
    result = live_contract("gap_up", {"min_pct": min_pct}, ctx)
    if open_ is None or prev is None or open_ <= 0 or prev <= 0:
        assert_no_data(result)
        assert result.reason == "no_data"
        return
    gap = 100 * (Fraction(open_, prev) - 1)
    assert result.passed == (gap >= oracle_pct(min_pct))
    assert result.reason in ("ok", "gap_too_small")


@example(volume=1_500, bars=SESSION_BARS, avg=1_000, mult=1.5)  # the tie passes
@example(volume=1_499, bars=SESSION_BARS, avg=1_000, mult=1.5)
@given(
    volume=st.one_of(st.none(), st.integers(-5, 10_000_000)),
    bars=st.one_of(st.none(), st.integers(0, SESSION_BARS + 1)),
    avg=st.one_of(st.none(), st.integers(-5, 2_000_000)),
    mult=st.sampled_from([0.5, 1.0, 1.5, 2.0, 3.0]),
)
def test_volume_pace_contract(
    volume: int | None, bars: int | None, avg: int | None, mult: float
) -> None:
    """HAT-CRIT-116: with `bars` five-minute bars final, the day's volume projected to the full
    session, V x 75 / bars, is at least vs_avg50 x the 50-day average volume. No elapsed share
    (before the first bar is final, or after the close), no volume or no average fails closed.
    Harm: a thin early burst, or a pace measured against the wrong share of the day, fires a
    breakout the volume rule rejects."""
    elapsed = None if bars is None else Fraction(bars, SESSION_BARS)
    ctx = LiveContext(
        at=AT,
        cum_volume=volume,
        elapsed=elapsed,
        avg50_volume=None if avg is None else Decimal(avg),
    )
    result = live_contract("volume_pace", {"vs_avg50": mult}, ctx)
    if volume is None or volume < 0 or avg is None or avg <= 0 or not bars or bars > SESSION_BARS:
        assert_no_data(result)
        return
    projected = Fraction(volume * SESSION_BARS, bars)
    assert result.passed == (projected >= oracle_pct(mult) * avg)


@example(volume=500, adv=1_000, mult=0.5)  # the tie passes
@example(volume=499, adv=1_000, mult=0.5)
@given(
    volume=st.one_of(st.none(), st.integers(-5, 10_000_000)),
    adv=st.one_of(st.none(), st.integers(-5, 5_000_000)),
    mult=st.sampled_from([0.1, 0.25, 0.5, 1.0, 2.0]),
)
def test_early_volume_contract(volume: int | None, adv: int | None, mult: float) -> None:
    """HAT-CRIT-117: the cumulative day volume at the trigger is at least vs_adv20 x ADV(20),
    exactly; no volume or no ADV fails closed. Harm: an EP gap on thin volume is bought."""
    ctx = LiveContext(at=AT, cum_volume=volume, adv20=None if adv is None else Decimal(adv))
    result = live_contract("early_volume", {"vs_adv20": mult}, ctx)
    if volume is None or volume < 0 or adv is None or adv <= 0:
        assert_no_data(result)
        return
    assert result.passed == (volume >= oracle_pct(mult) * adv)


@example(offsets=[24 * 3600], hours=24, required=["confirm"])  # exactly at the window's start
@example(offsets=[24 * 3600 + 1], hours=24, required=["confirm"])
@example(offsets=[0], hours=1, required=[])  # published at the trigger instant
@example(offsets=[-1], hours=1, required=["shadow"])  # one second in the future
@given(
    offsets=st.lists(st.integers(-6 * 3600, 200 * 3600), max_size=6),
    hours=st.integers(1, 168),
    required=st.lists(st.sampled_from(MODES), unique=True, max_size=3),
)
def test_catalyst_attached_contract(offsets: list[int], hours: int, required: list[str]) -> None:
    """HAT-CRIT-118: passes iff an announcement matched to the instrument was published in
    [at - within_hours, at] (both ends included, nothing after `at`); `required_in` does not
    change the result, only whether it blocks (`trigger_conjunction`). Harm: a stale or
    future-dated announcement is taken as the catalyst of a confirm-mode EP trade."""
    published = tuple(AT - timedelta(seconds=s) for s in offsets)
    ctx = LiveContext(at=AT, announcements=published)
    result = live_contract(
        "catalyst_attached", {"within_hours": hours, "required_in": required}, ctx
    )
    inside = [s for s in offsets if 0 <= s <= hours * 3600]
    assert result.passed == bool(inside)
    assert result.reason == ("ok" if inside else "no_catalyst")
    other = live_contract("catalyst_attached", {"within_hours": hours, "required_in": []}, ctx)
    assert other == result


LEVEL_EXPRESSIONS = ("pivot", "opening_range_high", "max(consolidation_high, opening_range_high)")
STEPS = st.lists(
    st.tuples(st.integers(0, 6 * 3600 + 14 * 60), st.integers(9_000, 11_000), st.integers(0, 50)),
    max_size=40,
)


def oracle_level(expr: str, pivot: int, consolidation: int, first_high: int | None) -> int | None:
    if expr == "pivot":
        return pivot
    if first_high is None:
        return None
    return first_high if expr == "opening_range_high" else max(consolidation, first_high)


@example(
    steps=[(0, 10_000, 1), (300, 10_001, 1)],
    expr="pivot",
    pivot=10_000,
    consolidation=10_000,
)  # the first bar touches the pivot without trading above it; the second breaks it
@given(
    steps=STEPS,
    expr=st.sampled_from(LEVEL_EXPRESSIONS),
    pivot=st.integers(9_500, 10_500),
    consolidation=st.integers(9_500, 10_500),
)
@settings(max_examples=150, deadline=None)
def test_price_breaks_level_contract(
    steps: list[tuple[int, int, int]], expr: str, pivot: int, consolidation: int
) -> None:
    """HAT-CRIT-119, HAT-BOT-071, HAT-EXEC-120: driven through the real bar builder and trigger
    monitor, a `price_breaks_level` playbook fires once, on the first final bar whose high is
    strictly above the level and whose final instant is in [09:20, 15:20]; the trigger's time is
    that instant, its price the level, its live price the bar's close. The opening range level
    does not exist before the first bar is final, and a touch is not a break. Harm: a breakout
    is bought on a touch, before the opening range exists, or after 15:20."""
    book = playbook(trigger=[{"type": "price_breaks_level", "params": {"level": expr}}])
    cand = candidate(book, pivot=paise(pivot), consolidation_high=paise(consolidation))
    builder = BarBuilder()
    monitor = TriggerMonitor(builder)
    monitor.add(cand)
    bars, fired = drive(monitor, builder, session_ticks("AAA", steps), ist(DAY, 16, 0))
    highs: dict[int, int] = {}
    closes: dict[int, tuple[int, int]] = {}
    for n, (second, price, _qty) in enumerate(steps):
        bucket = second // 300
        highs[bucket] = max(highs.get(bucket, 0), price)
        closes[bucket] = max(closes.get(bucket, (-1, 0)), (second * 1000 + n, price))
    level = oracle_level(expr, pivot, consolidation, highs.get(0))
    window_end = ist(DAY, 15, 20)
    expected = next(
        (
            b
            for b in sorted(highs)
            if level is not None
            and highs[b] > level
            and ist(DAY, 9, 15) + timedelta(seconds=300 * b + 302) <= window_end
        ),
        None,
    )
    assert [bar.high for bar in bars] == [paise(highs[b]) for b in sorted(highs)]
    if expected is None:
        assert fired == []
        return
    (trigger,) = fired
    assert trigger.at == ist(DAY, 9, 15) + timedelta(seconds=300 * expected + 302)
    assert trigger.evaluation.level == paise(level)
    assert trigger.plan.level == paise(level)
    assert trigger.context.price == paise(closes[expected][1])


@example(entry=10_000, stop=9_000, adtr=1_000, mult=1.0)  # width exactly one ADTR passes
@example(entry=10_000, stop=8_999, adtr=1_000, mult=1.0)
@example(entry=10_000, stop=10_000, adtr=1_000, mult=1.0)  # a stop at the entry fails closed
@given(
    entry=MAYBE_PAISE,
    stop=MAYBE_PAISE,
    adtr=MAYBE_PAISE,
    mult=st.sampled_from([0.5, 1.0, 1.5, 2.0]),
)
def test_adtr_filter_contract(
    entry: int | None, stop: int | None, adtr: int | None, mult: float
) -> None:
    """HAT-CRIT-121: passes iff entry - stop <= max_multiple x ADTR(20), exactly; a stop at or
    above the entry, or any missing input, fails closed. Harm: an entry whose stop is wider than
    the stock's daily range is taken, and the stop is hit by ordinary noise."""
    ctx = LiveContext(at=AT, entry=maybe(entry), stop=maybe(stop), adtr20=maybe(adtr))
    result = live_contract("adtr_filter", {"max_multiple": mult}, ctx)
    valid = all(v is not None and v > 0 for v in (entry, stop, adtr))
    if not valid or stop >= entry:  # type: ignore[operator]
        assert_no_data(result)
        return
    assert result.passed == (entry - stop <= oracle_pct(mult) * adtr)  # type: ignore[operator]


@st.composite
def chase_filters(draw: st.DrawFn) -> tuple[list[dict], str]:
    """Entry filters with max_chase enabled, disabled or deleted, and an adtr filter maybe."""
    state = draw(st.sampled_from(["enabled", "disabled", "deleted"]))
    items: list[dict] = []
    if draw(st.booleans()):
        items.append({"type": "adtr_filter", "params": {"max_multiple": 1.0}})
    if state != "deleted":
        pct = draw(st.sampled_from([1.0, 2.0, 3.5, 5.0]))
        items.insert(
            draw(st.integers(0, len(items))),
            {"type": "max_chase", "params": {"pct": pct}, "enabled": state == "enabled"},
        )
    # explicit orders anywhere in the allowed range, the highest one included
    orders = draw(
        st.lists(
            st.one_of(st.just(10_000), st.integers(0, 10_000)),
            min_size=len(items),
            max_size=len(items),
            unique=True,
        )
    )
    for item, order in zip(items, sorted(orders), strict=True):
        item["order"] = order
    return items, state


@example(price=10_200, level=10_000, pct=2.0, locked=False)  # exactly 2% above passes
@example(price=10_201, level=10_000, pct=2.0, locked=False)
@example(price=10_000, level=10_000, pct=2.0, locked=True)  # locked at the upper circuit
@example(price=10_000, level=10_000, pct=2.0, locked=None)  # circuit state unknown
@given(
    price=MAYBE_PAISE,
    level=MAYBE_PAISE,
    pct=st.sampled_from([0.5, 1.0, 2.0, 5.0, 12.5]),
    locked=st.sampled_from([True, False, None]),
)
def test_max_chase_contract(
    price: int | None, level: int | None, pct: float, locked: bool | None
) -> None:
    """HAT-CRIT-122: passes iff price <= level x (1 + pct/100), exactly, and the stock is not
    locked at its upper circuit; an unknown circuit state or a missing price or level fails
    closed. Harm: an entry far above the breakout, or one into a locked upper circuit that
    cannot be exited, is bought."""
    ctx = LiveContext(
        at=AT, price=maybe(price), trigger_level=maybe(level), locked_upper_circuit=locked
    )
    result = live_contract("max_chase", {"pct": pct}, ctx)
    if price is None or level is None or price <= 0 or level <= 0 or locked is None:
        assert_no_data(result)
        return
    assert result.passed == (not locked and price <= level * (1 + oracle_pct(pct) / 100))


DOCUMENTED_CHASE = {S1: Fraction(5), S2: Fraction(2)}  # HAT-CRIT-122 defaults; 2 otherwise


@example(
    filters=(
        [{"type": "adtr_filter", "params": {"max_multiple": 1.0}, "order": 10_000}],
        "deleted",
    ),
    configured=None,
    strategy=S1,
)  # a filter at the highest allowed order: the forced max_chase still comes after it
@given(
    filters=chase_filters(),
    configured=st.one_of(st.none(), st.sampled_from([Decimal("1.5"), Decimal(4)])),
    strategy=st.sampled_from([S1, S2, "some-other-bot"]),
)
def test_max_chase_always_applied(
    filters: tuple[list[dict], str], configured: Decimal | None, strategy: str
) -> None:
    """HAT-CRIT-122 (part of `max_chase_contract`): the entry filters evaluated at a trigger hold
    exactly one max_chase whatever the config says. An enabled one keeps its place and
    percentage; a disabled or deleted one is evaluated last with the playbook's last configured
    percentage, else the strategy's documented default (5 for S1, 2 for S2, else 2), even when
    a configured filter holds the highest allowed order. Harm: deleting max_chase in the editor
    lets a bot buy any distance above the breakout, or a filter at order 10000 makes every
    trigger of the playbook fail."""
    items, state = filters
    book = playbook(filters=items)
    instances = entry_filter_instances(book, configured, strategy)
    chases = [i for i in instances if i.type == "max_chase"]
    assert len(chases) == 1
    enabled = [i for i in book.entry_filters if i.enabled]
    if state == "enabled":
        assert instances == enabled
        return
    assert instances[:-1] == enabled
    assert instances[-1].type == "max_chase"
    assert all(i.order < instances[-1].order for i in book.entry_filters)
    want = Fraction(configured) if configured is not None else DOCUMENTED_CHASE.get(strategy, 2)
    assert oracle_pct(chases[0].params["pct"]) == want


@st.composite
def trigger_sets(draw: st.DrawFn) -> list[dict]:
    types = draw(st.lists(st.sampled_from(TRIGGER_TYPES), unique=True, max_size=5))
    items: list[dict] = []
    for code in types:
        params: dict = {}
        if code == "catalyst_attached":
            params = {"required_in": draw(st.lists(st.sampled_from(MODES), unique=True))}
        elif code == "price_breaks_level":
            params = {"level": draw(st.sampled_from(LEVEL_EXPRESSIONS))}
        items.append({"type": code, "params": params, "enabled": draw(st.booleans())})
    return items


@st.composite
def live_contexts(draw: st.DrawFn) -> LiveContext:
    def price() -> Decimal | None:
        return draw(st.one_of(st.none(), st.integers(9_000, 12_000).map(paise)))

    return LiveContext(
        at=AT,
        bar_high=price(),
        levels={"pivot": price(), "consolidation_high": price(), "opening_range_high": price()},
        cum_volume=draw(st.one_of(st.none(), st.integers(0, 3_000_000))),
        elapsed=Fraction(draw(st.integers(1, SESSION_BARS)), SESSION_BARS),
        avg50_volume=Decimal(draw(st.integers(1, 2_000_000))),
        adv20=Decimal(draw(st.integers(1, 2_000_000))),
        open_price=price(),
        prev_close=price(),
        announcements=tuple(
            AT - timedelta(hours=h) for h in draw(st.lists(st.integers(-2, 48), max_size=2))
        ),
    )


@example(
    items=[{"type": "neglect", "params": {}, "enabled": True}],
    ctx=LiveContext(at=AT),
    mode="shadow",
    daily=True,
)  # a playbook triggered by its daily-bar criterion alone
@given(
    items=trigger_sets(),
    ctx=live_contexts(),
    mode=st.sampled_from(MODES),
    daily=st.one_of(st.none(), st.booleans()),
)
def test_trigger_conjunction(
    items: list[dict], ctx: LiveContext, mode: str, daily: bool | None
) -> None:
    """HAT-EXEC-121, HAT-CRIT-118: a playbook triggers iff it has at least one blocking enabled
    trigger criterion and every blocking one passes on the same bar; a disabled criterion is not
    evaluated, and `catalyst_attached` blocks only in a mode listed in its `required_in` (its
    result is recorded either way). A daily-bar trigger (`neglect`) counts with the result the
    candidate was built with (`daily`) and fails closed without one. Harm: one passing
    criterion fires an entry the others reject, a playbook with every criterion disabled buys
    every bar, or a playbook holding `neglect` never fires."""
    book = playbook(trigger=items)
    results = {
        i.order: CriterionResult(daily, 1.0 if daily else 0.0, None, "ok" if daily else "x")
        for i in book.trigger
        if i.type == "neglect" and daily is not None
    }
    evaluation = evaluate_trigger(book, ctx, mode, results)
    enabled = [i for i in items if i["enabled"]]
    assert [o.type for o in evaluation.outcomes] == [i["type"] for i in enabled]

    def passed(n: int, item: dict) -> bool:
        if item["type"] == "neglect":
            return bool(daily)
        return evaluate_live(item["type"], book.trigger[n].params, ctx).passed

    blocking = [
        passed(n, i)
        for n, i in enumerate(items)
        if i["enabled"] and (i["type"] != "catalyst_attached" or mode in i["params"]["required_in"])
    ]
    assert evaluation.fired == (bool(blocking) and all(blocking))


@example(
    levels={"pivot": 0, "consolidation_high": 10_000, "opening_range_high": 10_100},
    expr="max(consolidation_high, opening_range_high)",
)
@given(
    levels=st.fixed_dictionaries(
        {
            "pivot": st.one_of(st.none(), st.integers(-10, 20_000)),
            "consolidation_high": st.one_of(st.none(), st.integers(-10, 20_000)),
            "opening_range_high": st.one_of(st.none(), st.integers(-10, 20_000)),
        }
    ),
    expr=st.sampled_from([*LEVEL_EXPRESSIONS, "close", "max(pivot, opening_range_high)"]),
)
def test_level_expression(levels: dict[str, int | None], expr: str) -> None:
    """HAT-BOT-071: `pivot` and `opening_range_high` name one level each; `max(consolidation_high,
    opening_range_high)` is the larger of the two and exists only when both do. A missing or
    non-positive level, or any other expression, has no level (the criterion fails closed with
    `level_undefined`), and the config refuses an unknown expression. Harm: a breakout level of
    zero, or the consolidation high alone before the opening range exists, fires at the open."""
    prices = {k: maybe(v) for k, v in levels.items()}
    positive = {k: v for k, v in levels.items() if v is not None and v > 0}
    if expr == "pivot":
        want = positive.get("pivot")
    elif expr == "opening_range_high":
        want = positive.get("opening_range_high")
    elif expr == LEVEL_EXPRESSIONS[2]:
        both = [positive.get("consolidation_high"), positive.get("opening_range_high")]
        want = max(both) if None not in both else None  # type: ignore[type-var]
    else:
        want = None
        with pytest.raises(ValueError):
            playbook(trigger=[{"type": "price_breaks_level", "params": {"level": expr}}])
    assert resolve_level(expr, prices) == maybe(want)
    if expr in LEVEL_EXPRESSIONS:
        ctx = LiveContext(at=AT, bar_high=paise(30_000), levels=prices)
        result = live_contract("price_breaks_level", {"level": expr}, ctx)
        assert result.passed == (want is not None)
        assert result.reason == ("ok" if want is not None else "level_undefined")


EP_BANDS_SEEN = st.sampled_from([None, Decimal(2), Decimal(5), Decimal(10), Decimal(20)])


@st.composite
def gappers(draw: st.DrawFn) -> list[Gapper]:
    out = []
    for n in range(draw(st.integers(0, 8))):
        prev = draw(st.one_of(st.none(), st.integers(1_000, 100_000)))
        opened = draw(st.one_of(st.none(), st.integers(1_000, 140_000)))
        out.append(
            Gapper(
                row=GapRow(f"S{n}", maybe(opened), maybe(prev), None),
                passes_universe=draw(st.booleans()),
                band_known=draw(st.booleans()),
                band_pct=draw(EP_BANDS_SEEN),
            )
        )
    return out


@example(
    items=[Gapper(GapRow("S0", paise(11_000), paise(10_000), None), True, True, Decimal(20))],
    passing={"S0"},
)  # exactly a 10% gap on a 20% band
@given(items=gappers(), passing=st.sets(st.sampled_from([f"S{n}" for n in range(8)])))
def test_ep_candidate_bands(items: list[Gapper], passing: set[str]) -> None:
    """HAT-BOT-052: the EP candidates of a gap scan are, in scan order, exactly the gappers with
    an open at least 10% above the previous close, in the universe, with a known 10% or 20% band
    or no band (an F&O name), whose EP setup passes. Harm: a 2% or 5% band stock, which can lock
    at its circuit right after the entry, or one whose band is unknown, becomes an EP trade."""
    picked = ep_candidates(items, lambda symbol: symbol in passing)
    want = [
        g.row.symbol
        for g in items
        if g.row.open is not None
        and g.row.prev_close is not None
        and Fraction(g.row.open) >= Fraction(g.row.prev_close) * Fraction(11, 10)
        and g.passes_universe
        and g.band_known
        and g.band_pct in (None, Decimal(10), Decimal(20))
        and g.row.symbol in passing
    ]
    assert picked == want


@given(
    catalyst=st.booleans(),
    required=st.one_of(st.none(), st.lists(st.sampled_from(MODES), unique=True)),
)
def test_catalyst_required_in_confirm(catalyst: bool, required: list[str] | None) -> None:
    """HAT-CRIT-118, HAT-EXEC-034: on the seeded episodic-pivot playbook, with every other
    trigger criterion passing, a missing catalyst blocks the trigger exactly in the modes its
    `required_in` lists (the seed: confirm only) and is recorded, not blocking, in the others.
    Harm: a confirm-mode EP trade reaches the owner with no news behind the gap, or shadow EP
    trades stop because shadow has no catalyst feed."""
    book = seed_playbook(S2, "episodic_pivot")
    if required is not None:
        trigger = [
            i.model_copy(update={"params": {**i.params, "required_in": required}})
            if i.type == "catalyst_attached"
            else i
            for i in book.trigger
        ]
        book = book.model_copy(update={"trigger": trigger})
    blocks_in = required if required is not None else ["confirm"]
    ctx = LiveContext(
        at=AT,
        bar_high=paise(11_500),
        levels={"opening_range_high": paise(11_400)},
        cum_volume=1_000_000,
        adv20=Decimal(1_000_000),
        open_price=paise(11_000),
        prev_close=paise(10_000),
        announcements=(AT - timedelta(hours=2),) if catalyst else (),
    )
    for mode in MODES:
        evaluation = evaluate_trigger(book, ctx, mode)
        assert evaluation.fired == (catalyst or mode not in blocks_in)
        (outcome,) = [o for o in evaluation.outcomes if o.type == "catalyst_attached"]
        assert outcome.result.passed == catalyst
        assert outcome.blocking == (mode in blocks_in)


@st.composite
def entry_filter_sets(draw: st.DrawFn) -> list[dict]:
    items: list[dict] = []
    if draw(st.booleans()):
        mult = draw(st.sampled_from([0.5, 1.0, 1.5]))
        items.append({"type": "adtr_filter", "params": {"max_multiple": mult}})
    if draw(st.booleans()):
        pct = draw(st.sampled_from([1.0, 2.0, 5.0]))
        chase = {"type": "max_chase", "params": {"pct": pct}, "enabled": draw(st.booleans())}
        items.insert(draw(st.integers(0, len(items))), chase)
    return items


def oracle_filter(
    item: dict, entry: Decimal, stop: Decimal | None, level: Decimal, adtr: int, locked: bool | None
) -> bool:
    params = item["params"]
    if item["type"] == "adtr_filter":
        if stop is None or stop >= entry:
            return False
        return Fraction(entry - stop) <= oracle_pct(params["max_multiple"]) * Fraction(adtr, 100)
    if locked is None or locked:
        return False
    return Fraction(entry) <= Fraction(level) * (1 + oracle_pct(params["pct"]) / 100)


@settings(max_examples=60, deadline=None)
@given(
    entry=st.integers(9_000, 11_000),
    level=st.integers(9_000, 11_000),
    stop_pct=st.sampled_from([0.5, 2.0, 5.0, 8.0]),
    adtr=st.integers(50, 1_500),
    items=entry_filter_sets(),
    band=st.sampled_from(["fno", "unknown", "20"]),
)
def test_entry_filters_skip(
    entry: int, level: int, stop_pct: float, adtr: int, items: list[dict], band: str
) -> None:
    """HAT-EXEC-122, HAT-CRIT-121, HAT-CRIT-122: at a trigger the stop is planned first, then the
    entry filters run in their configured order with max_chase always present (last, at the
    strategy default, when the config disabled or deleted it). The first failure is the skip:
    the signal is stored Rejected with that reason and stage `entry_filter`, no position opens
    and the risk check never runs; with no failure the signal goes to the risk check. Harm: a
    trade the filters reject is opened, or a skipped trade leaves no record of why."""
    book = playbook(filters=items, stop={"rule": "pct_below_entry", "pct": stop_pct})
    engine = memory_engine()
    with Session(engine) as session:
        world = seed_engine_world(session, instruments=1)
        daily = DailyInputs(
            prev_close=paise(10_000),
            adv20=Decimal(1_000_000),
            adtr20=paise(adtr),
            tick=Decimal("0.05"),
            band_known=band != "unknown",
            band_pct=Decimal(20) if band == "20" else None,
        )
        cand = candidate(book, daily=daily, bot_id=world.bot_id, instrument_id=world.instruments[0])
        ctx = LiveContext(at=AT, price=paise(entry))
        plan = plan_entry(cand, ctx, paise(level))
        locked = None if band == "unknown" else band == "20" and entry >= 12_000
        chase = [i for i in items if i["type"] == "max_chase" and i["enabled"]]
        order = [i for i in items if i.get("enabled", True)]
        if not chase:
            order.append({"type": "max_chase", "params": {"pct": 5.0}})  # S1's default
        failing = [
            i["type"]
            for i in order
            if not oracle_filter(i, paise(entry), plan.stop, paise(level), adtr, locked)
        ]
        want = plan.skip if plan.stop is None else (failing[0] if failing else None)
        assert plan.skip == want
        assert [o.type for o in plan.filters] == [i["type"] for i in order]
        decided: list[object] = []
        trigger = Trigger(cand, AT, TriggerEvaluation(True, (), paise(level)), plan, ctx)
        stored = replace(version_of(seed_policy()), version_id=world.policy_version_id)
        outcome = submit_trigger(
            session,
            trigger,
            valid_ctx(policy_versions=[stored]),
            decide=lambda c: decided.append(c) or evaluate_signal(c),
        )
        signal = session.get(HatSignal, outcome.signal_id)
        if want is not None:
            assert signal.state == "Rejected"
            assert signal.risk_decision["reason"] == want
            assert signal.risk_decision["stage"] == "entry_filter"
            assert outcome.position_id is None
            assert decided == []
        else:
            assert "stage" not in signal.risk_decision
            assert len(decided) == 1
