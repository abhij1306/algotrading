"""The owner's admin command line (HAT-OPS-091, PR 35).

    python -m app.hat.admin <command> [options]

It runs on the host, as the owner, and is never an HTTP route (HAT-DEC-T3). Each command is a
thin wrapper over one guarded service function (or a short sequence of them), in one database
transaction: a refusal prints a fixed code, exits 2 and writes nothing. CONF
`ops.admin_cli_calls_services` keeps it that way: this module reads no table and writes no column
itself.

Commands, in first-deploy order:

    show                                      strategies, versions and sleeves, with their ids
    seed-policy                               risk-policy version 1
    onboard-account --account-ref REF         capability check of the broker account (HAT-BRK-092)
    seed-strategies                           the two strategies, version 1 as a draft
    seed-bots --account ID                    one sleeve per strategy on an active account
    add-sleeve --strategy ID --account ID     a further sleeve of a strategy, in Research: a new
                                              version runs in shadow on it beside the Live one
    confirm-ddpi --account ID --since DATE --note TEXT
                                              the owner's word that DDPI is registered on the
                                              account's demat (HAT-GOV-085), outside market hours
    revoke-ddpi --account ID --note TEXT      withdraws it, at any time: the account's bots run
                                              shadow from their next routing decision
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
    resolve-action --position ID --action KEY --close-at PRICE
                                              closes a virtual position a corporate action
                                              locked, at PRICE (HAT-EXEC-144; a real one is
                                              refused: /flatten it)
    correct-action --isin ISIN --ex-date DATE --type TYPE (--factor F [--as TYPE] | --delete)
                   --note TEXT                replaces or deletes one stored corporate-action
                                              factor (a conflict alert, a misparsed bonus; D1)

Input files (`--constituents`, `--config`) are names inside `<HAT_DATA_ROOT>/admin/`: a path that
leaves that folder, or a file without the expected suffix, is refused (`file_outside_admin`).

`assign-version`, `confirm-ddpi`, `revoke-ddpi`, `promote`, `promote-version`, `resolve-action`
and `correct-action` are the owner's acts: their events carry `actor = user`, the allowlisted
chat id (`HAT_TELEGRAM_CHAT_ID`) and `via = cli`.
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
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .clock import SYSTEM_CLOCK, Clock
from .data.calendar import session_calendar
from .data.corporate_actions import ACTION_TYPES, Correction, correct_action
from .data.reference import parse_industry_csv
from .engine.owner import OwnerCommand
from .engine.portfolio import lifecycle, sleeve_service, universe_service
from .engine.portfolio.common import market_id_of
from .engine.rescale import CLOSE_REASON, resolve_action
from .engine.shadow import close_position
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
    revoke_ddpi,
)
from .execution.broker import HatBroker
from .money import exact
from .risk.policy import RiskPolicy, effective_version
from .risk.policy_service import load_versions, seed_initial_policy
from .risk.sessions import SessionCalendar
from .telegram.config import owner_chat_id
from .time import utc_now

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
    read from, the transaction, the broker of an onboarding, the
    environment and the clock. `production()` builds the real ones."""

    user_id: uuid.UUID
    files: Path
    scope: Callable[[], AbstractContextManager[Session]]
    broker_for: Callable[[str], HatBroker]
    env: Mapping[str, str]
    clock: Clock

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
            env=process_env(),
            clock=SYSTEM_CLOCK,
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
    sleeve = commands.add_parser("add-sleeve")
    sleeve.add_argument("--strategy", type=uuid.UUID, required=True)
    sleeve.add_argument("--account", type=uuid.UUID, required=True)
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
    revoke = commands.add_parser("revoke-ddpi")
    revoke.add_argument("--account", type=uuid.UUID, required=True)
    revoke.add_argument("--note", required=True)
    resolve = commands.add_parser("resolve-action")
    resolve.add_argument("--position", type=uuid.UUID, required=True)
    resolve.add_argument("--action", required=True)
    resolve.add_argument("--close-at", type=_price, required=True)
    correct = commands.add_parser("correct-action")
    correct.add_argument("--isin", required=True)
    correct.add_argument("--ex-date", type=date.fromisoformat, required=True)
    correct.add_argument("--type", choices=ACTION_TYPES, required=True)
    change = correct.add_mutually_exclusive_group(required=True)
    change.add_argument("--factor", type=_factor)
    change.add_argument("--delete", action="store_true")
    correct.add_argument("--as", dest="as_type", choices=ACTION_TYPES)
    correct.add_argument("--note", required=True)
    return parser


def _price(raw: str) -> Decimal:
    """A `--close-at` price: a finite decimal, never a float."""
    value = exact(raw)
    if value is None:
        raise argparse.ArgumentTypeError("not a price")
    return value


def _factor(raw: str) -> Decimal:
    """A `--factor`: a positive finite decimal, never a float."""
    value = exact(raw, positive=True)
    if value is None:
        raise argparse.ArgumentTypeError("not a positive factor")
    return value


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
    market_id = market_id_of(session)
    constituents, unknown = universe_service.resolve_symbols(
        session,
        market_id,
        [row.symbol for row in rows],
        args.as_of,
        {row.symbol: row.isin for row in rows},
    )
    include, unknown_in = universe_service.resolve_symbols(
        session, market_id, args.include, args.as_of
    )
    exclude, unknown_ex = universe_service.resolve_symbols(
        session, market_id, args.exclude, args.as_of
    )
    missing = sorted({*unknown, *unknown_in, *unknown_ex})
    if missing:
        # every symbol must name an instrument: a silent drop would trade an unapproved list
        raise AdminError("unknown_symbols: " + " ".join(missing))
    named = universe_service.universe_named(
        session, user_id=user_id, market_id=market_id, name=args.name
    )
    draft = universe_service.seed_universe_version(
        session,
        named.id,
        constituents=constituents,
        include=include,
        exclude=exclude,
        as_of=args.as_of,
    )
    live = universe_service.publish_version(session, draft.id, now)
    bot_ids = args.bot or [
        uuid.UUID(b["id"]) for b in versions.release_overview(session, user_id)["bots"]
    ]
    for bot_id in bot_ids:
        universe_service.assign_universe(
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
        require_active_account(session, args.account, user_id)
        bots = versions.seed_default_bots(session, user_id=user_id, broker_account_id=args.account)
        return {"bots": [str(b.id) for b in bots]}
    if command == "add-sleeve":
        require_active_account(session, args.account, user_id)
        bot = versions.add_sleeve(
            session, user_id=user_id, strategy_id=args.strategy, broker_account_id=args.account
        )
        return {"bot_id": str(bot.id), "lifecycle_state": bot.lifecycle_state}
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
    if command == "shadow-version":
        row = versions.start_shadow(session, args.version, calendar=_calendar(session), now=now)
        return {"version_id": str(row.id), "status": row.status}
    return None


def _owner(
    session: Session, args: argparse.Namespace, deps: AdminDeps, now: datetime
) -> dict[str, Any]:
    """The owner's acts (`assign-version`, `confirm-ddpi`, `revoke-ddpi`, `promote-version`,
    `resolve-action`, `correct-action` and `promote`) under the allowlisted chat id."""
    try:
        chat_id = owner_chat_id(deps.env)
    except HatConfigError:
        raise AdminError("no_owner_chat") from None
    if args.command == "correct-action":  # stored reference data: any time, no calendar
        correction = Correction(
            isin=args.isin,
            ex_date=args.ex_date,
            action_type=args.type,
            factor=None if args.delete else args.factor,
            new_type=args.as_type,
            note=args.note,
        )
        return correct_action(session, correction, chat_id=chat_id, user_id=deps.user_id, now=now)
    if args.command == "revoke-ddpi":  # only narrows, so it needs no calendar (any time)
        account = revoke_ddpi(
            session, args.account, user_id=deps.user_id, note=args.note, now=now, chat_id=chat_id
        )
        return {"broker_account_id": str(account.id), "ddpi_confirmed": account.ddpi_confirmed}
    owner = OwnerCommand(chat_id, now, _calendar(session))
    if args.command == "confirm-ddpi":
        account = confirm_ddpi(
            session,
            args.account,
            user_id=deps.user_id,
            since=args.since,
            note=args.note,
            command=owner,
        )
        return {"broker_account_id": str(account.id), "ddpi_confirmed": account.ddpi_confirmed}
    if args.command == "assign-version":
        bot = versions.assign_version(
            session, bot_id=args.bot, version_id=args.version, command=owner
        )
        return {"bot_id": str(bot.id), "live_version_id": str(bot.live_version_id)}
    if args.command == "resolve-action":
        position = resolve_action(session, args.position, args.action, args.close_at, owner)
        close_position(session, position.id, args.close_at, CLOSE_REASON, now)
        return {"position_id": str(position.id), "closed": position.closed_at is not None}
    if args.command == "promote-version":
        row = versions.promote_version(session, args.version, owner)
        return {"version_id": str(row.id), "status": row.status}
    bot = sleeve_service.owner_transition(
        session, args.bot, args.to, _policy(session, deps.user_id, now), owner
    )
    return {"bot_id": str(bot.id), "lifecycle_state": bot.lifecycle_state}


def _run(session: Session, args: argparse.Namespace, deps: AdminDeps) -> dict[str, Any]:
    now = utc_now(deps.clock)
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
