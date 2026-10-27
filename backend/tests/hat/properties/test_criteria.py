"""The library's criteria against exact edges: every seeded criterion at its threshold (one
side passes, a paisa or a share beyond fails), checked with the common contract (repeated calls
agree, a row dated after the evaluation date changes nothing), plus the VCP score, the inclusive
edges of the percentage rules and the EP candidate rule."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import polars as pl
import pytest
import yaml
from hat.hat_criteria_support import (
    EPOCH,
    Lcg,
    Row,
    context,
    contract,
    frame,
    universe_frame,
)
from hat.hat_trigger_support import (
    paise,
)
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

from app.hat.data.gapscan import Gapper, GapRow
from app.hat.engine.criteria import CriterionResult, LiveContext, evaluate, evaluate_live
from app.hat.engine.criteria import vcp as vcp_module
from app.hat.engine.criteria.base import EvalContext
from app.hat.engine.criteria.darvas import Box, DarvasBoxParams, find_box
from app.hat.engine.criteria.live import level_names, resolve_level
from app.hat.engine.criteria.pullback import ema, pullback_span, volume_ratio
from app.hat.engine.criteria.reversion import wilder_rsi
from app.hat.engine.live_entry import ep_candidates, ep_setup_passes, gap_floor
from app.hat.engine.strategy.config import Playbook, StrategyConfig, parse_config
from app.hat.engine.strategy.registry import STRATEGIES

SLOW = settings(
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large], deadline=None
)


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


# --- ema_pullback -----------------------------------------------------------------


def test_ema_is_the_recurrence_seeded_from_the_sma_of_its_own_window() -> None:
    closes = [None, *(float(c) for c in range(1, 15))]  # a null before the 12-close window
    alpha, value = Fraction(2, 3), Fraction(3 + 4, 2)
    for close in range(5, 15):
        value += alpha * (close - value)
    assert Fraction(ema(closes, 2)) == value
    assert ema(closes, 2, offset=3) is None  # its window reaches the null


def test_pullback_span_counts_from_the_last_session_of_the_high() -> None:
    assert pullback_span([1.0, 5.0, 3.0, 5.0, 4.0, 3.0], 2) == 2
    assert pullback_span([1.0, 5.0, 3.0, 5.0, 4.0], 2) == "no_pullback"
    assert pullback_span([1.0, 2.0, 3.0], 1) == "no_pullback"  # the date is the high


def test_volume_ratio_reads_the_whole_pullback_even_past_the_average_window() -> None:
    # a 55-session pullback: its first 5 sessions are heavy and fall outside the 50-session mean
    volumes = [1200.0] * 5 + [100.0] * 50
    assert volume_ratio(volumes, 55) == 2  # (6000 + 5000) / 55 = 200 over a mean of 100
    assert volume_ratio(volumes, 50) == 1
    assert volume_ratio(volumes[1:], 55) is None  # the pullback's window is not there
    assert volume_ratio([0.0] * 50, 2) is None


def test_ema_pullback_exposes_the_levels_the_trigger_and_stop_read() -> None:
    # a steady rise, the high on session 125, then two quieter sessions back to the 10 EMA (221.08)
    closes = [100.0 + i for i in range(125)] + [226.0, 223.0, 221.0]
    lows = [c - 1 for c in closes[:-1]] + [219.0]
    volumes = [1000.0] * 125 + [600.0] * 3
    days = [EPOCH + timedelta(days=i) for i in range(len(closes))]
    bars = pl.DataFrame(
        {
            "date": days,
            "high": [c + 1 for c in closes],
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )
    ctx = EvalContext(days[-1], "x", bars)
    result = evaluate("ema_pullback", {}, ctx)
    assert (result.passed, result.value) == (True, 10.0)
    assert result.extras == {"pivot": 222.0, "final_contraction_low": 219.0}
    heavy = EvalContext(days[-1], "x", bars.with_columns(volume=pl.lit(1000.0)))
    assert evaluate("ema_pullback", {}, heavy).reason == "volume_not_lighter"
    assert evaluate("ema_pullback", {"touch_pct": 0.0}, ctx).passed  # the low is under the EMA
    assert evaluate("ema_pullback", {"max_undercut_pct": 0.0}, ctx).reason == "below_ema"


# --- rsi_below ------------------------------------------------------------------


def test_wilder_rsi_is_the_recurrence_seeded_from_the_mean_of_its_own_window() -> None:
    # 21 closes for RSI(2): a null before them, alternating +3 / -1 changes, then a -4 at the end
    closes = [None, 100.0] + [100.0 + 2 * (i // 2) + 3 * (i % 2) for i in range(1, 20)] + [99.0]
    window = closes[-21:]
    changes = [Fraction(b) - Fraction(a) for a, b in zip(window, window[1:], strict=False)]
    gain = sum(max(c, 0) for c in changes[:2]) / 2
    loss = sum(max(-c, 0) for c in changes[:2]) / 2
    for change in changes[2:]:
        gain, loss = (gain + max(change, 0)) / 2, (loss + max(-change, 0)) / 2
    rsi = wilder_rsi(closes, 2)
    assert rsi is not None
    assert abs(Fraction(rsi) - (100 - 100 / (1 + gain / loss))) < Fraction(1, 10**20)
    assert wilder_rsi(closes[2:], 2) is None  # 20 closes: the window is not there
    assert wilder_rsi([float(c) for c in range(21)], 2) == 100  # no loss
    assert wilder_rsi([5.0] * 21, 2) is None  # no change at all


def test_rsi_below_exposes_the_signal_close_as_the_pivot() -> None:
    closes = [100.0 + i for i in range(18)] + [110.0, 104.0, 101.0]  # two hard down days
    days = [EPOCH + timedelta(days=i) for i in range(len(closes))]
    ctx = EvalContext(days[-1], "x", pl.DataFrame({"date": days, "close": closes}))
    result = evaluate("rsi_below", {"length": 2, "below": 10}, ctx)
    assert result.passed
    assert result.value < 10
    assert result.extras == {"pivot": 101.0}
    deeper = evaluate("rsi_below", {"length": 2, "below": result.value * 0.99}, ctx)
    assert deeper.reason == "rsi_not_below"
    short = EvalContext(days[-1], "x", pl.DataFrame({"date": days[1:], "close": closes[1:]}))
    assert evaluate("rsi_below", {}, short).reason == "no_data"


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


def maybe(value: int | None) -> Decimal | None:
    return None if value is None else paise(value)


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
    picked = ep_candidates(items, lambda symbol: symbol in passing, Decimal(10))
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


def _ep_config(setup: list[dict], gap_min: float = 10) -> StrategyConfig:
    """The seeded S2 config with its episodic pivot's setup and gap minimum replaced."""
    raw = yaml.safe_load(STRATEGIES["momentum-breakout-ep"].config_text)
    ep = next(p for p in raw["playbooks"] if p["name"] == "episodic_pivot")
    ep["setup"] = {"criteria": setup}
    next(t for t in ep["trigger"] if t["type"] == "gap_up")["params"]["min_pct"] = gap_min
    return parse_config(raw)


def _ep_playbook(setup: list[dict], gap_min: float = 10) -> Playbook:
    return _ep_config(setup, gap_min).playbook("episodic_pivot")


def test_ep_setup_reads_the_previous_session() -> None:
    """`adr_min` in a pre-open setup is judged as of the last session before the gap day (as the
    nightly scan would judge it that evening) and `neglect` on the bars before the gap day; the
    gap day's own wide bar is never read."""
    calm = [Row(EPOCH + timedelta(days=i), 10_390, 10_000, 10_200, 1000) for i in range(70)]
    gap_day = EPOCH + timedelta(days=70)
    rows = [*calm, Row(gap_day, 20_000, 10_000, 15_000, 1000)]
    neglect = {"type": "neglect", "mode": "hard", "params": {"max_return_63d_pct": 30}}

    def adr(pct: float) -> dict:
        return {"type": "adr_min", "mode": "hard", "params": {"days": 20, "pct": pct}}

    assert ep_setup_passes(_ep_playbook([neglect, adr(3.9)]), frame(rows), gap_day)
    assert not ep_setup_passes(_ep_playbook([neglect, adr(4)]), frame(rows), gap_day)
    assert not ep_setup_passes(_ep_playbook([neglect, adr(3.9)]), frame(rows[:1]), EPOCH)


def test_gap_floor_is_the_least_preopen_minimum() -> None:
    neglect = {"type": "neglect", "mode": "hard", "params": {}}
    configs = [_ep_config([neglect], 10), _ep_config([neglect], 6)]
    assert gap_floor(configs) == Decimal(6)
    assert gap_floor([]) is None


# --- the seeded criteria, one exact edge each ---------------------------------------------------


def judged(code: str, params: dict, ctx: EvalContext) -> CriterionResult:
    """The result, after the common contract: repeated calls agree and later rows change nothing."""
    return contract(code, params, ctx, Lcg(7))


def test_series_in() -> None:
    rows = flat_rows([10_000])
    assert judged("series_in", {"series": ["EQ", "BE"]}, context(rows, series="BE")).passed
    refused = judged("series_in", {"series": ["EQ", "BE"]}, context(rows, series="SM"))
    assert refused.reason == "series_not_allowed"
    assert judged("series_in", {}, context(rows)).reason == "no_data"


def test_sma_above_is_strict() -> None:
    """close > SMA(3) and SMA(2) > SMA(3) strictly; equal fails; too few bars is no data."""
    close_vs_3 = {"a": "close", "b": 3}
    assert not judged("sma_above", close_vs_3, context(flat_rows([10_000] * 3))).passed
    assert judged("sma_above", close_vs_3, context(flat_rows([10_000, 10_000, 10_001]))).passed
    two_vs_3 = {"a": 2, "b": 3}
    assert judged("sma_above", two_vs_3, context(flat_rows([9_900, 10_000, 10_100]))).passed
    flat = judged("sma_above", two_vs_3, context(flat_rows([10_000] * 3)))
    assert (flat.passed, flat.reason) == (False, "not_above")
    assert judged("sma_above", close_vs_3, context(flat_rows([10_000] * 2))).reason == "no_data"


def test_sma_rising_is_strict() -> None:
    """SMA(3) now against SMA(3) two sessions earlier: equal fails, a paisa higher passes."""
    params = {"sma": 3, "lookback_days": 2}
    flat = judged("sma_rising", params, context(flat_rows([10_000] * 5)))
    assert (flat.passed, flat.reason) == (False, "not_rising")
    assert judged("sma_rising", params, context(flat_rows([10_000] * 4 + [10_001]))).passed
    assert judged("sma_rising", params, context(flat_rows([10_000] * 4))).reason == "no_data"


def test_min_turnover_edge() -> None:
    """The mean traded value of 2 sessions at least 10 crore: exactly passes, a rupee short
    fails; close x volume stands in for a null traded value."""
    params = {"days": 2, "inr_cr": 10}
    crore10 = 10**8

    def rows(*traded: int | None) -> list[Row]:
        return [
            Row(EPOCH + timedelta(days=i), 10_000, 10_000, 10_000, 1_000_000, t)
            for i, t in enumerate(traded)
        ]

    assert judged("min_turnover", params, context(rows(crore10, crore10))).passed
    short = judged("min_turnover", params, context(rows(crore10, crore10 - 1)))
    assert (short.passed, short.reason) == (False, "turnover_below_min")
    # ₹100 x 1,000,000 shares is 10 crore
    assert judged("min_turnover", params, context(rows(crore10, None))).passed
    assert judged("min_turnover", params, context(rows(crore10))).reason == "no_data"


def test_adr_min_edge() -> None:
    """The mean of 100 x (high / low - 1) over 2 sessions at least 4: exactly passes."""
    params = {"days": 2, "pct": 4}
    assert judged("adr_min", params, context(flat_rows([10_000] * 2, highs=[10_400] * 2))).passed
    narrow = judged("adr_min", params, context(flat_rows([10_000] * 2, highs=[10_400, 10_399])))
    assert (narrow.passed, narrow.reason) == (False, "adr_below_min")
    assert judged("adr_min", params, context(flat_rows([10_000], highs=[10_400]))).reason == (
        "no_data"
    )


def test_momentum_rank_top_seats() -> None:
    """Top 2% of 100 instruments is 2 seats: passes with one return strictly above, fails with
    two; `any` passes on one window, `all` needs every window."""
    rows = flat_rows([10_000])

    def universe(ret21: float, ret63: float) -> pl.DataFrame:
        others = [(f"o{n}", {"ret21": float(n), "ret63": float(n)}) for n in range(99)]
        return universe_frame([("x", {"ret21": ret21, "ret63": ret63}), *others])

    any_ = {"windows": [21, 63], "top_pct": 2, "match": "any"}
    all_ = {**any_, "match": "all"}
    assert judged("momentum_rank", any_, context(rows, universe=universe(97.0, 0.0))).passed
    behind = judged("momentum_rank", any_, context(rows, universe=universe(96.5, 0.0)))
    assert (behind.passed, behind.reason) == (False, "not_top_momentum")
    assert not judged("momentum_rank", all_, context(rows, universe=universe(97.0, 0.0))).passed
    assert judged("momentum_rank", all_, context(rows, universe=universe(97.0, 97.0))).passed
    assert judged("momentum_rank", any_, context(rows)).reason == "no_data"


def test_close_vs_pivot_edge() -> None:
    """The close at most 0% above the pivot `vcp` exposed: at it passes, a paisa above fails."""
    params = {"max_above_pct": 0}
    at = context(flat_rows([10_000])).with_levels({"pivot": 100.0})
    assert judged("close_vs_pivot", params, at).passed
    above = context(flat_rows([10_001])).with_levels({"pivot": 100.0})
    assert judged("close_vs_pivot", params, above).reason == "above_pivot"
    assert judged("close_vs_pivot", params, context(flat_rows([10_000]))).reason == "no_pivot"


def test_earnings_growth_edge() -> None:
    """The latest filing at the minimums with EPS growth above the one before passes; a filing
    after the date is not read."""
    rows = flat_rows([10_000])
    as_of = rows[-1].day

    def filings(*rows_: tuple[int, float, float]) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "filed_at": [as_of + timedelta(days=d) for d, _, _ in rows_],
                "eps_yoy": [e for _, e, _ in rows_],
                "sales_yoy": [s for _, _, s in rows_],
            }
        )

    params = {"eps_yoy": 20, "sales_yoy": 20}
    edge = filings((-90, 19.0, 30.0), (0, 20.0, 20.0), (1, 0.0, 0.0))
    assert judged("earnings_growth", params, context(rows, fundamentals=edge)).passed
    slow = filings((-90, 19.0, 30.0), (0, 20.0, 19.99))
    assert judged("earnings_growth", params, context(rows, fundamentals=slow)).reason == (
        "growth_below_min"
    )
    flat = filings((-90, 20.0, 30.0), (0, 20.0, 20.0))
    assert not judged("earnings_growth", params, context(rows, fundamentals=flat)).passed


def test_neglect_edges() -> None:
    """|63-session move| up to the previous session at most 30%: +30% and -30% pass, a paisa
    beyond fails; the gap day's own bar is never read."""
    params = {"max_return_63d_pct": 30}

    def result(last_close: int) -> CriterionResult:
        rows = flat_rows([10_000] * 63 + [last_close, 50_000])  # the gap day closes 5x
        return judged("neglect", params, context(rows))

    assert result(13_000).passed
    assert result(13_001).reason == "already_extended"
    assert result(7_000).passed
    assert result(6_999).reason == "already_fell"
    assert judged("neglect", params, context(flat_rows([10_000] * 63))).reason == "no_data"


# (high, low, close) in paise of a base with two contractions: 100 -> 88 (12%), then 99 -> 92.50
# (6.57%, a ratio of 0.55), the pivot 99.
VCP_BASE = [
    (10_000, 9_800, 10_000),
    (9_900, 9_500, 9_600),
    (9_700, 9_200, 9_300),
    (9_500, 9_000, 9_100),
    (9_400, 8_800, 8_900),
    (9_500, 9_000, 9_400),
    (9_700, 9_300, 9_600),
    (9_800, 9_500, 9_700),
    (9_900, 9_600, 9_800),
    (9_800, 9_500, 9_600),
    (9_700, 9_400, 9_500),
    (9_600, 9_300, 9_400),
    (9_500, 9_250, 9_300),
    (9_600, 9_350, 9_500),
    (9_700, 9_400, 9_600),
    (9_800, 9_500, 9_700),
] + [(9_800, 9_600, 9_700)] * 4
VCP_PARAMS = {
    "contractions": [2, 6],
    "depth_ratio_max": 0.75,
    "final_depth_max_pct": 10,
    "lookback_days": 120,
    "dryup_vs_avg50": 0.7,
}


def vcp_rows(base_volume: int) -> list[Row]:
    """50 quiet sessions at volume 1000 below the base, then the base at `base_volume`."""
    before = [Row(EPOCH + timedelta(days=i), 9_100, 8_900, 9_000, 1000) for i in range(50)]
    base = [
        Row(EPOCH + timedelta(days=50 + i), h, lo, c, base_volume)
        for i, (h, lo, c) in enumerate(VCP_BASE)
    ]
    return before + base


def test_vcp_pivot_and_dryup_edge() -> None:
    """Two tightening contractions pass with the final one's high as the pivot; the final
    contraction's volume at exactly 0.7 of the 50-session mean passes, above it fails."""
    found = judged("vcp", VCP_PARAMS, context(vcp_rows(700)))
    assert found.passed
    assert (found.extras["pivot"], found.extras["final_contraction_low"]) == (99.0, 92.5)
    assert found.extras["contraction_count"] == 2
    loud = judged("vcp", VCP_PARAMS, context(vcp_rows(701)))
    assert (loud.passed, loud.reason) == (False, "no_volume_dryup")
    one = judged("vcp", {**VCP_PARAMS, "contractions": [3, 6]}, context(vcp_rows(500)))
    assert one.reason == "too_few_contractions"


# --- the seeded live triggers and entry filters ---

AT = datetime(2026, 10, 23, 4, 0, tzinfo=UTC)


def live(code: str, params: dict, **fields: object) -> CriterionResult:
    return evaluate_live(code, params, LiveContext(at=AT, **fields))  # type: ignore[arg-type]


def test_price_breaks_level_edge() -> None:
    """A bar whose high is strictly above the level and whose close holds at or above it."""
    params = {"level": "pivot"}
    level = {"pivot": paise(10_000)}
    assert live(
        "price_breaks_level", params, levels=level, bar_high=paise(10_001), bar_close=paise(10_000)
    ).passed
    touch = live(
        "price_breaks_level", params, levels=level, bar_high=paise(10_000), bar_close=paise(10_000)
    )
    assert touch.reason == "below_level"
    fell_back = live(
        "price_breaks_level", params, levels=level, bar_high=paise(10_100), bar_close=paise(9_999)
    )
    assert fell_back.reason == "below_level"
    assert live("price_breaks_level", params, bar_high=paise(10_100)).reason == "level_undefined"


def test_level_expressions() -> None:
    """A level expression names one level or the largest of several (`max(a, b, ...)`), each
    name any level a setup exposes; the opening range drops out on daily bars, and a level not
    yet known leaves the expression unknown. Harm: a new setup's level cannot trigger without
    code edits, or the backtest breaks on a level the playbook does not name."""
    assert level_names("flag_high") == ("flag_high",)
    assert level_names("max(flag_high, opening_range_high)") == ("flag_high", "opening_range_high")
    for bad in ("", "max(a)", "max(a, a)", "min(a, b)", "Pivot", "max(a,)", "a b"):
        with pytest.raises(ValueError, match="bad_level_expression"):
            level_names(bad)
    levels = {"flag_high": paise(10_000), "pivot": paise(12_000), "opening_range_high": None}
    assert resolve_level("flag_high", levels) == paise(10_000)
    assert resolve_level("max(flag_high, opening_range_high)", levels) is None
    assert resolve_level("max(flag_high, opening_range_high)", levels, daily=True) == paise(10_000)
    assert resolve_level("opening_range_high", levels, daily=True) is None
    levels["opening_range_high"] = paise(11_000)
    assert resolve_level("max(flag_high, opening_range_high)", levels) == paise(11_000)
    assert resolve_level("missing", levels) is None


def test_gap_up_edge() -> None:
    params = {"min_pct": 10}
    assert live("gap_up", params, open_price=paise(11_000), prev_close=paise(10_000)).passed
    small = live("gap_up", params, open_price=paise(10_999), prev_close=paise(10_000))
    assert small.reason == "gap_too_small"
    assert live("gap_up", params, open_price=paise(11_000)).reason == "no_data"


def test_volume_pace_edge() -> None:
    """V / f >= 1.5 x the 50-day average: 300 shares a quarter into the session on an average of
    800 projects to exactly 1200."""
    params = {"vs_avg50": 1.5}
    quarter = Fraction(1, 4)
    assert live(
        "volume_pace", params, cum_volume=300, elapsed=quarter, avg50_volume=Decimal(800)
    ).passed
    slow = live("volume_pace", params, cum_volume=299, elapsed=quarter, avg50_volume=Decimal(800))
    assert slow.reason == "volume_pace_low"
    before_first_bar = live("volume_pace", params, cum_volume=300, avg50_volume=Decimal(800))
    assert before_first_bar.reason == "no_data"


def test_early_volume_edge() -> None:
    params = {"vs_adv20": 0.5}
    assert live("early_volume", params, cum_volume=500, adv20=Decimal(1000)).passed
    assert live("early_volume", params, cum_volume=499, adv20=Decimal(1000)).reason == (
        "early_volume_low"
    )


def test_catalyst_attached_window() -> None:
    """An announcement in [at - 24h, at] counts, both ends included; one after `at` never does."""
    params = {"within_hours": 24}
    edge = AT - timedelta(hours=24)
    assert live("catalyst_attached", params, announcements=[edge]).passed
    assert live("catalyst_attached", params, announcements=[AT]).passed
    stale = [edge - timedelta(seconds=1), AT + timedelta(seconds=1)]
    assert live("catalyst_attached", params, announcements=stale).reason == "no_catalyst"


def test_adtr_filter_edge() -> None:
    """entry - stop at most 1.5 x ADTR(20): exactly passes, a paisa wider fails."""
    params = {"max_multiple": 1.5}
    adtr = paise(400)
    assert live("adtr_filter", params, entry=paise(10_000), stop=paise(9_400), adtr20=adtr).passed
    wide = live("adtr_filter", params, entry=paise(10_000), stop=paise(9_399), adtr20=adtr)
    assert wide.reason == "stop_wider_than_adtr"
    above = live("adtr_filter", params, entry=paise(10_000), stop=paise(10_000), adtr20=adtr)
    assert above.reason == "stop_not_below_entry"


def test_max_chase_edge() -> None:
    """At most 2% above the trigger level: exactly passes, a paisa above fails; a locked upper
    circuit fails and an unknown circuit state fails closed."""
    params = {"pct": 2}
    level = paise(10_000)
    assert live(
        "max_chase", params, price=paise(10_200), trigger_level=level, locked_upper_circuit=False
    ).passed
    chased = live(
        "max_chase", params, price=paise(10_201), trigger_level=level, locked_upper_circuit=False
    )
    assert chased.reason == "price_past_max_chase"
    locked = live("max_chase", params, price=level, trigger_level=level, locked_upper_circuit=True)
    assert locked.reason == "locked_upper_circuit"
    unknown = live("max_chase", params, price=level, trigger_level=level)
    assert unknown.reason == "no_data"
