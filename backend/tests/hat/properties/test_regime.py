"""PBT regime.* (HAT-PORT-030..034): the 3-day confirmation, the inputs, the classification and
the multiplier table, on the real `app.hat.engine.portfolio.regime`."""

import tempfile
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest
from hat_support import memory_engine
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.data.store import BREADTH_SCHEMA, DATASET_BREADTH, DataStore, IndexRow
from app.hat.engine.portfolio import jobs, regime, service
from app.hat.engine.portfolio.regime import RegimeError
from app.hat.models import HatEvent, HatMarket, HatRegimeState
from app.hat.ops.jobs import JobError

D = Decimal
RANK = {regime.BULL_QUIET: 0, regime.BULL_VOLATILE: 1, regime.CORRECTION: 2, regime.BEAR: 3}
RAWS = st.lists(st.sampled_from(regime.STATES), min_size=1, max_size=30)
LEVEL = st.integers(1, 100_000).map(lambda n: D(n) / 100)
PCT = st.integers(0, 10_000).map(lambda n: D(n) / 100)


def model_confirmed(raws: list[str], days: int = 3) -> list[str]:
    """The confirmed state after each day, written from the rule: the first reading is the
    confirmed state; it then changes to a state only after `days` readings of it in a row."""
    out = [raws[0]]
    for i in range(1, len(raws)):
        window = raws[max(0, i - days + 1) : i + 1]
        if len(window) == days and len(set(window)) == 1 and window[0] != out[-1]:
            out.append(window[0])
        else:
            out.append(out[-1])
    return out


@given(RAWS)
def test_three_day_confirmation(raws: list[str]) -> None:
    """The confirmed state equals the independent model on every prefix; it changes only on the
    third consecutive reading of a different state; and it only ever takes values that were
    read."""
    steps = regime.confirm_sequence(raws)
    assert [s.confirmed for s in steps] == model_confirmed(raws)
    for i in range(1, len(raws)):
        if steps[i].confirmed != steps[i - 1].confirmed:
            assert raws[i] == raws[i - 1] == raws[i - 2] == steps[i].confirmed
            assert i >= 2
    assert {s.confirmed for s in steps} <= set(raws)
    for i in range(2, len(raws)):
        if raws[i] == raws[i - 1] == raws[i - 2]:
            assert steps[i].confirmed == raws[i]  # three equal days always confirm
    # the sequence of days read so far gives the same answer in one go or step by step
    held = None
    for raw, step in zip(raws, steps, strict=True):
        held = regime.confirm_step(held, raw)
        assert held == step


@given(RAWS, st.integers(1, 6))
def test_confirmation_days_parameter(raws: list[str], days: int) -> None:
    """The rule holds for any confirmation length, not only 3."""
    steps = regime.confirm_sequence(raws, days)
    assert [s.confirmed for s in steps] == model_confirmed(raws, days)


@given(st.text(max_size=8))
def test_confirmation_refuses_an_unknown_state(raw: str) -> None:
    """A state outside the four is an error, never confirmed."""
    if raw in regime.STATES:
        return
    with pytest.raises(RegimeError):
        regime.confirm_step(None, raw)


SERIES_LEN = 262
FIRST = date(2025, 1, 1)


@st.composite
def series_pair(draw: st.DrawFn) -> tuple[dict[date, Decimal], dict[date, Decimal]]:
    days = [FIRST + timedelta(days=i) for i in range(SERIES_LEN)]
    closes = draw(st.lists(LEVEL, min_size=SERIES_LEN, max_size=SERIES_LEN))
    vix = draw(
        st.lists(
            st.integers(500, 4000).map(lambda n: D(n) / 100),
            min_size=SERIES_LEN,
            max_size=SERIES_LEN,
        )
    )
    return dict(zip(days, closes, strict=True)), dict(zip(days, vix, strict=True))


@settings(max_examples=40)
@given(series_pair(), PCT, st.integers(0, SERIES_LEN - 252), st.data())
def test_inputs(pair: tuple, breadth: Decimal, back: int, data: st.DataObject) -> None:
    """The inputs of a day read nothing dated after it, the percentile and the SMAs follow their
    definitions, and a missing or unusable input fails instead of being guessed."""
    closes, vix = pair
    day = FIRST + timedelta(days=SERIES_LEN - 1 - back)
    got = regime.compute_inputs(day, closes, vix, breadth)
    # no lookahead: dropping every later value changes nothing, and changing them changes nothing
    upto_c = {d: v for d, v in closes.items() if d <= day}
    upto_v = {d: v for d, v in vix.items() if d <= day}
    assert regime.compute_inputs(day, upto_c, upto_v, breadth) == got
    future = {d: v + 1 for d, v in closes.items() if d > day} | upto_c
    assert regime.compute_inputs(day, future, upto_v, breadth) == got
    # definitions
    last200 = [upto_c[d] for d in sorted(upto_c)][-200:]
    last50 = last200[-50:]
    window = [upto_v[d] for d in sorted(upto_v)][-252:]
    assert got.sma200 == sum(last200, D(0)) / 200
    assert got.sma50 == sum(last50, D(0)) / 50
    assert got.vix == window[-1]
    assert got.vix_pct == D(100) * sum(1 for v in window if v < window[-1]) / 252
    assert 0 <= got.vix_pct < 100
    assert got.breadth_pct == breadth
    # failures
    no_close_today = {d: v for d, v in upto_c.items() if d != day}
    no_vix_today = {d: v for d, v in upto_v.items() if d != day}
    with pytest.raises(RegimeError):
        regime.compute_inputs(day, no_close_today, upto_v, breadth)
    with pytest.raises(RegimeError):
        regime.compute_inputs(day, upto_c, no_vix_today, breadth)
    bad = data.draw(st.sampled_from((None, D(-1), D("100.01"), D("NaN"))))
    with pytest.raises(RegimeError):
        regime.compute_inputs(day, upto_c, upto_v, bad)  # type: ignore[arg-type]
    short = day - timedelta(days=SERIES_LEN - 1 - back - 100)
    short_closes = {d: v for d, v in closes.items() if d <= short}
    short_vix = {d: v for d, v in vix.items() if d <= short}
    with pytest.raises(RegimeError):
        regime.compute_inputs(short, short_closes, short_vix, breadth)


@given(st.lists(LEVEL, min_size=252, max_size=252), LEVEL, LEVEL)
def test_vix_percentile_is_monotone(values: list[Decimal], low: Decimal, high: Decimal) -> None:
    """With the other 251 days fixed, a higher VIX today never lowers its percentile."""
    low, high = min(low, high), max(low, high)
    p_low = regime.vix_percentile([*values[:-1], low])
    p_high = regime.vix_percentile([*values[:-1], high])
    assert p_low is not None
    assert p_high is not None
    assert 0 <= p_low <= p_high < 100


@given(LEVEL, LEVEL, LEVEL, PCT, PCT)
def test_classification_total(
    close: Decimal, sma50: Decimal, sma200: Decimal, vix_pct: Decimal, breadth: Decimal
) -> None:
    """Every input maps to exactly one of the four states, by the precedence Bear, Correction,
    Bull volatile, Bull quiet."""
    state = regime.classify(close, sma50, sma200, vix_pct, breadth)
    assert state in regime.STATES
    if close <= sma200:
        assert state == regime.BEAR
    elif close <= sma50 or breadth < 40:
        assert state == regime.CORRECTION
    elif vix_pct >= 67 or breadth < 50:
        assert state == regime.BULL_VOLATILE
    else:
        assert state == regime.BULL_QUIET


@given(LEVEL, LEVEL, LEVEL, PCT, PCT, LEVEL, PCT, PCT)
def test_classification_is_monotone(
    close: Decimal,
    sma50: Decimal,
    sma200: Decimal,
    vix_pct: Decimal,
    breadth: Decimal,
    lower_close: Decimal,
    higher_vix: Decimal,
    lower_breadth: Decimal,
) -> None:
    """Worse inputs never give a safer state: a lower close, a higher VIX percentile or lower
    breadth moves the state toward Bear or leaves it."""
    base = RANK[regime.classify(close, sma50, sma200, vix_pct, breadth)]
    worse_close = min(close, lower_close)
    assert RANK[regime.classify(worse_close, sma50, sma200, vix_pct, breadth)] >= base
    assert RANK[regime.classify(close, sma50, sma200, max(vix_pct, higher_vix), breadth)] >= base
    assert RANK[regime.classify(close, sma50, sma200, vix_pct, min(breadth, lower_breadth))] >= base


@given(st.sampled_from(regime.STATES), st.text(max_size=6))
def test_multiplier_table(state: str, other: str) -> None:
    """Every state has a multiplier in the table, the default table never rises toward Bear, and
    a state outside the table is an error and not a default."""
    table = regime.DEFAULT_MULTIPLIERS
    assert regime.multiplier_for(state, table) == table[state]
    ordered = [table[s] for s in sorted(regime.STATES, key=RANK.__getitem__)]
    assert ordered == sorted(ordered, reverse=True)
    if other not in regime.STATES:
        with pytest.raises(RegimeError):
            regime.multiplier_for(other, table)


@given(
    st.fixed_dictionaries(
        {s: st.integers(0, 200).map(lambda n: D(n) / 100) for s in regime.STATES}
    ),
    st.integers(0, 100).map(lambda n: D(n) / 100),
    st.integers(1, 50).map(lambda n: D(n) / 100),
    st.integers(50, 150).map(lambda n: D(n) / 100),
)
def test_multiplier_derivation(estimates: dict, shrink: Decimal, lo: Decimal, hi: Decimal) -> None:
    """A derived table is the estimate shrunk toward 1.0 and held within the policy bounds; with
    full shrinkage it is 1.0 (clamped), with none it is the estimate (clamped)."""
    hi = max(hi, lo)
    table = regime.derive_multipliers(estimates, shrink, lo, hi)
    for state, value in table.items():
        assert lo <= value <= hi
        shrunk = estimates[state] + shrink * (1 - estimates[state])
        assert value == min(max(shrunk, lo), hi)
        assert min(estimates[state], D(1)) <= shrunk <= max(estimates[state], D(1))
    assert regime.derive_multipliers(estimates, D(1), lo, hi) == dict.fromkeys(
        regime.STATES, min(max(D(1), lo), hi)
    )
    too_much = D(2)
    with pytest.raises(RegimeError):
        regime.derive_multipliers(estimates, too_much, lo, hi)


HISTORY = 252
JOB_DAYS = 6


def index_row(name: str, close: Decimal) -> IndexRow:
    return IndexRow(name, close, close, close, close)


def write_world(
    store: DataStore, closes: list[Decimal], vix: list[Decimal], breadth: dict[date, float | None]
) -> list[date]:
    days = [FIRST + timedelta(days=i) for i in range(len(closes))]
    for day, close, fear in zip(days, closes, vix, strict=True):
        store.upsert_index(day, [index_row("NIFTY 500", close), index_row("INDIA VIX", fear)])
    for day, value in breadth.items():
        if value is not None:
            table = pa.table(
                {
                    "universe_size": [100],
                    "with_sma50": [100],
                    "above_sma50": [int(value)],
                    "breadth_pct": [value],
                },
                schema=BREADTH_SCHEMA,
            )
            store.write_partition(DATASET_BREADTH, day, "breadth", table)
    return days


@settings(max_examples=4, deadline=None)
@given(
    st.lists(st.integers(1, 2000), min_size=HISTORY + JOB_DAYS, max_size=HISTORY + JOB_DAYS),
    st.lists(st.integers(500, 4000), min_size=HISTORY + JOB_DAYS, max_size=HISTORY + JOB_DAYS),
    st.lists(st.integers(0, 100), min_size=JOB_DAYS, max_size=JOB_DAYS),
)
def test_regime_job(closes: list[int], fear: list[int], breadth: list[int]) -> None:
    """Run nightly over consecutive days, the `regime` job stores one row per day whose raw state
    is the classification of that day's inputs, whose confirmed state follows the 3-day rule, and
    one event per row; a rerun keeps the row; a day without breadth fails with a stable code and
    stores nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        first_job = FIRST + timedelta(days=HISTORY)
        job_days = [first_job + timedelta(days=i) for i in range(JOB_DAYS)]
        day_breadth: dict[date, float | None] = {
            d: float(b) for d, b in zip(job_days, breadth, strict=True)
        }
        missing = job_days[-1] + timedelta(days=1)
        series_closes = [Decimal(c) for c in closes] + [Decimal(closes[-1])]
        series_vix = [Decimal(v) / 100 for v in fear] + [Decimal(fear[-1]) / 100]
        write_world(store, series_closes, series_vix, day_breadth)
        engine = memory_engine()
        with Session(engine) as session:
            session.add(
                HatMarket(
                    code="NSE",
                    timezone="Asia/Kolkata",
                    currency="INR",
                    session_open=time(9, 15),
                    session_close=time(15, 30),
                )
            )
            session.flush()
            raws = []
            for day in job_days:
                jobs.regime_job(session, store, day)
                jobs.regime_job(session, store, day)  # a rerun
                closes_map, vix_map = jobs.read_series(store, day)
                inputs = regime.compute_inputs(
                    day, closes_map, vix_map, Decimal(str(day_breadth[day]))
                )
                raws.append(regime.classify_inputs(inputs))
            rows = session.scalars(select(HatRegimeState).order_by(HatRegimeState.trade_date)).all()
            assert [r.state for r in rows] == raws
            market_id = service.market_id_of(session)
            model = service.ensure_regime_model(session, market_id)
            stored = [service.confirmed_state(session, market_id, model.id, d) for d in job_days]
            assert stored == model_confirmed(raws)
            assert [r.confirmed for r in rows] == [
                r == c for r, c in zip(raws, model_confirmed(raws), strict=True)
            ]
            events = session.scalar(
                select(func.count())
                .select_from(HatEvent)
                .where(HatEvent.event_type == "regime.state_recorded")
            )
            assert events == JOB_DAYS
            with pytest.raises(JobError) as caught:
                jobs.regime_job(session, store, missing)
            assert caught.value.code == "regime_no_breadth"
            assert session.scalar(select(func.count()).select_from(HatRegimeState)) == JOB_DAYS
