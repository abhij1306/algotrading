"""The owner's admin command line (10 §9 "First deploy", HAT-OPS-091, PR 35).

    python -m app.hat.admin <command> [options]

It runs on the host, as the owner, and is never an HTTP route (HAT-DEC-T3). Each command is a
thin wrapper over one guarded service function (or a short sequence of them), in one database
transaction: a refusal prints a fixed code, exits 2 and writes nothing. CONF
`ops.admin_cli_calls_services` keeps it that way: this module reads no table and writes no column
itself.

Commands, in first-deploy order:

    show                                      strategies, versions and sleeves, with their ids
    seed-policy                               risk-policy version 1 (06 §1)
    onboard-account --account-ref REF         capability check of the broker account (HAT-BRK-092)
    seed-strategies                           the two strategies, version 1 as a draft
    seed-bots --account ID                    one sleeve per strategy on an active account
    confirm-ddpi --account ID --since DATE --note TEXT
                                              the owner's word that DDPI is registered on the
                                              account's demat (HAT-GOV-085), outside market hours
    publish-universe --constituents CSV --as-of DATE [--name N] [--include S]... [--exclude S]...
                     [--bot ID]...            a universe version from an NSE index CSV, made live
                                              and given to the sleeves (default: all of them)
    save-draft --strategy ID --config YAML    a new draft version, outside market hours
                                              (HAT-BOT-084)
    publish-version --version ID --run ID     draft -> backtested on a completed run (HAT-BT-061)
    assign-version --bot ID --version ID      the version a sleeve scans and trades with
    shadow-version --version ID               backtested -> shadow; starts the 20 sessions
    promote --bot ID --to STATE               a lifecycle edge, under its guard (HAT-PORT-060)
    promote-version --version ID              shadow -> live after 20 sessions (HAT-BOT-084)

Input files (`--constituents`, `--config`) are names inside `<HAT_DATA_ROOT>/admin/`: a path that
leaves that folder, or a file without the expected suffix, is refused (`file_outside_admin`).

`confirm-ddpi`, `promote` and `promote-version` are the owner's acts: their events carry
`actor = user`, the allowlisted chat id (`HAT_TELEGRAM_CHAT_ID`) and `via = cli`.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .backtest.runs import backtest_evidence
from .data.calendar import session_calendar
from .data.reference import parse_industry_csv
from .engine.portfolio import lifecycle
from .engine.portfolio import service as portfolio
from .engine.portfolio.service import BacktestEvidence
from .engine.strategy import versions
from .engine.strategy.config import ConfigError
from .engine.strategy.yamlio import import_yaml
from .errors import HatConfigError, HatError
from .execution.accounts import (
    ADAPTER_FACTORIES,
    OnboardingError,
    confirm_ddpi,
    onboard_account,
    require_active_account,
)
from .execution.broker import HatBroker
from .risk.policy import RiskPolicy, effective_version
from .risk.policy_service import load_versions, seed_initial_policy
from .risk.sessions import SessionCalendar
from .telegram.config import owner_chat_id
from .time import trade_date_of, utc_now

__all__ = ["AdminDeps", "AdminError", "main"]

REFUSED = 2


class AdminError(HatError):
    """A command was refused before a service ran; `code` names the reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AdminDeps:
    """What a command needs from outside: the owner's user id, the folder its input files are
    read from, the transaction, the broker of an onboarding, the backtest evidence, the
    environment and the clock. `production()` builds the real ones."""

    user_id: uuid.UUID
    files: Path
    scope: Callable[[], AbstractContextManager[Session]]
    broker_for: Callable[[str], HatBroker]
    evidence: BacktestEvidence
    env: Mapping[str, str]
    clock: Callable[[], datetime]

    @staticmethod
    def production() -> AdminDeps:  # pragma: no cover - needs the database and .env
        from app.database import db_session_scope  # noqa: PLC0415

        from .settings import operator_settings, process_env  # noqa: PLC0415

        settings = operator_settings()  # loads .env first, as every command line does
        return AdminDeps(
            user_id=settings.user_id,
            files=settings.data_root / "admin",
            scope=db_session_scope,
            broker_for=lambda name: ADAPTER_FACTORIES[name](),
            evidence=backtest_evidence,
            env=process_env(),
            clock=utc_now,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.hat.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    commands.add_parser("seed-policy")
    onboard = commands.add_parser("onboard-account")
    onboard.add_argument("--account-ref", required=True)
    onboard.add_argument("--broker", default="fyers")
    commands.add_parser("seed-strategies")
    bots = commands.add_parser("seed-bots")
    bots.add_argument("--account", type=uuid.UUID, required=True)
    universe = commands.add_parser("publish-universe")
    universe.add_argument("--constituents", required=True)
    universe.add_argument("--as-of", type=date.fromisoformat, required=True)
    universe.add_argument("--name", default="nifty500")
    universe.add_argument("--include", action="append", default=[])
    universe.add_argument("--exclude", action="append", default=[])
    universe.add_argument("--bot", type=uuid.UUID, action="append", default=[])
    draft = commands.add_parser("save-draft")
    draft.add_argument("--strategy", type=uuid.UUID, required=True)
    draft.add_argument("--config", required=True)
    publish = commands.add_parser("publish-version")
    publish.add_argument("--version", type=uuid.UUID, required=True)
    publish.add_argument("--run", type=uuid.UUID, required=True)
    assign = commands.add_parser("assign-version")
    assign.add_argument("--bot", type=uuid.UUID, required=True)
    assign.add_argument("--version", type=uuid.UUID, required=True)
    for name in ("shadow-version", "promote-version"):
        commands.add_parser(name).add_argument("--version", type=uuid.UUID, required=True)
    promote = commands.add_parser("promote")
    promote.add_argument("--bot", type=uuid.UUID, required=True)
    promote.add_argument("--to", choices=lifecycle.STATES, required=True)
    ddpi = commands.add_parser("confirm-ddpi")
    ddpi.add_argument("--account", type=uuid.UUID, required=True)
    ddpi.add_argument("--since", type=date.fromisoformat, required=True)
    ddpi.add_argument("--note", required=True)
    return parser


def _calendar(session: Session) -> SessionCalendar:
    calendar = session_calendar(session)
    if calendar is None:
        raise AdminError("no_calendar")
    return calendar


def _policy(session: Session, user_id: uuid.UUID, now: datetime) -> RiskPolicy:
    """The risk policy in force now (the lifecycle guards read its health bounds)."""
    version = effective_version(load_versions(session, user_id), now)
    if version is None:
        raise AdminError("no_policy")
    return version.policy


def _read(root: Path, name: str, suffixes: tuple[str, ...], code: str) -> str:
    """A file named inside the admin folder: anything resolving outside it, or with another
    suffix, is refused before it is opened."""
    base = root.resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base) or path.suffix.lower() not in suffixes:
        raise AdminError("file_outside_admin")
    try:
        return path.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        raise AdminError(code) from None


def _publish_universe(
    session: Session, args: argparse.Namespace, deps: AdminDeps, now: datetime
) -> dict[str, Any]:
    user_id = deps.user_id
    calendar = _calendar(session)
    content = _read(deps.files, args.constituents, (".csv",), "constituents_unreadable")
    rows = parse_industry_csv(content)
    if not rows:
        raise AdminError("constituents_empty")
    market_id = portfolio.market_id_of(session)
    constituents, unknown = portfolio.resolve_symbols(
        session,
        market_id,
        [row.symbol for row in rows],
        args.as_of,
        {row.symbol: row.isin for row in rows},
    )
    include, unknown_in = portfolio.resolve_symbols(session, market_id, args.include, args.as_of)
    exclude, unknown_ex = portfolio.resolve_symbols(session, market_id, args.exclude, args.as_of)
    missing = sorted({*unknown, *unknown_in, *unknown_ex})
    if missing:
        # every symbol must name an instrument: a silent drop would trade an unapproved list
        raise AdminError("unknown_symbols: " + " ".join(missing))
    named = portfolio.universe_named(session, user_id=user_id, market_id=market_id, name=args.name)
    draft = portfolio.seed_universe_version(
        session,
        named.id,
        constituents=constituents,
        include=include,
        exclude=exclude,
        as_of=args.as_of,
    )
    live = portfolio.publish_version(session, draft.id, now)
    bot_ids = args.bot or [
        uuid.UUID(b["id"]) for b in versions.release_overview(session, user_id)["bots"]
    ]
    for bot_id in bot_ids:
        portfolio.assign_universe(
            session, bot_id=bot_id, universe_version_id=live.id, calendar=calendar, now=now
        )
    return {
        "universe_id": str(named.id),
        "universe_version_id": str(live.id),
        "version": live.version,
        "members": live.definition["members"],
        "bots": [str(b) for b in bot_ids],
    }


def _setup(
    session: Session, args: argparse.Namespace, deps: AdminDeps, now: datetime
) -> dict[str, Any] | None:
    """The setup commands; None for a release command."""
    user_id = deps.user_id
    command = args.command
    if command == "show":
        return versions.release_overview(session, user_id)
    if command == "seed-policy":
        row = seed_initial_policy(session, user_id, now)
        return {"risk_policy_version": row.version, "id": str(row.id)}
    if command == "onboard-account":
        broker = deps.broker_for(args.broker) if args.broker in ADAPTER_FACTORIES else None
        if broker is None:
            raise AdminError("unsupported_broker")
        account = onboard_account(
            session, broker, user_id=user_id, broker_name=args.broker, account_ref=args.account_ref
        )
        return {"broker_account_id": str(account.id), "status": account.status}
    if command == "seed-strategies":
        rows = versions.seed_default_strategies(session, user_id)
        return {"strategies": {s.name: str(s.id) for s in rows}}
    if command == "seed-bots":
        require_active_account(session, args.account)
        bots = versions.seed_default_bots(session, user_id=user_id, broker_account_id=args.account)
        return {"bots": [str(b.id) for b in bots]}
    if command == "publish-universe":
        return _publish_universe(session, args, deps, now)
    return None


def _release(
    session: Session, args: argparse.Namespace, deps: AdminDeps, now: datetime
) -> dict[str, Any] | None:
    """The version commands that need no owner chat; None for the owner's acts."""
    command = args.command
    if command == "save-draft":
        content = _read(deps.files, args.config, (".yaml", ".yml"), "config_unreadable")
        config = import_yaml(content)
        row = versions.save_draft(
            session,
            strategy_id=args.strategy,
            config=config,
            calendar=_calendar(session),
            now=now,
        )
        return {"version_id": str(row.id), "version": row.version, "status": row.status}
    if command == "publish-version":
        row = versions.publish_version(session, args.version, backtest_run_id=args.run)
        return {"version_id": str(row.id), "status": row.status}
    if command == "assign-version":
        bot = versions.assign_version(session, bot_id=args.bot, version_id=args.version)
        return {"bot_id": str(bot.id), "live_version_id": str(bot.live_version_id)}
    if command == "shadow-version":
        row = versions.start_shadow(session, args.version, calendar=_calendar(session), now=now)
        return {"version_id": str(row.id), "status": row.status}
    return None


def _owner(
    session: Session, args: argparse.Namespace, deps: AdminDeps, now: datetime
) -> dict[str, Any]:
    """The owner's acts, `confirm-ddpi`, `promote-version` and `promote`, under the allowlisted
    chat id."""
    calendar = _calendar(session)
    try:
        chat_id = owner_chat_id(deps.env)
    except HatConfigError:
        raise AdminError("no_owner_chat") from None
    if args.command == "confirm-ddpi":
        account = confirm_ddpi(
            session,
            args.account,
            user_id=deps.user_id,
            since=args.since,
            note=args.note,
            calendar=calendar,
            now=now,
            chat_id=chat_id,
        )
        return {"broker_account_id": str(account.id), "ddpi_confirmed": account.ddpi_confirmed}
    if args.command == "promote-version":
        row = versions.promote_version(
            session, args.version, calendar=calendar, now=now, chat_id=chat_id
        )
        return {"version_id": str(row.id), "status": row.status}
    trade_date = trade_date_of(now)
    bot = portfolio.owner_transition(
        session,
        args.bot,
        args.to,
        trade_date=trade_date,
        policy=_policy(session, deps.user_id, now),
        backtest=deps.evidence,
        calendar=calendar,
        chat_id=chat_id,
        now=now,
    )
    return {"bot_id": str(bot.id), "lifecycle_state": bot.lifecycle_state}


def _run(session: Session, args: argparse.Namespace, deps: AdminDeps) -> dict[str, Any]:
    now = deps.clock()
    result = _setup(session, args, deps, now)
    if result is None:
        result = _release(session, args, deps, now)
    return _owner(session, args, deps, now) if result is None else result


def _code(exc: HatError) -> str:
    if isinstance(exc, ConfigError):  # the paths and codes, never the submitted values
        return "invalid_config: " + " ".join(sorted(f"{p}:{c}" for p, c in exc.problems))
    if isinstance(exc, OnboardingError):
        return str(exc).replace(" ", "_")
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else type(exc).__name__


def main(argv: Sequence[str] | None = None, deps: AdminDeps | None = None) -> int:
    """Run one command in one transaction. Prints the result as JSON and returns 0, or prints
    `refused: <code>` to stderr and returns 2 with nothing written. Database errors print a
    fixed code, never the database's text."""
    args = _parser().parse_args(argv)
    deps = deps or AdminDeps.production()
    try:
        with deps.scope() as session:
            result = _run(session, args, deps)
    except HatError as exc:
        sys.stderr.write(f"refused: {_code(exc)}\n")
        return REFUSED
    except SQLAlchemyError:
        sys.stderr.write("refused: database_error\n")
        return REFUSED
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
