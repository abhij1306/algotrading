"""Point in time (CA5, CA6): the factor-pending sessions, the lag report and its command line.
The dating rules are `ca_known`'s, the selection's `ca_versions`'.

`series` gives every event the backtest's series applies (each price-changing action and each
dividend with an amount, the copy the series uses) under its chain id, with its versions; the
backtest turns them into factor-pending (chain, session) pairs (`ca_versions.pending_days`): no
value available yet, or an as-of value other than the series'.

Nothing here dates a row: a value stored without `known_on` stays undated until the owner's
correction dates it (`correct-action --known-on`), and a backtest skips the name from its ex-date.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import HatCorporateAction, HatInstrument, HatIsinLineage
from .ca_known import (
    BASES,
    lag_sessions,
)
from .ca_versions import (
    ACCEPTED,
    CONFLICT,
    DISPUTED,
    SELECTED,
    Value,
    Version,
    pending_days,
    projection_mismatches,
    read_versions,
    stored_rows,
)
from .chains import LINKED, chains, stored_adjustment, stored_links
from .corporate_actions import (
    PRICE_NEUTRAL_TYPES,
)
from .store import DATASET_BARS, DataStore

_SERIES = or_(
    HatCorporateAction.action_type.not_in(PRICE_NEUTRAL_TYPES),
    HatCorporateAction.cash_amount.is_not(None),
)


def series_events(
    rows: Iterable[tuple[str, date, str]], heads: Mapping[str, tuple[str, int]]
) -> dict[tuple[str, date, str], str]:
    """(chain, ex-date, type) -> the member whose stored row (instrument, ex-date, type) the
    series applies: the copy nearest the chain's current ISIN, as `chains.chain_factors` and
    `chain_dividends` pick it (`heads`: `chains.chains`), so a copy the series ignores opens no
    pending span."""
    best: dict[tuple[str, date, str], tuple[int, str]] = {}
    for instrument_id, ex_date, action_type in rows:
        chain, depth = heads.get(instrument_id, (instrument_id, 0))
        held = best.get((chain, ex_date, action_type))
        if held is None or (depth, instrument_id) < held:
            best[(chain, ex_date, action_type)] = (depth, instrument_id)
    return {event: held[1] for event, held in sorted(best.items())}


@dataclass(frozen=True)
class Event:
    """One event a backtest's series applies, under its chain: the stored value and the
    versions of the member copy it reads."""

    chain_id: str
    ex_date: date
    action_type: str
    applied: Value
    versions: tuple[Version, ...]


def series(session: Session, chains_wanted: Iterable[str] | None = None) -> list[Event]:
    """The events of every stored row a backtest's series applies (a price-changing action, a
    dividend with an amount), under its chain's current id (of `chains_wanted` when given). A row
    without an accepted version (none should be: `ca.adjust` alerts a projection mismatch) reads
    as one, its own value at its `known_on`."""
    rows = {
        (str(i), ex, kind): (Decimal(f), None if c is None else Decimal(c), known, basis)
        for i, ex, kind, f, c, known, basis in session.execute(
            select(
                HatCorporateAction.instrument_id,
                HatCorporateAction.ex_date,
                HatCorporateAction.action_type,
                HatCorporateAction.adjustment_factor,
                HatCorporateAction.cash_amount,
                HatCorporateAction.known_on,
                HatCorporateAction.known_basis,
            ).where(_SERIES)
        )
    }
    events = series_events(rows, chains(stored_links(session)))
    ids = None
    if chains_wanted is not None:
        wanted = set(chains_wanted)
        events = {e: i for e, i in events.items() if e[0] in wanted}
        ids = set(events.values())
    versions = read_versions(session, ids)
    found = []
    for (chain, ex, kind), instrument_id in events.items():
        factor, cash, known, basis = rows[(instrument_id, ex, kind)]
        own = versions.get((instrument_id, ex, kind), [])
        if not any(v.status in SELECTED for v in own):
            own = [Version(0, ACCEPTED, factor, cash, known, basis), *own]
        found.append(Event(chain, ex, kind, (factor, cash), tuple(own)))
    return found


def chain_pending(
    session: Session, days: Sequence[date], chains_wanted: Iterable[str]
) -> set[tuple[str, date]]:
    """The factor-pending (chain, session) pairs of `chains_wanted` over the sorted sessions
    `days`, from the versions (`ca_versions.pending_days`)."""
    return {
        (e.chain_id, d)
        for e in series(session, chains_wanted)
        for d in pending_days(e.versions, e.ex_date, days, e.applied)
    }


# --- the report ---------------------------------------------------------------------------------


LOG_COLUMNS = (
    "symbol",
    "isin",
    "ex_date",
    "action_type",
    "source",
    "factor",
    "cash",
    "known_on",
    "known_basis",
    "lag_sessions",
)


def adjustment_log(session: Session, days: Sequence[date]) -> list[dict[str, Any]]:
    """Every stored row a backtest's series applies, with when it became knowable and the
    sessions from its ex-date to then (`lag_sessions`, empty when undated)."""
    query = (
        select(HatCorporateAction, HatInstrument.symbol, HatInstrument.isin)
        .join(HatInstrument, HatInstrument.id == HatCorporateAction.instrument_id)
        .where(_SERIES)
        .order_by(HatCorporateAction.ex_date, HatInstrument.isin, HatCorporateAction.action_type)
    )
    return [
        {
            "symbol": symbol,
            "isin": isin,
            "ex_date": a.ex_date,
            "action_type": a.action_type,
            "source": a.source,
            "factor": Decimal(a.adjustment_factor).normalize(),
            "cash": None if a.cash_amount is None else Decimal(a.cash_amount).normalize(),
            "known_on": a.known_on,
            "known_basis": a.known_basis,
            "lag_sessions": lag_sessions(days, a.ex_date, a.known_on),
        }
        for a, symbol, isin in session.execute(query)
    ]


def report_lines(
    log: Sequence[Mapping[str, Any]], links: Sequence[tuple[date, date | None]]
) -> list[str]:
    """The counts: rows by basis, lagged (known after the ex-date) and undated, by source; then
    the linked successions known after their first session and undated; then every lagged or
    undated row."""
    by_basis = Counter(r["known_basis"] or "undated" for r in log)
    lagged = [r for r in log if r["known_on"] is not None and r["known_on"] > r["ex_date"]]
    undated = [r for r in log if r["known_on"] is None]
    lines = [
        f"actions {len(log)}: "
        + ", ".join(f"{b} {by_basis[b]}" for b in (*BASES, "undated") if by_basis[b]),
        f"lagged (known_on > ex_date) {len(lagged)}: "
        + ", ".join(f"{s} {n}" for s, n in sorted(Counter(r["source"] for r in lagged).items())),
        f"undated (known_on null) {len(undated)}: "
        + ", ".join(f"{s} {n}" for s, n in sorted(Counter(r["source"] for r in undated).items())),
        f"links {len(links)}: lagged {sum(k is not None and k > s for s, k in links)}, "
        f"undated {sum(k is None for _, k in links)}",
    ]
    for r in [*lagged, *undated]:
        lines.append(
            "\t".join(
                str(r[c]) if r[c] is not None else "-"
                for c in (
                    "symbol",
                    "ex_date",
                    "action_type",
                    "source",
                    "known_on",
                    "known_basis",
                    "lag_sessions",
                )
            )
        )
    return lines


def log_path(name: str) -> Path:
    """The adjustment log's file, named on the command line: a `.tsv` under the working
    directory, anything else refused before it is opened."""
    base = Path.cwd().resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or target.suffix.lower() != ".tsv":
        raise SystemExit("point_in_time: the log must be a .tsv file under the working directory")
    return target


def write_log(path: Path, log: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, LOG_COLUMNS, delimiter="\t")
        writer.writeheader()
        for r in log:
            writer.writerow({k: "" if r[k] is None else r[k] for k in LOG_COLUMNS})


def pending_lines(session: Session, store: DataStore, symbols: Sequence[str]) -> list[str]:
    """For each symbol: its chain's events with pending sessions and the sessions a backtest
    skips for each."""
    lineage = stored_adjustment(session).lineage
    days = store.days(DATASET_BARS)
    events = series(session)
    lines = []
    for symbol in symbols:
        ids = {
            str(i)
            for i in session.scalars(select(HatInstrument.id).where(HatInstrument.symbol == symbol))
        }
        heads = {lineage.get(i, i) for i in ids}
        if not heads:
            lines.append(f"{symbol}: no instrument")
            continue
        own = [
            (e, pending_days(e.versions, e.ex_date, days, e.applied))
            for e in events
            if e.chain_id in heads
        ]
        own = sorted(((e, p) for e, p in own if p), key=lambda x: x[0].ex_date)
        lines.append(f"{symbol}: {len(own)} event(s) with factor-pending sessions")
        for e, skipped in own:
            dates = ", ".join(str(v.available_on or "undated") for v in e.versions)
            lines.append(
                f"  from {e.ex_date} {e.action_type} ({len(e.versions)} version(s), available"
                f" {dates}): {len(skipped)} session(s) factor-pending {skipped[0]}..{skipped[-1]}:"
                f" {', '.join(d.isoformat() for d in skipped[:12])}"
                + (" ..." if len(skipped) > 12 else "")
            )
    return lines


def version_lines(session: Session) -> list[str]:
    """The ledger: versions per event, by status, the conflicts and disputed demerger values, and
    the projection check (`hat_corporate_action` against the hindsight selection; expect 0)."""
    versions = read_versions(session)
    per_event = Counter(len(v) for v in versions.values())
    by_status = Counter(v.status for vs in versions.values() for v in vs)
    mismatches = projection_mismatches(stored_rows(session), versions)

    def with_status(status: str) -> int:
        return sum(any(v.status == status for v in vs) for vs in versions.values())

    lines = [
        f"versions {sum(by_status.values())} over {len(versions)} events: "
        + ", ".join(f"{k} {n}" for k, n in sorted(by_status.items())),
        "versions per event: " + ", ".join(f"{k}: {n}" for k, n in sorted(per_event.items())),
        f"events with a conflict {with_status(CONFLICT)}",
        f"disputed demergers {with_status(DISPUTED)}",
        f"projection check (stored rows against the hindsight versions): {len(mismatches)}"
        " mismatch(es)",
    ]
    lines += [f"  {k[0]} {k[1]} {k[2]}: {why}" for k, why in mismatches[:50]]
    return lines


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.point_in_time report [--log TSV]
    python -m app.hat.data.point_in_time pending SYMBOL [SYMBOL ...]

    Read-only. `report` counts the lagged and undated actions and links and lists them; `--log`
    writes the adjustment log; then the version ledger and its projection check. `pending` lists
    the sessions a backtest holds a name factor-pending. The bars are read under
    `HAT_DATA_ROOT`."""
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.point_in_time")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report").add_argument("--log")
    sub.add_parser("pending").add_argument("symbols", nargs="+")
    args = parser.parse_args(argv)
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatDataSessionLocal

    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    write: Callable[[str], Any] = lambda s: sys.stdout.write(s + "\n")  # noqa: E731
    with HatDataSessionLocal() as session:
        if args.command == "report":
            target = log_path(args.log) if args.log else None
            log = adjustment_log(session, store.days(DATASET_BARS))
            links = [
                (s, k)
                for s, k in session.execute(
                    select(HatIsinLineage.effective_date, HatIsinLineage.known_on).where(
                        HatIsinLineage.status == LINKED
                    )
                )
            ]
            for line in report_lines(log, links):
                write(line)
            if target is not None:
                write_log(target, log)
                write(f"adjustment log: {len(log)} rows to {args.log}")
            for line in version_lines(session):
                write(line)
        else:
            for line in pending_lines(session, store, args.symbols):
                write(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
