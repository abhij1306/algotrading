"""Demergers before their ex-date (CA8, owner 2026-10-24).

A position held over a demerger's ex-date sees the parent's price fall by the resulting
companies' value, and HAT knows the factor only from that gap, after the event. So a position
whose instrument has a demerger with an announced record date exits at the close of the last
session before the ex-date (`exit_day`), in the backtest and live alike, with exit reason
`demerger`. Under T+1 the ex-date is the record date: a sale the session before settles before
it, so the position is out at the pre-demerger price and gets no resulting shares (intended).
Splits and bonuses keep their rescale and rights their manage-only lock.

- Point in time: the exit fires only when the record date was public (`announced_on`) before
  that session (strictly: live reads the `bc` file at night, so a notice first read on the exit
  session's night is too late, and the backtest holds a same-day filing to the same rule);
  announced on it or later, or never dated, the position holds through as before.
- Live: the nightly `ca.adjust` reads the DEMERGER rows of NSE's daily corporate-action file
  (the `bc` CSV it already fetches for the dividends, CA4) into `hat_demerger_notice`, dated
  the night it first lists them (`store_notices`), and dates a stored demerger's
  `hat_corporate_action.announced_on` from its notice (`announce_stored`). The positions'
  side is `engine/demerger_exit.py`.
- Entries: an instrument with a demerger notice takes no entry from the last session before
  its ex-date until a price-changing action of that ex-date is stored (`entry_blocked`, Q5,
  skip `demerger_pending`): the exit session's buyer would be sold at its close, and after the
  ex-date the history waits for the factor (the backtest's `factor_pending`).
- History: the stored demergers' `announced_on` comes from a dated public source (a record-date
  intimation), given as a reviewed file (`read_announced`, `plan_announced`): never guessed, and
  an unknown date stays NULL.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..models import (
    HatCorporateAction,
    HatDemergerNotice,
    HatInstrument,
    HatInstrumentSymbolHistory,
)
from .ca_dividends import SOURCE_BC, FeedRow, input_path
from .corporate_actions import PRICE_NEUTRAL_TYPES, lock_reference_data
from .point_in_time import demerger_text

__all__ = [
    "CLOSE_REASON",
    "SKIP_DEMERGER_PENDING",
    "AnnouncedRow",
    "Demerger",
    "demerger_rows",
    "entry_blocked",
    "exit_day",
    "last_session_before",
    "plan_announced",
    "read_announced",
    "store_notices",
]

CLOSE_REASON = "demerger"  # the exit reason of a position closed before a demerger
SKIP_DEMERGER_PENDING = "demerger_pending"  # an entry skipped from the exit session (Q5)
ANNOUNCED_COLUMNS = ("isin", "ex_date", "announced_on", "source_url", "note")
MAX_FILE_BYTES = 2_000_000


def exit_day(sessions: Sequence[date], ex_date: date, announced_on: date | None) -> date | None:
    """The session at whose close a position held into a demerger exits: the last of the sorted
    `sessions` before `ex_date` (a holiday is no session), when the record date was announced
    before it (live reads it the night before at the latest). None when it was announced on that
    session or later, or never dated (the position holds through), or no session comes before
    the ex-date."""
    at = bisect_left(sessions, ex_date)
    if at == 0 or announced_on is None:
        return None
    last = sessions[at - 1]
    return last if announced_on < last else None


def last_session_before(sessions: Sequence[date], ex_date: date) -> date | None:
    """The last of the sorted `sessions` before `ex_date`; None without one."""
    at = bisect_left(sessions, ex_date)
    return sessions[at - 1] if at else None


def entry_blocked(
    session: Session, instrument_id: object, trade_date: date, sessions: Sequence[date]
) -> bool:
    """Whether a demerger notice of the instrument blocks its entries on `trade_date` (Q5): on
    and after the last of the sorted `sessions` before its ex-date (the ex-date itself without
    one), until a price-changing action of the instrument ex that date is stored."""
    for notice in session.scalars(
        select(HatDemergerNotice).where(HatDemergerNotice.instrument_id == instrument_id)
    ):
        start = last_session_before(sessions, notice.ex_date) or notice.ex_date
        if trade_date < start:
            continue
        stored = session.scalar(
            select(HatCorporateAction.id)
            .where(
                HatCorporateAction.instrument_id == instrument_id,
                HatCorporateAction.ex_date == notice.ex_date,
                HatCorporateAction.action_type.not_in(PRICE_NEUTRAL_TYPES),
            )
            .limit(1)
        )
        if stored is None:
            return True
    return False


def demerger_rows(rows: Iterable[FeedRow]) -> list[FeedRow]:
    """The rows of a `bc` file (`ca_dividends.read_bc`) that name a demerger and an ex-date
    ("DEMERGER", "MERGER DEMERGER"), one per (symbol, ex-date)."""
    found: dict[tuple[str, date], FeedRow] = {}
    for row in rows:
        if row.ex_date is not None and "demerger" in row.subject:
            found.setdefault((row.symbol, row.ex_date), row)
    return [found[k] for k in sorted(found)]


def _instruments_of(session: Session, symbol: str, day: date) -> list[HatInstrument]:
    """The instruments trading as `symbol` on `day`: by the symbol history, else the active
    instrument with that symbol now (a parent often hands its symbol on after a demerger)."""
    by_history = list(
        session.scalars(
            select(HatInstrument)
            .join(
                HatInstrumentSymbolHistory,
                HatInstrumentSymbolHistory.instrument_id == HatInstrument.id,
            )
            .where(
                HatInstrumentSymbolHistory.symbol == symbol,
                HatInstrumentSymbolHistory.valid_from <= day,
                or_(
                    HatInstrumentSymbolHistory.valid_to.is_(None),
                    HatInstrumentSymbolHistory.valid_to > day,
                ),
            )
        )
    )
    if by_history:
        return by_history
    return list(
        session.scalars(
            select(HatInstrument).where(
                HatInstrument.symbol == symbol, HatInstrument.active.is_(True)
            )
        )
    )


def store_notices(session: Session, rows: Iterable[FeedRow], day: date) -> int:
    """Store each demerger of the night's `bc` rows not stored yet for its instrument and
    ex-date, announced on `day` (the night HAT first read it); a symbol HAT does not know is left
    out. Returns how many were stored. Works in the caller's transaction, under the
    reference-data lock, so no rename lands between the symbol's mapping and the write."""
    lock_reference_data(session)
    stored = 0
    for row in demerger_rows(rows):
        if row.ex_date is None:  # demerger_rows keeps dated rows only
            continue
        for instrument in _instruments_of(session, row.symbol, day):
            taken = session.scalar(
                select(HatDemergerNotice.id).where(
                    HatDemergerNotice.instrument_id == instrument.id,
                    HatDemergerNotice.ex_date == row.ex_date,
                )
            )
            if taken is not None:
                continue
            session.add(
                HatDemergerNotice(
                    instrument_id=instrument.id,
                    ex_date=row.ex_date,
                    announced_on=day,
                    source=SOURCE_BC,
                    raw_text=f"{row.symbol} {row.series} {row.subject}"[:200],
                )
            )
            stored += 1
    session.flush()
    return stored


def _stored_demergers(session: Session) -> list[HatCorporateAction]:
    return [
        a
        for a in session.scalars(
            select(HatCorporateAction).where(
                HatCorporateAction.action_type.not_in(PRICE_NEUTRAL_TYPES)
            )
        )
        if demerger_text(a.raw_text)
    ]


def announce_stored(session: Session) -> int:
    """Date each stored demerger without an `announced_on` from its notice (same instrument and
    ex-date), so a later backtest exits before it as live did. Returns how many were dated."""
    notices = {
        (n.instrument_id, n.ex_date): n.announced_on
        for n in session.scalars(select(HatDemergerNotice))
    }
    dated = 0
    for action in _stored_demergers(session):
        found = notices.get((action.instrument_id, action.ex_date))
        if action.announced_on is None and found is not None:
            action.announced_on = found
            dated += 1
    session.flush()
    return dated


# --- the back-fill of the stored demergers ------------------------------------------------------


class AnnouncedFileError(HatError):
    """The announced-on file cannot be read; `code` is stable and names the line."""

    def __init__(self, code: str, line: int = 0) -> None:
        super().__init__(code)
        self.code = f"{code}@{line}" if line else code


@dataclass(frozen=True)
class AnnouncedRow:
    """One line of the reviewed file: the date a public source first named the demerger's
    record date."""

    line: int
    isin: str
    ex_date: date
    announced_on: date
    source_url: str
    note: str


def _day(raw: str, line: int) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise AnnouncedFileError("bad_date", line) from None


def read_announced(text: str) -> list[AnnouncedRow]:
    """The file's rows (TSV, header `isin ex_date announced_on source_url note`, `#` lines
    ignored). Refused: another header, a short line, a bad date, a line without a source, and
    an (isin, ex-date) given twice."""
    if len(text) > MAX_FILE_BYTES:
        raise AnnouncedFileError("file_too_large")
    reader = csv.reader(io.StringIO(text.lstrip("﻿")), delimiter="\t")
    lines = [(n, r) for n, r in enumerate(reader, start=1) if r and not r[0].startswith("#")]
    if not lines or tuple(c.strip() for c in lines[0][1]) != ANNOUNCED_COLUMNS:
        raise AnnouncedFileError("bad_header")
    rows: list[AnnouncedRow] = []
    seen: set[tuple[str, date]] = set()
    for n, raw in lines[1:]:
        if len(raw) != len(ANNOUNCED_COLUMNS):
            raise AnnouncedFileError("bad_columns", n)
        isin, ex, announced, url, note = (c.strip() for c in raw)
        if not url:
            raise AnnouncedFileError("no_source", n)
        row = AnnouncedRow(n, isin, _day(ex, n), _day(announced, n), url, note)
        if (row.isin, row.ex_date) in seen:
            raise AnnouncedFileError("duplicate", n)
        seen.add((row.isin, row.ex_date))
        rows.append(row)
    return rows


@dataclass(frozen=True)
class Demerger:
    """A stored demerger as the back-fill and the exit report read it."""

    action_id: str
    symbol: str
    isin: str
    ex_date: date
    announced_on: date | None


def stored_demergers(session: Session) -> list[Demerger]:
    """Every stored demerger (a price-changing row whose text names one), oldest first."""
    ids = {a.id: a for a in _stored_demergers(session)}
    names = {
        i.id: (i.symbol, i.isin)
        for i in session.scalars(
            select(HatInstrument).where(
                HatInstrument.id.in_({a.instrument_id for a in ids.values()})
            )
        )
    }
    return sorted(
        (
            Demerger(str(a.id), *names[a.instrument_id], a.ex_date, a.announced_on)
            for a in ids.values()
        ),
        key=lambda d: (d.ex_date, d.isin),
    )


@dataclass(frozen=True)
class AnnouncedPlan:
    """What the file would write: the stored demergers it dates (action id -> date), the lines
    naming no stored demerger, and the ones whose stored date differs (left as they are)."""

    dated: Mapping[str, AnnouncedRow]
    unmatched: Sequence[AnnouncedRow]
    differing: Sequence[tuple[Demerger, AnnouncedRow]]


def plan_announced(demergers: Sequence[Demerger], rows: Sequence[AnnouncedRow]) -> AnnouncedPlan:
    """Match each line to the stored demerger of its (ISIN, ex-date): an undated one takes the
    date, a dated one keeps its own (listed when the file's differs)."""
    by_key = {(d.isin, d.ex_date): d for d in demergers}
    dated: dict[str, AnnouncedRow] = {}
    unmatched: list[AnnouncedRow] = []
    differing: list[tuple[Demerger, AnnouncedRow]] = []
    for row in rows:
        found = by_key.get((row.isin, row.ex_date))
        if found is None:
            unmatched.append(row)
        elif found.announced_on is None:
            dated[found.action_id] = row
        elif found.announced_on != row.announced_on:
            differing.append((found, row))
    return AnnouncedPlan(dated, unmatched, differing)


def apply_announced(session: Session, plan: AnnouncedPlan) -> int:
    """Write the plan's dates to the rows still undated; returns how many were written."""
    written = 0
    for action in session.scalars(
        select(HatCorporateAction).where(
            and_(
                HatCorporateAction.id.in_(list(plan.dated)),
                HatCorporateAction.announced_on.is_(None),
            )
        )
    ):
        action.announced_on = plan.dated[str(action.id)].announced_on
        written += 1
    session.flush()
    return written


def exit_lines(demergers: Sequence[Demerger], sessions: Sequence[date]) -> list[str]:
    """Each demerger with the session the rule exits at, or why it holds through."""
    lines = []
    for d in demergers:
        last = last_session_before(sessions, d.ex_date)
        chosen = exit_day(sessions, d.ex_date, d.announced_on)
        if chosen is not None:
            verdict = f"exit at the close of {chosen}"
        elif d.announced_on is None:
            verdict = "holds through (record date not dated)"
        else:
            verdict = "holds through (announced on or after the last session before the ex-date)"
        lines.append(
            f"{d.symbol:<12} {d.isin} ex {d.ex_date} announced {d.announced_on or '-'}"
            f" last session {last or '-'}: {verdict}"
        )
    exits = sum(exit_day(sessions, d.ex_date, d.announced_on) is not None for d in demergers)
    lines.append(
        f"demergers {len(demergers)}, announced_on dated "
        f"{sum(d.announced_on is not None for d in demergers)}, forced exit {exits}"
    )
    return lines


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.demerger_notice announced FILE.tsv [--apply]
    python -m app.hat.data.demerger_notice exits [SYMBOL ...]

    `announced` dates the stored demergers' `announced_on` from the reviewed file (under the
    working directory): a dry run without `--apply`; with it, one transaction under the
    reference-data lock, writing only rows still undated. `exits` lists each stored demerger
    (or those of the symbols) with the session its forced exit falls on, from the sessions of
    the bars under `HAT_DATA_ROOT`."""
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.demerger_notice")
    sub = parser.add_subparsers(dest="command", required=True)
    announced = sub.add_parser("announced")
    announced.add_argument("file")
    announced.add_argument("--apply", action="store_true")
    sub.add_parser("exits").add_argument("symbols", nargs="*")
    args = parser.parse_args(argv)
    rows: list[AnnouncedRow] = []
    if args.command == "announced":
        try:
            path = input_path(args.file, suffix=".tsv")
            rows = read_announced(path.read_text(encoding="utf-8"))
        except (HatError, OSError) as exc:
            sys.stderr.write(f"refused: {getattr(exc, 'code', 'unreadable')}\n")
            return 2
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatSessionLocal

    from .store import DATASET_BARS, DataStore

    write = sys.stdout.write
    with HatSessionLocal() as session:
        if args.command == "exits":
            store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
            wanted = set(args.symbols)
            found = [d for d in stored_demergers(session) if not wanted or d.symbol in wanted]
            write("\n".join(exit_lines(found, store.days(DATASET_BARS))) + "\n")
            return 0
        if args.apply:  # no correction or import lands between the plan and its write
            lock_reference_data(session)
        plan = plan_announced(stored_demergers(session), rows)
        write(f"lines {len(rows)}, to date {len(plan.dated)}\n")
        for row in plan.unmatched:
            write(f"  no stored demerger: line {row.line} {row.isin} {row.ex_date}\n")
        for d, row in plan.differing:
            write(f"  stored {d.announced_on} kept: line {row.line} {d.symbol} {d.ex_date}\n")
        if args.apply:
            written = apply_announced(session, plan)
            session.commit()
            write(f"applied: {written} demergers dated\n")
        else:
            session.rollback()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
