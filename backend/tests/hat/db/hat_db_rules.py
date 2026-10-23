"""The database rules of each as a check body over a Postgres connection.

A check raises AssertionError when the database does not enforce its rule. `test_db_*.py` runs
each check against the real schema; `test_db_broken_variants.py` removes the trigger, index or
constraint inside a transaction and requires the same check to fail.
"""

import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import hat_db_builders as b
from sqlalchemy import Connection, Engine, delete, select, text, update
from sqlalchemy.exc import DBAPIError

from app.hat import models as m
from app.hat.ids import uuid7
from app.hat.schema import PG_DDL

SEVEN_DAYS = timedelta(days=7)


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
        "a second instrument with the same (market, isin, series)",
        lambda: b.instrument(conn, market, symbol="ZZZ", name="Other", face_value=10),
    )
    accepts(conn, "same isin, other series", lambda: b.instrument(conn, market, series="BE"))
    other = b.market(conn)
    accepts(conn, "same isin and series, other market", lambda: b.instrument(conn, other))


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
    market = b.market(conn)
    strategy = b.strategy(conn, market)
    account = b.broker_account(conn)
    counter = iter(range(1, 10_000))

    def version(status: str) -> None:
        b.strategy_version(conn, strategy, next(counter), status=status)

    def risk(direction: str) -> None:
        # a far-future effective date keeps the cooling-off CHECK out of this domain check
        b.risk_policy(conn, next(counter), direction=direction, effective_from=b.later(30))

    def bot_row(column: str) -> Callable[[str], None]:
        return lambda value: b.bot(conn, strategy, account, **{column: value})

    def kill(column: str) -> Callable[[str], None]:
        def make(value: str) -> None:
            values = {"scope": "account", "state": "tripped", "reason": "manual"}
            values[column] = value
            b.add(conn, m.HatKillSwitch, tripped_at=b.NOW, **values)

        return make

    def exposure(value: str) -> None:
        other = b.bot(conn, strategy, account)
        b.add(
            conn,
            m.HatExposureState,
            bot_id=other,
            level=value,
            consecutive_wins=0,
            consecutive_losses=0,
            updated_at=b.NOW,
        )

    def criterion(value: str) -> None:
        b.add(
            conn,
            m.HatCriterionType,
            code=f"c{next(counter)}",
            version=1,
            stage=value,
            param_schema={},
            data_needs={},
        )

    return [
        (
            "hat_strategy_version.status",
            version,
            ("draft", "backtested", "shadow", "retired"),
            ("", "signal", "LIVE", "done"),
        ),
        (
            "hat_risk_policy_version.direction",
            risk,
            ("initial", "increase", "decrease"),
            ("", "raise", "INCREASE"),
        ),
        (
            "hat_bot.execution_mode",
            bot_row("execution_mode"),
            ("shadow", "confirm", "auto"),
            ("", "signal", "AUTO", "live"),
        ),
        (
            "hat_bot.status",
            bot_row("status"),
            ("active", "paused", "manage_only"),
            ("", "stopped", "ACTIVE"),
        ),
        (
            "hat_bot.lifecycle_state",
            bot_row("lifecycle_state"),
            ("research", "backtested", "shadow", "live", "probation", "retired"),
            ("", "draft", "Live"),
        ),
        ("hat_kill_switch.scope", kill("scope"), ("account",), ("", "global", "user")),
        ("hat_kill_switch.state", kill("state"), ("tripped", "armed"), ("", "off", "TRIPPED")),
        ("hat_exposure_state.level", exposure, ("L1", "L2", "L3"), ("", "L0", "L4", "l1")),
        (
            "hat_criterion_type.stage",
            criterion,
            ("universe", "setup", "trigger", "entry_filter"),
            ("", "exit", "Setup"),
        ),
    ]


def check_governance__domain_checks(conn: Connection) -> None:
    for column, make, good, bad in _domain_cases(conn):
        for value in good:
            accepts(conn, f"{column} = {value!r}", lambda v=value, f=make: f(v))
        for value in bad:
            rejects(conn, f"{column} = {value!r}", lambda v=value, f=make: f(v))
    # Updates are held to the same domains.
    market = b.market(conn)
    strategy = b.strategy(conn, market)
    bot = b.bot(conn, strategy, b.broker_account(conn))
    rejects(
        conn,
        "updating hat_bot.execution_mode to 'signal'",
        lambda: conn.execute(
            update(m.HatBot.__table__).where(m.HatBot.id == bot).values(execution_mode="signal")
        ),
    )
    # A bot starts in shadow mode (B2), whether or not the writer says so.
    raw = uuid7()
    accepts(
        conn,
        "a bot row that does not name an execution mode",
        lambda: conn.execute(
            text(
                "INSERT INTO hat_bot (id, user_id, strategy_id, broker_account_id, status,"
                " lifecycle_state, risk_cluster) SELECT :id, user_id, strategy_id,"
                " broker_account_id, status, lifecycle_state, risk_cluster"
                " FROM hat_bot WHERE id = :bot"
            ),
            {"id": raw, "bot": bot},
        ),
    )
    assert (
        scalar(conn, text("SELECT execution_mode FROM hat_bot WHERE id = :i").bindparams(i=raw))
        == "confirm"
    )


def check_governance__kill_switch_scope_check(conn: Connection) -> None:
    market = b.market(conn)
    strategy = b.strategy(conn, market)
    bot = b.bot(conn, strategy, b.broker_account(conn))

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
    market = b.market(conn)
    strategy = b.strategy(conn, market)
    account = b.broker_account(conn)
    bot = b.bot(conn, strategy, account)

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
    other = b.bot(conn, strategy, account)
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

    model = b.add(
        conn,
        m.HatRegimeModelVersion,
        market_id=market,
        kind="rules",
        version=1,
        status="live",
        config={},
    )

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

    strategy = b.strategy(conn, market)
    bot = b.bot(conn, strategy, b.broker_account(conn))

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

    def model(kind: str) -> uuid.UUID:
        return b.add(
            conn,
            m.HatRegimeModelVersion,
            market_id=market,
            kind=kind,
            version=1,
            status="x",
            config={},
        )  # type: ignore[return-value]

    for kind in ("rules", "hmm", "gmm"):
        accepts(conn, f"regime model kind {kind}", lambda k=kind: model(k))
    for kind in ("", "ml", "HMM"):
        rejects(conn, f"regime model kind {kind!r}", lambda k=kind: model(k))

    parent = model("rules")

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


def check_ops__family_key_not_null(conn: Connection) -> None:
    market = b.market(conn)
    version = b.strategy_version(conn, b.strategy(conn, market))
    policy = b.risk_policy(conn)
    rejects(
        conn,
        "a backtest run with no family_key",
        lambda: b.backtest_run(conn, version, policy, family_key=None),
    )
    accepts(conn, "a backtest run with a family_key", lambda: b.backtest_run(conn, version, policy))


# --- governance (.. 087) -----------------------------------------------------

NON_DRAFT = ("backtested", "shadow", "live", "retired")


def _published_version(
    conn: Connection, strategy: uuid.UUID, number: int, status: str
) -> uuid.UUID:
    """A strategy version moved from draft to `status` (a live one with its evidence)."""
    version = b.strategy_version(conn, strategy, number)
    values: dict[str, object] = {"status": status}
    if status == "live":
        policy = b.risk_policy(conn, number)
        run = b.backtest_run(conn, version, policy)
        values.update(backtest_run_id=run, shadow_started_at=b.NOW)
    conn.execute(
        update(m.HatStrategyVersion.__table__)
        .where(m.HatStrategyVersion.id == version)
        .values(**values)
    )
    return version


def check_governance__version_config_immutable(conn: Connection) -> None:
    market = b.market(conn)
    table = m.HatStrategyVersion.__table__
    draft = b.strategy_version(conn, b.strategy(conn, market), 1)
    accepts(
        conn,
        "changing a draft's config",
        lambda: conn.execute(
            update(table).where(table.c.id == draft).values(config={"a": 2}, config_hash="h2")
        ),
    )
    for number, status in enumerate(NON_DRAFT, start=2):
        # one strategy per version, so a live one never meets another live one
        version = _published_version(conn, b.strategy(conn, market), number, status)
        for new in ({"a": 3}, {"x": [1, 2]}, {}):
            rejects(
                conn,
                f"changing the config of a {status} version",
                lambda n=new, v=version: conn.execute(
                    update(table).where(table.c.id == v).values(config=n)
                ),
            )
        rejects(
            conn,
            f"changing the config_hash of a {status} version",
            lambda v=version: conn.execute(
                update(table).where(table.c.id == v).values(config_hash="zzz")
            ),
        )
        rejects(
            conn,
            f"sending a {status} version back to draft",
            lambda v=version: conn.execute(
                update(table).where(table.c.id == v).values(status="draft")
            ),
        )
        row = conn.execute(
            select(table.c.config, table.c.config_hash).where(table.c.id == version)
        ).one()
        assert row.config == {"a": 1}, f"{status} config changed"
        assert row.config_hash == "h", f"{status} config hash changed"
        accepts(
            conn,
            f"a non-config update of a {status} version",
            lambda v=version: conn.execute(
                update(table).where(table.c.id == v).values(promoted_at=b.NOW)
            ),
        )


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


def check_governance__one_live_version(conn: Connection) -> None:
    market = b.market(conn)
    table = m.HatStrategyVersion.__table__
    strategy = b.strategy(conn, market)
    first = _published_version(conn, strategy, 1, "live")
    second = b.strategy_version(conn, strategy, 2)
    evidence = b.backtest_run(conn, second, b.risk_policy(conn, 99))
    go_live = {"status": "live", "backtest_run_id": evidence, "shadow_started_at": b.NOW}
    rejects(
        conn,
        "a second live version of one strategy",
        lambda: conn.execute(update(table).where(table.c.id == second).values(**go_live)),
    )
    other = b.strategy(conn, market)
    accepts(
        conn,
        "a live version of another strategy",
        lambda: _published_version(conn, other, 3, "live"),
    )
    accepts(
        conn,
        "retiring the live version",
        lambda: conn.execute(update(table).where(table.c.id == first).values(status="retired")),
    )
    accepts(
        conn,
        "going live once the other is retired",
        lambda: conn.execute(update(table).where(table.c.id == second).values(**go_live)),
    )

    universe = b.universe(conn, market)
    b.universe_version(conn, universe, 1, status="live")
    rejects(
        conn,
        "a second live version of one universe",
        lambda: b.universe_version(conn, universe, 2, status="live"),
    )
    accepts(
        conn,
        "a draft next to the live universe version",
        lambda: b.universe_version(conn, universe, 3),
    )
    accepts(
        conn,
        "a live version of another universe",
        lambda: b.universe_version(conn, b.universe(conn, market), 1, status="live"),
    )


def check_governance__live_requires_evidence(conn: Connection) -> None:
    market = b.market(conn)
    table = m.HatStrategyVersion.__table__
    strategy = b.strategy(conn, market)
    seed = b.strategy_version(conn, strategy, 1)
    run = b.backtest_run(conn, seed, b.risk_policy(conn))
    cases = {
        "no backtest run": {"backtest_run_id": None, "shadow_started_at": b.NOW},
        "no shadow start": {"backtest_run_id": run, "shadow_started_at": None},
        "neither": {"backtest_run_id": None, "shadow_started_at": None},
    }
    for number, (label, values) in enumerate(cases.items(), start=10):
        rejects(
            conn,
            f"inserting a live version with {label}",
            lambda n=number, v=values: b.strategy_version(
                conn, b.strategy(conn, market), n, status="live", **v
            ),
        )
        candidate = b.strategy_version(conn, b.strategy(conn, market), number + 10)
        rejects(
            conn,
            f"updating a version to live with {label}",
            lambda c=candidate, v=values: conn.execute(
                update(table).where(table.c.id == c).values(status="live", **v)
            ),
        )
    accepts(
        conn,
        "a live version with a backtest run and a shadow start",
        lambda: b.strategy_version(
            conn,
            b.strategy(conn, market),
            50,
            status="live",
            backtest_run_id=run,
            shadow_started_at=b.NOW,
        ),
    )


COOLING_OFFSETS = (
    timedelta(days=-3),
    timedelta(0),
    timedelta(hours=1),
    timedelta(days=1),
    timedelta(days=6, hours=23, minutes=59, seconds=59, microseconds=999999),
    SEVEN_DAYS - timedelta(microseconds=1),
    SEVEN_DAYS,
    SEVEN_DAYS + timedelta(microseconds=1),
    timedelta(days=30),
)


def check_governance__risk_increase_cooling_off(conn: Connection) -> None:
    requested = datetime(2026, 10, 20, 3, 30, 15, 123456, tzinfo=UTC)
    number = 0
    for direction in ("initial", "increase", "decrease"):
        for offset in COOLING_OFFSETS:
            number += 1

            def write(n: int = number, d: str = direction, o: timedelta = offset) -> None:
                b.risk_policy(
                    conn, n, direction=d, requested_at=requested, effective_from=requested + o
                )

            if direction == "increase" and offset < SEVEN_DAYS:
                rejects(conn, f"an increase effective {offset} after its request", write)
            else:
                accepts(conn, f"a {direction} effective {offset} after its request", write)


def check_governance__risk_version_immutable(conn: Connection) -> None:
    table = m.HatRiskPolicyVersion.__table__
    row = b.risk_policy(conn, 1)
    before = conn.execute(select(table).where(table.c.id == row)).one()
    changes = {
        "policy": {"risk_pct": "9"},
        "direction": "decrease",
        "version": 7,
        "effective_from": b.later(-5),
        "requested_at": b.later(1),
        "user_id": uuid.uuid4(),
        "gate_evidence": {"ok": True},
    }
    for column, value in changes.items():
        rejects(
            conn,
            f"changing {column} of a risk policy version",
            lambda c=column, v=value: conn.execute(
                update(table).where(table.c.id == row).values(**{c: v})
            ),
        )
    rejects(
        conn,
        "deleting a risk policy version",
        lambda: conn.execute(delete(table).where(table.c.id == row)),
    )
    assert conn.execute(select(table).where(table.c.id == row)).one() == before
    accepts(
        conn,
        "setting superseded_at on a current version",
        lambda: conn.execute(
            update(table).where(table.c.id == row).values(superseded_at=b.later(1))
        ),
    )
    rejects(
        conn,
        "changing superseded_at once set",
        lambda: conn.execute(
            update(table).where(table.c.id == row).values(superseded_at=b.later(2))
        ),
    )
    rejects(
        conn,
        "changing any column of a superseded version",
        lambda: conn.execute(update(table).where(table.c.id == row).values(policy={"x": 1})),
    )
    rejects(
        conn,
        "deleting a superseded version",
        lambda: conn.execute(delete(table).where(table.c.id == row)),
    )


# --- migration ------------------------------------------------

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


def broken_ddl_without_replace() -> tuple[str, ...]:
    """PG_DDL with `CREATE OR REPLACE TRIGGER` weakened to `CREATE TRIGGER`."""
    return tuple(s.replace("CREATE OR REPLACE TRIGGER", "CREATE TRIGGER") for s in PG_DDL)
