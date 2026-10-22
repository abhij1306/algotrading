"""PBT dsr.* (09 §3, §4, §6; HAT-BT-030, 031, 042, 060; HAT-GOV-091): the deflated Sharpe ratio
of `app.hat.backtest.dsr` against the formula written out with `math.erf`, and the trial
counting, family keys, lifecycle evidence and risk-increase gate evidence of
`app.hat.backtest.runs` on SQLite."""

import math
import tempfile
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from hat_support import memory_engine
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.backtest import runs
from app.hat.backtest.dsr import (
    UNDEFINED,
    ReturnStats,
    deflated_sharpe,
    expected_max_sharpe,
    return_stats,
)
from app.hat.engine.backtest import BacktestResult
from app.hat.engine.portfolio.lifecycle import discounted_benchmark
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
    """The sign of d DSR / d SR at s.sr (09 §4): 1 - g3 (SR + SR0) / 2 + (g4 - 1) / 4 SR SR0."""
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
    heavy skew can score lower; that is the formula, not the code (recorded in 09 §4)."""
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
    sr: float, t: int, trades: int, expectancy: str, cooldown: int | None = None
) -> BacktestResult:
    metrics: dict[str, object] = {
        "trades": trades,
        "expectancy_r": expectancy,
        "returns": {"sr": sr, "t": t, "skew": 0.0, "kurtosis": 3.0},
    }
    if cooldown is not None:
        metrics["cooldown_sessions"] = cooldown
    return BacktestResult((), (), (), (), (), {}, metrics)


COOLDOWNS = st.sampled_from([None, 0, 3, 5])  # None: a run or a user without the field


@given(COOLDOWNS, st.lists(COOLDOWNS, max_size=5))
@settings(max_examples=30, deadline=None)
@example(0, [0, 3])  # a sweep run newer than the run under the policy in force
@example(3, [3, 0])  # a run made before the cooldown was raised to 3
def test_evidence_cooldown_in_force(in_force: int | None, made: list[int | None]) -> None:
    """The sleeve's evidence is its newest completed run made under the cooldown in force
    (HAT-RISK-036): a `--cooldown-sessions` sweep run, or one made before the cooldown changed,
    is skipped, and a run or a policy without the field counts as 0. With no such run there is
    no evidence. Harm: Research to Backtested is gated on a run of a rule that is not live."""
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
        if in_force is not None:
            row = seed_initial_policy(session, user, datetime(2026, 1, 1, tzinfo=UTC))
            row.policy = {**row.policy, "cooldown_sessions": in_force}
            session.flush()
        bot = _sleeve_bot(session, user, strategy, live.id, uv)
        for n, cooldown in enumerate(made):
            run = _start(session, live, universe)
            runs.complete_run(session, run, _result(0.1, 60, n + 1, "0.5", cooldown), root=root)
        want = [n + 1 for n, c in enumerate(made) if (c or 0) == (in_force or 0)]
        facts = runs.backtest_evidence(session, bot)
        assert facts.trades == (want[-1] if want else 0)
        assert (facts.dsr_prob is None) == (not want)


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
        want = runs.deflate(
            ReturnStats(0.12, t, 0.0, 3.0), facts.trials, runs.family_sharpes(session, family)
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
