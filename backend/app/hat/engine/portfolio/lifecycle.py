"""The sleeve lifecycle (05 §6, HAT-PORT-060..065) as pure rules.

Research -> Backtested -> Shadow -> Live; Live -> Probation; Probation -> Live (an owner promotion
only) or Retired; Retired -> Research. Every edge has a guard over an `Evidence` record, and
`guard` is the only place that decides whether a sleeve may move. Regime never appears here: it
cannot change a lifecycle state (HAT-PORT-034). The model is `verification/tla/SleeveLifecycle.tla`.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext

from app.hat.errors import HatError
from app.hat.risk.policy import MIN_LIVE_TRADES, Z_90

RESEARCH = "research"
BACKTESTED = "backtested"
SHADOW = "shadow"
LIVE = "live"
PROBATION = "probation"
RETIRED = "retired"
STATES = (RESEARCH, BACKTESTED, SHADOW, LIVE, PROBATION, RETIRED)

# The allowed edges, and nothing else (HAT-PORT-060).
EDGES: frozenset[tuple[str, str]] = frozenset(
    {
        (RESEARCH, BACKTESTED),
        (BACKTESTED, SHADOW),
        (SHADOW, LIVE),
        (LIVE, PROBATION),
        (PROBATION, LIVE),
        (PROBATION, RETIRED),
        (RETIRED, RESEARCH),
    }
)

DSR_MIN = Decimal("0.95")  # [A] HAT-PORT-061
MIN_BACKTEST_TRADES = 200  # [A] HAT-PORT-061
SHADOW_DAYS = 20  # [A] HAT-PORT-062
PROBATION_MONTHS = 3  # HAT-PORT-064
PROBATION_SHADOW_TRADES = 50  # HAT-PORT-064
DEFAULT_DISCOUNT = Decimal("0.58")  # [OWNER-PENDING] HAT-PORT-065


class LifecycleError(HatError):
    """A transition was refused; `code` names the rule and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- Evidence and the guards ------------------------------------------------------------------


def backtest_gate(dsr_prob: Decimal | None, trades: int) -> bool:
    """Research -> Backtested: deflated Sharpe probability at least 0.95 and at least 200
    trades (HAT-PORT-061). The probability must come from the family's full trial count (09 §3);
    an absent value fails."""
    if dsr_prob is None or isinstance(trades, bool) or not isinstance(trades, int):
        return False
    return dsr_prob.is_finite() and dsr_prob >= DSR_MIN and trades >= MIN_BACKTEST_TRADES


def sessions_after(sessions: Iterable[date], start: date, end: date) -> int:
    """How many distinct trading sessions fall after `start` and up to `end`."""
    return len({d for d in sessions if start < d <= end})


def shadow_gate(sessions: Iterable[date], started_on: date | None, today: date) -> bool:
    """Shadow -> Live: at least 20 trading sessions on `hat_trading_calendar` since the sleeve
    entered Shadow; weekends and holidays are not sessions (HAT-PORT-062)."""
    if started_on is None:
        return False
    return sessions_after(sessions, started_on, today) >= SHADOW_DAYS


def t_quantile_95(df: int) -> Decimal:
    """The 0.95 quantile of Student's t with `df` degrees of freedom (the upper end of a
    two-sided 90% interval), by the Cornish-Fisher expansion about the normal quantile. The
    error is below 1e-6 from 49 degrees of freedom up (CI trades are at least 50)."""
    if isinstance(df, bool) or not isinstance(df, int) or df < 1:
        raise LifecycleError("lifecycle_bad_df")
    z = Z_90
    with localcontext() as ctx:
        ctx.prec = 40
        n = Decimal(df)
        g1 = (z**3 + z) / 4
        g2 = (5 * z**5 + 16 * z**3 + 3 * z) / 96
        g3 = (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / 384
        g4 = (79 * z**9 + 776 * z**7 + 1482 * z**5 - 1920 * z**3 - 945 * z) / 92160
        return z + g1 / n + g2 / n**2 + g3 / n**3 + g4 / n**4


def ci90_upper(r_multiples: Sequence[Decimal]) -> Decimal | None:
    """Upper end of the two-sided 90% Student-t interval of the mean R; None with fewer than 2
    values. [OWNER-PENDING] method, HAT-PORT-063."""
    n = len(r_multiples)
    if n < 2:
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        mean = sum(r_multiples, Decimal(0)) / n
        variance = sum(((r - mean) ** 2 for r in r_multiples), Decimal(0)) / (n - 1)
        return mean + t_quantile_95(n - 1) * (variance / n).sqrt()


def expectancy_trigger(live_r: Sequence[Decimal]) -> bool:
    """After at least 50 live trades, the 90% interval of the mean R lies below zero."""
    if len(live_r) < MIN_LIVE_TRADES:
        return False
    upper = ci90_upper(live_r)
    return upper is not None and upper < 0


def probation_trigger(health_zero: bool, live_r: Sequence[Decimal]) -> bool:
    """Live -> Probation: the health multiplier is 0, or the expectancy rule fires
    (HAT-PORT-063)."""
    return health_zero or expectancy_trigger(live_r)


def discounted_benchmark(
    backtest_expectancy: Decimal, discount: Decimal = DEFAULT_DISCOUNT
) -> Decimal:
    """The backtest expectancy reduced by the decay discount, which live results are compared
    with (HAT-PORT-065). `discount` is in [0, 1]."""
    if not discount.is_finite() or not Decimal(0) <= discount <= Decimal(1):
        raise LifecycleError("lifecycle_bad_discount")
    return backtest_expectancy * (Decimal(1) - discount)


def still_failing(shadow_r: Sequence[Decimal], benchmark: Decimal | None) -> bool:
    """A Probation sleeve is still failing while it has no shadow trade yet, no benchmark is
    known, or the mean R of its shadow trades is below the discounted benchmark.
    [OWNER-PENDING] HAT-PORT-064."""
    if not shadow_r or benchmark is None:
        return True
    return sum(shadow_r, Decimal(0)) / len(shadow_r) < benchmark


def add_months(day: date, months: int) -> date:
    """`day` plus calendar months, the day clamped to the last day of a shorter month."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def retire_gate(entered_on: date | None, today: date, shadow_trades: int, failing: bool) -> bool:
    """Probation -> Retired: still failing after 3 calendar months or after 50 more shadow
    trades in Probation (HAT-PORT-064)."""
    if entered_on is None or not failing:
        return False
    return (
        today >= add_months(entered_on, PROBATION_MONTHS)
        or shadow_trades >= PROBATION_SHADOW_TRADES
    )


@dataclass(frozen=True)
class BacktestFacts:
    """What a sleeve's backtest gives the lifecycle (09 §6, HAT-BT-060): the deflated Sharpe
    probability at the family's current trial count, the run's trade count and expectancy in R,
    and that trial count. The default is no evidence."""

    dsr_prob: Decimal | None = None
    trades: int = 0
    expectancy: Decimal | None = None
    trials: int = 0


@dataclass(frozen=True)
class Evidence:
    """Everything a guard reads. A field that is absent makes the guard that needs it fail."""

    today: date
    dsr_prob: Decimal | None = None
    backtest_trades: int = 0
    backtest_trials: int = 0  # the family's trial count the DSR was deflated with
    sessions: frozenset[date] = field(default_factory=frozenset)
    shadow_started_on: date | None = None
    health_zero: bool = False
    live_r: tuple[Decimal, ...] = ()
    probation_entered_on: date | None = None
    probation_shadow_r: tuple[Decimal, ...] = ()
    backtest_expectancy: Decimal | None = None
    discount: Decimal = DEFAULT_DISCOUNT


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str


def _benchmark(ev: Evidence) -> Decimal | None:
    if ev.backtest_expectancy is None:
        return None
    return discounted_benchmark(ev.backtest_expectancy, ev.discount)


def _gate(ok: bool, reason: str) -> Verdict:
    return Verdict(ok, "ok" if ok else reason)


def _backtest_edge(ev: Evidence) -> Verdict:
    return _gate(backtest_gate(ev.dsr_prob, ev.backtest_trades), "backtest_gate")


def _shadow_edge(ev: Evidence) -> Verdict:
    return _gate(shadow_gate(ev.sessions, ev.shadow_started_on, ev.today), "shadow_days")


def _probation_edge(ev: Evidence) -> Verdict:
    return _gate(probation_trigger(ev.health_zero, ev.live_r), "no_probation_trigger")


def _retire_edge(ev: Evidence) -> Verdict:
    failing = still_failing(ev.probation_shadow_r, _benchmark(ev))
    ok = retire_gate(ev.probation_entered_on, ev.today, len(ev.probation_shadow_r), failing)
    return _gate(ok, "retire_gate")


_EDGE_GUARDS: dict[tuple[str, str], Callable[[Evidence], Verdict]] = {
    (RESEARCH, BACKTESTED): _backtest_edge,
    (SHADOW, LIVE): _shadow_edge,
    (LIVE, PROBATION): _probation_edge,
    (PROBATION, RETIRED): _retire_edge,
}


def guard(current: str, target: str, ev: Evidence, *, by_owner: bool = False) -> Verdict:
    """Whether a sleeve in `current` may move to `target` on the evidence. An edge outside
    `EDGES` is never allowed; Probation -> Live needs `by_owner` and a recovered sleeve."""
    if current not in STATES or target not in STATES:
        return Verdict(False, "unknown_state")
    edge = (current, target)
    if edge not in EDGES:
        return Verdict(False, "no_such_edge")
    if edge == (PROBATION, LIVE):
        if not by_owner:
            return Verdict(False, "owner_promotion_only")
        return _gate(not still_failing(ev.probation_shadow_r, _benchmark(ev)), "still_failing")
    check = _EDGE_GUARDS.get(edge)
    # Backtested -> Shadow starts the paper trading and Retired -> Research restarts the gate:
    # neither has a condition beyond being on the edge.
    return Verdict(True, "ok") if check is None else check(ev)


def routes_as_shadow(state: str) -> bool:
    """Whether a sleeve's signals route as shadow whatever its mode: every state but Live
    (HAT-PORT-063). An unknown state routes as shadow too."""
    return state != LIVE
