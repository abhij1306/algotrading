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
    show-policy                               the risk policy in force, as YAML: the starting
                                              point of a research run's candidate policy
                                              (`python -m app.hat.backtest --policy FILE`)
    set-policy --file FILE                    the policy in FILE (YAML or JSON) in force at
                                              once, outside market hours, only while nothing has
                                              traded for real (the pre-trading reset; then every
                                              change is gated: docs/invariants.md)
    onboard-account --account-ref REF [--broker B]
                                              capability check of the broker account (HAT-BRK-092);
                                              only the active broker's (`HAT_BROKER`, the default)
    refresh-kite-symbols [--dump CSV]         the zerodha broker symbols from NSE's latest
                                              bhavcopy and Kite's instrument dump (downloaded, or
                                              the dump CSV given), as the weekly refresh does
    seed-strategies                           the two strategies, version 1 as a draft
    seed-bots --account ID                    one sleeve per strategy on an active account
    add-sleeve --strategy ID --account ID     a further sleeve of a strategy, in Research: a new
                                              version runs in shadow on it beside the Live one
    confirm-ddpi --account ID --since DATE --note TEXT
                                              the owner's word that DDPI is registered on the
                                              account's demat (HAT-GOV-085), outside market hours
    revoke-ddpi --account ID --note TEXT      withdraws it, at any time: the account's bots run
                                              shadow from their next routing decision
    publish-universe --constituents CSV --as-of DATE [--name N] [--index I] [--include S]...
                     [--exclude S]... [--bot ID]...
                                              a universe version from an NSE index CSV, made live
                                              and given to the sleeves (default: all of them);
                                              stocks only (a REIT or placeholder row is skipped),
                                              and an --exclude drops its row before resolving;
                                              --index (default NIFTY 500) names the index whose
                                              point-in-time members a backtest of it reads
    load-index-membership --csv CSV --as-of DATE [--size N] [--apply]
                                              an index's membership history (QA5): replaces the
                                              file's index's rows; without --apply a dry run that
                                              writes nothing
    load-industry-history --dir DIR [--apply]  the point-in-time industries (hat_industry) from
                                              the NIFTY 500 list snapshots DIR/YYYY-MM-DD.csv
                                              (docs/decisions.md, Industry history), with the
                                              NIFTY 500 coverage from 2016; without --apply a
                                              dry run that writes nothing
    save-draft --strategy ID --config YAML    a new draft version, outside market hours
                                              (HAT-BOT-084)
    publish-version --version ID --run ID     draft -> frozen on a completed run (HAT-BT-061)
    assign-version --bot ID --version ID      the version a sleeve scans and trades with
    trial-version --version ID                frozen -> trial; starts the 20 paper sessions
    promote --bot ID --to STATE               a lifecycle edge, under its guard (HAT-PORT-060)
    release-version --version ID              trial -> released after 20 sessions (HAT-BOT-084)
    resolve-action --position ID --action KEY --close-at PRICE
                                              closes a virtual position a corporate action
                                              locked, at PRICE (HAT-EXEC-144; a real one is
                                              refused: /flatten it)
    resolve-sale --tag TAG --qty N --price P --at TIME --note NOTE [--fees RS]
                                              books a lost sale settled from the broker's
                                              holdings from its contract note (Q12): its fill,
                                              and the trade result's proceeds; outside market
                                              hours. TIME is ISO 8601 (IST when no offset)
    correct-action --isin ISIN --ex-date DATE --type TYPE
                   (--factor F [--as TYPE] | --cash RS | --delete) --note TEXT
                   [--known-on DATE]
                                              replaces or deletes one stored corporate-action
                                              factor (a conflict alert, a misparsed bonus; D1),
                                              or sets a dividend's cash per share (CA4);
                                              --known-on: when the value was public (CA5)
    correct-actions --list TSV [--apply]      the owner's reviewed list of such corrections
                                              (replace, delete, move, add; CA2; optional
                                              `cash` (CA4) and `known_on` (CA5) columns), one
                                              transaction; without --apply a dry run that
                                              writes nothing
    repair-symbol-history [--apply]           closes the overlapping symbol intervals from the
                                              raw bars' first days (audit M3, one-off);
                                              without --apply a dry run that writes nothing

Input files (`--constituents`, `--csv`, `--config`, `--list`, `--dump`, `--file`) are names inside
`<HAT_DATA_ROOT>/admin/`: a path that leaves that folder, or a file without the expected suffix, is
refused (`file_outside_admin`).

`set-policy`, `assign-version`, `confirm-ddpi`, `revoke-ddpi`, `promote`, `release-version`,
`resolve-action`, `resolve-sale`, `correct-action`, `correct-actions` and `repair-symbol-history`
are the owner's acts: their
events carry `actor = user`, the allowlisted chat id (`HAT_TELEGRAM_CHAT_ID`) and `via = cli`.
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
from .daily.books import read_books, report_doc
from .data.broker_symbols import refresh_kite_symbols
from .data.ca_corrections import apply_list, parse_list
from .data.calendar import session_calendar
from .data.corporate_actions import ACTION_TYPES, Correction, correct_action
from .data.index_membership import NIFTY_500, load_membership, parse_membership_csv
from .data.industry_history import Snapshot, load_industry_history, read_snapshots
from .data.kite_symbols import KiteSymbolData
from .data.reference import parse_industry_csv
from .data.renames import symbol_first_bars
from .data.store import DataStore
from .data.symbol_history import repair_symbol_history
from .engine.live_trades import BrokerFill, resolve_sale
from .engine.owner import OwnerCommand
from .engine.portfolio import lifecycle, sleeve_service, universe_service
from .engine.portfolio.common import market_id_of
from .engine.rescale import CLOSE_REASON, resolve_action
from .engine.shadow import close_position
from .engine.strategy import release, versions
from .engine.strategy.config import ConfigError
from .engine.strategy.yamlio import import_yaml
from .errors import HatConfigError, HatError
from .execution.accounts import (
    ADAPTER_FACTORIES,
    OnboardingError,
    active_adapters,
    confirm_ddpi,
    onboard_account,
    require_active_account,
    revoke_ddpi,
)
from .execution.broker import HatBroker
from .execution.kite_adapter import kite_symbol_source
from .money import exact
from .risk.policy import PolicyError, RiskPolicy, effective_version
from .risk.policy_file import policy_text, policy_yaml
from .risk.policy_service import load_versions, reset_policy_before_trading, seed_initial_policy
from .risk.sessions import SessionCalendar
from .settings import active_broker
from .telegram.config import owner_chat_id
from .time import IST, trade_date_of, utc_now

__all__ = ["AdminDeps", "AdminError", "main"]

REFUSED = 2


class AdminError(HatError):
    """A command was refused before a service ran; `code` names the reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# The commands that write hat_live's stock data (instruments, symbols, universes, index
# membership, corporate actions): the only ones given its writer (`db_session_scope`).
DATA_COMMANDS = frozenset(
    {
        "refresh-kite-symbols",
        "publish-universe",
        "load-index-membership",
        "load-industry-history",
        "correct-action",
        "correct-actions",
        "repair-symbol-history",
    }
)


@dataclass(frozen=True)
class AdminDeps:
    """What a command needs from outside: the owner's user id, the folder its input files are
    read from, the transaction (`writes_data` for the commands that write hat_live's stock data,
    `DATA_COMMANDS`), the broker of an onboarding, the
    environment, the clock and the bar store. `production()` builds the real ones."""

    user_id: uuid.UUID
    files: Path
    scope: Callable[..., AbstractContextManager[Session]]
    broker_for: Callable[[str], HatBroker]
    env: Mapping[str, str]
    clock: Clock
    store: Callable[[], DataStore]
    active_broker: str | None = None  # None: the settings' (`HAT_BROKER`)
    # the NSE tokens and Kite's listings as of a date, from a dump CSV's text or (None) downloaded
    kite_symbols: Callable[[date, str | None], KiteSymbolData] | None = None

    @staticmethod
    def production() -> AdminDeps:  # pragma: no cover - needs the database and .env
        from app.database import db_session_scope  # noqa: PLC0415

        from .settings import operator_settings, process_env  # noqa: PLC0415

        settings = operator_settings()  # loads .env first, as every command line does
        store = lambda: DataStore(settings.data_root, memory_limit=settings.duckdb_memory)  # noqa: E731
        return AdminDeps(
            user_id=settings.user_id,
            files=settings.data_root / "admin",
            scope=db_session_scope,
            broker_for=lambda name: active_adapters(settings.broker)[name](),
            env=process_env(),
            clock=SYSTEM_CLOCK,
            store=store,
            active_broker=settings.broker,
            kite_symbols=lambda day, dump: kite_symbol_source(store())(day, dump),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.hat.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    books = commands.add_parser("books")  # read-only: the live and paper books' report
    books.add_argument("--date", type=date.fromisoformat, default=None)
    commands.add_parser("seed-policy")
    commands.add_parser("show-policy")
    commands.add_parser("set-policy").add_argument("--file", required=True)
    onboard = commands.add_parser("onboard-account")
    onboard.add_argument("--account-ref", required=True)
    onboard.add_argument("--broker")
    kite = commands.add_parser("refresh-kite-symbols")
    kite.add_argument("--dump")
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
    universe.add_argument("--index", default=NIFTY_500)
    universe.add_argument("--include", action="append", default=[])
    universe.add_argument("--exclude", action="append", default=[])
    universe.add_argument("--bot", type=uuid.UUID, action="append", default=[])
    draft = commands.add_parser("save-draft")
    draft.add_argument("--strategy", type=uuid.UUID, required=True)
    draft.add_argument("--config", required=True)
    publish = commands.add_parser("publish-version")
    publish.add_argument("--version", type=uuid.UUID, required=True)
    publish.add_argument("--run", required=True)
    assign = commands.add_parser("assign-version")
    assign.add_argument("--bot", type=uuid.UUID, required=True)
    assign.add_argument("--version", type=uuid.UUID, required=True)
    for name in ("trial-version", "release-version"):
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
    sale = commands.add_parser("resolve-sale")
    sale.add_argument("--tag", required=True)
    sale.add_argument("--qty", type=int, required=True)
    sale.add_argument("--price", type=_price, required=True)
    sale.add_argument("--at", type=_instant, required=True)
    sale.add_argument("--note", required=True)
    sale.add_argument("--fees", type=_price, default=Decimal(0))
    correct = commands.add_parser("correct-action")
    correct.add_argument("--isin", required=True)
    correct.add_argument("--ex-date", type=date.fromisoformat, required=True)
    correct.add_argument("--type", choices=ACTION_TYPES, required=True)
    change = correct.add_mutually_exclusive_group(required=True)
    change.add_argument("--factor", type=_factor)
    change.add_argument("--cash", type=_cash)
    change.add_argument("--delete", action="store_true")
    correct.add_argument("--as", dest="as_type", choices=ACTION_TYPES)
    correct.add_argument("--note", required=True)
    correct.add_argument("--known-on", type=date.fromisoformat)
    membership = commands.add_parser("load-index-membership")
    membership.add_argument("--csv", required=True)
    membership.add_argument("--as-of", type=date.fromisoformat, required=True)
    membership.add_argument("--size", type=int, default=500)
    membership.add_argument("--apply", action="store_true")
    industries = commands.add_parser("load-industry-history")
    industries.add_argument("--dir", required=True)
    industries.add_argument("--apply", action="store_true")
    corrections = commands.add_parser("correct-actions")
    corrections.add_argument("--list", required=True)
    corrections.add_argument("--apply", action="store_true")
    repair = commands.add_parser("repair-symbol-history")
    repair.add_argument("--apply", action="store_true")
    return parser


def _price(raw: str) -> Decimal:
    """A `--close-at` price: a finite decimal, never a float."""
    value = exact(raw)
    if value is None:
        raise argparse.ArgumentTypeError("not a price")
    return value


def _instant(raw: str) -> datetime:
    """An `--at` time: ISO 8601, read in IST when it names no offset."""
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("not an ISO 8601 time") from None
    return value if value.tzinfo is not None else value.replace(tzinfo=IST)


def _factor(raw: str) -> Decimal:
    """A `--factor`: a positive finite decimal, never a float."""
    value = exact(raw, positive=True)
    if value is None:
        raise argparse.ArgumentTypeError("not a positive factor")
    return value


def _cash(raw: str) -> Decimal:
    """A `--cash`: a dividend's rupees per share, a positive finite decimal, never a float."""
    value = exact(raw, positive=True)
    if value is None:
        raise argparse.ArgumentTypeError("not a positive amount")
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


def _snapshots(root: Path, name: str) -> list[Snapshot]:
    """The industry snapshots of a folder inside the admin folder (refused outside it)."""
    base = root.resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base):
        raise AdminError("file_outside_admin")
    if not path.is_dir():
        raise AdminError("industry_snapshots_missing")
    return read_snapshots(path)


def _publish_universe(
    session: Session, args: argparse.Namespace, deps: AdminDeps, now: datetime
) -> dict[str, Any]:
    user_id = deps.user_id
    calendar = _calendar(session)
    content = _read(deps.files, args.constituents, (".csv",), "constituents_unreadable")
    excluded = _symbols(args.exclude)
    listed = parse_industry_csv(content)
    rows, not_stocks = universe_service.stock_rows(listed, excluded)
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
    exclude, unknown_ex = universe_service.resolve_symbols(session, market_id, excluded, args.as_of)
    # an exclude naming a row of the list has dropped it, known instrument or not; one naming
    # nothing in the list must still name an instrument (a mistyped exclude excludes nothing)
    unknown_ex = tuple(s for s in unknown_ex if s not in {row.symbol for row in listed})
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
        index=" ".join(args.index.upper().split()),
    )
    live = universe_service.publish_version(session, draft.id, now)
    bot_ids = args.bot or [
        uuid.UUID(b["id"]) for b in release.release_overview(session, user_id)["bots"]
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
        "not_stocks": list(not_stocks),
        "bots": [str(b) for b in bot_ids],
    }


def _symbols(raw: Sequence[str]) -> frozenset[str]:
    return frozenset(s.strip().upper() for s in raw if s.strip())


def _onboard(session: Session, args: argparse.Namespace, deps: AdminDeps) -> dict[str, Any]:
    """Onboard an account of the active broker (`HAT_BROKER`): one broker at a time."""
    active = deps.active_broker or active_broker(deps.env)
    name = args.broker or active
    if name not in ADAPTER_FACTORIES:
        raise AdminError("unsupported_broker")
    if name != active:
        raise AdminError("broker_not_active")
    account = onboard_account(
        session,
        deps.broker_for(name),
        user_id=deps.user_id,
        broker_name=name,
        account_ref=args.account_ref,
    )
    return {"broker_account_id": str(account.id), "status": account.status}


def _refresh_kite(
    session: Session, args: argparse.Namespace, deps: AdminDeps, now: datetime
) -> dict[str, Any]:
    """The zerodha broker symbols, as the weekly refresh writes them."""
    if deps.kite_symbols is None:
        raise AdminError("kite_symbols_unavailable")
    dump = _read(deps.files, args.dump, (".csv",), "dump_unreadable") if args.dump else None
    try:
        nse_tokens, listings = deps.kite_symbols(trade_date_of(now), dump)
    except HatError:
        raise
    except Exception:  # a download failure: its text is not kept
        raise AdminError("kite_symbols_unavailable") from None
    report = refresh_kite_symbols(session, market_id_of(session), nse_tokens, listings)
    return {"written": report.written, "removed": report.removed, "skipped": len(report.skipped)}


def _setup(
    session: Session, args: argparse.Namespace, deps: AdminDeps, now: datetime
) -> dict[str, Any] | None:
    """The setup commands; None for a release command."""
    user_id = deps.user_id
    command = args.command
    if command == "show":
        return release.release_overview(session, user_id)
    if command == "books":
        as_of = args.date or trade_date_of(now)
        return {
            "as_of": as_of.isoformat(),
            "books": report_doc(read_books(session, deps.store(), as_of)),
        }
    if command == "show-policy":
        version = effective_version(load_versions(session, user_id), now)
        if version is None:
            raise AdminError("no_policy")
        return {"version": version.version, "policy": version.policy}
    if command == "seed-policy":
        row = seed_initial_policy(session, user_id, now)
        return {"risk_policy_version": row.version, "id": str(row.id)}
    if command == "onboard-account":
        return _onboard(session, args, deps)
    if command == "refresh-kite-symbols":
        return _refresh_kite(session, args, deps, now)
    if command == "seed-strategies":
        rows = versions.seed_default_strategies(session, user_id)
        return {"strategies": {s.name: str(s.id) for s in rows}}
    if command == "seed-bots":
        require_active_account(session, args.account, user_id)
        bots = sleeve_service.seed_default_bots(
            session, user_id=user_id, broker_account_id=args.account
        )
        return {"bots": [str(b.id) for b in bots]}
    if command == "add-sleeve":
        require_active_account(session, args.account, user_id)
        bot = sleeve_service.add_sleeve(
            session, user_id=user_id, strategy_id=args.strategy, broker_account_id=args.account
        )
        return {"bot_id": str(bot.id), "lifecycle_state": bot.lifecycle_state}
    if command == "publish-universe":
        return _publish_universe(session, args, deps, now)
    if command == "load-index-membership":  # reference data: any time, no calendar
        index_name, members = parse_membership_csv(
            _read(deps.files, args.csv, (".csv",), "membership_unreadable")
        )
        return load_membership(
            session,
            members,
            index_name=index_name,
            as_of=args.as_of,
            size=args.size,
            source=Path(args.csv).name,
            apply=args.apply,
        )
    if command == "load-industry-history":  # reference data: any time, no calendar
        return load_industry_history(
            session, deps.store(), _snapshots(deps.files, args.dir), apply=args.apply
        )
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
        row = versions.publish_version(session, args.version, run_id=args.run)
        return {"version_id": str(row.id), "status": row.status}
    if command == "trial-version":
        row = release.start_trial(session, args.version, calendar=_calendar(session), now=now)
        return {"version_id": str(row.id), "status": row.status}
    return None


def _owner(
    session: Session, args: argparse.Namespace, deps: AdminDeps, now: datetime
) -> dict[str, Any]:
    """The owner's acts (`set-policy`, `assign-version`, `confirm-ddpi`, `revoke-ddpi`,
    `release-version`, `resolve-action`, `resolve-sale`, `correct-action`, `correct-actions`,
    `repair-symbol-history` and `promote`) under the allowlisted chat id."""
    try:
        chat_id = owner_chat_id(deps.env)
    except HatConfigError:
        raise AdminError("no_owner_chat") from None
    if args.command == "correct-action":  # stored reference data: any time, no calendar
        cash = args.cash  # a dividend's amount: its factor stays 1
        if args.delete:
            factor = None
        else:
            factor = Decimal(1) if cash is not None else args.factor
        correction = Correction(
            isin=args.isin,
            ex_date=args.ex_date,
            action_type=args.type,
            factor=factor,
            new_type=args.as_type,
            note=args.note,
            cash=cash,
            known_on=args.known_on,
        )
        return correct_action(session, correction, chat_id=chat_id, user_id=deps.user_id, now=now)
    if args.command == "correct-actions":  # the reviewed list (CA2), a dry run without --apply
        items = parse_list(_read(deps.files, args.list, (".tsv",), "list_unreadable"))
        return apply_list(
            session, items, apply=args.apply, chat_id=chat_id, user_id=deps.user_id, now=now
        )
    if args.command == "repair-symbol-history":  # stored reference data (audit M3), any time
        first_bars = symbol_first_bars(deps.store())
        return repair_symbol_history(
            session, first_bars, apply=args.apply, owner=(chat_id, deps.user_id, now)
        )
    if args.command == "revoke-ddpi":  # only narrows, so it needs no calendar (any time)
        account = revoke_ddpi(
            session, args.account, user_id=deps.user_id, note=args.note, now=now, chat_id=chat_id
        )
        return {"broker_account_id": str(account.id), "ddpi_confirmed": account.ddpi_confirmed}
    owner = OwnerCommand(chat_id, now, _calendar(session))
    if args.command == "set-policy":
        document = _read(deps.files, args.file, (".yaml", ".yml", ".json"), "policy_unreadable")
        try:
            policy = policy_text(document)
        except PolicyError:
            raise AdminError("policy_invalid") from None
        row = reset_policy_before_trading(
            session,
            user_id=deps.user_id,
            policy=policy,
            calendar=owner.calendar,
            chat_id=chat_id,
            now=now,
        )
        return {"risk_policy_version": row.version, "id": str(row.id)}
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
        bot = release.assign_version(
            session, bot_id=args.bot, version_id=args.version, command=owner
        )
        return {"bot_id": str(bot.id), "live_version_id": str(bot.live_version_id)}
    if args.command == "resolve-action":
        position = resolve_action(session, args.position, args.action, args.close_at, owner)
        close_position(session, position.id, args.close_at, CLOSE_REASON, now)
        return {"position_id": str(position.id), "closed": position.closed_at is not None}
    if args.command == "resolve-sale":
        fill = BrokerFill(args.qty, args.price, args.note, args.at, args.fees)
        position = resolve_sale(session, args.tag, fill, args.note, owner)
        return {"position_id": str(position.id), "closed": position.closed_at is not None}
    if args.command == "release-version":
        row = release.release_version(session, args.version, owner)
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
        with deps.scope(writes_data=args.command in DATA_COMMANDS) as session:
            result = _run(session, args, deps)
    except HatError as exc:
        sys.stderr.write(f"refused: {_code(exc)}\n")
        return REFUSED
    except SQLAlchemyError:
        sys.stderr.write("refused: database_error\n")
        return REFUSED
    if args.command == "show-policy":  # a policy file a research run reads back
        heading = f"risk policy version {result['version']}, in force"
        sys.stdout.write(policy_yaml(result["policy"], heading))
        return 0
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
