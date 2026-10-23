"""PBT dsr.* (HAT-BT-030, 031, 042, 060; HAT-GOV-091; HAT-PORT-061): the deflated
Sharpe ratio of `app.hat.backtest.dsr` against the formula written out with `math.erf`, its
family floors and the probability of backtest overfitting (PR 66), and the trial counting,
family keys, returns files, lifecycle evidence and risk-increase gate evidence of
`app.hat.backtest.runs` on SQLite."""

import math
import statistics
import tempfile
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import combinations, count
from pathlib import Path
from unittest import mock

import pyarrow.parquet as pq
import pytest
from hat_support import memory_engine
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.backtest import runs
from app.hat.backtest.dsr import (
    MIN_TRIALS,
    UNDEFINED,
    VARIANCE_FLOOR,
    ReturnStats,
    deflated_sharpe,
    expected_max_sharpe,
    family_deflated_sharpe,
    pbo,
    return_stats,
    trial_variance,
)
from app.hat.engine.backtest import BacktestResult
from app.hat.engine.portfolio.lifecycle import DEFAULT_DISCOUNT, discounted_benchmark
from app.hat.models import (
    HatBacktestRun,
    HatBot,
    HatMarket,
    HatPosition,
    HatRiskPolicyVersion,
    HatStrategy,
    HatStrategyVersion,
    HatTradeResult,
    HatUniverse,
    HatUniverseVersion,
)
from app.hat.risk.policy import TradeOutcome, build_gate_evidence
from app.hat.risk.policy_service import seed_initial_policy
from app.hat.schema import seed_hat_reference
from app.hat.time import as_utc

ENGINE = memory_engine()
seed_hat_reference(ENGINE)
SR = st.integers(-500, 500).map(lambda n: n / 1000)  # per-period Sharpe ratios
SKEW = st.integers(-300, 300).map(lambda n: n / 100)
EXCESS = st.integers(0, 2000).map(lambda n: n / 100)
T = st.integers(2, 3000)
N = st.integers(1, 10_000)
V = st.integers(0, 1000).map(lambda n: n / 100_000)
START, END = date(2025, 1, 1), date(2025, 12, 31)


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@st.composite
def stats(draw: st.DrawFn, sr: st.SearchStrategy[float] = SR) -> ReturnStats:
    skew = draw(SKEW)
    # a distribution's kurtosis is at least 1 + skew^2
    return ReturnStats(sr=draw(sr), t=draw(T), skew=skew, kurtosis=1 + skew**2 + draw(EXCESS))


def _slope(s: ReturnStats, sr0: float) -> float:
    """The sign of d DSR / d SR at s.sr: 1 - g3 (SR + SR0) / 2 + (g4 - 1) / 4 SR SR0."""
    return 1 - s.skew * (s.sr + sr0) / 2 + (s.kurtosis - 1) / 4 * s.sr * sr0


# --- HAT-BT-042 -------------------------------------------------------------------------------


@given(stats(), N, V, N)
@example(ReturnStats(0.1, 250, 0.0, 3.0), 1, 0.0, 2)
def test_bounds_and_monotonicity(s: ReturnStats, trials: int, variance: float, more: int) -> None:
    """DSR is in [0, 1] and never NaN, and it does not rise with the trial count."""
    got = deflated_sharpe(s, trials, variance)
    assert 0.0 <= got.prob <= 1.0
    assert not math.isnan(got.prob)
    assert deflated_sharpe(s, trials + more, variance).prob <= got.prob + 1e-12


@given(stats(), SR, N, V)
def test_bounds_and_monotonicity_in_sr(s: ReturnStats, other: float, trials: int, v: float) -> None:
    """With everything else fixed, DSR does not fall as SR rises wherever the formula's slope in
    SR is non-negative: 1 - g3 (SR + SR0) / 2 + (g4 - 1) / 4 SR SR0 >= 0 at both ends (the slope
    is linear in SR, so then on the whole interval). Outside that region a higher SR with a
    heavy skew can score lower; that is the formula, not the code (recorded)."""
    low, high = sorted((s.sr, other))
    a, b = ReturnStats(low, s.t, s.skew, s.kurtosis), ReturnStats(high, s.t, s.skew, s.kurtosis)
    sr0 = expected_max_sharpe(trials, v)
    assume(_slope(a, sr0) >= 0 and _slope(b, sr0) >= 0)
    first, second = deflated_sharpe(a, trials, v), deflated_sharpe(b, trials, v)
    assume(first.reason is None and second.reason is None)
    assert first.prob <= second.prob + 1e-12


@given(stats(), st.sampled_from(["one_trial", "no_variance"]), N, V)
def test_bounds_and_monotonicity_first_trial(
    s: ReturnStats, case: str, trials: int, v: float
) -> None:
    """With N = 1 or V = 0, SR0 = 0 and DSR is the probabilistic Sharpe ratio against zero."""
    trials, v = (1, v) if case == "one_trial" else (trials, 0.0)
    got = deflated_sharpe(s, trials, v)
    radicand = 1 - s.skew * s.sr + (s.kurtosis - 1) / 4 * s.sr**2
    assert got.sr0 == 0.0
    if radicand <= 0:
        assert (got.prob, got.reason) == (0.0, UNDEFINED)
    else:
        want = phi(s.sr * math.sqrt(s.t - 1) / math.sqrt(radicand))
        assert math.isclose(got.prob, want, rel_tol=1e-9, abs_tol=1e-12)


@given(
    st.one_of(
        st.lists(st.integers(-50, 50).map(lambda n: n / 1000), max_size=1),
        st.tuples(st.integers(-50, 50), st.integers(2, 30)).map(lambda p: [p[0] / 1000] * p[1]),
        st.lists(st.sampled_from([math.nan, math.inf, 0.01]), min_size=2, max_size=5).filter(
            lambda xs: not all(math.isfinite(x) for x in xs)
        ),
    ),
    N,
    V,
)
def test_bounds_and_monotonicity_undefined(returns: list[float], trials: int, v: float) -> None:
    """Fewer than 2 returns, no variance, or a value that is not finite scores 0 with reason
    `dsr_undefined`, never NaN; so does a non-positive denominator."""
    got = deflated_sharpe(return_stats(returns), trials, v)
    assert (got.prob, got.reason) == (0.0, UNDEFINED)


@given(st.integers(1, 300).map(lambda n: n / 100), st.integers(0, 500), T, N, V)
def test_bounds_and_monotonicity_bad_denominator(
    sr: float, extra: int, t: int, trials: int, v: float
) -> None:
    """A non-positive denominator 1 - g3 SR + (g4 - 1)/4 SR^2 (or one within floating-point
    noise of zero) scores 0, never NaN or 1. Real moments
    keep it a square (g4 >= 1 + g3^2 makes it >= (1 - g3 SR / 2)^2), so this takes moments
    below that bound: g4 = 1 and g3 >= 1 / SR."""
    s = ReturnStats(sr, t, 1 / sr + extra / 100, 1.0)
    got = deflated_sharpe(s, trials, v)
    assert (got.prob, got.reason) == (0.0, UNDEFINED)


# --- HAT-BT-042 / HAT-PORT-061: the family floors and PBO (PR 66) ------------------------------


@given(stats(), N, V)
@example(ReturnStats(0.1, 250, 0.0, 3.0), 1, 0.0)  # the first run of a new strategy
def test_family_floors(s: ReturnStats, trials: int, variance: float) -> None:
    """A family's DSR is the paper's at a trial count of at least `MIN_TRIALS` and a variance of
    at least `VARIANCE_FLOOR`: never above the unfloored DSR, and the first run of a new
    strategy (N = 1) or a family of identical variants (V = 0) is deflated against a positive
    SR0. Harm (N15): a new strategy's first lucky run is scored undeflated and promoted."""
    got = family_deflated_sharpe(s, trials, variance)
    floored = deflated_sharpe(s, max(trials, MIN_TRIALS), max(variance, VARIANCE_FLOOR))
    assert (got.prob, got.sr0, got.reason) == (floored.prob, floored.sr0, floored.reason)
    assert got.sr0 >= expected_max_sharpe(MIN_TRIALS, VARIANCE_FLOOR) > 0
    raw = deflated_sharpe(s, trials, variance)
    assert got.prob <= raw.prob + 1e-12


NOISE = st.lists(st.integers(-40, 40).map(lambda n: n / 1000), min_size=16, max_size=48)
BLOCKS = st.sampled_from([2, 4, 6, 8])


@given(NOISE, st.lists(st.integers(-20, 20), min_size=2, max_size=6, unique=True), BLOCKS)
@example([0.01, -0.01, 0.02, 0.0] * 4, [0, 1], 2)
def test_pbo_ranked_variants(noise: list[float], shifts: list[int], blocks: int) -> None:
    """Variants that differ only by a constant shift of the same returns keep their Sharpe order
    on every half of the sessions, so the in-sample best is the out-of-sample best and PBO is
    0; identical variants are never distinguishable, so PBO is 1 (no evidence of skill). Every
    PBO is in [0, 1] over C(S, S/2) splits. Harm: a family whose best variant is genuinely
    best reads as overfit, or one with nothing to choose between reads as skill."""
    assume(len(noise) >= 2 * blocks)
    cut = [len(noise) * k // blocks for k in range(blocks + 1)]
    # every block varies, so every half has a volatility and the shift orders the Sharpe ratios
    assume(all(len(set(noise[cut[k] : cut[k + 1]])) > 1 for k in range(blocks)))
    shifted = [[x + k / 10_000 for x in noise] for k in shifts]
    found = pbo(shifted, blocks)
    assert found is not None
    assert found.splits == math.comb(blocks, blocks // 2)
    assert found.prob == 0.0
    same = pbo([noise] * len(shifts), blocks)
    assert same is not None
    assert same.prob == 1.0


def _sr(values: list[float]) -> float:
    sd = statistics.pstdev(values)
    return statistics.fmean(values) / sd if sd > 1e-12 else 0.0


@given(NOISE, NOISE, BLOCKS)
@example([0.01, 0.02, 0.03, 0.04, -0.01, -0.02, -0.03, -0.04] * 2, [0.0, 0.01] * 8, 2)
def test_pbo_two_variants(a: list[float], b: list[float], blocks: int) -> None:
    """With two variants, PBO is the share of the splits of the sessions (half the S blocks in
    sample) on which the in-sample winner (the first on a tie) has an out-of-sample Sharpe
    ratio no higher than the other's, counted here split by split. Harm: an in-sample
    winner that loses out of sample is counted as robust."""
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    assume(n >= 2 * blocks)
    cut = [n * k // blocks for k in range(blocks + 1)]
    lost = 0
    halves = list(combinations(range(blocks), blocks // 2))
    for half in halves:
        ins = [i for k in half for i in range(cut[k], cut[k + 1])]
        out = [i for i in range(n) if i not in set(ins)]
        winner, other = (a, b) if _sr([a[i] for i in ins]) >= _sr([b[i] for i in ins]) else (b, a)
        lost += _sr([winner[i] for i in out]) <= _sr([other[i] for i in out])
    found = pbo([a, b], blocks)
    assert found is not None
    assert math.isclose(found.prob, lost / len(halves), abs_tol=1e-12)


@given(st.sampled_from(["one_variant", "odd_blocks", "unequal", "short", "not_finite"]), NOISE)
def test_pbo_refused(case: str, noise: list[float]) -> None:
    """PBO is not computed (None) for fewer than 2 variants, an odd block count, series of
    unequal length, fewer than 2 sessions per block or a value that is not finite. Harm: a
    one-variant family or a short history reads as a measured overfitting probability."""
    matrix, blocks = {
        "one_variant": ([noise], 2),
        "odd_blocks": ([noise, noise], 3),
        "unequal": ([noise, noise[:-1]], 2),
        "short": ([noise, noise], len(noise) // 2 + 2 + len(noise) % 2),
        "not_finite": ([noise, [*noise[:-1], math.inf]], 2),
    }[case]
    if case == "short" and blocks % 2:
        blocks += 1
    assert pbo(matrix, blocks) is None


# --- HAT-BT-030 / 031 / 060: runs on SQLite ---------------------------------------------------


def _world(session: Session, strategies: int, universes: int) -> tuple[list, list]:
    market_id = session.scalars(select(HatMarket.id)).first()  # seeded by seed_hat_reference
    found_s, found_u = [], []
    for n in range(strategies):
        strategy = HatStrategy(user_id=uuid.uuid4(), market_id=market_id, name=f"s{n}", persona="p")
        session.add(strategy)
        session.flush()
        versions = []
        for v in range(2):
            version = HatStrategyVersion(
                strategy_id=strategy.id, version=v + 1, status="draft", config={}, config_hash="h"
            )
            session.add(version)
            versions.append(version)
        found_s.append((strategy, versions))
    for n in range(universes):
        universe = HatUniverse(
            user_id=uuid.uuid4(), market_id=market_id, name=f"u{n}", kind="manual"
        )
        session.add(universe)
        found_u.append(universe)
    session.flush()
    return found_s, found_u


def _start(session: Session, version: HatStrategyVersion, universe: HatUniverse) -> HatBacktestRun:
    return runs.start_run(
        session,
        strategy_version_id=version.id,
        risk_policy_version_id=uuid.uuid4(),
        universe_id=universe.id,
        start=START,
        end=END,
        data_snapshot={},
    )


STEP = st.tuples(st.integers(0, 2), st.integers(0, 1), st.integers(0, 1), st.booleans())


@given(st.lists(STEP, max_size=12))
@settings(max_examples=40, deadline=None)
def test_trial_counting(steps: list[tuple[int, int, int, bool]]) -> None:
    """A family's trial count is the number of runs recorded for it, failed ones included; it
    never falls, and runs of other families never change it."""
    with Session(ENGINE) as session:
        strategies, universes = _world(session, 3, 2)
        keys = {runs.family_key(st_.id, u.id) for st_, _ in strategies for u in universes}
        before = dict.fromkeys(keys, 0)
        for si, vi, ui, failed in steps:
            strategy, versions = strategies[si]
            run = _start(session, versions[vi], universes[ui])
            if failed:
                runs.fail_run(session, run, "backtest_error")
            after = {k: runs.trial_count(session, k) for k in keys}
            for key in keys:
                rows = session.scalar(
                    select(func.count())
                    .select_from(HatBacktestRun)
                    .where(HatBacktestRun.family_key == key)
                )
                assert after[key] == rows
                assert after[key] == before[key] + (key == run.family_key)
            before = after
        session.rollback()


@given(STEP, STEP)
@settings(max_examples=40, deadline=None)
def test_family_key(a: tuple[int, int, int, bool], b: tuple[int, int, int, bool]) -> None:
    """Two runs share a family key iff they share strategy and universe, whatever their version
    or period."""
    with Session(ENGINE) as session:
        strategies, universes = _world(session, 3, 2)
        first = _start(session, strategies[a[0]][1][a[1]], universes[a[2]])
        second = runs.start_run(
            session,
            strategy_version_id=strategies[b[0]][1][b[1]].id,
            risk_policy_version_id=uuid.uuid4(),
            universe_id=universes[b[2]].id,
            start=date(2020, 1, 1),
            end=date(2020, 6, 30),
            data_snapshot={"other": True},
        )
        same = a[0] == b[0] and a[2] == b[2]
        assert (first.family_key == second.family_key) is same
        session.rollback()


def _result(
    sr: float,
    t: int,
    trades: int,
    expectancy: str,
    cooldown: int | None = None,
    promoted: bool | None = True,
    slots: str | None = "time",
    path: bool = True,
    regime_mult: str | None = "1",
) -> BacktestResult:
    metrics: dict[str, object] = {
        "trades": trades,
        "expectancy_r": expectancy,
        "returns": {"sr": sr, "t": t, "skew": 0.0, "kurtosis": 3.0},
    }
    if cooldown is not None:
        metrics["cooldown_sessions"] = cooldown
    if promoted is not None:  # None: a run recorded before PR 62
        metrics["for_promotion"] = promoted
    if slots is not None:  # None: a run recorded before PR 63 (score order)
        metrics["slot_order"] = slots
    if path:  # False: a run recorded before PR 64 (no lower-circuit carry)
        metrics["lower_circuit"] = "carry_to_next_unlocked_open"
    if regime_mult is not None:  # None: a run recorded before PR 68 (sized off the account)
        metrics["regime_mult"] = regime_mult
    return BacktestResult((), (), (), (), (), {}, metrics)


COOLDOWNS = st.sampled_from([None, 0, 3, 5])  # None: a run or a user without the field


@given(COOLDOWNS, COOLDOWNS, st.lists(COOLDOWNS, max_size=5))
@settings(max_examples=30, deadline=None)
@example(0, None, [0, 3])  # a sweep run newer than the run under the policy in force
@example(3, None, [3, 0])  # a run made before the cooldown was raised to 3
@example(0, 3, [0, 3])  # the cooldown was raised after the increase was requested
def test_evidence_cooldown_in_force(
    in_force: int | None, later: int | None, made: list[int | None]
) -> None:
    """The sleeve's evidence is its newest completed run made under the cooldown in force when
    it is read (HAT-RISK-036): now for the lifecycle, the request's instant for the
    risk-increase gate. A `--cooldown-sessions` sweep run, or one made before the cooldown
    changed, is skipped, and a run or a policy without the field counts as 0. With no such run
    there is no evidence. Harm: a gate is passed on a run of a rule that is not live."""
    with Session(ENGINE) as session, tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        strategies, universes = _world(session, 1, 1)
        strategy, (live, _) = strategies[0]
        universe = universes[0]
        uv = HatUniverseVersion(
            universe_id=universe.id, version=1, status="live", definition={}, config_hash="h"
        )
        session.add(uv)
        session.flush()
        user = uuid.uuid4()
        now_cooldown = in_force
        if in_force is not None:
            row = seed_initial_policy(session, user, datetime(2026, 1, 1, tzinfo=UTC))
            row.policy = {**row.policy, "cooldown_sessions": in_force}
            if later is not None:  # in force from 2026-02-01, after the request below
                now_cooldown = later
                session.add(
                    HatRiskPolicyVersion(
                        user_id=user,
                        version=2,
                        policy={**row.policy, "cooldown_sessions": later},
                        direction="increase",
                        requested_at=datetime(2026, 1, 20, tzinfo=UTC),
                        effective_from=datetime(2026, 2, 1, tzinfo=UTC),
                    )
                )
            session.flush()
        bot = _sleeve_bot(session, user, strategy, live.id, uv)
        for n, cooldown in enumerate(made):
            run = _start(session, live, universe)
            result = _result(0.1, 60, n + 1, str(n + 1), cooldown)
            runs.complete_run(session, run, result, root=root)
        want = [n + 1 for n, c in enumerate(made) if (c or 0) == (now_cooldown or 0)]
        facts = runs.backtest_evidence(session, bot)
        assert facts.trades == (want[-1] if want else 0)
        assert (facts.dsr_prob is None) == (not want)
        then = [n + 1 for n, c in enumerate(made) if (c or 0) == (in_force or 0)]
        got = runs.gate_evidence(session, user, datetime(2026, 1, 15, tzinfo=UTC))
        if not then:
            assert got is None
        else:
            benchmark = discounted_benchmark(Decimal(then[-1]), DEFAULT_DISCOUNT)
            assert _numbers(got) == _numbers(build_gate_evidence([], benchmark))
        session.rollback()


FLAGS = st.tuples(
    st.sampled_from([True, False, None]),
    st.sampled_from(["time", "score", None]),
    st.booleans(),
    st.sampled_from(["1", "policy", None]),
)


@given(st.lists(FLAGS, max_size=5))
@settings(max_examples=30, deadline=None)
@example([(True, "time", True, "1"), (False, "time", True, "1")])  # a sensitivity run newer
@example([(None, None, False, None)])  # a run recorded before PR 62
@example([(True, "time", True, "1"), (True, None, False, None)])  # recorded before PR 63
@example([(True, "time", True, "1"), (True, "score", True, "1")])  # a promotion run in score order
@example([(True, "time", True, "1"), (True, "time", False, None)])  # recorded before PR 64
@example([(True, "time", True, "1"), (True, "time", True, None)])  # recorded before PR 68
@example([(True, "time", True, "1"), (True, "time", True, "policy")])  # the regime variant
def test_evidence_promoted_only(
    flags: list[tuple[bool | None, str | None, bool, str | None]],
) -> None:
    """The sleeve's evidence, for the lifecycle and for the risk-increase gate, is its newest
    completed run flagged for promotion (PR 62, O5) whose slots went in the estimated trigger
    order (`slot_order` `time`, PR 63) under the path rules (`lower_circuit`, PR 64), sized by
    the sleeve's allocation at regime rule `1` (`regime_mult`, PR 68): a sensitivity run
    (`touch`, 0 bps, a sweep, another slot order, the regime variant) or one recorded before
    the flag, the slot order, the path rules or the allocation sizing existed is skipped, and
    with none
    there is no evidence. Harm: a sleeve is promoted, or its risk raised, on perfect or
    optimistic fills."""
    with Session(ENGINE) as session, tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        strategies, universes = _world(session, 1, 1)
        strategy, (live, _) = strategies[0]
        universe = universes[0]
        uv = HatUniverseVersion(
            universe_id=universe.id, version=1, status="live", definition={}, config_hash="h"
        )
        session.add(uv)
        session.flush()
        user = uuid.uuid4()
        bot = _sleeve_bot(session, user, strategy, live.id, uv)
        for n, (flag, slots, path, sizing) in enumerate(flags):
            run = _start(session, live, universe)
            result = _result(
                0.1,
                60,
                n + 1,
                str(n + 1),
                promoted=flag,
                slots=slots,
                path=path,
                regime_mult=sizing,
            )
            runs.complete_run(session, run, result, root=root)
        want = [
            n + 1
            for n, (flag, slots, path, sizing) in enumerate(flags)
            if flag is True and slots == "time" and path and sizing == "1"
        ]
        facts = runs.backtest_evidence(session, bot)
        assert facts.trades == (want[-1] if want else 0)
        got = runs.gate_evidence(session, user, datetime(2026, 1, 15, tzinfo=UTC))
        if not want:
            assert got is None
        else:
            benchmark = discounted_benchmark(Decimal(want[-1]), DEFAULT_DISCOUNT)
            assert _numbers(got) == _numbers(build_gate_evidence([], benchmark))
        session.rollback()


@given(st.lists(st.tuples(SR, st.booleans()), min_size=1, max_size=6), st.integers(30, 600))
@settings(max_examples=30, deadline=None)
def test_evidence_current_trials(later: list[tuple[float, bool]], t: int) -> None:
    """The lifecycle reads a sleeve's newest completed run with the DSR deflated by the family's
    trial count and Sharpe variance at evaluation time, not at the run's completion: later
    runs of the family (failed ones too, other versions too) are counted."""
    with Session(ENGINE) as session, tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        strategies, universes = _world(session, 1, 1)
        strategy, versions = strategies[0]
        universe = universes[0]
        live, other = versions
        uv = HatUniverseVersion(
            universe_id=universe.id, version=1, status="live", definition={}, config_hash="h"
        )
        session.add(uv)
        session.flush()
        bot = HatBot(
            user_id=uuid.uuid4(),
            strategy_id=strategy.id,
            live_version_id=live.id,
            broker_account_id=uuid.uuid4(),
            execution_mode="shadow",
            status="active",
            universe_version_id=uv.id,
            lifecycle_state="research",
            risk_cluster="c",
        )
        session.add(bot)
        session.flush()
        assert runs.backtest_evidence(session, bot).dsr_prob is None  # no run: no evidence
        run = _start(session, live, universe)
        runs.complete_run(session, run, _result(0.12, t, 250, "0.31"), root=root)
        stored = Decimal(run.metrics["dsr"]["prob"])
        assert runs.backtest_evidence(session, bot).dsr_prob == stored
        for sr, failed in later:
            extra = _start(session, other, universe)  # another version: same family
            if failed:
                runs.fail_run(session, extra, "backtest_error")
            else:
                runs.complete_run(session, extra, _result(sr, t, 10, "0"), root=root)
        facts = runs.backtest_evidence(session, bot)
        family = run.family_key
        assert facts.trials == runs.trial_count(session, family) == 1 + len(later)
        want = family_deflated_sharpe(
            ReturnStats(0.12, t, 0.0, 3.0),
            facts.trials,
            trial_variance(runs.family_sharpes(session, family)),
        )
        assert facts.dsr_prob == Decimal(repr(want.prob)).quantize(Decimal("0.000001"))
        assert (facts.trades, facts.expectancy) == (250, Decimal("0.31"))
        session.rollback()


# --- HAT-GOV-091: the risk-increase gate's backtest benchmark ---------------------------------

R = st.integers(-300, 500).map(lambda n: Decimal(n) / 100)
SLEEVE = st.tuples(
    # the newest completed run of the live version: (expectancy, trades), or no run
    st.one_of(st.none(), st.tuples(R, st.integers(0, 400))),
    # the sleeve's closed trades: (R, virtual, minutes after the first close)
    st.lists(st.tuples(R, st.booleans(), st.integers(0, 50)), max_size=6),
)
CLOSED = datetime(2025, 6, 2, 10, 0, tzinfo=UTC)


def _sleeve_bot(
    session: Session,
    user: uuid.UUID,
    strategy: HatStrategy,
    live: uuid.UUID,
    universe_version: HatUniverseVersion,
) -> HatBot:
    bot = HatBot(
        user_id=user,
        strategy_id=strategy.id,
        live_version_id=live,
        broker_account_id=uuid.uuid4(),
        execution_mode="shadow",
        status="active",
        universe_version_id=universe_version.id,
        lifecycle_state="research",
        risk_cluster="c",
    )
    session.add(bot)
    session.flush()
    return bot


def _closed_trade(session: Session, bot: HatBot, r: Decimal, virtual: bool, minute: int) -> None:
    position = HatPosition(
        bot_id=bot.id,
        signal_id=uuid.uuid4(),
        instrument_id=uuid.uuid4(),
        is_virtual=virtual,
        qty_open=0,
        avg_entry=Decimal(100),
        initial_stop=Decimal(90),
        current_stop=Decimal(90),
        currency="INR",
        opened_at=CLOSED - timedelta(days=3),
        closed_at=CLOSED + timedelta(minutes=minute),
        state="Closed",
        rules_fired=[],
    )
    session.add(position)
    session.flush()
    session.add(
        HatTradeResult(
            position_id=position.id,
            realized_pnl=r * 10,
            fees=Decimal(0),
            currency="INR",
            r_multiple=r,
            holding_days=3,
            exit_reason="hard_stop",
            mfe_r=Decimal(0),
            mae_r=Decimal(0),
        )
    )


def _numbers(evidence: dict | None) -> dict | None:
    """The evidence with its decimal strings read as numbers (the stored scale may differ)."""
    if evidence is None:
        return None
    return {k: Decimal(v) if isinstance(v, str) else v for k, v in evidence.items()}


@given(
    st.lists(SLEEVE, min_size=1, max_size=3),
    st.integers(0, 100).map(lambda n: Decimal(n) / 100),
    st.one_of(st.none(), st.integers(0, 51)),
)
@example(
    sleeves=[((Decimal(1), 10), [(Decimal(2), False, 0), (Decimal(-1), False, 30)])],
    discount=Decimal("0.5"),
    step=10,
)
@settings(max_examples=30, deadline=None)
def test_gate_evidence(sleeves: list, discount: Decimal, step: int | None) -> None:
    """The risk-increase gate's evidence is `build_gate_evidence` over the trades of the user's
    sleeves closed since the policy version in force at the request took effect (every closed
    trade when none is), in closing order (shadow ones are dropped by it), against the
    discounted trade-weighted mean expectancy of each sleeve's newest completed run of its live
    version. Older runs, runs of other versions, other users' sleeves, trades closed under an
    earlier version and a version still pending are not read; with no such run (or none with
    trades) there is no evidence. Harm (item 9): a second step up rides on the trades that
    already justified the first."""
    with Session(ENGINE) as session, tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        user = uuid.uuid4()
        strategies, universes = _world(session, len(sleeves) + 1, 1)
        universe = universes[0]
        uv = HatUniverseVersion(
            universe_id=universe.id, version=1, status="live", definition={}, config_hash="h"
        )
        session.add(uv)
        session.flush()
        weighted, count = Decimal(0), 0
        trades: list[tuple[datetime, uuid.UUID, Decimal, bool]] = []
        for (strategy, (live, other)), (newest, closed) in zip(strategies, sleeves, strict=False):
            bot = _sleeve_bot(session, user, strategy, live.id, uv)
            if newest is not None:
                expectancy, n = newest
                older = _start(session, live, universe)  # superseded by the newest run
                runs.complete_run(session, older, _result(0.1, 60, 999, "9.99"), root=root)
                run = _start(session, live, universe)
                runs.complete_run(session, run, _result(0.1, 60, n, str(expectancy)), root=root)
                if n > 0:
                    weighted, count = weighted + expectancy * n, count + n
            foreign = _start(session, other, universe)  # another version of the strategy
            runs.complete_run(session, foreign, _result(0.1, 60, 999, "-9.99"), root=root)
            for r, virtual, minute in closed:
                _closed_trade(session, bot, r, virtual, minute)
        # another user's sleeve, with a run and trades, never counts
        stranger_strategy, (stranger_live, _) = strategies[-1]
        stranger = _sleeve_bot(session, uuid.uuid4(), stranger_strategy, stranger_live.id, uv)
        stranger_run = _start(session, stranger_live, universe)
        runs.complete_run(session, stranger_run, _result(0.1, 60, 999, "7.77"), root=root)
        _closed_trade(session, stranger, Decimal(-3), False, 0)
        session.flush()
        rows = session.execute(
            select(HatPosition.closed_at, HatPosition.id, HatTradeResult.r_multiple)
            .join(HatTradeResult, HatTradeResult.position_id == HatPosition.id)
            .join(HatBot, HatBot.id == HatPosition.bot_id)
            .where(HatBot.user_id == user)
        ).all()
        for closed_at, position_id, r in rows:
            virtual = session.get(HatPosition, position_id).is_virtual
            trades.append((as_utc(closed_at), position_id, Decimal(r), bool(virtual)))
        since = None
        if step is not None:  # versions 1 (long before), 2 (in force) and 3 (still pending)
            first = seed_initial_policy(session, user, CLOSED - timedelta(days=10))
            since = CLOSED + timedelta(minutes=step)
            for version, effective in ((2, since), (3, CLOSED + timedelta(hours=2))):
                session.add(
                    HatRiskPolicyVersion(
                        user_id=user,
                        version=version,
                        policy=dict(first.policy),
                        direction="decrease",
                        requested_at=effective - timedelta(days=1),
                        effective_from=effective,
                    )
                )
            session.flush()
        got = runs.gate_evidence(session, user, CLOSED + timedelta(hours=1), discount)
        if count == 0:
            assert got is None
        else:
            counted = [t for t in sorted(trades) if since is None or t[0] >= since]
            outcomes = [TradeOutcome(r, v) for _, _, r, v in counted]
            want = build_gate_evidence(outcomes, discounted_benchmark(weighted / count, discount))
            assert _numbers(got) == _numbers(want)
        session.rollback()


# --- HAT-BT-030 / HAT-BT-060: run-gated DSR, returns files, completion order, PBO (PR 66) ----


def _bot_world(
    session: Session,
) -> tuple[HatBot, HatStrategyVersion, HatStrategyVersion, HatUniverse]:
    strategies, universes = _world(session, 1, 1)
    strategy, (live, other) = strategies[0]
    universe = universes[0]
    uv = HatUniverseVersion(
        universe_id=universe.id, version=1, status="live", definition={}, config_hash="h"
    )
    session.add(uv)
    session.flush()
    return _sleeve_bot(session, uuid.uuid4(), strategy, live.id, uv), live, other, universe


@given(st.lists(SR, max_size=4), SR, st.booleans())
@settings(max_examples=30, deadline=None)
def test_dsr_needs_recorded_run(others: list[float], sr: float, recorded: bool) -> None:
    """A DSR is computed only for a recorded run: `run_dsr` of a run that is not a
    `hat_backtest_run` row is refused `backtest_run_unrecorded`, and `complete_run` refuses it
    too, while a recorded run is scored at its family's trial count and Sharpe variance (its
    own Sharpe ratio included) through the floors. Harm (G33): a variant tried outside the
    recorded runs is scored as evidence without counting as a trial."""
    with Session(ENGINE) as session, tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _, live, _, universe = _bot_world(session)
        for other in others:
            runs.complete_run(
                session, _start(session, live, universe), _result(other, 60, 1, "0"), root=root
            )
        if not recorded:
            stray = HatBacktestRun(
                id=uuid.uuid4(),
                strategy_version_id=live.id,
                risk_policy_version_id=uuid.uuid4(),
                family_key="x",
                period_start=START,
                period_end=END,
                data_snapshot={},
                status=runs.RUNNING,
                metrics=_result(sr, 60, 1, "0").metrics,
            )
            with pytest.raises(runs.RunError, match="backtest_run_unrecorded"):
                runs.run_dsr(session, stray)
            result = _result(sr, 60, 1, "0")
            with pytest.raises(runs.RunError, match="backtest_run_unrecorded"):
                runs.complete_run(session, stray, result, root=root)
            session.rollback()
            return
        run = _start(session, live, universe)
        runs.complete_run(session, run, _result(sr, 60, 1, "0"), root=root)
        got, trials, variance = runs.run_dsr(session, run)
        assert trials == len(others) + 1
        assert variance == trial_variance([*others, sr])
        want = family_deflated_sharpe(ReturnStats(sr, 60, 0.0, 3.0), trials, variance)
        assert (got.prob, got.sr0) == (want.prob, want.sr0)
        assert run.metrics["dsr"]["prob"] == str(
            Decimal(repr(want.prob)).quantize(Decimal("0.000001"))
        )
        session.rollback()


EQUITY = st.lists(st.integers(1, 3_000_000).map(Decimal), min_size=0, max_size=12)


@given(EQUITY, st.integers(1, 3_000_000).map(Decimal))
@settings(max_examples=30, deadline=None)
def test_returns_persisted(curve: list[Decimal], initial: Decimal) -> None:
    """A completed run writes `returns.parquet` next to its trades: one row per session close,
    its equity, and its return against the close before (the first against the initial
    equity), the series the run's return moments were computed from. Harm (G33): PBO and any
    later re-deflation cannot be computed because the returns were never kept."""
    days = [START + timedelta(days=n) for n in range(len(curve))]
    with Session(ENGINE) as session, tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _, live, _, universe = _bot_world(session)
        run = _start(session, live, universe)
        metrics = {**_result(0.1, 60, 1, "0").metrics, "initial_equity": str(initial)}
        result = BacktestResult((), tuple(zip(days, curve, strict=True)), (), (), (), {}, metrics)
        runs.complete_run(session, run, result, root=root)
        table = pq.read_table(runs.returns_path(root, run.id))
        assert table.column("date").to_pylist() == days
        assert table.column("equity").to_pylist() == curve
        points = [float(initial), *(float(v) for v in curve)]
        want = [points[n + 1] / points[n] - 1.0 for n in range(len(curve))]
        assert table.column("ret").to_pylist() == want
        assert runs.read_returns(root, run.id) == dict(zip(days, want, strict=True))
        session.rollback()


@given(st.permutations(range(4)))
@settings(max_examples=24, deadline=None)
@example([3, 2, 1, 0])  # completed in the reverse of the order started
def test_evidence_latest_by_completion(order: list[int]) -> None:
    """The sleeve's evidence is its run that completed last, whatever order the runs started in
    (their uuid7 ids). Harm (G33): a long run started first but finished last is shadowed by
    an older result, or a stale run is read as the newest."""
    clock = count()
    with Session(ENGINE) as session, tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bot, live, _, universe = _bot_world(session)
        started = [_start(session, live, universe) for _ in range(4)]
        with mock.patch.object(
            runs,
            "utc_now",
            side_effect=lambda: datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=next(clock)),
        ):
            for n in order:
                runs.complete_run(session, started[n], _result(0.1, 60, n + 1, "1"), root=root)
        assert runs.backtest_evidence(session, bot).trades == order[-1] + 1
        session.rollback()


@given(st.lists(st.lists(st.integers(-30, 30), min_size=24, max_size=24), min_size=1, max_size=4))
@settings(max_examples=20, deadline=None)
@example([[1, -2, 3, 0] * 6, [1, -2, 3, 0] * 6, [2, 1, -1, -3] * 6])  # a re-run, then a variant
def test_family_pbo_recorded(variants: list[list[int]]) -> None:
    """Each completed run records its family's PBO (`metrics.pbo`) over the completed runs with
    a returns file, itself included, on the sessions they share: `dsr.pbo` of their return
    series in run order with the default blocks, a re-run of an equal series counted once,
    and no probability but `pbo_too_few_variants` while the family has one distinct variant.
    Harm: the overfitting check reads another family's runs, reports a probability from one
    variant, or scores a deterministic re-run as an overfit second variant."""
    days = [START + timedelta(days=n) for n in range(24)]
    with Session(ENGINE) as session, tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _, live, other, universe = _bot_world(session)
        series: list[list[float]] = []
        for n, steps in enumerate(variants):
            curve, value = [], Decimal(1_000_000)
            for step in steps:
                value += step * 100
                curve.append(value)
            metrics = {**_result(0.1, 24, 1, "0").metrics, "initial_equity": "1000000"}
            result = BacktestResult(
                (), tuple(zip(days, curve, strict=True)), (), (), (), {}, metrics
            )
            run = _start(session, live if n % 2 else other, universe)
            runs.complete_run(session, run, result, root=root)
            points = [1_000_000.0, *(float(v) for v in curve)]
            row = [points[i + 1] / points[i] - 1.0 for i in range(24)]
            if row not in series:  # a re-run of the same series is the same variant
                series.append(row)
            got = run.metrics["pbo"]
            assert (got["runs"], got["variants"], got["sessions"]) == (n + 1, len(series), 24)
            if len(series) < 2:
                assert (got["prob"], got["reason"]) == (None, "pbo_too_few_variants")
            else:
                want = pbo(series)
                assert want is not None
                assert got["prob"] == str(Decimal(repr(want.prob)).quantize(Decimal("0.000001")))
                assert got["splits"] == want.splits
        session.rollback()


@given(st.binary(max_size=64))
@settings(max_examples=20, deadline=None)
@example(b"")
def test_family_pbo_skips_unreadable(garbage: bytes) -> None:
    """A sibling run whose returns file cannot be read as one is left out of the family's PBO;
    the run being completed still completes with its DSR and PBO. Harm: one damaged file fails
    every later backtest of the family, so its trials go unscored."""
    days = [START + timedelta(days=n) for n in range(24)]
    with Session(ENGINE) as session, tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _, live, _, universe = _bot_world(session)
        curve = tuple((d, Decimal(1_000_000 + 100 * (n % 5))) for n, d in enumerate(days))
        metrics = {**_result(0.1, 24, 1, "0").metrics, "initial_equity": "1000000"}
        first = _start(session, live, universe)
        runs.complete_run(
            session, first, BacktestResult((), curve, (), (), (), {}, metrics), root=root
        )
        runs.returns_path(root, first.id).write_bytes(garbage)
        second = _start(session, live, universe)
        runs.complete_run(
            session, second, BacktestResult((), curve, (), (), (), {}, metrics), root=root
        )
        assert second.status == runs.COMPLETED
        assert (second.metrics["pbo"]["runs"], second.metrics["pbo"]["reason"]) == (
            1,
            "pbo_too_few_variants",
        )
        session.rollback()
