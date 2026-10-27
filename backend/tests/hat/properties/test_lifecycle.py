"""PBT lifecycle.*: the sleeve lifecycle guards on the real `app.hat.engine.portfolio.lifecycle`
and `sleeve_service.transition` (SQLite), against rules written out independently. The state
machine itself is proved in verification/tla/SleeveLifecycle.tla."""

import math
import statistics
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from app.hat.backtest.spec import (
    ESTIMATE_GAPS,
    GAP_PLAYBOOKS,
    KNOWN_GAPS,
    REGIME_MULT_POLICY,
)
from app.hat.engine.portfolio import lifecycle
from app.hat.engine.portfolio.lifecycle import BacktestFacts, Evidence, LifecycleError

D = Decimal
EP_PLAYBOOK = "episodic_pivot"
START = date(2026, 3, 2)  # a Monday
DAYS = st.integers(0, 120).map(lambda n: START + timedelta(days=n))
R = st.integers(-300, 400).map(lambda n: D(n) / 100)
TRADES = st.integers(0, 400)
PROB = st.one_of(st.none(), st.integers(0, 110).map(lambda n: D(n) / 100))


@given(PROB, TRADES)
@example(D("0.95"), 200)
@example(D("0.9499"), 200)
@example(D("0.95"), 199)
def test_backtest_gate(dsr: Decimal | None, trades: int) -> None:
    """Research to Backtested needs a deflated Sharpe probability of at least 0.95 and at least
    200 trades; an absent probability fails."""
    assert lifecycle.backtest_gate(dsr, trades) == (
        dsr is not None and dsr >= D("0.95") and trades >= 200
    )


FIRST = date(2016, 2, 29)  # a leap day: its fifth anniversary is 2021-02-28
PLAYBOOKS = st.frozensets(st.sampled_from(["breakout", EP_PLAYBOOK, "pullback"]))


@st.composite
def backtest_facts(draw: st.DrawFn) -> BacktestFacts:
    """Evidence near every boundary of the gate: each condition passes about half the time."""
    first = FIRST + timedelta(days=draw(st.integers(0, 400)))
    anniversary = date(first.year + 5, first.month, 28) + timedelta(days=first.day - 28)
    if first.month == 2 and first.day == 29:
        anniversary = date(first.year + 5, 2, 28)
    last = anniversary + timedelta(days=draw(st.integers(-12, 3)))
    return BacktestFacts(
        dsr_prob=draw(st.sampled_from([None, D("0.9499"), D("0.95"), D("0.99")])),
        trades=draw(st.sampled_from([199, 200, 1000])),
        pbo=draw(st.sampled_from([None, D("0.1"), D("0.4999"), D("0.5")])),
        expectancy=draw(st.one_of(st.none(), R)),
        first_session=draw(st.one_of(st.just(first), st.none())),
        last_session=draw(st.one_of(st.just(last), st.none())),
        ambiguous_days=draw(st.one_of(st.integers(0, 40), st.none())),
        held_sessions=draw(st.integers(0, 300)),
        gapped_playbooks=draw(PLAYBOOKS),
        playbooks=draw(PLAYBOOKS),
        sleeve_weight=draw(st.sampled_from([D("0.5"), D("0.50"), D("0.25"), None])),
        base_weight=draw(st.sampled_from([D("0.50"), D("0.25"), None])),
        regime_mult=draw(st.sampled_from(["policy", "1", None])),
    )


def model_refusal(f: BacktestFacts) -> str | None:
    """And D150 as a list of conditions, written without the module's helpers."""
    if f.dsr_prob is None or f.dsr_prob < D("0.95") or f.trades < 200:
        return "backtest_gate"
    if f.pbo is None or f.pbo >= D("0.5"):
        return "backtest_pbo"
    if f.first_session is None or f.last_session is None:
        return "backtest_span_short"
    try:
        anniversary = f.first_session.replace(year=f.first_session.year + 5)
    except ValueError:  # 29 February
        anniversary = date(f.first_session.year + 5, 2, 28)
    if (anniversary - f.last_session).days > 7:
        return "backtest_span_short"
    if not f.playbooks:
        return "backtest_no_playbooks"
    if f.gapped_playbooks & f.playbooks:
        return "backtest_gap_in_playbook"
    if f.ambiguous_days is None or f.held_sessions == 0 or f.ambiguous_days * 10 > f.held_sessions:
        return "backtest_ambiguous_days"
    if f.sleeve_weight is None or f.base_weight is None or f.sleeve_weight != f.base_weight:
        return "backtest_off_base_weight"
    if f.regime_mult != "policy":
        return "backtest_regime_rule"
    return None


PASSING = BacktestFacts(
    dsr_prob=D("0.95"),
    trades=200,
    pbo=D("0.4999"),
    first_session=date(2018, 1, 1),
    last_session=date(2022, 12, 25),  # 7 days short of five years
    ambiguous_days=10,
    held_sessions=100,  # exactly the cap of 10%
    gapped_playbooks=frozenset({EP_PLAYBOOK}),
    playbooks=frozenset({"breakout"}),
    sleeve_weight=D("0.5"),
    base_weight=D("0.50"),
    regime_mult="policy",
)


@given(backtest_facts())
@example(PASSING)
@example(replace(PASSING, last_session=date(2022, 12, 24)))  # 8 days short
@example(replace(PASSING, first_session=None))
@example(replace(PASSING, playbooks=frozenset({"breakout", EP_PLAYBOOK})))  # S2 before PR 70
@example(replace(PASSING, playbooks=frozenset()))
@example(replace(PASSING, ambiguous_days=11))
@example(replace(PASSING, ambiguous_days=None))
@example(replace(PASSING, ambiguous_days=0, held_sessions=0))
@example(replace(PASSING, sleeve_weight=D("0.25")))
@example(replace(PASSING, base_weight=None))
@example(replace(PASSING, regime_mult="1"))
@example(replace(PASSING, dsr_prob=D("0.9499")))
@example(replace(PASSING, pbo=D("0.5")))
@example(replace(PASSING, pbo=None))
def test_backtest_refusal(facts: BacktestFacts) -> None:
    """Research to Backtested passes only on evidence whose DSR probability is
    at least 0.95 with 200 trades (`backtest_gate`), whose family's PBO is below 0.5
    (`backtest_pbo`, QA6), whose sessions span five years, the last no
    more than 7 days short of the first's fifth anniversary (`backtest_span_short`), whose version's
    playbooks are known (`backtest_no_playbooks`) and whose known gaps name none of them
    (`backtest_gap_in_playbook`), whose ambiguous days are at most 10% of the sessions a trade was
    held (`backtest_ambiguous_days`), and which was sized at the policy's base weight of the sleeve
    now (`backtest_off_base_weight`) under the live regime rule `policy` (`backtest_regime_rule`);
    the first failing condition is the refusal's code, and an absent fact fails its condition. The
    guard gives the same answer. Harm: a sleeve is promoted on a short or optimistic history, on a
    playbook the backtest never traded, or on a sizing live does not use."""
    want = model_refusal(facts)
    assert lifecycle.backtest_refusal(facts) == want
    verdict = lifecycle.guard("research", "backtested", Evidence(today=START, backtest=facts))
    assert (verdict.allowed, verdict.reason) == (want is None, want or "ok")
    # the rule the gate pins is the backtest's, and every gap it maps is a listed one
    assert lifecycle.EVIDENCE_REGIME_RULE == REGIME_MULT_POLICY
    # (or the code stored runs hold from before the episodic pivot was backtested)
    stored = "episodic_pivot_not_backtested"
    assert set(GAP_PLAYBOOKS) <= {*KNOWN_GAPS, *ESTIMATE_GAPS, stored}


@given(st.sets(st.integers(0, 120), max_size=120), st.integers(0, 60), st.integers(0, 100))
def test_shadow_days(offsets: set[int], start_offset: int, length: int) -> None:
    """Shadow to Live needs at least 20 trading sessions after the day the sleeve entered
    Shadow, up to and including today. Days that are not sessions (weekends, holidays) and the
    start day itself do not count; no start day fails."""
    # trading sessions are weekdays: weekends are never in the set the gate counts
    sessions = {d for n in offsets if (d := START + timedelta(days=n)).weekday() < 5}
    started = START + timedelta(days=start_offset)
    today = started + timedelta(days=length)
    counted = sum(1 for d in sessions if started < d <= today)
    assert lifecycle.shadow_gate(sessions, started, today) == (counted >= 20)
    assert not lifecycle.shadow_gate(sessions, None, today)
    assert lifecycle.sessions_after(sessions, started, today) == counted
    # one more session in the window opens the gate exactly when it was at 19
    if counted == 19:
        extra = next(
            (
                d
                for d in (started + timedelta(days=i) for i in range(1, length + 1))
                if d not in sessions
            ),
            None,
        )
        if extra is not None:
            assert lifecycle.shadow_gate(sessions | {extra}, started, today)


@given(
    st.sets(st.integers(0, 120), max_size=120),
    st.sets(st.integers(-10, 130), max_size=120),
    st.integers(0, 60),
    st.integers(0, 100),
)
def test_paper_sessions(offsets: set[int], ran: set[int], start_offset: int, length: int) -> None:
    """A version's trial counts only the sessions a sleeve ran it on paper: published trade dates
    that are calendar sessions after the trial began, up to today. Elapsed sessions alone, or
    publications outside the window or on non-session days, count nothing. Harm: a version no
    sleeve ever ran could be released after 20 idle sessions."""
    sessions = {d for n in offsets if (d := START + timedelta(days=n)).weekday() < 5}
    published = [START + timedelta(days=n) for n in ran]
    started = START + timedelta(days=start_offset)
    today = started + timedelta(days=length)
    want = len({d for d in published if d in sessions and started < d <= today})
    assert lifecycle.paper_sessions(published, sessions, started, today) == want
    assert lifecycle.paper_sessions([], sessions, started, today) == 0
    assert want <= lifecycle.sessions_after(sessions, started, today)


def t_cdf_upper_tail_check(df: int, q: float) -> float:
    """P(0 < T <= q) of Student's t by Simpson's rule on the density (independent of the
    expansion the code uses)."""
    const = math.exp(math.lgamma((df + 1) / 2) - math.lgamma(df / 2)) / math.sqrt(df * math.pi)
    steps = 2000

    def pdf(x: float) -> float:
        return const * (1 + x * x / df) ** (-(df + 1) / 2)

    h = q / steps
    total = pdf(0) + pdf(q)
    for i in range(1, steps):
        total += pdf(i * h) * (4 if i % 2 else 2)
    return total * h / 3


@given(st.integers(49, 600))
def test_t_quantile(df: int) -> None:
    """The 0.95 quantile used for the 90% interval is the one for which the Student-t density
    puts 0.45 between 0 and it, to 1e-5, from 49 degrees of freedom up (the CI needs 50
    trades)."""
    q = float(lifecycle.t_quantile_95(df))
    assert abs(t_cdf_upper_tail_check(df, q) - 0.45) < 1e-5


@given(st.lists(R, min_size=2, max_size=80))
def test_ci_upper_bound(values: list[Decimal]) -> None:
    """The interval's upper end is mean + t x sd / sqrt(n) from the sample; below two values
    there is none."""
    got = lifecycle.ci90_upper(values)
    assert got is not None
    floats = [float(v) for v in values]
    n = len(floats)
    expected = statistics.fmean(floats) + float(lifecycle.t_quantile_95(n - 1)) * (
        statistics.stdev(floats) / math.sqrt(n)
    )
    assert abs(float(got) - expected) < 1e-8
    assert lifecycle.ci90_upper(values[:1]) is None


@given(st.booleans(), st.lists(R, max_size=90))
def test_probation_trigger(health_zero: bool, results: list[Decimal]) -> None:
    """A live sleeve goes to Probation when its health multiplier is 0 or, after at least 50
    live trades, the 90% interval of the mean R lies below zero; with fewer than 50 trades only
    health counts."""
    expect = health_zero
    if len(results) >= 50:
        upper = lifecycle.ci90_upper(results)
        assert upper is not None
        expect = expect or upper < 0
    assert lifecycle.probation_trigger(health_zero, results) == expect
    if len(results) < 50:
        assert lifecycle.expectancy_trigger(results) is False
    if all(r >= 0 for r in results):
        assert lifecycle.expectancy_trigger(results) is False


@given(st.integers(50, 90), st.integers(1, 300), st.integers(0, 40))
def test_probation_trigger_fires_on_clear_losses(n: int, loss_cents: int, noise: int) -> None:
    """A sleeve with a clearly negative mean and a small spread is flagged; the mirror image
    with a positive mean is not."""
    sign = [D(1), D(-1)]
    spread = [D(noise) / 1000 * sign[i % 2] for i in range(n)]
    losing = [-D(loss_cents) / 100 - 1 + s for s in spread]
    winning = [D(loss_cents) / 100 + 1 + s for s in spread]
    assert lifecycle.expectancy_trigger(losing) is True
    assert lifecycle.expectancy_trigger(winning) is False


@given(R, st.integers(0, 100).map(lambda n: D(n) / 100))
def test_discounted_benchmark(expectancy: Decimal, discount: Decimal) -> None:
    """The benchmark is the backtest expectancy reduced by the discount: equal to it with no
    discount, 0 with all of it, and for a positive expectancy between the two; a discount
    outside [0, 1] is refused."""
    got = lifecycle.discounted_benchmark(expectancy, discount)
    assert got == expectancy * (1 - discount)
    assert lifecycle.discounted_benchmark(expectancy, D(0)) == expectancy
    assert lifecycle.discounted_benchmark(expectancy, D(1)) == 0
    if expectancy >= 0:
        assert 0 <= got <= expectancy
    assert lifecycle.discounted_benchmark(expectancy) == expectancy * D("0.42")
    for bad in (D("-0.01"), D("1.01"), D("NaN")):
        with pytest.raises(LifecycleError):
            lifecycle.discounted_benchmark(expectancy, bad)


@given(st.lists(R, max_size=30), st.one_of(st.none(), R))
def test_still_failing(results: list[Decimal], benchmark: Decimal | None) -> None:
    """Failing while there are no shadow trades or no benchmark, or the mean R is below the
    benchmark; recovered otherwise."""
    expect = (not results) or benchmark is None or sum(results, D(0)) / len(results) < benchmark
    assert lifecycle.still_failing(results, benchmark) == expect


@given(DAYS, st.integers(0, 400), st.integers(0, 120), st.booleans())
def test_retire_gate(entered: date, after_days: int, trades: int, failing: bool) -> None:
    """Probation to Retired: still failing and either three calendar months have passed or 50
    shadow trades were made; never while recovered, never without a start date."""
    today = entered + timedelta(days=after_days)
    due = today >= lifecycle.add_months(entered, 3) or trades >= 50
    assert lifecycle.retire_gate(entered, today, trades, failing) == (failing and due)
    assert not lifecycle.retire_gate(None, today, trades, failing)
    assert lifecycle.add_months(entered, 3) > entered


@st.composite
def evidences(draw: st.DrawFn) -> Evidence:
    today = draw(DAYS)
    sessions = frozenset(
        today - timedelta(days=n) for n in draw(st.sets(st.integers(0, 60), max_size=40))
    )
    return Evidence(
        today=today,
        backtest=draw(backtest_facts()),
        sessions=sessions,
        shadow_started_on=draw(st.one_of(st.none(), st.just(today - timedelta(days=45)))),
        health_zero=draw(st.booleans()),
        live_r=tuple(draw(st.lists(R, max_size=60))),
        probation_entered_on=draw(
            st.one_of(st.none(), st.integers(0, 150).map(lambda n: today - timedelta(days=n)))
        ),
        probation_shadow_r=tuple(draw(st.lists(R, max_size=60))),
    )


def model_allowed(current: str, target: str, ev: Evidence, by_owner: bool) -> bool:
    """The rules as one table, written without the module's helpers' structure."""
    if (current, target) not in lifecycle.EDGES:
        return False
    if (current, target) == ("research", "backtested"):
        return model_refusal(ev.backtest) is None
    if (current, target) == ("shadow", "live"):
        if ev.shadow_started_on is None:
            return False
        return len([d for d in ev.sessions if ev.shadow_started_on < d <= ev.today]) >= 20
    if (current, target) == ("live", "probation"):
        return lifecycle.probation_trigger(ev.health_zero, ev.live_r)
    expectancy = ev.backtest.expectancy
    benchmark = None if expectancy is None else expectancy * (1 - ev.discount)
    failing = (
        not ev.probation_shadow_r
        or benchmark is None
        or sum(ev.probation_shadow_r, D(0)) / len(ev.probation_shadow_r) < benchmark
    )
    if (current, target) == ("probation", "live"):
        return by_owner and not failing
    if (current, target) == ("probation", "retired"):
        return lifecycle.retire_gate(
            ev.probation_entered_on, ev.today, len(ev.probation_shadow_r), failing
        )
    return True


@given(
    st.sampled_from(lifecycle.STATES), st.sampled_from(lifecycle.STATES), evidences(), st.booleans()
)
def test_guard_matches_the_rules(current: str, target: str, ev: Evidence, owner: bool) -> None:
    """A move is allowed exactly when it is an edge of the lifecycle and its guard holds on the
    evidence: no skipping Shadow, no return from Retired except to Research, and Probation back
    to Live only by the owner."""
    verdict = lifecycle.guard(current, target, ev, by_owner=owner)
    assert verdict.allowed == model_allowed(current, target, ev, owner)
    if (current, target) not in lifecycle.EDGES:
        assert not verdict.allowed
    if current == "backtested" and target == "live":
        assert not verdict.allowed
    if (current, target) == ("probation", "live") and not owner:
        assert not verdict.allowed


@st.composite
def drifting_results(draw: st.DrawFn) -> list[Decimal]:
    """R multiples around a drawn mean with a small spread, 2 to 90 of them, so that both clear
    losers and clear winners (and every length around 50) are generated."""
    mean = draw(st.integers(-150, 150).map(lambda n: D(n) / 100))
    spread = draw(st.lists(st.integers(-30, 30).map(lambda n: D(n) / 100), min_size=2, max_size=90))
    return [mean + x for x in spread]


@given(drifting_results(), st.booleans())
def test_probation_trigger_by_length(results: list[Decimal], health_zero: bool) -> None:
    """With results clustered around a drawn mean, the trigger equals health 0 or (50 or more
    trades and an upper bound below zero), and a sleeve with fewer than 50 trades is never
    flagged by its results however bad they are."""
    upper = lifecycle.ci90_upper(results)
    expect = health_zero or (len(results) >= 50 and upper is not None and upper < 0)
    assert lifecycle.probation_trigger(health_zero, results) == expect
    if len(results) < 50:
        assert lifecycle.expectancy_trigger(results) is False


@given(
    st.integers(1, 200).map(lambda n: D(n) / 100),
    st.lists(st.integers(0, 300).map(lambda n: D(n) / 100), min_size=1, max_size=20),
    st.booleans(),
)
def test_probation_promotion_is_owner_only(expectancy: Decimal, extra: list, owner: bool) -> None:
    """A Probation sleeve whose shadow results meet the discounted benchmark may go back to Live
    only on the owner's step; anyone else is refused whatever the evidence says."""
    benchmark = lifecycle.discounted_benchmark(expectancy)
    results = tuple(benchmark + x for x in extra)  # every result at or above the benchmark
    ev = Evidence(
        today=START,
        backtest=BacktestFacts(expectancy=expectancy),
        probation_shadow_r=results,
        probation_entered_on=START,
    )
    verdict = lifecycle.guard("probation", "live", ev, by_owner=owner)
    assert verdict.allowed is owner
    failing = replace(ev, probation_shadow_r=tuple(benchmark - x - D("0.01") for x in extra))
    assert not lifecycle.guard("probation", "live", failing, by_owner=owner).allowed
