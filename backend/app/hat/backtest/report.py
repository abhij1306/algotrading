"""What a backtest run reports (HAT-BT-032, HAT-BT-040..042): its rows (watchlists, signals,
fills, trades, sessions) and its metrics; `folder` writes them as the run's result files."""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

import polars as pl

from ..confighash import config_hash
from ..data.demerger_notice import CLOSE_REASON
from ..engine.exits import CLOSE_PHASE_RULES
from ..engine.portfolio.allocator import Allocation
from . import metrics as bt_metrics
from .dsr import ReturnStats, return_stats
from .spec import ENTRY_DAY_LOW, RunSpec, known_gaps

__all__ = [
    "DELISTED",
    "DEMERGER",
    "END_OF_PERIOD",
    "BacktestResult",
    "FillRow",
    "SessionRow",
    "SignalRow",
    "TradeRow",
    "WatchRow",
    "auction_risk_summary",
    "compute_metrics",
    "flag_auction_risk",
]

# The exit reasons the loop itself closes a position with.
END_OF_PERIOD = "end_of_period"
DELISTED = "delisted"
DEMERGER = CLOSE_REASON  # the close before a demerger's ex-date (CA8)
_ZERO = Decimal(0)


@dataclass(frozen=True)
class WatchRow:
    trade_date: date
    playbook: str
    instrument_id: str
    score: Decimal
    pivot: Decimal | None
    planned_stop: Decimal | None


@dataclass(frozen=True)
class SignalRow:
    trade_date: date
    playbook: str
    instrument_id: str
    state: str  # the state right after the decision: Open or Rejected
    reason: str | None
    trigger_price: Decimal
    stop_price: Decimal
    qty: int
    strategy_version_id: uuid.UUID
    risk_policy_version_id: uuid.UUID
    allocation: Allocation | None = None  # the night's allocation the entry was sized from
    # What the result files report of it (`folder`): the planned entry, the book when it fired
    # (ladder level, open positions and their open risk, the last close's equity), the
    # strategy's regime gate that day and the market breadth of the night before, its stock
    # and setup features (`signal_features`), and for a refusal the R it would have reached
    # (`standard.fwd_r`, set when the run ends).
    entry_price: Decimal | None = None
    scan_date: date | None = None  # the night the watchlist row was scanned
    ladder_level: str | None = None
    open_positions: int | None = None
    open_risk: Decimal | None = None
    equity: Decimal | None = None
    regime_on: bool | None = None
    breadth_pct: Decimal | None = None
    features: Mapping[str, Any] = field(default_factory=dict)
    fwd_r_20d: Decimal | None = None
    fwd_r_60d: Decimal | None = None


@dataclass(frozen=True)
class FillRow:
    day: date
    instrument_id: str
    purpose: str
    side: str
    qty: int
    price: Decimal
    fees: Decimal


@dataclass(frozen=True)
class LegRow:
    """One sale of a trade, in order: its session, shares, price, the sale's own fees and the
    exit rule that sold it (`exit_partial` sales by their partial rule)."""

    day: date
    qty: int
    price: Decimal
    fees: Decimal
    rule: str


@dataclass(frozen=True)
class TradeRow:
    entry_date: date
    exit_date: date
    playbook: str
    instrument_id: str
    qty: int
    entry_price: Decimal
    initial_stop: Decimal
    exit_reason: str
    realized_pnl: Decimal
    fees: Decimal
    r_multiple: Decimal
    mfe_r: Decimal
    mae_r: Decimal
    holding_days: int
    strategy_version_id: uuid.UUID
    risk_policy_version_id: uuid.UUID
    score: Decimal | None = None  # the watchlist row's score (PR 65, N10)
    dividends: Decimal = Decimal(0)  # the cash dividends in `realized_pnl` and R (R-DIV)
    # an exit priced at a daily close far from live's pre-15:15 price (`auction_days`): its tags
    auction_risk: str | None = None
    # `qty` is in price-basis shares, adjusted for every later split and bonus; the whole real
    # shares it was on the entry day (`data.real_shares`), None without a bar to read it from
    qty_real: int | None = None
    mfe_date: date | None = None  # the session of the highest high between entry and exit
    exit_price: Decimal | None = None  # the mean sale price over the trade's legs
    final_stop: Decimal | None = None  # the stop when it closed
    rules_fired: tuple[str, ...] = ()  # the exit rule ids that acted on it (`index:rule`)
    legs: tuple[LegRow, ...] = ()  # its sales in order (none: the trade closed without one)


@dataclass(frozen=True)
class SessionRow:
    """One session's close of the account: its mark, cash and positions' value, the open
    positions and their open risk ((entry - stop) x shares, none below 0), the ladder level,
    the regime gate the session traded under and the breadth of the night before."""

    day: date
    equity: Decimal
    cash: Decimal
    position_value: Decimal
    open_positions: int
    open_risk: Decimal
    ladder_level: str
    regime_on: bool | None
    breadth_pct: Decimal | None


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[TradeRow, ...]
    equity: tuple[tuple[date, Decimal], ...]  # mark to market at each session's close
    watchlists: tuple[WatchRow, ...]
    signals: tuple[SignalRow, ...]
    fills: tuple[FillRow, ...]
    skips: Mapping[str, int]  # triggers that never became a signal, by reason
    metrics: dict[str, Any]
    # PR 68 (HAT-BT-014): the positions' share of each session's mark, and the sleeve's
    # allocation each trade date's entries were sized from (None: the night had none)
    invested: tuple[Decimal, ...] = ()
    allocations: Mapping[date, Allocation | None] = field(default_factory=dict)
    sessions: tuple[SessionRow, ...] = ()
    # the gates' pass counts (`loop._Run._funnel`), written to run.json's `funnel`
    funnel: Mapping[str, Any] = field(default_factory=dict)
    # the EP log's rows (`ep_log.EpRecord.row`), one per pre-open candidate-day of the gap scan:
    # ep_candidates.csv; empty without a pre-open playbook
    ep_candidates: tuple[Mapping[str, Any], ...] = ()
    # the ETF overlay's block (run.json `overlay`) and its sessions (overlay.csv, its columns
    # `overlay.OVERLAY_CSV_HEADER`); empty without an overlay
    overlay: Mapping[str, Any] = field(default_factory=dict)
    overlay_rows: tuple[Sequence[str], ...] = ()


def _count_by(values: Iterable[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for value in values:
        found[value] = found.get(value, 0) + 1
    return dict(sorted(found.items()))


def _ratio(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001")))


def _float(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _stats_json(stats: ReturnStats | None) -> dict[str, Any] | None:
    if stats is None:
        return None
    return {"sr": stats.sr, "t": stats.t, "skew": stats.skew, "kurtosis": stats.kurtosis}


# the shares of the trades, best first, that `tail_trimmed_r` drops (HAT-BT-040)
TAIL_TRIM_PCTS = (1, 5, 10)


def _tail_trimmed(rs: Sequence[Decimal]) -> dict[str, str | None]:
    """The mean R without the best ceil(p% of the trades), for each p of `TAIL_TRIM_PCTS`."""
    out: dict[str, str | None] = {}
    for pct in TAIL_TRIM_PCTS:
        mean = bt_metrics.tail_trimmed_mean(rs, -(-len(rs) * pct // 100))
        out[f"top_{pct}pct"] = None if mean is None else _ratio(mean)
    return out


def _cagr(final: Decimal, initial: Decimal, sessions: int) -> float | None:
    """The CAGR over `sessions` (`annualized_return`); None when it is not a finite real: a
    final equity at or below 0, or a short run whose extrapolation overflows."""
    if final <= 0:
        return None
    try:
        return _float(bt_metrics.annualized_return(float(final), float(initial), sessions))
    except OverflowError:
        return None


def compute_metrics(
    trades_: Sequence[TradeRow],
    equity: Sequence[tuple[date, Decimal]],
    spec: RunSpec,
    invested: Sequence[Decimal],
    *,
    signals: Sequence[SignalRow],
    skips: Mapping[str, int],
    ambiguous_days: int,
    lower_circuit_days: int,
    estimated: Iterable[str] | None = None,
    entry_bars: Mapping[int, Mapping[str, int]] | None = None,
) -> dict[str, Any]:
    """The run's metrics (HAT-BT-040, HAT-BT-041, HAT-BT-042): R statistics in Decimal, the
        standard drawdown, return and Sharpe figures from `app.hat.backtest.metrics`, the return
        moments the deflated Sharpe ratio reads, and the robustness figures of PR 65 (exposure, MAR,
        the longest drawdown, the tail-trimmed mean R and the cost share). The DSR itself depends
        on the family's trials and is added when the run is recorded. The percentage figures are of
        the whole account: `pct_basis` names the sleeve weight and regime rule they were sized
        under, and `invested_share` is the mean of `invested`, the positions' share of each
        session's mark (PR 68, HAT-BT-014). Besides: the triggers that never became a signal
    (`skips`), the signals and their rejections by reason, and the ambiguous and lower-circuit days
    the path rules counted (HAT-BT-021, PR 64), the known gaps of what the run `estimated`
    (`spec.known_gaps`) and its entries by year and what decided them (`entry_bars`).
    `folder.build` turns them into the run's files; `evidence.RunMetrics` reads the facts a
    gate needs."""
    rs = [t.r_multiple for t in trades_]
    count = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    total_r = sum(rs, _ZERO)
    curve = [spec.initial_equity, *(value for _, value in equity)]
    floats = pl.Series([float(v) for v in curve], dtype=pl.Float64)
    returns = (floats / floats.shift(1) - 1.0).slice(1)
    drawdown = bt_metrics.max_drawdown(bt_metrics.drawdown_series(floats))
    stats = return_stats([float(r) for r in returns])
    trade_stats = bt_metrics.trade_stats([float(r) for r in rs])
    final = curve[-1]
    net = sum((t.realized_pnl for t in trades_), _ZERO)
    fees = sum((t.fees for t in trades_), _ZERO)
    cagr = _cagr(final, spec.initial_equity, len(equity))
    mar = None if cagr is None else bt_metrics.mar_ratio(cagr, drawdown)
    share = bt_metrics.cost_share(fees, net + fees)  # realized P&L is net of the charges
    held = [(t.entry_date, t.exit_date) for t in trades_]
    used = spec.recorded_conventions()
    share_held = bt_metrics.invested_share(invested)
    return {
        "trades": count,
        "sum_r": _ratio(total_r),
        "expectancy_r": _ratio(total_r / count) if count else None,
        "win_rate": trade_stats.win_rate,
        "avg_win_r": _ratio(sum(wins, _ZERO) / len(wins)) if wins else None,
        "avg_loss_r": _ratio(sum(losses, _ZERO) / len(losses)) if losses else None,
        "avg_mfe_r": _ratio(sum((t.mfe_r for t in trades_), _ZERO) / count) if count else None,
        "avg_mae_r": _ratio(sum((t.mae_r for t in trades_), _ZERO) / count) if count else None,
        "net_pnl": str(net),
        "fees": str(fees),
        "initial_equity": str(spec.initial_equity),
        "final_equity": str(final),
        "total_return": _float(bt_metrics.total_return(float(final), float(spec.initial_equity))),
        "max_drawdown": _float(-drawdown) if drawdown else 0.0,
        "sharpe_annual": _float(bt_metrics.sharpe_mean_std(returns)),
        "returns": _stats_json(stats),
        "sessions": len(equity),
        # the span and held sessions the lifecycle gate reads (PR 69, HAT-PORT-061)
        "first_session": equity[0][0].isoformat() if equity else None,
        "last_session": equity[-1][0].isoformat() if equity else None,
        "held_sessions": bt_metrics.held_sessions([day for day, _ in equity], held),
        "exposure": bt_metrics.exposure([day for day, _ in equity], held),  # HAT-BT-041
        "cagr": cagr,
        "mar": None if mar is None else _float(mar),
        "longest_drawdown_sessions": bt_metrics.longest_drawdown(floats),
        "tail_trimmed_r": _tail_trimmed(rs),
        "cost_share": None if share is None else _ratio(share),
        "invested_share": None if share_held is None else _ratio(share_held),
        "pct_basis": {  # what the percentage figures are of (PR 68)
            "equity": "account",
            "sleeve_weight": used["sleeve_weight"],
            "regime_mult": used["regime_mult"],
        },
        "strategy_version_id": str(spec.strategy_version_id),
        "risk_policy_version_id": str(spec.policy.version_id),
        "cooldown_sessions": spec.policy.policy.cooldown_sessions,
        "known_gaps": known_gaps(spec, estimated),
        # the entries decided on each basis, by year (`loop.BASIS_5M` ...)
        "entry_bars_by_year": {
            str(year): dict(sorted(counts.items()))
            for year, counts in sorted((entry_bars or {}).items())
        },
        "entry_day_low": ENTRY_DAY_LOW,
        **used,
        "conventions": used,  # the block `config_digest` hashes (PR 65)
        "for_promotion": spec.for_promotion,
        "config_digest": config_hash(used),
        "skips": dict(skips),
        "signals": len(signals),
        "rejections": _count_by(r.reason or "" for r in signals if r.qty == 0),
        "ambiguous_days": ambiguous_days,
        "lower_circuit_days": lower_circuit_days,
        "demerger_exits": forced_exits(trades_, DEMERGER),
    }


# The exits priced at the day's close: the close-phase rules, and the forced exits the loop fills
# at the session's close (a demerger, a delisting). A stop fills at its own price; the end of the
# period is a mark, not an exit decision.
CLOSE_PRICED = CLOSE_PHASE_RULES | {DEMERGER, DELISTED}


def flag_auction_risk(
    trades_: Sequence[TradeRow], days: Mapping[date, str]
) -> tuple[TradeRow, ...]:
    """`trades_` with `auction_risk` set on each close-priced exit dated on a flagged session
    (`auction_days.auction_risk_days`: its tags), so the run can discount them."""
    return tuple(
        replace(t, auction_risk=days.get(t.exit_date) if t.exit_reason in CLOSE_PRICED else None)
        for t in trades_
    )


def auction_risk_summary(trades_: Sequence[TradeRow]) -> dict[str, Any]:
    """The run summary's count of flagged exits, in all and by tag, with their sum of R."""
    flagged = [t for t in trades_ if t.auction_risk]
    by_tag: dict[str, int] = {}
    for t in flagged:
        for tag in (t.auction_risk or "").split(","):
            by_tag[tag] = by_tag.get(tag, 0) + 1
    return {
        "rule": "close-priced exits on an F&O monthly expiry or the session before an index"
        " reconstitution takes effect (`auction_days`)",
        "exits": len(flagged),
        "by_tag": dict(sorted(by_tag.items())),
        "sum_r": _ratio(sum((t.r_multiple for t in flagged), _ZERO)),
    }


def forced_exits(trades_: Sequence[TradeRow], reason: str) -> dict[str, Any]:
    """The trades the loop closed for `reason` (the CA8 demerger exits), counted apart: how
    many, their net P&L and their sum of R (both inside the run's totals too)."""
    closed = [t for t in trades_ if t.exit_reason == reason]
    return {
        "trades": len(closed),
        "net_pnl": str(sum((t.realized_pnl for t in closed), _ZERO)),
        "sum_r": _ratio(sum((t.r_multiple for t in closed), _ZERO)),
    }
