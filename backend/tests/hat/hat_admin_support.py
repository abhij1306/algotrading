"""The rig of the owner's admin command line checks (HAT-OPS-091): a
`World` runs the commands through `main` against a database (in memory for the properties,
Postgres for the postgres suite), with a fake broker for the onboarding, a scripted backtest
evidence and a settable clock; no network is touched.
"""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from hat.fakes.broker import FakeBroker
from hat.hat_live_support import add_instrument
from hat.hat_mcp_support import seed_market
from hat.hat_risk_support import calendar_around
from hat.hat_support import memory_engine
from sqlalchemy import Connection, Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.base import Base
from app.hat.admin import AdminDeps, main
from app.hat.data.sessions import IST
from app.hat.engine.portfolio import lifecycle
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.models import HatBacktestRun, HatRiskPolicyVersion, HatTradingCalendar
from app.hat.time import as_utc

START = date(2026, 11, 2)  # a Monday
CALENDAR = calendar_around(START + timedelta(days=30), span=40)
SYMBOLS = ("AAA", "BBB", "CCC", "DDD")
CHAT = 4242
# not the settings' default user, so an event filed under that default instead of the owner's
# user is seen
USER = uuid.UUID("00000000-0000-7000-8000-000000000073")
EVIDENCE = lifecycle.BacktestFacts(
    dsr_prob=Decimal("0.99"), trades=250, expectancy=Decimal("0.3"), trials=1
)


def at(day: date, hour: int) -> datetime:
    """An IST wall-clock hour as UTC (SQLite keeps no offset, so the clock hands out UTC)."""
    return datetime.combine(day, time(hour, 0), tzinfo=IST).astimezone(UTC)


def night(day: date) -> datetime:
    return at(day, 20)


def trading_days_after(day: date, count: int) -> date:
    """The `count`-th trading session after `day` on the test calendar."""
    later = [d for d in CALENDAR.trading_days() if d > day]
    return later[count - 1]


@dataclass
class World:
    engine: Engine | Connection
    tmp: Path
    now: datetime = field(default_factory=lambda: night(START))
    env: dict[str, str] = field(default_factory=lambda: {"HAT_TELEGRAM_CHAT_ID": str(CHAT)})
    evidence: lifecycle.BacktestFacts = EVIDENCE

    def session(self) -> Session:
        # on a Connection a commit releases a savepoint, so the caller's transaction rolls back
        return sessionmaker(
            self.engine, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )()

    @contextmanager
    def scope(self) -> Iterator[Session]:
        session = self.session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def deps(self) -> AdminDeps:
        return AdminDeps(
            user_id=USER,
            files=self.tmp,
            scope=self.scope,
            broker_for=lambda _name: FyersHatBroker(FakeBroker(), instruments=lambda: []),  # type: ignore[arg-type]
            evidence=lambda _s, _b: self.evidence,
            env=self.env,
            clock=lambda: self.now,
        )

    def run(self, *argv: str) -> tuple[int, Any, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv), self.deps())
        return code, (json.loads(out.getvalue()) if code == 0 else None), err.getvalue()

    def ok(self, *argv: str) -> Any:  # noqa: ANN401 - the command's JSON
        code, result, err = self.run(*argv)
        assert code == 0, (argv, err)
        return result


def new_world(tmp: Path, engine: Engine | Connection | None = None) -> World:
    engine = memory_engine() if engine is None else engine
    with Session(engine, join_transaction_mode="create_savepoint") as session:
        market_id = seed_market(session)
        for day, row in CALENDAR.days.items():
            session.add(
                HatTradingCalendar(
                    market_id=market_id,
                    day=day,
                    is_trading=row.is_trading,
                    open_at=as_utc(row.open_at) if row.open_at else None,
                    close_at=as_utc(row.close_at) if row.close_at else None,
                )
            )
        for symbol in SYMBOLS:
            add_instrument(session, market_id, symbol, isin_of(symbol))
        session.commit()
    return World(engine, tmp)


def isin_of(symbol: str) -> str:
    return f"INE00000{SYMBOLS.index(symbol)}A01" if symbol in SYMBOLS else "INE999999A01"


def constituents_csv(
    tmp: Path, symbols: tuple[str, ...], isins: dict[str, str] | None = None
) -> Path:
    path = tmp / f"nifty-{uuid.uuid4().hex}.csv"
    isin = {s: isin_of(s) for s in symbols} | (isins or {})
    rows = "".join(f"{s} Ltd,Finance,{s},EQ,{isin[s]}\n" for s in symbols)
    path.write_text("Company Name,Industry,Symbol,Series,ISIN Code\n" + rows, "utf-8")
    return path


def add_run(world: World, version_id: str) -> str:
    """A completed backtest run of the version (the backtest itself is HAT-BT's)."""
    with world.scope() as session:
        policy = session.scalars(select(HatRiskPolicyVersion.id)).first()
        assert policy is not None  # `seed-policy` ran
        run = HatBacktestRun(
            id=uuid.uuid4(),
            strategy_version_id=uuid.UUID(version_id),
            risk_policy_version_id=policy,
            family_key="probe",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            data_snapshot={},
            status="completed",
            metrics={"for_promotion": True, "slot_order": "time"},  # a promotion run (HAT-BT-061)
        )
        session.add(run)
        return str(run.id)


def deploy_to_shadow(world: World) -> tuple[str, str]:
    """The runbook up to a sleeve in Shadow on a version in its shadow period: (bot, version)."""
    world.ok("seed-policy")
    account = world.ok("onboard-account", "--account-ref", "XA00000")
    assert account["status"] == "active"
    strategies = world.ok("seed-strategies")["strategies"]
    bots = world.ok("seed-bots", "--account", account["broker_account_id"])["bots"]
    csv = constituents_csv(world.tmp, SYMBOLS[:3])
    universe = world.ok(
        "publish-universe", "--constituents", csv.name, "--as-of", START.isoformat()
    )
    assert universe["members"] == 3
    assert sorted(universe["bots"]) == sorted(bots)
    shown = world.ok("show")
    strategy_id = strategies["stage2-contraction-breakout"]
    bot = next(b["id"] for b in shown["bots"] if b["strategy_id"] == strategy_id)
    version = next(s for s in shown["strategies"] if s["id"] == strategy_id)["versions"][0]["id"]
    world.ok("publish-version", "--version", version, "--run", add_run(world, version))
    world.ok("assign-version", "--bot", bot, "--version", version)
    world.ok("promote", "--bot", bot, "--to", "backtested")
    world.ok("promote", "--bot", bot, "--to", "shadow")
    world.ok("shadow-version", "--version", version)
    return bot, version


def snapshot(world: World, prefix: str = "") -> dict[str, list[str]]:
    """Every row of every table (whose name starts with `prefix`), as text: what a refused
    command must leave unchanged."""
    with world.scope() as session:
        return {
            t.name: sorted(repr(tuple(r)) for r in session.execute(select(t)))
            for t in Base.metadata.sorted_tables
            if t.name.startswith(prefix)
        }
