"""Properties of the owner's admin command line (10 §9 "First deploy", HAT-OPS-091, PR 35).

The commands run through `main` against an in-memory database, with a fake broker for the
onboarding, a scripted backtest evidence and a settable clock; no network is touched.
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
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.base import Base
from app.hat.admin import AdminDeps, main
from app.hat.data.sessions import IST
from app.hat.engine.portfolio import lifecycle
from app.hat.engine.strategy.versions import seed_text
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.models import (
    HatBacktestRun,
    HatBot,
    HatEvent,
    HatStrategyVersion,
    HatTradingCalendar,
)
from app.hat.time import as_utc

START = date(2026, 11, 2)  # a Monday
CALENDAR = calendar_around(START + timedelta(days=30), span=40)
SYMBOLS = ("AAA", "BBB", "CCC", "DDD")
CHAT = 4242
USER = uuid.UUID("00000000-0000-7000-8000-000000000001")
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
    engine: Engine
    tmp: Path
    now: datetime = field(default_factory=lambda: night(START))
    env: dict[str, str] = field(default_factory=lambda: {"HAT_TELEGRAM_CHAT_ID": str(CHAT)})
    evidence: lifecycle.BacktestFacts = EVIDENCE

    def session(self) -> Session:
        return sessionmaker(self.engine, expire_on_commit=False)()

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


def new_world(tmp: Path) -> World:
    engine = memory_engine()
    with Session(engine) as session:
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
        run = HatBacktestRun(
            id=uuid.uuid4(),
            strategy_version_id=uuid.UUID(version_id),
            risk_policy_version_id=uuid.uuid4(),
            family_key="probe",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            data_snapshot={},
            status="completed",
            metrics={},
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
        "publish-universe", "--constituents", str(csv), "--as-of", START.isoformat()
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


@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(waited=st.integers(0, 25))
@example(waited=19)
@example(waited=20)
def test_first_deploy(tmp_path: Path, waited: int) -> None:
    """PBT admin.first_deploy (HAT-OPS-091, HAT-PORT-060, HAT-BOT-084): the first-deploy
    runbook, run through the command line, takes a fresh database to a sleeve in Shadow that
    scans (a version and a live universe), then to Live only after 20 trading sessions of shadow
    and only on a promoted version; the owner's steps are recorded as the owner's. Harm: a fresh
    deploy never scans, or a sleeve trades real money on an unproven version."""
    world = new_world(tmp_path)
    bot, version = deploy_to_shadow(world)
    with world.scope() as session:
        row = session.get(HatBot, uuid.UUID(bot))
        assert row is not None
        assert row.lifecycle_state == lifecycle.SHADOW
        assert str(row.live_version_id) == version
        assert row.universe_version_id is not None
    code, _, err = world.run("promote", "--bot", bot, "--to", "live")
    assert code == 2
    assert err == "refused: version_not_live\n"
    day = trading_days_after(START, waited) if waited else START
    world.now = night(day)
    code, result, err = world.run("promote-version", "--version", version)
    if waited < lifecycle.SHADOW_DAYS:
        assert code == 2
        assert err == "refused: shadow_days\n"
        return
    assert code == 0, err
    assert result["status"] == "live"
    assert world.ok("promote", "--bot", bot, "--to", "live")["lifecycle_state"] == "live"
    config = world.tmp / "next.yaml"
    config.write_text(seed_text("stage2-contraction-breakout"), "utf-8")
    with world.scope() as session:
        strategy_id = session.get(HatStrategyVersion, uuid.UUID(version)).strategy_id  # type: ignore[union-attr]
    draft = world.ok("save-draft", "--strategy", str(strategy_id), "--config", str(config))
    assert (draft["version"], draft["status"]) == (2, "draft")
    with world.scope() as session:
        owner_events = session.scalars(
            select(HatEvent).where(
                HatEvent.event_type.in_(("strategy.promoted", "sleeve.lifecycle_changed"))
            )
        ).all()
        assert len(owner_events) == 4  # backtested, shadow, version promoted, live
        for event in owner_events:
            assert event.actor == "user"
            assert event.payload["chat_id"] == CHAT
            assert event.payload["via"] == "cli"


def snapshot(world: World) -> dict[str, list[str]]:
    """Every row of every table, as text: what a refused command must leave unchanged."""
    with world.engine.connect() as conn:
        return {
            t.name: sorted(repr(tuple(r)) for r in conn.execute(select(t)))
            for t in Base.metadata.sorted_tables
        }


REFUSALS = {
    "promote_live_from_shadow": "version_not_live",
    "promote_version_early": "shadow_days",
    "promote_version_in_hours": "market_hours",
    "shadow_version_twice": "not_backtested",
    "assign_draft": "version_not_released",
    "unknown_symbol": "unknown_symbols: ZZZ",
    "no_owner_chat": "no_owner_chat",
    "no_such_edge": "no_such_edge",
    "publish_other_run": "run_not_for_version",
    "promote_in_hours": "market_hours",
    "universe_unknown_bot": "bot_unknown",
    "universe_in_hours": "market_hours",
    "isin_mismatch": "unknown_symbols: BBB(INE123456A01)",
    "bots_on_unknown_account": "account_not_active",
    "draft_in_hours": "market_hours",
    "draft_bad_utf8": "config_unreadable",
}


@settings(
    max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(case=st.sampled_from(sorted(REFUSALS)))
def test_refusal_writes_nothing(tmp_path: Path, case: str) -> None:
    """PBT admin.refusal_writes_nothing (HAT-OPS-091): a command the services refuse exits 2
    with the fixed code of the rule that refused it on stderr and leaves every table as it was.
    Harm: a half-applied release step (a version promoted but the sleeves not moved, a universe
    published but not given to the sleeves)."""
    world = new_world(tmp_path)
    bot, version = deploy_to_shadow(world)
    with world.scope() as session:
        strategy_id = session.get(HatStrategyVersion, uuid.UUID(version)).strategy_id  # type: ignore[union-attr]
        draft = session.scalar(
            select(HatStrategyVersion.id).where(
                HatStrategyVersion.strategy_id != strategy_id,
                HatStrategyVersion.status == "draft",
            )
        )
        other_bot = session.scalar(select(HatBot.id).where(HatBot.strategy_id != strategy_id))
    later = trading_days_after(START, lifecycle.SHADOW_DAYS)
    argv: tuple[str, ...]
    if case == "promote_live_from_shadow":
        argv = ("promote", "--bot", bot, "--to", "live")
    elif case == "promote_version_early":
        argv = ("promote-version", "--version", version)
    elif case == "promote_version_in_hours":
        world.now = at(later, 11)
        argv = ("promote-version", "--version", version)
    elif case == "shadow_version_twice":
        argv = ("shadow-version", "--version", version)
    elif case == "assign_draft":
        argv = ("assign-version", "--bot", str(other_bot), "--version", str(draft))
    elif case == "unknown_symbol":
        csv = constituents_csv(world.tmp, (*SYMBOLS[:2], "ZZZ"))
        argv = ("publish-universe", "--constituents", str(csv), "--as-of", START.isoformat())
    elif case == "no_owner_chat":
        world.env = {}
        world.now = night(later)
        argv = ("promote-version", "--version", version)
    elif case == "no_such_edge":
        argv = ("promote", "--bot", bot, "--to", "retired")
    elif case == "promote_in_hours":
        world.now = at(START + timedelta(days=1), 11)
        argv = ("promote", "--bot", bot, "--to", "probation")
    elif case == "universe_in_hours":
        world.now = at(START + timedelta(days=1), 11)
        csv = constituents_csv(world.tmp, SYMBOLS)
        argv = ("publish-universe", "--constituents", str(csv), "--as-of", START.isoformat())
    elif case == "isin_mismatch":
        csv = constituents_csv(world.tmp, SYMBOLS, {"BBB": "INE123456A01"})
        argv = ("publish-universe", "--constituents", str(csv), "--as-of", START.isoformat())
    elif case == "bots_on_unknown_account":
        argv = ("seed-bots", "--account", str(uuid.uuid4()))
    elif case in ("draft_in_hours", "draft_bad_utf8"):
        config = world.tmp / f"draft-{uuid.uuid4().hex}.yaml"
        config.write_text(seed_text("stage2-contraction-breakout"), "utf-8")
        if case == "draft_in_hours":
            world.now = at(START + timedelta(days=1), 11)
        else:
            config.write_bytes(b"bot: \xff\xfe\n")
        argv = ("save-draft", "--strategy", str(strategy_id), "--config", str(config))
    elif case == "universe_unknown_bot":
        csv = constituents_csv(world.tmp, SYMBOLS)
        argv = ("publish-universe", "--constituents", str(csv), "--as-of", START.isoformat())
        argv = (*argv, "--bot", bot, "--bot", str(uuid.uuid4()))
    else:
        run = add_run(world, version)
        argv = ("publish-version", "--version", str(draft), "--run", run)
    before = snapshot(world)
    code, _, err = world.run(*argv)
    assert code == 2
    assert err == f"refused: {REFUSALS[case]}\n"
    assert snapshot(world) == before
    with world.scope() as session:
        assert session.scalar(select(func.count()).select_from(HatEvent)) > 0
