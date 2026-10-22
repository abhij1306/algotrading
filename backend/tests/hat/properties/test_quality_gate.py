"""PBT quality_gate.* (HAT-DATA-060..065, HAT-DEC-I14): the nightly quality gate's four checks
(coverage, OHLC, big moves, index snapshot), the rows and events it writes, and the health kill
it trips and clears. Pure checks plus the real ingest and gate over an in-memory database and a
temporary store; no network."""

import tempfile
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

from hat_ingest_support import (
    TRADE_DAY,
    Quote,
    World,
    day_at,
    make_world,
    store_previous,
    symbols_of,
)
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from app.hat.data.ingest import ingest_body, outcome_recorder
from app.hat.data.quality import (
    CHECK_NAMES,
    JOB_QUALITY,
    big_moves_check,
    coverage_check,
    excluded_for,
    gate_body,
    index_snapshot_check,
    scan_universe,
)
from app.hat.models import HatCorporateAction, HatEvent, HatJobRun, HatQualityCheck
from app.hat.ops.jobs import run_job
from app.hat.risk.controls import kill_rows, sync_health
from app.hat.risk.kill import TRIPPED, current_state

DAY_BEFORE = TRADE_DAY - timedelta(days=1)


def _ingest(world: World) -> None:
    holder: dict = {}
    run_job(
        "ingest.nightly",
        TRADE_DAY,
        ingest_body(world.deps(), TRADE_DAY, holder),
        session_factory=world.factory,
        on_finish=outcome_recorder(holder, TRADE_DAY),
    )


def _gate(world: World, excluded: tuple[str, ...] = ()) -> bool:
    """Run the gate job; True when it passed."""
    out = run_job(
        JOB_QUALITY,
        TRADE_DAY,
        gate_body(
            session_factory=world.factory,
            store=world.store,
            trade_date=TRADE_DAY,
            notifier=world.notifier,
            excluded=excluded,
        ),
        session_factory=world.factory,
    )
    return out.status == "succeeded"


def _rows(world: World) -> dict[str, str]:
    with world.factory() as session:
        return {r.check_name: r.status for r in session.scalars(select(HatQualityCheck))}


def _health_tripped(world: World) -> bool:
    with world.factory() as session:
        return current_state(kill_rows(session), "account", None, "health") == TRIPPED


@given(universe=st.integers(0, 6000), stored=st.integers(0, 6000))
@example(universe=100, stored=99)
@example(universe=100, stored=98)
@example(universe=3121, stored=3090)
@example(universe=3121, stored=3089)
def test_coverage_threshold(universe: int, stored: int) -> None:
    """For every universe and stored count, coverage passes iff stored / universe is at least
    0.99 by exact fractions (100 of 100 and 99 of 100 pass, 98 of 100 fails); an empty universe
    never passes."""
    expected = universe >= 1 and Fraction(stored, universe) >= Fraction(99, 100)
    assert coverage_check(universe, stored).passed == expected


@settings(max_examples=12)
@given(bad=st.sets(st.integers(0, 7), max_size=7), n=st.integers(8, 8))
@example(bad=set(), n=8)
@example(bad={3}, n=8)
def test_ohlc_zero_tolerance(bad: set[int], n: int) -> None:
    """For every set of fetched rows that break a row check (a negative volume), the gate's ohlc
    check fails iff the set is not empty: one bad row fails the night."""
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, symbols_of(n))
        for i in bad:
            world.nse.quotes[f"S{i:04d}"] = Quote(volume=-5)
        _ingest(world)
        _gate(world)
        assert (_rows(world)["ohlc"] == "FAIL") == bool(bad)


@settings(max_examples=40)
@given(
    moved=st.sets(st.integers(0, 9)),
    explained=st.sets(st.integers(0, 9)),
    excluded=st.sets(st.integers(0, 9)),
    universe=st.sets(st.integers(0, 9)),
)
def test_big_moves_explained(
    moved: set[int], explained: set[int], excluded: set[int], universe: set[int]
) -> None:
    """For every set of instruments that moved over 20%, those with a corporate action on the day
    and those excluded, the check fails iff some mover is neither explained nor excluded; and a
    scan sees the universe less the excluded, never an excluded one."""
    check = big_moves_check(moved, explained, excluded)
    assert check.passed == (moved <= (explained | excluded))
    assert set(check.detail["unexplained"]) == {str(k) for k in moved - explained - excluded}
    scanned = scan_universe(sorted(universe), excluded)
    assert set(scanned) == universe - excluded
    assert not set(scanned) & excluded


@settings(max_examples=12)
@given(
    movers=st.sets(st.integers(0, 5)),
    explained=st.sets(st.integers(0, 5)),
    excluded=st.sets(st.integers(0, 5)),
)
def test_big_moves_end_to_end(movers: set[int], explained: set[int], excluded: set[int]) -> None:
    """The same rule over stored bars and corporate actions: the gate's big_moves check fails iff
    a mover has no corporate action on the trade date and is not excluded; excluded movers are
    recorded for the day's scans."""
    names = [s for s, _, _ in symbols_of(6)]
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, symbols_of(6))
        store_previous(world, dict.fromkeys(names, Decimal(100)), DAY_BEFORE)
        for i in movers:
            world.nse.quotes[names[i]] = Quote(close=Decimal(150), prev_close=Decimal(100))
        with world.factory() as session:
            for i in explained:
                session.add(
                    HatCorporateAction(
                        instrument_id=world.ids[f"{names[i]}:EQ"],
                        ex_date=TRADE_DAY,
                        action_type="split",
                        adjustment_factor=Decimal("0.5"),
                        raw_text="split",
                        source="nse_mcp",
                    )
                )
            session.commit()
        ids = [str(world.ids[f"{names[i]}:EQ"]) for i in sorted(excluded)]
        _ingest(world)
        _gate(world, tuple(ids))
        with world.factory() as session:
            skipped = excluded_for(session, TRADE_DAY)
    assert (_rows(world)["big_moves"] == "PASS") == (movers <= (explained | excluded))
    assert skipped == {i for i in ids if i in {str(world.ids[f"{names[k]}:EQ"]) for k in movers}}


@given(
    names=st.lists(
        st.sampled_from(
            ["NIFTY 50", "NIFTY 500", "Nifty 500", "INDIA VIX", "India Vix", "NIFTY BANK"]
        ),
        max_size=6,
    )
)
def test_index_snapshot_present(names: list[str]) -> None:
    """For every set of stored index names, the check passes iff NIFTY 500 and India VIX are both
    in it, whatever the case."""
    upper = {n.upper() for n in names}
    assert index_snapshot_check(names).passed == ({"NIFTY 500", "INDIA VIX"} <= upper)


@settings(max_examples=12)
@given(
    kind=st.sampled_from(["good", "bad_row", "no_vix", "unexplained_move"]),
    tripped_before=st.booleans(),
    retries=st.integers(min_value=0, max_value=2),
)
@example(kind="good", tripped_before=True, retries=0)
@example(kind="bad_row", tripped_before=False, retries=2)
def test_rows_written(kind: str, tripped_before: bool, retries: int) -> None:
    """For every night's data (good, a bad row, no India VIX stored, an unexplained 50% move) and
    either health state before it, the gate writes exactly four rows, one per check, PASS iff all
    four pass; a failure writes one `quality.failed` event, raises one alert, fails the job and
    leaves the health kill tripped, however often the scheduler retries it on the same data
    (audit L4: a retry every 10 minutes until midnight re-alerted each time); only a passing
    gate clears it."""
    names = [s for s, _, _ in symbols_of(4)]
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, symbols_of(4))
        store_previous(world, dict.fromkeys(names, Decimal(100)), DAY_BEFORE)
        if tripped_before:
            with world.factory() as session:
                sync_health(
                    session,
                    scope="account",
                    bot_id=None,
                    failing=True,
                    reason="data_health",
                    now=day_at(17),
                )
                session.commit()
        if kind == "bad_row":
            world.nse.quotes["S0000"] = Quote(volume=-1)
        elif kind == "unexplained_move":
            world.nse.quotes["S0000"] = Quote(close=Decimal(150), prev_close=Decimal(100))
        _ingest(world)
        if kind == "no_vix":
            read = world.store.read_index
            world.store.read_index = lambda day: [  # type: ignore[method-assign]
                r for r in read(day) if r.index_name.upper() != "INDIA VIX"
            ]
        passed = _gate(world)
        for _ in range(retries):
            assert _gate(world) == passed
        rows = _rows(world)
        with world.factory() as session:
            failed_events = session.scalars(
                select(HatEvent).where(HatEvent.event_type == "quality.failed")
            ).all()
            job = session.scalars(select(HatJobRun).where(HatJobRun.job == JOB_QUALITY)).one()
        alerts = list(world.notifier.alerts)
        tripped = _health_tripped(world)
    assert sorted(rows) == sorted(CHECK_NAMES)
    assert passed == (kind == "good")
    assert passed == all(v == "PASS" for v in rows.values())
    assert len(failed_events) == (0 if passed else 1)
    assert job.status == ("succeeded" if passed else "failed")
    assert len(alerts) == (0 if passed else 1)
    assert tripped == (not passed)
