"""PBT quality_gate.* (HAT-DATA-060..065, HAT-DEC-I14): the nightly quality gate's four checks
(coverage, OHLC, big moves, index snapshot), the rows and events it writes, the health kill it
trips and clears, and the quarantine of unexplained big movers (PR 33). Pure checks plus the
real ingest and gate over an in-memory database and a temporary store; no network."""

import tempfile
from collections.abc import Collection
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace

from hat_ingest_support import (
    TRADE_DAY,
    Quote,
    World,
    day_at,
    make_world,
    store_previous,
    symbols_of,
)
from hat_mcp_support import ScriptedTransport
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from app.hat.daily.nightly import Nightly
from app.hat.data.ingest import ingest_body, outcome_recorder
from app.hat.data.mcp_client import McpClient
from app.hat.data.quality import (
    CHECK_NAMES,
    CODE_FAILED,
    CODE_QUARANTINED,
    JOB_QUALITY,
    RELEASED_BY_ACTION,
    RELEASED_BY_OWNER,
    big_moves_check,
    coverage_check,
    excluded_for,
    gate_body,
    index_snapshot_check,
    quarantine,
    scan_universe,
    standing_quarantine,
)
from app.hat.data.quarantine import clear, open_quarantines
from app.hat.models import (
    HatCorporateAction,
    HatEvent,
    HatJobRun,
    HatQualityCheck,
    HatQualityQuarantine,
)
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


def _action(world: World, symbol: str, day: date) -> None:
    with world.factory() as session:
        session.add(
            HatCorporateAction(
                instrument_id=world.ids[f"{symbol}:EQ"],
                ex_date=day,
                action_type="split",
                adjustment_factor=Decimal("0.5"),
                raw_text="split",
                source="nse_mcp",
            )
        )
        session.commit()


def _night(world: World, size: int, movers: Collection[int]) -> None:
    """A stored flat day before TRADE_DAY, then the trade day's quotes with `movers` up 50%."""
    names = [s for s, _, _ in symbols_of(size)]
    store_previous(world, dict.fromkeys(names, Decimal(100)), DAY_BEFORE)
    for i in movers:
        world.nse.quotes[names[i]] = Quote(close=Decimal(150), prev_close=Decimal(100))


def _quarantined(world: World) -> set[tuple[str, date, bool]]:
    with world.factory() as session:
        return {
            (str(q.instrument_id), q.since, q.released_at is None)
            for q in session.scalars(select(HatQualityQuarantine))
        }


def _quarantine_alerts(world: World) -> list[tuple[str, str]]:
    return [
        (str(d["instrument"]), str(d["symbol"]))
        for code, _day, d in world.notifier.alerts
        if code == CODE_QUARANTINED
    ]


@settings(max_examples=60)
@given(
    moved=st.sets(st.integers(0, 9)),
    explained=st.sets(st.integers(0, 9)),
    excluded=st.sets(st.integers(0, 9)),
    universe=st.sets(st.integers(0, 9)),
    size=st.integers(0, 1100),
)
@example(moved={1}, explained=set(), excluded=set(), universe=set(), size=100)
@example(moved={1, 2}, explained=set(), excluded=set(), universe=set(), size=199)
@example(moved={1, 2}, explained=set(), excluded=set(), universe=set(), size=200)
def test_big_moves_explained(
    moved: set[int], explained: set[int], excluded: set[int], universe: set[int], size: int
) -> None:
    """HAT-DATA-062 (PR 33). For every set of instruments that moved over 20%, those with a
    corporate action on the day, those already excluded and every active-universe size N: the
    unexplained are the movers neither explained nor excluded; the check fails iff they are more
    than 1% of N (one of 100 passes, two of 199 fail, two of 200 pass); the day's exclusion list
    is the excluded plus, on a pass only, the unexplained; and a scan sees the universe less the
    exclusion list, never an excluded one."""
    check = big_moves_check(moved, explained, excluded, size)
    unexplained = moved - explained - excluded
    assert set(check.detail["unexplained"]) == {str(k) for k in unexplained}
    assert check.passed == (Fraction(len(unexplained)) <= Fraction(size, 100))
    out = excluded | (unexplained if check.passed else set())
    assert set(check.detail["excluded"]) == {str(k) for k in out}
    scanned = scan_universe(sorted(universe), out)
    assert set(scanned) == universe - out
    assert not set(scanned) & out


@settings(max_examples=12)
@given(
    size=st.integers(100, 250),
    movers=st.sets(st.integers(0, 4)),
    explained=st.sets(st.integers(0, 4)),
    excluded=st.sets(st.integers(0, 6)),
)
@example(size=100, movers={0}, explained=set(), excluded=set())
@example(size=100, movers={0, 1}, explained=set(), excluded=set())
@example(size=200, movers={0, 1}, explained=set(), excluded={5})
def test_big_moves_end_to_end(
    size: int, movers: set[int], explained: set[int], excluded: set[int]
) -> None:
    """The same rule over stored bars and corporate actions: for every universe of 100 to 250
    instruments, the gate's big_moves check fails iff the movers with no corporate action on the
    trade date that are not excluded are more than 1% of it, and the day's scans exclude the
    given instruments plus, on a pass, those movers."""
    names = [s for s, _, _ in symbols_of(size)]
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, symbols_of(size))
        _night(world, size, movers)
        for i in explained:
            _action(world, names[i], TRADE_DAY)
        ids = [str(world.ids[f"{names[i]}:EQ"]) for i in range(size)]
        _ingest(world)
        _gate(world, tuple(ids[i] for i in sorted(excluded)))
        with world.factory() as session:
            skipped = excluded_for(session, TRADE_DAY)
    unexplained = movers - explained - excluded
    passed = len(unexplained) * 100 <= size
    assert (_rows(world)["big_moves"] == "PASS") == passed
    assert skipped == {ids[i] for i in excluded | (unexplained if passed else set())}


@settings(max_examples=12)
@given(
    size=st.integers(100, 300),
    wanted=st.integers(1, 3),
    failing=st.integers(0, 2),
)
@example(size=100, wanted=1, failing=0)
@example(size=100, wanted=1, failing=2)
@example(size=300, wanted=3, failing=1)
def test_quarantine_one_mover(size: int, wanted: int, failing: int) -> None:
    """HAT-DATA-062 (PR 33, design review item 12): for every universe of N instruments and up to
    1% of it moving over 20% with no corporate action, the big_moves check passes. While another
    check fails (India VIX not stored, for `failing` runs) nothing is quarantined or alerted as
    quarantined; once the data is fixed the retry passes (no health kill left), and each mover
    is quarantined from the trade date, is on the day's exclusion list and is alerted exactly
    once with its symbol. A passing gate is not run again (one success per job and day)."""
    movers = set(range(min(wanted, size // 100)))
    names = [s for s, _, _ in symbols_of(size)]
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, symbols_of(size))
        _night(world, size, movers)
        _ingest(world)
        read = world.store.read_index
        world.store.read_index = lambda day: [  # type: ignore[method-assign]
            r for r in read(day) if r.index_name.upper() != "INDIA VIX"
        ]
        results = [_gate(world) for _ in range(failing)]
        held = (_quarantined(world), _quarantine_alerts(world))
        world.store.read_index = read  # type: ignore[method-assign]
        results += [_gate(world), _gate(world)]
        with world.factory() as session:
            skipped = excluded_for(session, TRADE_DAY)
        quarantined = _quarantined(world)
        alerts = _quarantine_alerts(world)
        failed = [a for a in world.notifier.alerts if a[0] == CODE_FAILED]
        tripped = _health_tripped(world)
    ids = {i: str(world.ids[f"{names[i]}:EQ"]) for i in movers}
    assert _rows(world)["big_moves"] == "PASS"
    assert results == [False] * failing + [True, True]
    assert held == (set(), [])
    assert len(failed) == int(failing > 0)
    assert not tripped
    assert quarantined == {(ids[i], TRADE_DAY, True) for i in movers}
    assert skipped == set(ids.values())
    assert sorted(alerts) == sorted((ids[i], names[i]) for i in movers)


@settings(max_examples=10)
@given(size=st.integers(100, 300), over=st.integers(1, 2), retries=st.integers(0, 1))
@example(size=100, over=1, retries=0)
@example(size=299, over=1, retries=1)
def test_quarantine_cap(size: int, over: int, retries: int) -> None:
    """HAT-DATA-062 (PR 33): for every universe of N instruments and more than 1% of it moving
    over 20% unexplained (2 of 100, 3 of 299), the gate fails the big_moves check, trips the
    health kill and alerts `quality.failed` once; nothing is quarantined, alerted as quarantined
    or added to the day's exclusion list, also on a retry."""
    movers = set(range(size // 100 + over))
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, symbols_of(size))
        _night(world, size, movers)
        _ingest(world)
        results = [_gate(world) for _ in range(retries + 1)]
        with world.factory() as session:
            skipped = excluded_for(session, TRADE_DAY)
        quarantined = _quarantined(world)
        alerts = _quarantine_alerts(world)
        failed = [a for a in world.notifier.alerts if a[0] == CODE_FAILED]
        tripped = _health_tripped(world)
    assert not any(results)
    assert _rows(world)["big_moves"] == "FAIL"
    assert tripped
    assert len(failed) == 1
    assert quarantined == set()
    assert alerts == []
    assert skipped == set()


OPS = st.lists(
    st.tuples(
        st.sampled_from(("quarantine", "action", "clear", "check")),
        st.integers(0, 2),  # instrument
        st.integers(1, 4),  # days before TRADE_DAY
    ),
    max_size=14,
)


@settings(max_examples=40)
@given(ops=OPS)
@example(ops=[("quarantine", 0, 2), ("check", 0, 1)])
@example(ops=[("quarantine", 0, 2), ("action", 0, 2)])
@example(ops=[("quarantine", 0, 2), ("action", 0, 3)])
@example(ops=[("quarantine", 1, 3), ("clear", 1, 1)])
def test_quarantine_release(ops: list[tuple[str, int, int]]) -> None:
    """HAT-DATA-062 (PR 33): for every sequence of quarantines, stored corporate actions, owner
    clears and gate reads over earlier days, against a model: a quarantine stands on a later day
    iff no corporate action with ex-date `since` was stored for its instrument and the owner did
    not clear it (a corporate action on another date does not release it); a gate read releases
    those with their action (`corporate_action`), `clear` the open ones of its symbol, typed in
    any case and for an inactive instrument too (`owner`), and a quarantine is never reopened.
    Then the real gate of TRADE_DAY, with no mover, excludes exactly the standing ones from that
    day's scans."""
    universe = [*symbols_of(2), ("S0002", "EQ", False)]  # an inactive one can be cleared too
    names = [s for s, _, _ in universe]
    rows: dict[tuple[int, date], str | None] = {}  # open: None, else who released it
    actions: set[tuple[int, date]] = set()

    def standing(day: date) -> set[int]:
        out = set()
        for (i, since), by in rows.items():
            if by is None and since <= day:
                if (i, since) in actions:
                    rows[(i, since)] = RELEASED_BY_ACTION
                else:
                    out.add(i)
        return out

    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, universe)
        key = {i: str(world.ids[f"{names[i]}:EQ"]) for i in range(3)}
        for op, i, back in ops:
            day = TRADE_DAY - timedelta(days=back)
            now = day_at(19, day=day)
            with world.factory() as session:
                if op == "quarantine":
                    quarantine(session, [key[i]], day, now)
                    rows.setdefault((i, day), None)
                elif op == "clear":
                    released = clear(session, names[i].lower(), now)
                    model = [k for k, by in rows.items() if k[0] == i and by is None]
                    assert released == len(model)
                    rows.update(dict.fromkeys(model, RELEASED_BY_OWNER))
                elif op == "check":
                    got = standing_quarantine(session, day, now)
                    assert got == {key[k] for k in standing(day)}
                elif (i, day) not in actions:
                    _action(world, names[i], day)
                    actions.add((i, day))
                session.commit()
            with world.factory() as session:
                stored = {
                    (str(q.instrument_id), q.since): q.released_by
                    for q in session.scalars(select(HatQualityQuarantine))
                }
            assert stored == {(key[k], since): by for (k, since), by in rows.items()}
        _night(world, 2, ())
        _ingest(world)
        assert _gate(world)
        with world.factory() as session:
            skipped = excluded_for(session, TRADE_DAY)
            listed = {str(q.instrument_id) for q in open_quarantines(session)}
    assert skipped == {key[k] for k in standing(TRADE_DAY)}
    assert listed == skipped


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


@settings(max_examples=20)
@given(
    quarantined=st.dictionaries(st.integers(0, 3), st.integers(1, 3), max_size=4),
    found=st.sets(st.integers(0, 4)),
    unreadable=st.sets(st.integers(0, 4)),
)
@example(quarantined={0: 2}, found={0}, unreadable=set())
@example(quarantined={0: 2, 1: 2}, found={1}, unreadable={0})
def test_quarantine_lookup(
    quarantined: dict[int, int], found: set[int], unreadable: set[int]
) -> None:
    """HAT-DATA-062 (PR 33): each night `ca.adjust` asks the MCP again for the actions of every
    open quarantine's instrument on its `since` date, and only those; an action it stores
    releases that quarantine at the next gate read, while an unreadable answer or none leaves
    it standing, and nothing is alerted for it (the quarantine was alerted already). Harm: a
    split whose lookup failed on its day keeps the stock out of the scans until someone
    notices."""
    names = [s for s, _, _ in symbols_of(5)]
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, symbols_of(5))
        since = {i: TRADE_DAY - timedelta(days=back) for i, back in quarantined.items()}
        with world.factory() as session:
            for i, day in since.items():
                quarantine(session, [str(world.ids[f"{names[i]}:EQ"])], day, day_at(19, day=day))
            session.commit()
        for i in range(5):
            day = since.get(i, TRADE_DAY).isoformat()
            if i in unreadable:  # no factor: unreadable, so the lookup counts as failed
                world.nse.actions[names[i]] = [
                    {"exDate": day, "actionType": "BONUS", "purpose": "Bonus"}
                ]
            elif i in found:
                world.nse.actions[names[i]] = [
                    {
                        "exDate": day,
                        "actionType": "SPLIT",
                        "purpose": "Face Value Split",
                        "adjustmentFactor": 0.5,
                    }
                ]
        transport = ScriptedTransport(world.nse.respond)
        client = McpClient(transport, sleep=lambda _s: None)
        deps = SimpleNamespace(
            session_factory=world.factory,
            mcp=lambda: client,
            notifier=world.notifier,
        )
        Nightly(deps)._quarantine_actions()  # type: ignore[arg-type]  # noqa: SLF001
        asked = {
            (a["symbol"], a["fromDate"], a["toDate"])
            for _, tool, a in transport.calls
            if tool == "get_corporate_actions"
        }
        with world.factory() as session:
            standing = standing_quarantine(session, TRADE_DAY, day_at(19))
    assert asked == {(names[i], d.isoformat(), d.isoformat()) for i, d in since.items()}
    released = set(since) & found - unreadable
    assert standing == {str(world.ids[f"{names[i]}:EQ"]) for i in set(since) - released}
    assert world.notifier.alerts == []
