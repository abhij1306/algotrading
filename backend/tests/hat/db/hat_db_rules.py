"""The database rules, each as a check body over a Postgres connection.

A check raises AssertionError when the database does not enforce its rule. `test_db_*.py` runs
each check against the real schema: triggers, partial unique indexes, CHECK constraints and row
locks only a real server enforces.
"""

import threading
import time
import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal

import hat_db_builders as b
from sqlalchemy import Connection, Engine, select, text, update
from sqlalchemy.exc import DBAPIError

from app.hat import models as m
from app.hat.ids import uuid7
from app.hat.schema import ensure_live_schema, ensure_state_schema


def rejects(conn: Connection, what: str, action: Callable[[], object]) -> None:
    """`action` must be refused by the database (inside a savepoint, so the check goes on)."""
    savepoint = conn.begin_nested()
    try:
        action()
    except DBAPIError:
        savepoint.rollback()
        return
    savepoint.rollback()
    raise AssertionError(f"the database accepted: {what}")


def accepts(conn: Connection, what: str, action: Callable[[], object]) -> None:
    """`action` must succeed; its effect is kept."""
    try:
        with conn.begin_nested():
            action()
    except DBAPIError as exc:
        raise AssertionError(f"the database refused: {what}") from exc


def scalar(conn: Connection, statement: object) -> object:
    return conn.execute(statement).scalar_one()


# --- reference ------------------------------------------------


def check_reference__instrument_unique(conn: Connection) -> None:
    market = b.market(conn)
    b.instrument(conn, market)
    rejects(
        conn,
        "a second instrument with the same (market, isin)",
        lambda: b.instrument(conn, market, symbol="ZZZ", name="Other", face_value=10),
    )
    rejects(conn, "same isin, other series", lambda: b.instrument(conn, market, series="BE"))
    other = b.market(conn)
    accepts(conn, "same isin, other market", lambda: b.instrument(conn, other))


def check_reference__broker_symbol_unique(conn: Connection) -> None:
    market = b.market(conn)
    first = b.instrument(conn, market, isin="INE000A01011")
    second = b.instrument(conn, market, isin="INE000A01012", symbol="BBB")

    def symbol(instrument_id: uuid.UUID, broker: str, broker_symbol: str) -> None:
        b.add(
            conn,
            m.HatBrokerSymbol,
            instrument_id=instrument_id,
            broker=broker,
            broker_symbol=broker_symbol,
            refreshed_at=b.NOW,
        )

    symbol(first, "fyers", "NSE:AAA-EQ")
    rejects(
        conn, "one broker symbol for two instruments", lambda: symbol(second, "fyers", "NSE:AAA-EQ")
    )
    accepts(
        conn,
        "the same symbol text at another broker",
        lambda: symbol(second, "zerodha", "NSE:AAA-EQ"),
    )


def check_reference__corporate_action_unique(conn: Connection) -> None:
    market = b.market(conn)
    instrument = b.instrument(conn, market)

    def action(action_type: str, ex_date: date, factor: str) -> None:
        b.add(
            conn,
            m.HatCorporateAction,
            instrument_id=instrument,
            ex_date=ex_date,
            action_type=action_type,
            adjustment_factor=Decimal(factor),
            raw_text="t",
            source="mcp",
        )

    action("split", date(2024, 1, 2), "0.5")
    rejects(conn, "a split applied twice", lambda: action("split", date(2024, 1, 2), "0.25"))
    accepts(conn, "another type the same day", lambda: action("bonus", date(2024, 1, 2), "0.5"))
    accepts(conn, "the same type another day", lambda: action("split", date(2024, 1, 3), "0.5"))


def check_reference__announcement_guid_unique(conn: Connection) -> None:
    def announcement(guid: str) -> None:
        b.add(
            conn,
            m.HatAnnouncement,
            guid=guid,
            feed="announcements",
            company_raw="c",
            category="x",
            title="t",
            url="u",
            published_at=b.NOW,
            raw={},
        )

    announcement("g1")
    rejects(conn, "a repeated announcement guid", lambda: announcement("g1"))
    accepts(conn, "another guid", lambda: announcement("g2"))


BAND_OK = (2, 5, 10, 20, None)
BAND_BAD = (-5, 0, 1, 3, 4, 6, 15, 19, 21, 25, 50, 100, Decimal("2.5"), Decimal("10.0001"))


def check_reference__band_pct_check(conn: Connection) -> None:
    market = b.market(conn)
    for index, band in enumerate(BAND_OK):
        instrument = b.instrument(conn, market, isin=f"INE0OK{index:06d}")
        accepts(
            conn,
            f"band_pct {band}",
            lambda i=instrument, v=band: b.add(
                conn, m.HatPriceBand, instrument_id=i, band_pct=v, effective_from=date(2024, 1, 1)
            ),
        )
    instrument = b.instrument(conn, market, isin="INE0BAD00001")
    for band in BAND_BAD:
        rejects(
            conn,
            f"band_pct {band}",
            lambda v=band: b.add(
                conn,
                m.HatPriceBand,
                instrument_id=instrument,
                band_pct=v,
                effective_from=date(2024, 1, 1),
            ),
        )


# --- domains ---------------------------------------------------------------


def _domain_cases(conn: Connection) -> list[tuple[str, Callable[[str], None], tuple, tuple]]:
    """(column, insert-with-that-value, good values, bad values) for every domain column."""

    def kill(column: str) -> Callable[[str], None]:
        def make(value: str) -> None:
            values = {"scope": "account", "state": "tripped", "reason": "manual"}
            values[column] = value
            b.add(conn, m.HatKillSwitch, tripped_at=b.NOW, **values)

        return make

    def exposure(value: str) -> None:
        other = uuid7()  # a bot id: the sleeve is a config record, named by a plain column
        b.add(
            conn,
            m.HatExposureState,
            bot_id=other,
            level=value,
            consecutive_wins=0,
            consecutive_losses=0,
            updated_at=b.NOW,
        )

    return [
        ("hat_kill_switch.scope", kill("scope"), ("account",), ("", "global", "user")),
        ("hat_kill_switch.state", kill("state"), ("tripped", "armed"), ("", "off", "TRIPPED")),
        ("hat_exposure_state.level", exposure, ("L1", "L2", "L3"), ("", "L0", "L4", "l1")),
    ]


def check_governance__domain_checks(conn: Connection) -> None:
    for column, make, good, bad in _domain_cases(conn):
        for value in good:
            accepts(conn, f"{column} = {value!r}", lambda v=value, f=make: f(v))
        for value in bad:
            rejects(conn, f"{column} = {value!r}", lambda v=value, f=make: f(v))


def check_governance__kill_switch_scope_check(conn: Connection) -> None:
    bot = uuid7()

    def kill(scope: str, bot_id: uuid.UUID | None) -> None:
        b.add(
            conn,
            m.HatKillSwitch,
            scope=scope,
            bot_id=bot_id,
            state="tripped",
            tripped_at=b.NOW,
            reason="manual",
        )

    rejects(conn, "a bot-scope trip with no bot", lambda: kill("bot", None))
    rejects(conn, "an account-scope trip naming a bot", lambda: kill("account", bot))
    accepts(conn, "a bot-scope trip with a bot", lambda: kill("bot", bot))
    accepts(conn, "an account-scope trip", lambda: kill("account", None))


def check_governance__exposure_checks(conn: Connection) -> None:
    bot = uuid7()

    def exposure(bot_id: uuid.UUID, level: str = "L1", wins: int = 0, losses: int = 0) -> None:
        b.add(
            conn,
            m.HatExposureState,
            bot_id=bot_id,
            level=level,
            consecutive_wins=wins,
            consecutive_losses=losses,
            updated_at=b.NOW,
        )

    exposure(bot)
    rejects(conn, "a second exposure row for one bot", lambda: exposure(bot, "L2"))
    other = uuid7()
    rejects(conn, "level L4", lambda: exposure(other, "L4"))
    rejects(conn, "a negative win counter", lambda: exposure(other, wins=-1))
    rejects(conn, "a negative loss counter", lambda: exposure(other, losses=-1))
    accepts(conn, "level L3 with counters", lambda: exposure(other, "L3", 4, 0))


# --- portfolio -------------------------------------------------------------


def check_portfolio__unique_keys(conn: Connection) -> None:
    market = b.market(conn)
    universe = b.universe(conn, market)
    b.universe_version(conn, universe, 1)
    rejects(conn, "a duplicate (universe, version)", lambda: b.universe_version(conn, universe, 1))
    accepts(conn, "the next universe version", lambda: b.universe_version(conn, universe, 2))

    model = uuid7()  # a regime model version: a config record, named by a plain column

    def regime(day: date) -> None:
        b.add(
            conn,
            m.HatRegimeState,
            trade_date=day,
            market_id=market,
            model_version_id=model,
            state="bull_quiet",
            inputs={},
            confirmed=False,
        )

    regime(date(2026, 10, 19))
    rejects(
        conn, "two regime states for one (market, model, date)", lambda: regime(date(2026, 10, 19))
    )
    accepts(conn, "another date", lambda: regime(date(2026, 10, 20)))

    bot = uuid7()

    def allocation(day: date) -> None:
        b.add(
            conn,
            m.HatAllocationDaily,
            trade_date=day,
            bot_id=bot,
            base_weight=Decimal("0.5"),
            regime_mult=Decimal(1),
            health_mult=Decimal(1),
            final_weight=Decimal("0.5"),
            capital=Decimal(100000),
            risk_pct=Decimal("0.0025"),
            currency="INR",
        )

    allocation(date(2026, 10, 19))
    rejects(conn, "two allocations for one (date, bot)", lambda: allocation(date(2026, 10, 19)))
    accepts(conn, "another date", lambda: allocation(date(2026, 10, 20)))


def check_portfolio__domain_checks(conn: Connection) -> None:
    market = b.market(conn)
    for kind in ("manual", "screener"):
        accepts(conn, f"universe kind {kind}", lambda k=kind: b.universe(conn, market, k))
    for kind in ("", "auto", "Manual", "etf"):
        rejects(conn, f"universe kind {kind!r}", lambda k=kind: b.universe(conn, market, k))

    parent = uuid7()

    def state(value: str, day: date) -> None:
        b.add(
            conn,
            m.HatRegimeState,
            trade_date=day,
            market_id=market,
            model_version_id=parent,
            state=value,
            inputs={},
            confirmed=False,
        )

    for offset, value in enumerate(("bull_quiet", "bull_volatile", "correction", "bear")):
        accepts(
            conn, f"regime state {value}", lambda v=value, o=offset: state(v, date(2026, 1, 1 + o))
        )
    for value in ("", "bull", "Bear", "sideways"):
        rejects(conn, f"regime state {value!r}", lambda v=value: state(v, date(2026, 2, 1)))


# --- ops ------------------------------------------------------


def check_ops__job_run_unique(conn: Connection) -> None:
    def run(job: str, day: date) -> None:
        b.add(
            conn,
            m.HatJobRun,
            job=job,
            trade_date=day,
            started_at=b.NOW,
            status="running",
            calls=0,
            rows=0,
        )

    run("ingest.nightly", date(2026, 10, 20))
    rejects(
        conn,
        "a second run of one (job, trade_date)",
        lambda: run("ingest.nightly", date(2026, 10, 20)),
    )
    accepts(conn, "another date", lambda: run("ingest.nightly", date(2026, 10, 21)))
    accepts(conn, "another job", lambda: run("scan", date(2026, 10, 20)))


def check_ops__job_run_unique_concurrent(engine: Engine) -> None:
    """Two transactions insert the same (job, trade_date) at once: exactly one commits."""
    job, day = f"race.{uuid.uuid4().hex[:8]}", date(2026, 10, 20)
    insert_sql = text(
        "INSERT INTO hat_job_run (id, job, trade_date, started_at, status, calls, rows)"
        " VALUES (:id, :job, :day, now(), 'running', 0, 0)"
    )
    outcomes: list[str] = []
    first_in = threading.Event()
    release = threading.Event()

    def worker(hold: bool) -> None:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                if not hold:
                    first_in.wait(5)
                connection.execute(insert_sql, {"id": uuid7(), "job": job, "day": day})
                if hold:
                    first_in.set()
                    release.wait(10)
                transaction.commit()
                outcomes.append("committed")
            except DBAPIError:
                transaction.rollback()
                outcomes.append("rejected")

    holder = threading.Thread(target=worker, args=(True,))
    racer = threading.Thread(target=worker, args=(False,))
    try:
        holder.start()
        racer.start()
        deadline = time.monotonic() + 5
        waiting = text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
        while time.monotonic() < deadline:
            with engine.connect() as probe:
                if probe.execute(waiting).scalar_one() >= 1:
                    break
            time.sleep(0.05)  # noqa: ASYNC251
        release.set()
        holder.join(10)
        racer.join(10)
        assert sorted(outcomes) == ["committed", "rejected"], outcomes
    finally:
        release.set()
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM hat_job_run WHERE job = :job"), {"job": job})


# --- governance (.. 087) -----------------------------------------------------

NON_DRAFT = ("backtested", "shadow", "live", "retired")  # a universe version's


def check_governance__universe_version_immutable(conn: Connection) -> None:
    market = b.market(conn)
    table = m.HatUniverseVersion.__table__
    draft = b.universe_version(conn, b.universe(conn, market), 1)
    accepts(
        conn,
        "changing a draft universe's definition",
        lambda: conn.execute(
            update(table).where(table.c.id == draft).values(definition={"members": ["A"]})
        ),
    )
    for number, status in enumerate(NON_DRAFT, start=2):
        version = b.universe_version(conn, b.universe(conn, market), number, status=status)
        rejects(
            conn,
            f"changing the definition of a {status} universe version",
            lambda v=version: conn.execute(
                update(table).where(table.c.id == v).values(definition={"members": ["B"]})
            ),
        )
        rejects(
            conn,
            f"changing the config_hash of a {status} universe version",
            lambda v=version: conn.execute(
                update(table).where(table.c.id == v).values(config_hash="zzz")
            ),
        )
        stored = conn.execute(
            select(table.c.definition, table.c.config_hash).where(table.c.id == version)
        ).one()
        assert stored.definition == {"members": []}
        assert stored.config_hash == "h"


# --- migration ------------------------------------------------


def ensure_both_schemas(bind: Engine | Connection) -> None:
    """hat_live's and hat_state's upkeep, on the one test database."""
    ensure_live_schema(bind)
    ensure_state_schema(bind)


CATALOG_QUERIES = {
    "tables": (
        "SELECT tablename FROM pg_tables WHERE tablename LIKE 'hat\\_%' ESCAPE '\\' ORDER BY 1"
    ),
    "indexes": (
        "SELECT indexname, indexdef FROM pg_indexes"
        " WHERE tablename LIKE 'hat\\_%' ESCAPE '\\' ORDER BY 1, 2"
    ),
    "constraints": (
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid::regclass::text LIKE 'hat\\_%' ESCAPE '\\' ORDER BY 1, 2"
    ),
    "triggers": (
        "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger WHERE NOT tgisinternal"
        " AND tgrelid::regclass::text LIKE 'hat\\_%' ESCAPE '\\' ORDER BY 1, 2"
    ),
    "functions": (
        "SELECT proname, prosrc FROM pg_proc WHERE proname LIKE 'hat\\_%' ESCAPE '\\' ORDER BY 1"
    ),
    "columns": (
        "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns"
        " WHERE table_name LIKE 'hat\\_%' ESCAPE '\\' ORDER BY 1, 2"
    ),
}


def catalog(engine: Engine) -> dict[str, list[tuple]]:
    with engine.connect() as connection:
        return catalog_of(connection)


def catalog_of(connection: Connection) -> dict[str, list[tuple]]:
    """The catalog as `connection` sees it, inside its open transaction."""
    return {
        name: [tuple(row) for row in connection.execute(text(sql)).all()]
        for name, sql in CATALOG_QUERIES.items()
    }


def check_migration__ensure_idempotent(
    engine: Engine, ensure: Callable[[Engine], None], runs: int = 3
) -> None:
    """Running `ensure` n times leaves the same catalog as running it once, and never fails."""
    try:
        ensure(engine)
        once = catalog(engine)
        for _ in range(runs - 1):
            ensure(engine)
    except DBAPIError as exc:
        raise AssertionError("the ensure step failed when run again") from exc
    assert catalog(engine) == once, "a repeated ensure changed the catalog"
    assert once["triggers"], "ensure created no triggers"
