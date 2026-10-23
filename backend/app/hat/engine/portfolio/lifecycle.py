"""The sleeve lifecycle (HAT-PORT-060..065) as pure rules.

Research -> Backtested -> Shadow -> Live; Live -> Probation; Probation -> Live (an owner promotion
only) or Retired; Retired -> Research. Every edge has a guard over an `Evidence` record, and
`guard` is the only place that decides whether a sleeve may move. Regime never appears here: it
cannot change a lifecycle state (HAT-PORT-034). The model is `verification/tla/SleeveLifecycle.tla`.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
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
MIN_SPAN_YEARS = 5  # D150: the evidence run's sessions span at least five years
# A run asked for whole years ends on the last session, which year-end holidays and a weekend
# can put a few days before the anniversary of the first.
SPAN_SLACK = timedelta(days=7)
MAX_AMBIGUOUS_SHARE = Decimal("0.10")  # [OWNER-PENDING] of the sessions a trade was held
# The regime rule the evidence run sizes under: the live regime engine's, as live sizes
# (`engine.backtest.REGIME_MULT_POLICY`, PR 69).
EVIDENCE_REGIME_RULE = "policy"
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
    trades (HAT-PORT-061). The probability must come from the family's full trial count;
    an absent value fails."""
    if dsr_prob is None or isinstance(trades, bool) or not isinstance(trades, int):
        return False
    return dsr_prob.is_finite() and dsr_prob >= DSR_MIN and trades >= MIN_BACKTEST_TRADES


def span_gate(first: date | None, last: date | None) -> bool:
    """The evidence run's sessions span at least `MIN_SPAN_YEARS` years: its last session is
    no more than `SPAN_SLACK` before the anniversary of its first (D150)."""
    if first is None or last is None:
        return False
    return add_months(first, 12 * MIN_SPAN_YEARS) <= last + SPAN_SLACK


def ambiguous_gate(ambiguous_days: int | None, held_sessions: int) -> bool:
    """Ambiguous days (09 HAT-BT-021) are at most `MAX_AMBIGUOUS_SHARE` of the sessions a
    trade was held; an absent count fails, as does a run that held nothing."""
    if ambiguous_days is None or held_sessions <= 0:
        return False
    return ambiguous_days <= MAX_AMBIGUOUS_SHARE * held_sessions


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
    """What a sleeve's backtest gives the lifecycle (HAT-BT-060): the deflated Sharpe
    probability at the family's current trial count, the run's trade count and expectancy in R,
    and that trial count; since PR 69 also the run's first and last session, its ambiguous days
    and held sessions, the playbooks its known gaps name and those the version enables, and the
    sleeve weight and regime rule it sized under beside the policy's base weight now. The
    default is no evidence."""

    dsr_prob: Decimal | None = None
    trades: int = 0
    expectancy: Decimal | None = None
    trials: int = 0
    first_session: date | None = None
    last_session: date | None = None
    ambiguous_days: int | None = None
    held_sessions: int = 0
    gapped_playbooks: frozenset[str] = frozenset()
    playbooks: frozenset[str] = frozenset()
    sleeve_weight: Decimal | None = None
    base_weight: Decimal | None = None
    regime_mult: str | None = None
    # the playbooks whose trades are not compared with the run (`runs.unbacktested`, PR 40)
    unbacktested: frozenset[str] = frozenset()


def backtest_refusal(facts: BacktestFacts) -> str | None:
    """Research -> Backtested (HAT-PORT-061): why the evidence run fails the gate, the first failing
    condition's code, or None when it passes. The run needs a deflated Sharpe probability of at
    least 0.95 and 200 trades (`backtest_gate`), sessions spanning five years
    (`backtest_span_short`), the version's playbooks known (`backtest_no_playbooks`: its config does
    not parse) and no known gap naming one of them (`backtest_gap_in_playbook`), ambiguous days
    within the cap (`backtest_ambiguous_days`), and sizing as live: the policy's base weight of the
    sleeve now (`backtest_off_base_weight`) under the live regime rule (`backtest_regime_rule`). An
    absent fact fails its condition."""
    if not backtest_gate(facts.dsr_prob, facts.trades):
        return "backtest_gate"
    if not span_gate(facts.first_session, facts.last_session):
        return "backtest_span_short"
    if not facts.playbooks:
        return "backtest_no_playbooks"
    if facts.gapped_playbooks & facts.playbooks:
        return "backtest_gap_in_playbook"
    if not ambiguous_gate(facts.ambiguous_days, facts.held_sessions):
        return "backtest_ambiguous_days"
    if (
        facts.sleeve_weight is None
        or facts.base_weight is None
        or facts.sleeve_weight != facts.base_weight
    ):
        return "backtest_off_base_weight"
    if facts.regime_mult != EVIDENCE_REGIME_RULE:
        return "backtest_regime_rule"
    return None


@dataclass(frozen=True)
class Evidence:
    """Everything a guard reads. A field that is absent makes the guard that needs it fail."""

    today: date
    backtest: BacktestFacts = field(default_factory=BacktestFacts)
    sessions: frozenset[date] = field(default_factory=frozenset)
    shadow_started_on: date | None = None
    health_zero: bool = False
    live_r: tuple[Decimal, ...] = ()
    probation_entered_on: date | None = None
    probation_shadow_r: tuple[Decimal, ...] = ()
    discount: Decimal = DEFAULT_DISCOUNT


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str


def _benchmark(ev: Evidence) -> Decimal | None:
    if ev.backtest.expectancy is None:
        return None
    return discounted_benchmark(ev.backtest.expectancy, ev.discount)


def _gate(ok: bool, reason: str) -> Verdict:
    return Verdict(ok, "ok" if ok else reason)


def _backtest_edge(ev: Evidence) -> Verdict:
    reason = backtest_refusal(ev.backtest)
    return Verdict(reason is None, reason or "ok")


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
