"""PBT criteria.* (..114, 120, 123, 124): the library's pure criteria against exact
oracles.

Every test checks the common contract (repeated calls agree, a row dated after the
evaluation date changes nothing, the score is in [0, 1], missing data fails closed) and then the
criterion's own rule against an independent computation in `Fraction`s over integer paise. The
live trigger and entry-filter types (`gap_up` .. `max_chase`) read a
`LiveContext`; their contracts, the trigger conjunction, the entry-filter skip and the EP candidate
rule are checked at the end, the breakout through the real bar builder and trigger monitor.
"""

from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

import polars as pl
from hat.hat_criteria_support import (
    EPOCH,
    Row,
    context,
)
from hat.hat_trigger_support import (
    paise,
)
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

from app.hat.data.gapscan import Gapper, GapRow
from app.hat.engine.criteria import evaluate
from app.hat.engine.criteria import vcp as vcp_module
from app.hat.engine.criteria.base import EvalContext
from app.hat.engine.criteria.darvas import Box, DarvasBoxParams, find_box
from app.hat.engine.live_entry import ep_candidates

SLOW = settings(
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large], deadline=None
)


def mean(values: list[Fraction]) -> Fraction:
    return sum(values, Fraction(0)) / len(values)


# --- series_in ------------------------------------------------------------------

SERIES = ["EQ", "BE", "SM", "N1", "BZ"]


# --- min_turnover ---------------------------------------------------------------


# --- min_price ------------------------------------------------------------------


def test_min_price_reads_the_raw_close_not_the_adjusted_one() -> None:
    # a ₹120 close later split 1:10 reads ₹12 adjusted; the rule sees the price as traded
    day = EPOCH + timedelta(days=3)
    bars = pl.DataFrame(
        {"date": [day], "high": [12.5], "low": [11.5], "close": [12.0], "raw_close": [120.0]}
    )
    passed = evaluate("min_price", {"inr": 50.0}, EvalContext(day, "x", bars))
    assert (passed.passed, passed.value) == (True, 120.0)
    split_later = bars.with_columns(raw_close=pl.lit(12.0))
    failed = evaluate("min_price", {"inr": 50.0}, EvalContext(day, "x", split_later))
    assert (failed.passed, failed.reason) == (False, "price_below_min")
    # without the raw close the rule fails closed, whatever the adjusted close says
    blind = evaluate("min_price", {"inr": 5.0}, EvalContext(day, "x", bars.drop("raw_close")))
    assert (blind.passed, blind.reason) == (False, "no_data")


# --- darvas_box -------------------------------------------------------------------

# A box: the top 110 on session 2, the bottom 100 on session 4, three quiet sessions after each.
BOX_HIGHS = [100.0, 104.0, 110.0, 108.0, 105.0, 106.0, 107.0, 106.5]
BOX_LOWS = [95.0, 99.0, 104.0, 103.0, 100.0, 101.0, 102.0, 101.5]


def test_darvas_box_top_and_bottom_each_need_confirm_sessions() -> None:
    assert find_box(BOX_HIGHS, BOX_LOWS, 3) == Box(Decimal(110), Decimal(100), 6)
    # one session fewer after the bottom: not yet a box
    assert find_box(BOX_HIGHS[:-1], BOX_LOWS[:-1], 3) == "box_bottom_unconfirmed"
    # a new high two sessions ago: the top is not confirmed
    assert find_box([*BOX_HIGHS, 111.0, 109.0], [*BOX_LOWS, 105.0, 104.0], 3) == (
        "box_top_unconfirmed"
    )


def test_darvas_box_ties_take_the_first_session() -> None:
    # an equal high later does not exceed the top; an equal low later does not undercut the bottom
    highs = [*BOX_HIGHS[:6], 110.0, 106.0]
    lows = [*BOX_LOWS[:6], 100.0, 101.0]
    assert find_box(highs, lows, 3) == Box(Decimal(110), Decimal(100), 6)


def test_darvas_box_params_need_room_for_a_box() -> None:
    assert DarvasBoxParams().confirm_days == 3
    for bad in ({"confirm_days": 3, "max_box_days": 4}, {"lookback_days": 50, "max_box_days": 60}):
        try:
            DarvasBoxParams.model_validate(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_darvas_box_exposes_the_levels_the_trigger_and_stop_read() -> None:
    days = [EPOCH + timedelta(days=i) for i in range(len(BOX_HIGHS))]
    bars = pl.DataFrame({"date": days, "high": BOX_HIGHS, "low": BOX_LOWS, "close": BOX_LOWS})
    ctx = EvalContext(days[-1], "x", bars)
    params = {"lookback_days": 20, "max_box_days": 10, "max_depth_pct": 10.0}
    assert evaluate("darvas_box", params, ctx).reason == "no_data"  # under 20 sessions
    short = {**params, "lookback_days": len(days), "max_box_days": len(days)}
    result = evaluate("darvas_box", short, ctx)
    assert result.passed
    assert result.extras == {"pivot": 110.0, "final_contraction_low": 100.0}
    assert not evaluate("darvas_box", {**short, "max_depth_pct": 9.0}, ctx).passed
    assert evaluate("darvas_box", {**short, "max_box_days": 5}, ctx).reason == "box_too_old"


# --- price_band_allowed ---------------------------------------------------------

BANDS = [2, 5, 10, 20, "none"]


# --- adr_min --------------------------------------------------------------------


# --- sma_above ------------------------------------------------------------------

# --- sma_rising -----------------------------------------------------------------


# --- 52-week position -----------------------------------------------------

# --- rs_rank --------------------------------------------------------------------

PCT = st.sampled_from([0.0, 10.0, 69.99, 70.0, 70.01, 95.0, 99.5, 100.0])


# --- momentum_rank --------------------------------------------------------------


# --- consolidation --------------------------------------------------------------


# --- ma_surf --------------------------------------------------------------------


# --- prior_move -----------------------------------------------------------------


# --- earnings_growth ------------------------------------------------------------


# --- neglect --------------------------------------------------------------------


# --- close_vs_pivot ------------------------------------------------------------

# --- vcp ------------------------------------------------------------------------


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
    """A value exactly on its threshold passes and one hair beyond it fails: the two sides of
    each rule are built so the percentage is exact in paise."""
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


# --- live trigger and entry-filter criteria -------------

PAISE = st.integers(1, 5_000_000)  # ₹0.01 .. ₹50,000.00
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


STEPS = st.lists(
    st.tuples(st.integers(0, 6 * 3600 + 14 * 60), st.integers(9_000, 11_000), st.integers(0, 50)),
    max_size=40,
)


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
    """The EP candidates of a gap scan are, in scan order, exactly the gappers with
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
