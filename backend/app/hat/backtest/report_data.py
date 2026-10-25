"""A run's report data (`backtests/run_id=<id>/report.json`): what the results page charts, built
once when the run completes from its result and the NIFTY 500 closes, and read back by the API
(`/api/hat/backtests`). Pure but for `write_report` and `read_report`.

The document (`REPORT_VERSION`): the equity curve with its drawdown and the NIFTY 500 rebased to
the run's first session, the yearly returns of both, the R curve (cumulative R in exit order),
the trades with MFE/MAE and the index regime at entry, the breakdowns by exit reason, hold time
and regime, and the signal funnel. Money and R are exact decimal strings, return and drawdown
ratios floats, dates ISO strings.

The regime at entry (`REGIME_RULE`) is the NIFTY 500's trend on the session before the entry:
`up` when its close is above its 200-session SMA and that SMA is above its value 20 sessions
earlier, else `not_up`; `unknown` without 220 sessions of the index before the entry. It is a
reading of the market, not the live regime engine's state.
"""

from __future__ import annotations

import json
import math
import uuid
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..data.store import atomic_replace
from . import metrics as bt_metrics
from .report import BacktestResult, TradeRow

__all__ = [
    "BENCHMARK",
    "HOLD_BUCKETS",
    "REGIME_RULE",
    "REPORT_VERSION",
    "build_report",
    "group_stats",
    "hold_bucket",
    "index_regimes",
    "read_report",
    "regime_before",
    "report_path",
    "write_report",
    "yearly_returns",
]

REPORT_VERSION = 1
BENCHMARK = "NIFTY 500"
REGIME_SMA = 200
REGIME_RISING = 20
REGIME_RULE = "nifty500_close_above_sma200_and_sma200_above_20_sessions_ago_on_prior_session"
UP, NOT_UP, UNKNOWN = "up", "not_up", "unknown"
# (label, the last holding day of the bucket; None: no end)
HOLD_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("0-5", 5),
    ("6-20", 20),
    ("21-60", 60),
    ("61+", None),
)
_ZERO = Decimal(0)
_R_PLACES = Decimal("0.000001")


def report_path(root: Path, run_id: uuid.UUID) -> Path:
    """`backtests/run_id=<id>/report.json` under the HAT data root."""
    return root / "backtests" / f"run_id={run_id}" / "report.json"


def _r(value: Decimal) -> str:
    return str(value.quantize(_R_PLACES))


def _float(value: float) -> float | None:
    return value if math.isfinite(value) else None


def index_regimes(closes: Sequence[tuple[date, Decimal]]) -> list[tuple[date, str]]:
    """Each index session and its trend state (`REGIME_RULE`, before the shift to the next
    session): `up`, `not_up`, or `unknown` before `REGIME_SMA` + `REGIME_RISING` sessions."""
    values = [float(c) for _, c in closes]
    sums = [0.0]
    for v in values:
        sums.append(sums[-1] + v)

    def sma(i: int) -> float | None:
        return None if i + 1 < REGIME_SMA else (sums[i + 1] - sums[i + 1 - REGIME_SMA]) / REGIME_SMA

    out = []
    for i, (day, _) in enumerate(closes):
        now, then = sma(i), sma(i - REGIME_RISING) if i >= REGIME_RISING else None
        if now is None or then is None:
            out.append((day, UNKNOWN))
        else:
            out.append((day, UP if values[i] > now and now > then else NOT_UP))
    return out


def regime_before(states: Sequence[tuple[date, str]], day: date) -> str:
    """The state of the last index session before `day` (`index_regimes`); `unknown` without."""
    at = bisect_left([d for d, _ in states], day)
    return states[at - 1][1] if at else UNKNOWN


def hold_bucket(days: int) -> str:
    """The `HOLD_BUCKETS` label of a trade held `days` sessions."""
    for label, last in HOLD_BUCKETS:
        if last is None or days <= last:
            return label
    return HOLD_BUCKETS[-1][0]  # pragma: no cover - the last bucket has no end


def group_stats(
    trades: Iterable[TradeRow], key: Callable[[TradeRow], str], order: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """The trades by `key`: count, sum and mean R, win rate; the groups in `order`, then the
    others by name."""
    groups: dict[str, list[Decimal]] = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t.r_multiple)
    names = [n for n in order if n in groups] + sorted(n for n in groups if n not in order)
    out = []
    for name in names:
        rs = groups[name]
        total = sum(rs, _ZERO)
        out.append(
            {
                "key": name,
                "trades": len(rs),
                "sum_r": _r(total),
                "mean_r": _r(total / len(rs)),
                "win_rate": sum(1 for r in rs if r > 0) / len(rs),
            }
        )
    return out


def yearly_returns(
    equity: Sequence[tuple[date, Decimal]], initial: Decimal
) -> dict[int, float | None]:
    """Each calendar year's return: its last mark over the year before's (the first year's over
    `initial`); None after a mark at or below 0."""
    out: dict[int, float | None] = {}
    previous = initial
    last_of: dict[int, Decimal] = {}
    for day, value in equity:
        last_of[day.year] = value
    for year in sorted(last_of):
        value = last_of[year]
        out[year] = None if previous <= 0 else _float(float(value / previous) - 1.0)
        previous = value
    return out


def _benchmark_on(closes: Sequence[tuple[date, Decimal]], days: Sequence[date]) -> list[Decimal]:
    """The index's last close on or before each of `days` (forward-filled); empty when the index
    has no close on or before the first day."""
    dates = [d for d, _ in closes]
    out = []
    for day in days:
        at = bisect_right(dates, day)
        if at == 0:
            return []
        out.append(closes[at - 1][1])
    return out


def _drawdowns(values: Sequence[float]) -> list[float]:
    peak = -math.inf
    out = []
    for v in values:
        peak = max(peak, v)
        out.append(v / peak - 1.0 if peak > 0 else 0.0)
    return out


def _benchmark(
    closes: Sequence[tuple[date, Decimal]], equity: Sequence[tuple[date, Decimal]]
) -> tuple[list[float] | None, dict[str, Any] | None, dict[int, float | None]]:
    """(the index rebased to 1 at the run's first session, its summary, its yearly returns)."""
    days = [d for d, _ in equity]
    on = _benchmark_on(closes, days)
    if not on or on[0] <= 0:
        return None, None, {}
    base = on[0]
    rebased = [float(v / base) for v in on]
    try:
        cagr = _float(bt_metrics.annualized_return(rebased[-1], 1.0, len(rebased)))
    except (OverflowError, ValueError, ZeroDivisionError):
        cagr = None
    summary = {
        "name": BENCHMARK,
        "total_return": _float(rebased[-1] - 1.0),
        "cagr": cagr,
        "max_drawdown": min(_drawdowns(rebased), default=0.0),
    }
    yearly = yearly_returns(list(zip(days, on, strict=True)), base)
    return rebased, summary, yearly


def _funnel(result: BacktestResult) -> dict[str, Any]:
    rejected: dict[str, int] = {}
    for s in result.signals:
        if s.qty == 0:
            reason = s.reason or ""
            rejected[reason] = rejected.get(reason, 0) + 1
    return {
        "watchlisted": len(result.watchlists),
        "skipped": sum(result.skips.values()),
        "skips": dict(sorted(result.skips.items())),
        "signals": len(result.signals),
        "rejected": sum(rejected.values()),
        "rejections": dict(sorted(rejected.items())),
        "entered": sum(1 for s in result.signals if s.qty > 0),
        "trades": len(result.trades),
    }


def _trade_json(t: TradeRow, regime: str) -> dict[str, Any]:
    return {
        "entry_date": t.entry_date.isoformat(),
        "exit_date": t.exit_date.isoformat(),
        "playbook": t.playbook,
        "instrument_id": t.instrument_id,
        "qty": t.qty,
        "entry_price": str(t.entry_price),
        "initial_stop": str(t.initial_stop),
        "exit_reason": t.exit_reason,
        "realized_pnl": str(t.realized_pnl),
        "fees": str(t.fees),
        "r_multiple": _r(t.r_multiple),
        "mfe_r": _r(t.mfe_r),
        "mae_r": _r(t.mae_r),
        "holding_days": t.holding_days,
        "score": None if t.score is None else str(t.score),
        "regime": regime,
        "auction_risk": t.auction_risk,
    }


def build_report(
    result: BacktestResult, initial: Decimal, benchmark: Sequence[tuple[date, Decimal]]
) -> dict[str, Any]:
    """The run's report document (see the module docstring) from its `result`, its initial
    equity and the NIFTY 500 closes (oldest first; empty: no benchmark)."""
    equity = list(result.equity)
    values = [float(v) for _, v in equity]
    drawdown = _drawdowns(values)
    rebased, bench, bench_yearly = _benchmark(benchmark, equity)
    states = index_regimes(benchmark)
    regime_of = {id(t): regime_before(states, t.entry_date) for t in result.trades}
    curve = []
    total = _ZERO
    for t in result.trades:
        total += t.r_multiple
        curve.append({"date": t.exit_date.isoformat(), "r": _r(total)})
    strategy_yearly = yearly_returns(equity, initial)
    return {
        "version": REPORT_VERSION,
        "benchmark": bench,
        "regime_rule": REGIME_RULE,
        "equity": [
            {
                "date": day.isoformat(),
                "equity": str(value),
                "drawdown": dd,
                "benchmark": None if rebased is None else rebased[n],
            }
            for n, ((day, value), dd) in enumerate(zip(equity, drawdown, strict=True))
        ],
        "yearly": [
            {"year": year, "return": value, "benchmark": bench_yearly.get(year)}
            for year, value in strategy_yearly.items()
        ],
        "r_curve": curve,
        "exit_reasons": group_stats(result.trades, lambda t: t.exit_reason),
        "hold_time": group_stats(
            result.trades, lambda t: hold_bucket(t.holding_days), [b for b, _ in HOLD_BUCKETS]
        ),
        "regime": group_stats(result.trades, lambda t: regime_of[id(t)], [UP, NOT_UP, UNKNOWN]),
        "funnel": _funnel(result),
        "trades": [_trade_json(t, regime_of[id(t)]) for t in result.trades],
    }


def write_report(root: Path, run_id: uuid.UUID, report: Mapping[str, Any]) -> None:
    """Write the run's `report.json` in one rename."""
    text = json.dumps(report, separators=(",", ":"))
    atomic_replace(report_path(root, run_id), lambda tmp: tmp.write_text(text, encoding="utf-8"))


def read_report(root: Path, run_id: uuid.UUID) -> dict[str, Any] | None:
    """A run's report document; None when it has none (an older or failed run) or the file is
    not one of this `REPORT_VERSION`."""
    path = report_path(root, run_id)
    if not path.is_file():
        return None
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(found, dict) or found.get("version") != REPORT_VERSION:
        return None
    return found
