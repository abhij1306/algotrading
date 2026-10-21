"""PBT metrics.* (HAT-BT, PR 12 inputs): the drawdown, return, Sharpe and trade statistics that
the HAT backtest reuses, on the real functions of `app.hat.backtest.metrics`. Series are bounded
(at most 60 points) and positive, as an equity curve is."""

import math

import pandas as pd
from hypothesis import given
from hypothesis import strategies as st

from app.hat.backtest import metrics

equity_curves = st.lists(
    st.floats(min_value=1.0, max_value=1e7, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=60,
).map(pd.Series)
returns = st.lists(
    st.floats(min_value=-0.5, max_value=0.5, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=60,
).map(pd.Series)
pnl = st.lists(
    st.one_of(
        st.just(0.0),
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    ),
    max_size=60,
)


@given(equity_curves)
def test_drawdown_bounds(equity: pd.Series) -> None:
    """The drawdown of a positive equity curve is in (-1, 0], is exactly 0 at every new running
    peak, and `max_drawdown` is its minimum. Harm: a backtest reports a loss deeper than
    the account, or none after a fall."""
    dd = metrics.drawdown_series(equity)
    assert len(dd) == len(equity)
    assert ((dd <= 0) & (dd > -1)).all()
    at_peak = equity == equity.cummax()
    assert (dd[at_peak] == 0).all()
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
    flat = pd.Series([level] * n)
    assert metrics.sharpe_mean_std(flat) == 0.0
    assert metrics.sharpe_excess_cagr(0.1, flat, 0.05) == 0.0
    tiny = pd.Series([0.0, 2e-13] * 5)  # a per-period volatility of 1e-13 is still no volatility
    assert metrics.sharpe_mean_std(tiny) == 0.0
    assert metrics.sharpe_excess_cagr(0.1, tiny, 0.05) == 0.0


@given(returns)
def test_sharpe_sign_follows_mean(series: pd.Series) -> None:
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
