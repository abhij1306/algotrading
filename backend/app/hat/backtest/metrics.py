"""Pure backtest metric calculations: drawdown, return, Sharpe and trade statistics.

HAT's own copy of the legacy `app.services.backtest_metrics` (K1, backend review C5), taken so the
legacy module can be deleted without losing what PR 12 (the backtest) reuses. Two Sharpe
conventions are kept so a characterization against the legacy numbers stays possible. Series are
polars (PR 12 moved the module off pandas, HAT-DATA-078); the arithmetic is unchanged.

* `sharpe_mean_std`: `mean / std * sqrt(periods)` of per-period returns, risk-free 0.
* `sharpe_excess_cagr`: `(cagr - rf) / annualised std`.

PR 65 (G35) adds what a tuning run reports besides the return figures: the share of sessions
with a position held (`exposure`), MAR (`mar_ratio`), the longest stretch under the running peak
(`longest_drawdown`), the mean after dropping the best trades (`tail_trimmed_mean`) and the
charges' share of the gross P&L (`cost_share`). The two on R and money take Decimals and stay
exact.

No I/O and no state beyond the inputs passed in.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import polars as pl

TRADING_DAYS_PER_YEAR = 252
# Rounding makes the standard deviation of a constant series about 1e-17, not 0, and a Sharpe
# ratio divided by that is about 1e17 (found by `metrics.sharpe_zero_volatility`; the legacy
# module has the flaw). A per-period return volatility below this is no volatility, and a
# gross loss below it (a subnormal float, found by `metrics.trade_stats_partition`) is no loss:
# the profit factor stays finite.
VOLATILITY_FLOOR = 1e-12


@dataclass(frozen=True)
class TradeStats:
    """Win/loss summary of a list of per-trade P&L (or return) values."""

    total: int
    wins: int
    losses: int
    win_rate: float  # fraction 0..1
    avg_win: float
    avg_loss: float
    profit_factor: float  # |gross win / gross loss|; 0.0 when there are no losers


def drawdown_series(equity: pl.Series) -> pl.Series:
    """Fractional drawdown from the running peak (values <= 0)."""
    equity = equity.cast(pl.Float64)
    if equity.is_empty():
        return equity
    return ((equity / equity.cum_max()) - 1.0).fill_nan(0.0).fill_null(0.0)


def max_drawdown(drawdown: pl.Series) -> float:
    """Worst drawdown as a non-positive fraction; 0.0 for an empty or flat series."""
    return float(drawdown.min()) if len(drawdown) else 0.0  # type: ignore[arg-type]


def _std(returns: pl.Series) -> float:
    """The population standard deviation; 0.0 for an empty series."""
    return float(returns.cast(pl.Float64).std(ddof=0) or 0.0) if len(returns) else 0.0  # type: ignore[arg-type]


def total_return(final_equity: float, initial_capital: float) -> float:
    """Fractional total return; 0.0 when the initial capital is not positive."""
    return (final_equity / initial_capital) - 1.0 if initial_capital > 0 else 0.0


def annualized_return(
    final_equity: float,
    initial_capital: float,
    n_periods: int,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """CAGR by period count: ``(final / initial) ** (periods_per_year / n_periods) - 1``."""
    if n_periods <= 0 or initial_capital <= 0:
        return 0.0
    return float((final_equity / initial_capital) ** (periods_per_year / max(1, n_periods)) - 1.0)


def sharpe_mean_std(returns: pl.Series, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Annualised Sharpe with risk-free 0; 0.0 when volatility is zero."""
    std = _std(returns)
    if std > VOLATILITY_FLOOR:
        mean = float(returns.cast(pl.Float64).mean())  # type: ignore[arg-type]
        return (mean / std) * math.sqrt(periods_per_year)
    return 0.0


def sharpe_excess_cagr(
    cagr: float,
    returns: pl.Series,
    risk_free: float,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """``(cagr - risk_free) / annualised std``; 0.0 when volatility is zero."""
    std = _std(returns)
    if std <= VOLATILITY_FLOOR:  # the floor is per period, like the series
        return 0.0
    return (cagr - risk_free) / (std * math.sqrt(periods_per_year))


def trade_stats(values: Sequence[float]) -> TradeStats:
    """Summarise per-trade P&L or returns. A value of exactly 0 counts as a loss."""
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]
    gross_loss = sum(losses)
    ratio = abs(sum(wins) / gross_loss) if abs(gross_loss) > VOLATILITY_FLOOR else 0.0
    profit_factor = ratio if math.isfinite(ratio) else 0.0
    return TradeStats(
        total=len(values),
        wins=len(wins),
        losses=len(losses),
        win_rate=(len(wins) / len(values)) if values else 0.0,
        avg_win=(sum(wins) / len(wins)) if wins else 0.0,
        avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
        profit_factor=float(profit_factor),
    )


def longest_drawdown(equity: Sequence[float] | pl.Series) -> int:
    """The longest run of consecutive points below the running peak of the points before them
    (the longest time under water, in the curve's periods); 0 for a curve that never falls."""
    longest = run = 0
    peak = -math.inf
    for value in equity:
        if value < peak:
            run += 1
            longest = max(longest, run)
        else:
            peak, run = value, 0
    return longest


def exposure(sessions: Sequence[date], held: Sequence[tuple[date, date]]) -> float:
    """The share of `sessions` on which at least one position was held: a session within some
    (entry date, exit date) span, both ends included; 0.0 without sessions."""
    if not sessions:
        return 0.0
    days = sorted(sessions)
    invested: set[int] = set()
    for a, b in held:
        invested.update(range(bisect_left(days, a), bisect_right(days, b)))
    return len(invested) / len(days)


def mar_ratio(cagr: float, drawdown: float) -> float | None:
    """MAR: the CAGR over the depth of the maximum drawdown (a non-positive fraction, as
    `max_drawdown` gives it); None without a drawdown."""
    return cagr / -drawdown if drawdown < 0 else None


def tail_trimmed_mean(values: Sequence[Decimal], drop: int) -> Decimal | None:
    """The mean of `values` without its `drop` largest; None when nothing would be left."""
    if drop < 0 or drop >= len(values):
        return None
    kept = sorted(values)[: len(values) - drop]
    return sum(kept, Decimal(0)) / len(kept)


def cost_share(charges: Decimal, gross: Decimal) -> Decimal | None:
    """The charges' share of the gross P&L (before charges); None unless the gross is a gain."""
    return charges / gross if gross > 0 else None
