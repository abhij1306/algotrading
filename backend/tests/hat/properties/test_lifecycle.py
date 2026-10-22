"""PBT lifecycle.* (HAT-PORT-060..065, HAT-SCH-083): the sleeve lifecycle guards on the real
`app.hat.engine.portfolio.lifecycle` and `service.transition` (SQLite), against rules written
out independently. The state machine itself is proved in verification/tla/SleeveLifecycle.tla."""

import math
import statistics
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hat_portfolio_support import TRADE_DAY, USER, put_regime, seed_world
from hat_support import memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.engine.portfolio import lifecycle, service
from app.hat.engine.portfolio.lifecycle import Evidence, LifecycleError
from app.hat.models import (
    HatAllocationDaily,
    HatBot,
    HatDailyBotSnapshot,
    HatEvent,
    HatPosition,
    HatTradeResult,
)

D = Decimal
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
        dsr_prob=draw(PROB),
        backtest_trades=draw(TRADES),
        sessions=sessions,
        shadow_started_on=draw(st.one_of(st.none(), st.just(today - timedelta(days=45)))),
        health_zero=draw(st.booleans()),
        live_r=tuple(draw(st.lists(R, max_size=60))),
        probation_entered_on=draw(
            st.one_of(st.none(), st.integers(0, 150).map(lambda n: today - timedelta(days=n)))
        ),
        probation_shadow_r=tuple(draw(st.lists(R, max_size=60))),
        backtest_expectancy=draw(st.one_of(st.none(), R)),
    )


def model_allowed(current: str, target: str, ev: Evidence, by_owner: bool) -> bool:
    """The rules of 05 §6 as one table, written without the module's helpers' structure."""
    if (current, target) not in lifecycle.EDGES:
        return False
    if (current, target) == ("research", "backtested"):
        return ev.dsr_prob is not None and ev.dsr_prob >= D("0.95") and ev.backtest_trades >= 200
    if (current, target) == ("shadow", "live"):
        if ev.shadow_started_on is None:
            return False
        return len([d for d in ev.sessions if ev.shadow_started_on < d <= ev.today]) >= 20
    if (current, target) == ("live", "probation"):
        return lifecycle.probation_trigger(ev.health_zero, ev.live_r)
    benchmark = (
        None if ev.backtest_expectancy is None else ev.backtest_expectancy * (1 - ev.discount)
    )
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


@given(st.sampled_from(lifecycle.STATES))
def test_only_live_routes_real(state: str) -> None:
    """Signals route as shadow in every state but Live, whatever the bot's mode."""
    assert lifecycle.routes_as_shadow(state) == (state != "live")


def make_bot(session: Session, state: str) -> uuid.UUID:
    bot = HatBot(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        strategy_id=uuid.uuid4(),
        broker_account_id=uuid.uuid4(),
        execution_mode="shadow",
        status="active",
        lifecycle_state=state,
        risk_cluster="c1",
    )
    session.add(bot)
    session.flush()
    return bot.id


@settings(max_examples=60)
@given(
    st.sampled_from(lifecycle.STATES), st.sampled_from(lifecycle.STATES), evidences(), st.booleans()
)
def test_transition_records_its_event(current: str, target: str, ev: Evidence, owner: bool) -> None:
    """A lifecycle change and its `sleeve.lifecycle_changed` event are written together: an
    allowed move changes the state and adds exactly one event naming from and to; a refused one
    changes nothing and adds nothing."""
    engine = memory_engine()
    with Session(engine) as session:
        bot_id = make_bot(session, current)
        allowed = model_allowed(current, target, ev, owner)
        kwargs = {"actor": "user", "chat_id": 7, "by_owner": True} if owner else {}
        events = select(func.count()).select_from(HatEvent)
        try:
            service.transition(
                session, bot_id, target, ev, now=datetime(2026, 5, 1, tzinfo=UTC), **kwargs
            )
        except LifecycleError:
            assert not allowed
            assert session.get(HatBot, bot_id).lifecycle_state == current
            assert session.scalar(events) == 0
            return
        assert allowed
        assert session.get(HatBot, bot_id).lifecycle_state == target
        rows = session.scalars(select(HatEvent)).all()
        assert len(rows) == 1
        assert rows[0].event_type == "sleeve.lifecycle_changed"
        assert rows[0].payload["from"] == current
        assert rows[0].payload["to"] == target
        assert rows[0].aggregate_id == bot_id


@settings(max_examples=40)
@given(st.integers(1, 10_000_000), st.integers(0, 40))
@example(peak_cents=10_000_000, drop_pct=10)  # the Probation band's edge (D128)
@example(peak_cents=10_000_000, drop_pct=9)
def test_allocation_moves_a_health_zero_sleeve_to_probation(peak_cents: int, drop_pct: int) -> None:
    """In the nightly allocation a live sleeve whose last snapshot is 10% (D128) or more under
    its peak moves to Probation before the weights are written, with its event and weight 0; a
    shallower drawdown leaves it live; a rerun moves nothing again (HAT-PORT-040, HAT-PORT-063)."""
    peak = D(peak_cents) / 100
    equity = peak * (100 - drop_pct) / 100
    engine = memory_engine()
    with Session(engine) as session:
        (bot_id,) = seed_world(session, [("momentum-breakout-ep", "live", "c1")])
        session.add(
            HatDailyBotSnapshot(
                bot_id=bot_id,
                trade_date=TRADE_DAY - timedelta(days=1),
                equity=equity,
                equity_peak=peak,
                open_risk=D(0),
                realized_r=D(0),
                exposure_level="L1",
                currency="INR",
            )
        )
        put_regime(session, "bull_quiet")
        first = service.run_allocation(session, TRADE_DAY, user_id=USER, total_equity=D(100_000))
        again = service.run_allocation(session, TRADE_DAY, user_id=USER, total_equity=D(100_000))
        bot = session.get(HatBot, bot_id)
        row = session.scalars(select(HatAllocationDaily)).one()
        moves = session.scalars(
            select(HatEvent).where(HatEvent.event_type == "sleeve.lifecycle_changed")
        ).all()
        assert again.moved == ()
        if (peak - equity) * 100 >= 10 * peak:
            assert bot.lifecycle_state == "probation"
            assert first.moved == ((bot_id, "probation"),)
            assert (D(row.health_mult), D(row.final_weight), D(row.capital)) == (0, 0, 0)
            assert [(m.payload["from"], m.payload["to"]) for m in moves] == [("live", "probation")]
        else:
            assert bot.lifecycle_state == "live"
            assert first.moved == ()
            assert moves == []
            assert D(row.health_mult) > 0


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
        backtest_expectancy=expectancy,
        probation_shadow_r=results,
        probation_entered_on=START,
    )
    verdict = lifecycle.guard("probation", "live", ev, by_owner=owner)
    assert verdict.allowed is owner
    failing = replace(ev, probation_shadow_r=tuple(benchmark - x - D("0.01") for x in extra))
    assert not lifecycle.guard("probation", "live", failing, by_owner=owner).allowed


@settings(max_examples=30)
@given(st.lists(st.tuples(st.integers(0, 40), R), min_size=1, max_size=15), st.integers(0, 40))
def test_trade_results_ignore_later_trades(trades: list, cutoff: int) -> None:
    """The results a lifecycle decision reads are those closed up to the trade date's close: a
    replay of an earlier date never sees a later trade."""
    engine = memory_engine()
    base = datetime(2026, 3, 1, 10, tzinfo=UTC)
    with Session(engine) as session:
        bot_id = make_bot(session, "live")
        for offset, r in trades:
            position = HatPosition(
                id=uuid.uuid4(),
                bot_id=bot_id,
                signal_id=uuid.uuid4(),
                instrument_id=uuid.uuid4(),
                is_virtual=False,
                qty_open=0,
                avg_entry=D(100),
                initial_stop=D(95),
                current_stop=D(95),
                currency="INR",
                opened_at=base - timedelta(days=1),
                closed_at=base + timedelta(days=offset),
                state="Closed",
                rules_fired={},
            )
            session.add(position)
            session.flush()
            session.add(
                HatTradeResult(
                    position_id=position.id,
                    realized_pnl=r,
                    fees=D(0),
                    currency="INR",
                    r_multiple=r,
                    holding_days=1,
                    exit_reason="stop",
                    mfe_r=D(0),
                    mae_r=D(0),
                )
            )
        session.flush()
        until = base + timedelta(days=cutoff)
        got = service.trade_results(session, bot_id, virtual=False, until=until)
        expected = [r for offset, r in sorted(trades, key=lambda t: t[0]) if offset <= cutoff]
        assert sorted(got) == sorted(expected)
