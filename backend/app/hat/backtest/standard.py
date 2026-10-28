"""The Backtest Result Standard's pure rules (docs/backtests/result-standard.md): the refusal
codes and the map onto them of every reason the engine refuses an entry with, the buckets, the
trade-list hash and binding, the forward R of a refused entry, and the metric formulas. No I/O;
`folder` assembles and writes a run's files from them.
"""

from __future__ import annotations

import hashlib
import math
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..risk.decision import Reason
from .overlay import INSUFFICIENT_CREDIT
from .spec import BacktestError

__all__ = [
    "BREADTH_BUCKETS",
    "FORMAT_VERSION",
    "LADDER_LEVEL",
    "NOT_A_SIGNAL",
    "REFUSAL_CODES",
    "REFUSAL_OF",
    "Drawdowns",
    "binding",
    "breadth_bucket",
    "cagr_pct",
    "changed_paths",
    "drawdowns",
    "exit_code",
    "fwd_r",
    "max_consecutive_losses",
    "quintile",
    "refusal_code",
    "sessions_held",
    "sharpe",
    "sortino",
    "top_share_pct",
    "trade_list_hash",
]

FORMAT_VERSION = "1.5"
TRADING_DAYS = 252
LADDER_LEVEL = "ladder_level"
REFUSAL_CODES = (
    "max_positions",
    LADDER_LEVEL,
    "industry_cap",
    "size_zero_cash",
    "size_zero_risk",
    "size_below_min",
    "max_chase",
    "stopout_cap",
    "open_risk_cap",
    "regime_off",
    "kill_switch",
    INSUFFICIENT_CREDIT,
)
# Every reason the engine refuses a signal with, onto its code. `max_positions` splits by
# which cap bound (`refusal_code`). The table is the spec's (Refusal codes); a reason missing
# from it fails the run (`backtest_unmapped_refusal`).
REFUSAL_OF: Mapping[str, str] = {
    Reason.KILL_ACCOUNT: "kill_switch",
    Reason.KILL_BOT: "kill_switch",
    Reason.KILL_HEALTH: "kill_switch",
    Reason.BOT_PAUSED: "kill_switch",
    Reason.MARKET_CLOSED: "kill_switch",
    Reason.STALE_FEED: "kill_switch",
    Reason.QUALITY_FAILED: "kill_switch",
    Reason.REGIME_GATE: "regime_off",
    Reason.NO_ALLOCATION: "regime_off",
    Reason.MAX_CHASE: "max_chase",  # the risk check's and the entry filter's
    Reason.ADD_TO_LOSER: "max_positions",
    Reason.POSITION_EXISTS: "max_positions",
    Reason.MAX_POSITIONS: "max_positions",
    Reason.ENTRY_CAP: "max_positions",
    Reason.INDUSTRY_CAP: "industry_cap",
    Reason.STOPOUT_CAP: "stopout_cap",
    Reason.COOLDOWN: "stopout_cap",
    Reason.SIZE_ZERO_CASH: "size_zero_cash",
    Reason.SIZE_ZERO_RISK: "size_zero_risk",
    Reason.SIZE_BELOW_MIN: "size_below_min",
    Reason.SIZE_ZERO_POSITION: "size_zero_risk",
    Reason.SIZE_ZERO_LIQUIDITY: "size_zero_risk",
    Reason.ALLOCATION_EXCEEDS_POLICY: "size_zero_risk",
    Reason.MISSING_INPUT: "size_zero_risk",
    Reason.INVALID_INPUT: "size_zero_risk",
    Reason.STOP_NOT_BELOW_ENTRY: "size_zero_risk",
    Reason.GLOBAL_OPEN_RISK: "open_risk_cap",
    # an overlay run's cash refusal while the sleeve held units (`overlay.refusal_reason`)
    INSUFFICIENT_CREDIT: INSUFFICIENT_CREDIT,
}
# An entry filter that judges the setup, not the account (the stop's width in ADTR): a trigger
# it fails is not a signal, as a stop the playbook's width cap refuses is not.
NOT_A_SIGNAL = frozenset({"adtr_filter"})


def refusal_code(reason: str, open_positions: int, policy_max: int) -> str:
    """The spec's code of a refused signal: `REFUSAL_OF[reason]`, with `max_positions` split
    into `max_positions` (the policy's `max_open_positions` is full) and `ladder_level` (the
    current ladder level's lower cap is). `backtest_unmapped_refusal` for a reason the table
    lacks: the run fails rather than count it under a wrong code."""
    code = REFUSAL_OF.get(reason)
    if code is None:
        raise BacktestError("backtest_unmapped_refusal")
    if reason == Reason.MAX_POSITIONS and open_positions < policy_max:
        return LADDER_LEVEL
    return code


def quintile(value: float, universe: Sequence[float]) -> str:
    """`q1` (least liquid) .. `q5` of `value` within the sorted `universe` it belongs to: its
    mid rank over the count (ties share it), cut at fifths. `q3` for an empty universe."""
    if not universe:
        return "q3"
    rank = (bisect_left(universe, value) + bisect_right(universe, value)) / 2
    return f"q{min(5, int(rank / len(universe) * 5) + 1)}"


# (label, upper bound exclusive; None: no bound) of the market breadth at entry, in %
BREADTH_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("breadth_lt40", 40),
    ("breadth_40_50", 50),
    ("breadth_50_60", 60),
    ("breadth_ge60", None),
)


def breadth_bucket(pct: Decimal | float | None) -> str:
    """The `BREADTH_BUCKETS` label of a breadth %; `breadth_unknown` without one."""
    if pct is None:
        return "breadth_unknown"
    for label, bound in BREADTH_BUCKETS:
        if bound is None or float(pct) < bound:
            return label
    return BREADTH_BUCKETS[-1][0]  # pragma: no cover - the last bucket has no bound


def trade_list_hash(trades: Iterable[tuple[str, date, date]]) -> str:
    """sha256 of the sorted (symbol, entry date, exit date) list: equal for two runs that made
    the same trades, whatever their sizes."""
    rows = sorted(f"{s},{a.isoformat()},{b.isoformat()}" for s, a, b in trades)
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def binding(
    own: Iterable[tuple[str, date, date]], base: Iterable[tuple[str, date, date]] | None
) -> dict[str, object]:
    """The run's `binding` block against its base's trades: the trades in one list and not the
    other (`trades_changed`), its `trade_list_hash`, and `no_op` (the hashes match). Without a
    base, only the hash."""
    mine = list(own)
    digest = trade_list_hash(mine)
    if base is None:
        return {"trades_changed": None, "trade_list_hash": digest, "no_op": None}
    theirs = list(base)
    return {
        "trades_changed": len(set(mine) ^ set(theirs)),
        "trade_list_hash": digest,
        "no_op": digest == trade_list_hash(theirs),
    }


def fwd_r(
    entry: Decimal,
    stop: Decimal,
    path: Sequence[tuple[Decimal | None, Decimal | None]],
    sessions: int,
) -> Decimal | None:
    """The R a refused entry at `entry` with `stop` would hold at the close `sessions` sessions
    after its entry day, `path` being the (low, close) of each session after it in order (a
    session the stock did not trade: no low, the last close): -1 when a low reaches the stop on
    or before that session (the stop fills first, costs excluded), None when `path` ends before
    it, there is no close yet, or the risk is not positive."""
    risk = entry - stop
    if risk <= 0:
        return None
    for n, (low, close) in enumerate(path[:sessions], start=1):
        if low is not None and low <= stop:
            return Decimal(-1)
        if n == sessions:
            return None if close is None else (close - entry) / risk
    return None


def cagr_pct(final: Decimal, start: Decimal, days: int) -> float | None:
    """(final / start) ^ (365.25 / calendar days) - 1, in %; None without a positive start,
    final or span, or when it overflows."""
    if start <= 0 or final <= 0 or days <= 0:
        return None
    try:
        value = (float(final) / float(start)) ** (365.25 / days) - 1.0
    except OverflowError:
        return None
    return value * 100 if math.isfinite(value) else None


def _annualised(mean: float, deviation: float) -> float | None:
    if deviation <= 1e-12:
        return None
    return mean / deviation * math.sqrt(TRADING_DAYS)


def sharpe(returns: Sequence[float], rf_daily: float = 0.0) -> float | None:
    """Annualised Sharpe of daily returns (population deviation, sqrt 252); None without a
    deviation."""
    if len(returns) < 2:
        return None
    excess = [r - rf_daily for r in returns]
    mean = sum(excess) / len(excess)
    deviation = math.sqrt(sum((r - mean) ** 2 for r in excess) / len(excess))
    return _annualised(mean, deviation)


def sortino(returns: Sequence[float], rf_daily: float = 0.0) -> float | None:
    """Annualised Sortino: the mean excess daily return over the downside deviation (the root
    mean square of the returns below 0 over every session); None without a downside."""
    if len(returns) < 2:
        return None
    excess = [r - rf_daily for r in returns]
    mean = sum(excess) / len(excess)
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in excess) / len(excess))
    return _annualised(mean, downside)


@dataclass(frozen=True)
class Drawdowns:
    """A closing-equity curve's drawdowns, in % of the running peak (positive numbers)."""

    max_pct: float
    avg_pct: float | None  # the mean of each episode's deepest point; None without one
    longest_sessions: int
    longest_start: date | None  # the peak the longest episode fell from
    longest_end: date | None  # the session it recovered on, else the curve's last
    longest_recovered: bool | None


def drawdowns(curve: Sequence[tuple[date, Decimal]], start: Decimal) -> Drawdowns:
    """The drawdowns of the daily closes `curve` from a peak starting at `start` (the run's
    capital, dated before the first session). An episode runs from a peak to the first close at
    or above it (recovered) or to the curve's end; its length is in sessions after the peak."""
    peak, peak_day, peak_at = float(start), None, -1
    episodes: list[tuple[float, int, date | None, date, bool]] = []
    depth = 0.0
    for n, (day, value) in enumerate(curve):
        v = float(value)
        if v >= peak:
            if depth > 0:
                episodes.append((depth, n - peak_at, peak_day, day, True))
            peak, peak_day, peak_at, depth = v, day, n, 0.0
        elif peak > 0:
            depth = max(depth, (1 - v / peak) * 100)
    if depth > 0 and curve:
        episodes.append((depth, len(curve) - 1 - peak_at, peak_day, curve[-1][0], False))
    if not episodes:
        return Drawdowns(0.0, None, 0, None, None, None)
    longest = max(episodes, key=lambda e: e[1])
    return Drawdowns(
        max_pct=max(e[0] for e in episodes),
        avg_pct=sum(e[0] for e in episodes) / len(episodes),
        longest_sessions=longest[1],
        longest_start=longest[2] or (curve[0][0] if curve else None),
        longest_end=longest[3],
        longest_recovered=longest[4],
    )


def sessions_held(days: Sequence[date], entry: date, exit_day: date) -> int:
    """The sessions of `days` (sorted) after `entry` up to and including `exit_day`: a trade's
    hold in sessions (0 for a same-day exit)."""
    return max(0, bisect_right(days, exit_day) - bisect_right(days, entry))


def daily_returns(curve: Sequence[tuple[date, Decimal]], start: Decimal) -> dict[date, float]:
    """Each session's return against the close before (the first against `start`), by date; a
    session after a close at or below 0 has none."""
    previous = float(start)
    out: dict[date, float] = {}
    for day, value in curve:
        if previous > 0:
            out[day] = float(value) / previous - 1.0
        previous = float(value)
    return out


def distinct_on_common(series: Sequence[Mapping[date, float]]) -> tuple[list[list[float]], int]:
    """The return series on the sessions every one of them has a return for, a series equal to
    an earlier one's there (a rerun of the same variant) left out, and the common session
    count: what the family's PBO scores."""
    common = sorted(set.intersection(*(set(s) for s in series))) if series else []
    distinct: list[list[float]] = []
    for s in series:
        row = [s[d] for d in common]
        if row not in distinct:
            distinct.append(row)
    return distinct, len(common)


@dataclass(frozen=True)
class LegShare:
    """A sale's share of its trade: gross P&L, fees, net P&L and net R."""

    gross: Decimal
    fees: Decimal
    net: Decimal
    r: Decimal


_MONEY = Decimal("0.0001")
_R = Decimal("0.000001")


def leg_shares(
    legs: Sequence[tuple[int, Decimal, Decimal]],
    entry: Decimal,
    stop: Decimal,
    net: Decimal,
    fees: Decimal,
    r: Decimal,
) -> list[LegShare]:
    """Each sale's (`qty`, `price`, its own fees) share of a trade with net P&L `net`, all fees
    `fees` and net R `r`: the entry's fees and the dividends by shares, the R over the whole
    position's risk. The last sale takes what is left, so the legs sum to the trade exactly."""
    if not legs:
        return []
    shares = sum(q for q, _, _ in legs)
    entry_fees = fees - sum((f for _, _, f in legs), Decimal(0))
    dividends = net - (sum(((p - entry) * q for q, p, _ in legs), Decimal(0)) - fees)
    risk = (entry - stop) * shares
    out: list[LegShare] = []
    for q, price, own in legs[:-1]:
        leg_fees = (own + entry_fees * q / shares).quantize(_MONEY)
        leg_net = ((price - entry) * q + dividends * q / shares - leg_fees).quantize(_MONEY)
        leg_r = (leg_net / risk).quantize(_R) if risk > 0 else Decimal(0)
        out.append(LegShare(leg_net + leg_fees, leg_fees, leg_net, leg_r))
    last_fees = fees - sum((leg.fees for leg in out), Decimal(0))
    last_net = net - sum((leg.net for leg in out), Decimal(0))
    last_r = r - sum((leg.r for leg in out), Decimal(0))
    out.append(LegShare(last_net + last_fees, last_fees, last_net, last_r))
    return out


def max_consecutive_losses(rs: Iterable[Decimal]) -> int:
    """The longest run of trades with net R <= 0, in exit order."""
    longest = run = 0
    for r in rs:
        run = run + 1 if r <= 0 else 0
        longest = max(longest, run)
    return longest


def top_share_pct(pnls: Iterable[Decimal], top: int, total: Decimal) -> float | None:
    """The `top` largest of `pnls` over `total` net P&L, in %; None unless `total` > 0."""
    if total <= 0:
        return None
    best = sorted(pnls, reverse=True)[:top]
    return float(sum(best, Decimal(0)) / total * 100)


# The engine's exit rules onto the spec's exit codes; any other reason is written as the engine
# names it (`demerger`, `delisted`, `early_failure`, `trailing_atr`, ...).
EXIT_OF: Mapping[str, str] = {
    "close_below_sma": "sma50_close",
    "time_stop": "time_stop",
    "max_hold": "time_stop",
    "partial_at_r": "partial_target",
    "partial_after_days": "partial_target",
    "pct_gain_target": "partial_target",
    "end_of_period": "period_end",
}
# The rules that raise the stop, in the order a hard-stop exit is credited to them.
_TRAILS = (
    ("step_trail", "trail_step"),
    ("trailing_swing_low", "trail_swing_low"),
    ("trailing_atr", "trailing_atr"),
    ("move_stop_to_breakeven", "breakeven_stop"),
)


def exit_code(
    reason: str, initial_stop: Decimal, final_stop: Decimal | None, rules_fired: Iterable[str]
) -> str:
    """The spec's exit code of a closed trade: a `hard_stop` exit is `initial_stop` while the
    stop never moved, else the trail that raised it (`trail_step`, `trail_swing_low`, ...);
    the other engine reasons through `EXIT_OF`, or as named."""
    if reason != "hard_stop":
        return EXIT_OF.get(reason, reason)
    if final_stop is None or final_stop <= initial_stop:
        return "initial_stop"
    fired = {rule.split(":", 1)[-1] for rule in rules_fired}
    for rule, code in _TRAILS:
        if rule in fired:
            return code
    return "raised_stop"


def _flat(doc: object, prefix: str = "") -> dict[str, object]:
    """A JSON document's leaves by dotted path (a list item by its index)."""
    if isinstance(doc, Mapping):
        items: Iterable[tuple[str, object]] = ((str(k), v) for k, v in doc.items())
    elif isinstance(doc, list):
        items = ((str(n), v) for n, v in enumerate(doc))
    else:
        return {prefix: doc}
    out: dict[str, object] = {}
    for key, value in items:
        out.update(_flat(value, f"{prefix}.{key}" if prefix else key))
    if not out and prefix:
        out[prefix] = doc  # an empty mapping or list is a value of its own
    return out


def changed_paths(resolved: Mapping[str, object], base: Mapping[str, object]) -> dict[str, object]:
    """The leaves of `resolved` that differ from `base`'s, by dotted path (a path only `base`
    has: None): a variant's `config.overrides` against its base run."""
    mine, theirs = _flat(resolved), _flat(base)
    return {
        path: mine.get(path)
        for path in sorted(set(mine) | set(theirs))
        if mine.get(path) != theirs.get(path) or (path in mine) != (path in theirs)
    }
