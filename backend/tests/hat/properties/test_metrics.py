"""PBT metrics.* (HAT-BT, PR 12 inputs): the drawdown, return, Sharpe and trade statistics that
the HAT backtest reuses, and the robustness figures of PR 65 (exposure, MAR, the longest
drawdown, the tail-trimmed mean and the cost share), on the real functions of
`app.hat.backtest.metrics`. Series are bounded
(at most 60 points) and positive, as an equity curve is."""

import math
from datetime import date, timedelta
from decimal import Decimal

import polars as pl
from hypothesis import example, given
from hypothesis import strategies as st

from app.hat.backtest import metrics

equity_curves = st.lists(
    st.floats(min_value=1.0, max_value=1e7, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=60,
).map(lambda xs: pl.Series(xs, dtype=pl.Float64))
returns = st.lists(
    st.floats(min_value=-0.5, max_value=0.5, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=60,
).map(lambda xs: pl.Series(xs, dtype=pl.Float64))
pnl = st.lists(
    st.one_of(
        st.just(0.0),
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    ),
    max_size=60,
)


@given(equity_curves)
def test_drawdown_bounds(equity: pl.Series) -> None:
    """The drawdown of a positive equity curve is in (-1, 0], is exactly 0 at every new running
    peak, and `max_drawdown` is its minimum. Harm: a backtest reports a loss deeper than
    the account, or none after a fall."""
    dd = metrics.drawdown_series(equity)
    assert len(dd) == len(equity)
    assert ((dd <= 0) & (dd > -1)).all()
    at_peak = equity == equity.cum_max()
    assert (dd.filter(at_peak) == 0).all()
    assert metrics.max_drawdown(dd) == dd.min()
    assert metrics.max_drawdown(dd) <= 0


@given(pnl)
def test_trade_stats_partition(values: list[float]) -> None:
    """Every trade is a win or a loss (a trade of exactly 0 is a loss), the counts add up, the win
    rate is in [0, 1], average win is positive and average loss is not, and the profit factor
    is never negative. Harm: a flat trade is counted as a win and the win rate is overstated."""
    stats = metrics.trade_stats(values)
    assert stats.total == len(values)
    assert stats.wins + stats.losses == stats.total
    assert stats.wins == sum(1 for v in values if v > 0)
    assert 0.0 <= stats.win_rate <= 1.0
    assert stats.avg_win >= 0.0
    assert stats.avg_loss <= 0.0
    assert stats.profit_factor >= 0.0
    assert math.isfinite(stats.profit_factor)


@given(
    st.floats(min_value=-0.2, max_value=0.2, allow_nan=False),
    st.integers(min_value=1, max_value=60),
)
def test_sharpe_zero_volatility(level: float, n: int) -> None:
    """Returns that never vary have no volatility, so both Sharpe conventions are exactly 0.0
    rather than a division by zero or an infinity. Harm: an infinite Sharpe ranks a flat
    strategy first."""
    flat = pl.Series([level] * n, dtype=pl.Float64)
    assert metrics.sharpe_mean_std(flat) == 0.0
    assert metrics.sharpe_excess_cagr(0.1, flat, 0.05) == 0.0
    tiny = pl.Series([0.0, 2e-13] * 5)  # a per-period volatility of 1e-13 is still no volatility
    assert metrics.sharpe_mean_std(tiny) == 0.0
    assert metrics.sharpe_excess_cagr(0.1, tiny, 0.05) == 0.0


@given(returns)
def test_sharpe_sign_follows_mean(series: pl.Series) -> None:
    """With risk-free 0 the Sharpe has the sign of the mean return (zero when there is no
    volatility), and is finite. Harm: a losing strategy is reported as profitable."""
    value = metrics.sharpe_mean_std(series)
    assert math.isfinite(value)
    mean = float(series.mean())
    if value > 0:
        assert mean > 0
    if value < 0:
        assert mean < 0


@given(
    st.floats(min_value=-1e6, max_value=1e7, allow_nan=False),
    st.floats(min_value=-1e6, max_value=1e7, allow_nan=False),
    st.integers(min_value=-5, max_value=600),
)
def test_returns_guard_bad_capital(final: float, initial: float, periods: int) -> None:
    """A non-positive initial capital (or no periods) gives a return of 0.0, never a division by
    zero or a sign flip; with positive capital, ending where it began is a return of 0.
    Harm: a run with no capital reports a huge return."""
    if initial <= 0:
        assert metrics.total_return(final, initial) == 0.0
        assert metrics.annualized_return(final, initial, periods) == 0.0
    else:
        assert metrics.total_return(initial, initial) == 0.0
        if periods <= 0:
            assert metrics.annualized_return(final, initial, periods) == 0.0


@given(equity_curves)
@example(pl.Series([3.0, 1.0, 2.0, 3.0, 2.0], dtype=pl.Float64))
def test_longest_drawdown(equity: pl.Series) -> None:
    """The longest drawdown is the longest run of consecutive points below the curve's running
    maximum (counted here from polars' `cum_max`), so it is 0 exactly when the curve never
    falls and it is shorter than the curve. Harm: a run that sat under water for months reports
    a short recovery, or a recovery to the old peak still counts as under water."""
    under = (equity < equity.cum_max()).to_list()
    runs = "".join("1" if u else "0" for u in under).split("0")
    longest = metrics.longest_drawdown(equity)
    assert longest == max(len(r) for r in runs)
    assert (longest == 0) is bool((equity == equity.cum_max()).all())
    assert 0 <= longest < len(equity)


DAY0 = date(2026, 1, 1)
spans = st.lists(
    st.tuples(st.integers(0, 40), st.integers(0, 10)).map(
        lambda p: (DAY0 + timedelta(p[0]), DAY0 + timedelta(p[0] + p[1]))
    ),
    max_size=8,
)


@given(st.sets(st.integers(0, 50), max_size=40), spans)
@example({0, 3, 6}, [(DAY0 + timedelta(1), DAY0 + timedelta(5))])  # a span between sessions
def test_exposure(days: set[int], held: list[tuple[date, date]]) -> None:
    """Exposure is the share of the sessions inside some held span, both ends included: the
    sessions within the union of the spans (counted here day by day) over all sessions, in
    [0, 1], 0.0 without sessions. Harm: the report counts calendar days or misses the exit day,
    so a strategy that sits in cash looks fully invested or the other way round."""
    sessions = sorted(DAY0 + timedelta(n) for n in days)
    covered = {a + timedelta(n) for a, b in held for n in range((b - a).days + 1)}
    value = metrics.exposure(sessions, held)
    if not sessions:
        assert value == 0.0
        return
    assert value == sum(1 for d in sessions if d in covered) / len(sessions)
    assert 0.0 <= value <= 1.0


@given(
    st.floats(min_value=-0.99, max_value=10.0, allow_nan=False),
    # a curve's drawdown: none, or at least a millionth (a tick on a large account)
    st.one_of(st.just(0.0), st.floats(min_value=-0.99, max_value=-1e-6)),
)
@example(0.2, 0.0)
def test_mar_ratio(cagr: float, drawdown: float) -> None:
    """MAR is the CAGR over the drawdown's depth: it has the CAGR's sign and times the depth
    gives the CAGR back, and without a drawdown it is None, never a division by zero.
    Harm: a deeper drawdown raises a strategy's MAR, or a smooth run reports infinity."""
    mar = metrics.mar_ratio(cagr, drawdown)
    if drawdown == 0:
        assert mar is None
        return
    assert mar is not None
    assert math.isclose(mar * -drawdown, cagr, rel_tol=1e-9, abs_tol=1e-12)
    assert (mar > 0) is (cagr > 0)
    assert (mar < 0) is (cagr < 0)


r_values = st.lists(st.integers(-300, 500).map(lambda n: Decimal(n) / 100), max_size=30)


@given(r_values, st.integers(0, 32))
@example([Decimal(-1), Decimal(5), Decimal(1)], 1)
def test_tail_trimmed_mean(values: list[Decimal], drop: int) -> None:
    """The tail-trimmed mean drops the `drop` largest values: it is the exact mean of the rest
    (so drop 0 is the plain mean), never above the mean with one fewer dropped, and None when
    nothing is left. Harm: a strategy whose edge is one outlier trade passes the tail test, or
    dropping the best trades raises the mean."""
    trimmed = metrics.tail_trimmed_mean(values, drop)
    if drop >= len(values):
        assert trimmed is None
        return
    rest = sorted(values, reverse=True)[drop:]
    assert trimmed == sum(rest, Decimal(0)) / len(rest)
    if drop:
        fewer = metrics.tail_trimmed_mean(values, drop - 1)
        assert fewer is not None
        assert trimmed <= fewer


money = st.integers(-(10**8), 10**8).map(lambda n: Decimal(n) / 100)


@given(money.map(abs), money)
@example(Decimal(0), Decimal(0))
def test_cost_share(charges: Decimal, net: Decimal) -> None:
    """The cost share is the charges over the gross P&L (net + charges): exact, in [0, 1] when
    the net is not a loss, above 1 when the charges turned a gross gain into a loss, and None
    unless the gross is a gain. Harm: costs that ate the whole edge read as a small share, or a
    losing run divides by zero or a negative."""
    gross = net + charges
    share = metrics.cost_share(charges, gross)
    if gross <= 0:
        assert share is None
        return
    assert share == charges / gross
    assert (share <= 1) is (net >= 0)
    assert share >= 0
