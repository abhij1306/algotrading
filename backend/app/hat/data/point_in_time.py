"""Point in time (CA5): the stored factor-pending windows, the back-fill of `known_on` for rows
stored before CA5, the lag report and its command line. The rules are `ca_known`'s.

`stored_windows` gives the window of every stored row the backtest's series applies (each
price-changing action and each dividend with an amount), under its chain id; the backtest turns
them into factor-pending (chain, session) pairs (`ca_known.pending_days`).

`backfill` dates the rows whose `known_basis` is NULL from what is stored, never by guess:
- an owner's value (a row `correct-action`, the correction list or `add_action` wrote, read from
  the `corporate_action.corrected` journal): a split or bonus the bars confirm by CA1's gate at
  the ex-date open (`ex_open`), a demerger at its market gap (`settled_close`); any other value
  stays undated (the owner dates it with a correction's `known_on`);
- a price-neutral row, a split or bonus (the MCP, NSE's archive, CA1), a CA1 rights factor and a
  feed dividend: `ex_open`; an MCP factor of another type: undated;
- a CA1 demerger: its journalled resulting company's first close (`resulting_first_close`), or
  the close `SETTLE_SESSIONS` on when the gap was the factor (`settled_close`);
- a CA2 demerger (`market_gap`): `settled_close` (its cross-checks rest on research rows that
  stored no source date); a CA2 rights factor on terms NSE's text gave: `ex_open`, on terms only
  research gave (`ca_research.research_supplied`): undated; a research distribution: undated.
A linked or review succession gets the last bar its rules read (`lineage.Decision.known_on`).
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import HatCorporateAction, HatEvent, HatInstrument, HatIsinLineage
from .ca_corrections import SOURCE_OWNER
from .ca_import import (
    EVENT_IMPORTED,
    SOURCE_FILE,
    Bar,
    around,
    gate,
    read_windows,
    same_factor,
)
from .ca_known import (
    BASES,
    RESULTING_FIRST_CLOSE,
    UNKNOWN,
    Known,
    Window,
    at_ex_open,
    lag_sessions,
    pending_days,
    settle_known,
)
from .ca_research import SOURCE_RESEARCH, research_supplied
from .corporate_actions import (
    EVENT_CORRECTED,
    PRICE_NEUTRAL_TYPES,
    SOURCE_ARCHIVE,
    SOURCE_MCP,
    TYPE_BONUS,
    TYPE_DIVIDEND,
    TYPE_SPLIT,
)
from .lineage import read_spans
from .store import DATASET_BARS, DataStore

_SERIES = or_(
    HatCorporateAction.action_type.not_in(PRICE_NEUTRAL_TYPES),
    HatCorporateAction.cash_amount.is_not(None),
)


def stored_windows(session: Session, lineage: Mapping[str, str]) -> list[Window]:
    """The factor-pending window of every stored row a backtest's series applies (a
    price-changing action, a dividend with an amount), under its chain's id (`lineage`:
    `Adjustment.lineage`, a linked older ISIN's id to its chain's)."""
    rows = session.execute(
        select(
            HatCorporateAction.instrument_id,
            HatCorporateAction.ex_date,
            HatCorporateAction.known_on,
        ).where(_SERIES)
    )
    return [Window(lineage.get(str(iid), str(iid)), ex, known_on) for iid, ex, known_on in rows]


def chain_pending(
    session: Session, lineage: Mapping[str, str], days: Sequence[date], chains: Iterable[str]
) -> set[tuple[str, date]]:
    """The factor-pending (chain, session) pairs of `chains` over the sorted sessions `days`."""
    wanted = set(chains)
    return pending_days((w for w in stored_windows(session, lineage) if w.chain_id in wanted), days)


# --- the back-fill ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """A stored action, as the back-fill reads it."""

    id: str
    instrument_id: str
    isin: str
    ex_date: date
    action_type: str
    factor: Decimal
    text: str
    source: str
    cash: Decimal | None


_RESULTING = re.compile(r"first close \S+ on (\d{4}-\d{2}-\d{2})")
_MARKET = re.compile(r"market (?:gap|factor)", re.IGNORECASE)


def demerger_text(text: str) -> bool:
    """A demerger row: CA1 and CA2 texts begin "Demerger", the owner's notes name it."""
    return "demerger" in text.lower()


def classify(
    row: Row,
    *,
    owner: bool,
    derived: str,
    market: tuple[Fraction, Sequence[date]] | None,
) -> Known:
    """When one stored row became knowable, from what is stored (the module docstring): `owner`
    when the owner's correction wrote its value, `derived` the import journal's `check.derived`,
    `market` the ex-date gap (ex open over the previous close) and the ex instrument's sessions
    from the raw bars, None without bars."""
    if row.action_type in PRICE_NEUTRAL_TYPES:
        if row.action_type == TYPE_DIVIDEND and "research f" in row.text:
            return UNKNOWN  # a distribution's value only research gave, with no source date
        return at_ex_open(row.ex_date)
    if owner or row.source == SOURCE_OWNER:
        if market is None:
            return UNKNOWN
        gap, days = market
        if row.action_type in (TYPE_SPLIT, TYPE_BONUS):
            confirmed = gate(Fraction(row.factor), Fraction(1), gap).confirmed
            return at_ex_open(row.ex_date) if confirmed else UNKNOWN
        if demerger_text(row.text) and same_factor(row.factor, gap):
            return settle_known(days, row.ex_date)
        return UNKNOWN
    if row.action_type in (TYPE_SPLIT, TYPE_BONUS):
        return at_ex_open(row.ex_date)
    if row.source in (SOURCE_MCP, SOURCE_ARCHIVE):
        return UNKNOWN  # an MCP factor of another type: its basis is not known
    if row.source == SOURCE_FILE:
        if not demerger_text(row.text):
            return at_ex_open(row.ex_date)  # rights at TERP from NSE's terms
        found = _RESULTING.search(derived)
        if found:
            return Known(max(row.ex_date, date.fromisoformat(found[1])), RESULTING_FIRST_CLOSE)
        if "gap is the factor" in derived and market is not None:
            return settle_known(market[1], row.ex_date)
        return UNKNOWN
    if row.source == SOURCE_RESEARCH:
        if demerger_text(row.text):
            return UNKNOWN if market is None else settle_known(market[1], row.ex_date)
        return UNKNOWN if research_supplied(row.text) else at_ex_open(row.ex_date)
    return UNKNOWN


def _rows(session: Session) -> list[Row]:
    query = (
        select(HatCorporateAction, HatInstrument.isin)
        .join(HatInstrument, HatInstrument.id == HatCorporateAction.instrument_id)
        .where(HatCorporateAction.known_basis.is_(None))
    )
    return [
        Row(
            str(a.id),
            str(a.instrument_id),
            isin,
            a.ex_date,
            a.action_type,
            Decimal(a.adjustment_factor),
            a.raw_text,
            a.source,
            None if a.cash_amount is None else Decimal(a.cash_amount),
        )
        for a, isin in session.execute(query)
    ]


def owner_keys(session: Session) -> set[tuple[str, date, str]]:
    """(instrument, ex-date, type) of every stored value an owner's correction wrote: the target
    of a replacement or an addition (a replacement whose key another row already held leaves
    that row's own value, `kept_stored`)."""
    found = set()
    for e in session.scalars(select(HatEvent).where(HatEvent.event_type == EVENT_CORRECTED)):
        after = e.payload.get("after", {})
        if after.get("deleted") or after.get("kept_stored") or "type" not in after:
            continue
        found.add((str(e.aggregate_id), date.fromisoformat(e.payload["ex_date"]), after["type"]))
    return found


def import_derived(session: Session) -> dict[tuple[str, date, str], str]:
    """(instrument, ex-date, type) -> the `check.derived` text the CA1/CA2 import journalled."""
    found = {}
    for e in session.scalars(select(HatEvent).where(HatEvent.event_type == EVENT_IMPORTED)):
        derived = (e.payload.get("check") or {}).get("derived")
        if derived and "type" in e.payload:
            key = (str(e.aggregate_id), date.fromisoformat(e.payload["ex_date"]), e.payload["type"])
            found[key] = str(derived)
    return found


def neighbours(session: Session) -> dict[str, set[str]]:
    """Each instrument -> its linked predecessor and successor (the bar before an ex-date or the
    ex bar may be on either)."""
    rows = session.execute(
        select(HatIsinLineage.old_instrument_id, HatIsinLineage.new_instrument_id).where(
            HatIsinLineage.status == "linked"
        )
    )
    found: dict[str, set[str]] = defaultdict(set)
    for old, new in rows:
        found[str(old)].add(str(new))
        found[str(new)].add(str(old))
    return found


def market_gaps(
    store: DataStore, rows: Sequence[Row], session: Session
) -> dict[str, tuple[Fraction, Sequence[date]]]:
    """Each row's ex-date gap (ex open over the previous close, divided by the other stored
    factors ex between the two sessions, as CA1's gate reads it) and the ex instrument's
    sessions near it, from the raw bars."""
    near = neighbours(session)
    wanted = {k: (r.ex_date, {r.instrument_id, *near[r.instrument_id]}) for k, r in enumerate(rows)}
    stored: dict[str, list[tuple[date, str, Decimal]]] = defaultdict(list)
    for iid, ex, kind, factor in session.execute(
        select(
            HatCorporateAction.instrument_id,
            HatCorporateAction.ex_date,
            HatCorporateAction.action_type,
            HatCorporateAction.adjustment_factor,
        ).where(HatCorporateAction.action_type.not_in(PRICE_NEUTRAL_TYPES))
    ):
        stored[str(iid)].append((ex, kind, Decimal(factor)))
    windows = read_windows(store, wanted)
    out = {}
    for k, r in enumerate(rows):
        bars: Sequence[Bar] = windows.get(k, ())
        pair = around(bars, r.ex_date, r.isin)
        if isinstance(pair, str) or pair[0].close <= 0:
            continue
        before, on = pair
        others = {
            (ex, kind): factor
            for iid in wanted[k][1]
            for ex, kind, factor in stored[iid]
            if before.day < ex <= on.day and (ex, kind) != (r.ex_date, r.action_type)
        }
        same_day = math.prod((Fraction(f) for f in others.values()), start=Fraction(1))
        days = sorted(b.day for b in bars if b.instrument_id == on.instrument_id)
        out[r.id] = (Fraction(on.open) / Fraction(before.close) / same_day, days)
    return out


@dataclass
class Backfill:
    """What a back-fill found: the rows it dates, by id, and the links."""

    actions: dict[str, tuple[Row, Known]]
    links: dict[str, date | None]


def plan_backfill(session: Session, store: DataStore) -> Backfill:
    """The `known_on` of every undated row and link, from what is stored; nothing is written."""
    rows = _rows(session)
    owned = owner_keys(session)
    derived = import_derived(session)
    needs_bars = [
        r
        for r in rows
        if r.action_type not in PRICE_NEUTRAL_TYPES
        and ((r.instrument_id, r.ex_date, r.action_type) in owned or demerger_text(r.text))
    ]
    gaps = market_gaps(store, needs_bars, session)
    actions = {}
    for r in rows:
        key = (r.instrument_id, r.ex_date, r.action_type)
        known = classify(r, owner=key in owned, derived=derived.get(key, ""), market=gaps.get(r.id))
        actions[r.id] = (r, known)
    links: dict[str, date | None] = {}
    undated = list(session.scalars(select(HatIsinLineage).where(HatIsinLineage.known_on.is_(None))))
    if undated:
        heads = {s.instrument_id: s.head for s in read_spans(store)}
        for link in undated:
            head = heads.get(str(link.new_instrument_id), ())
            links[str(link.id)] = head[-1].day if head else None
    return Backfill(actions, links)


def apply_backfill(session: Session, plan: Backfill) -> tuple[int, int]:
    """Write the plan's dated rows and links; returns how many of each were dated."""
    dated = {k: known for k, (_, known) in plan.actions.items() if known.on is not None}
    for row in session.scalars(
        select(HatCorporateAction).where(
            HatCorporateAction.id.in_([uuid.UUID(k) for k in dated]),
            HatCorporateAction.known_basis.is_(None),
        )
    ):
        known = dated[str(row.id)]
        row.known_on, row.known_basis = known.on, known.basis
    links = {k: v for k, v in plan.links.items() if v is not None}
    for link in session.scalars(
        select(HatIsinLineage).where(HatIsinLineage.id.in_([uuid.UUID(k) for k in links]))
    ):
        link.known_on = links[str(link.id)]
    session.flush()
    return len(dated), len(links)


def backfill_lines(plan: Backfill) -> list[str]:
    counts = Counter(
        (r.source, r.action_type, known.basis or "undated") for r, known in plan.actions.values()
    )
    lines = [f"rows to date: {len(plan.actions)}"]
    lines += [f"  {s:<12} {t:<9} {b:<22} {n}" for (s, t, b), n in sorted(counts.items())]
    undated = sorted(
        (r for r, known in plan.actions.values() if known.on is None),
        key=lambda r: (r.ex_date, r.isin),
    )
    lines.append(f"rows staying undated: {len(undated)}")
    lines += [
        f"  {r.isin} {r.ex_date} {r.action_type} {r.source} {r.factor.normalize()} {r.text[:80]}"
        for r in undated
    ]
    linked = sum(v is not None for v in plan.links.values())
    lines.append(f"links to date: {linked} of {len(plan.links)}")
    return lines


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


def write_log(path: Path, log: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, LOG_COLUMNS, delimiter="\t")
        writer.writeheader()
        for r in log:
            writer.writerow({k: "" if r[k] is None else r[k] for k in LOG_COLUMNS})


def pending_lines(session: Session, store: DataStore, symbols: Sequence[str]) -> list[str]:
    """For each symbol: its chain's lagged windows and the sessions a backtest skips for it."""
    from .lineage import stored_adjustment

    lineage = stored_adjustment(session).lineage
    days = store.days(DATASET_BARS)
    windows = stored_windows(session, lineage)
    lines = []
    for symbol in symbols:
        ids = {
            str(i)
            for i in session.scalars(select(HatInstrument.id).where(HatInstrument.symbol == symbol))
        }
        chains = {lineage.get(i, i) for i in ids}
        if not chains:
            lines.append(f"{symbol}: no instrument")
            continue
        own = sorted(
            (w for w in windows if w.chain_id in chains and w.lagged), key=lambda w: w.start
        )
        lines.append(f"{symbol}: {len(own)} lagged window(s)")
        for w in own:
            skipped = sorted(d for _, d in pending_days([w], days))
            span = f"{skipped[0]}..{skipped[-1]}" if skipped else "-"
            lines.append(
                f"  from {w.start} known_on {w.known_on or 'undated'}: {len(skipped)} session(s)"
                f" factor-pending {span}: {', '.join(d.isoformat() for d in skipped[:12])}"
                + (" ..." if len(skipped) > 12 else "")
            )
    return lines


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.point_in_time backfill [--apply]
    python -m app.hat.data.point_in_time report [--log TSV]
    python -m app.hat.data.point_in_time pending SYMBOL [SYMBOL ...]

    `backfill` dates the undated rows and links from what is stored (a dry run without
    `--apply`; with it, one transaction) and lists the rows that stay undated. `report` counts
    the lagged and undated actions and links and lists them; `--log` writes the adjustment log.
    `pending` lists the sessions a backtest holds a name factor-pending. The bars are read under
    `HAT_DATA_ROOT`."""
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.point_in_time")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backfill").add_argument("--apply", action="store_true")
    sub.add_parser("report").add_argument("--log")
    sub.add_parser("pending").add_argument("symbols", nargs="+")
    args = parser.parse_args(argv)
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatSessionLocal

    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    write: Callable[[str], Any] = lambda s: sys.stdout.write(s + "\n")  # noqa: E731
    with HatSessionLocal() as session:
        if args.command == "backfill":
            plan = plan_backfill(session, store)
            for line in backfill_lines(plan):
                write(line)
            if args.apply:
                actions, links = apply_backfill(session, plan)
                session.commit()
                write(f"applied: {actions} rows and {links} links dated")
        elif args.command == "report":
            log = adjustment_log(session, store.days(DATASET_BARS))
            links = [
                (s, k)
                for s, k in session.execute(
                    select(HatIsinLineage.effective_date, HatIsinLineage.known_on).where(
                        HatIsinLineage.status == "linked"
                    )
                )
            ]
            for line in report_lines(log, links):
                write(line)
            if args.log:
                write_log(Path(args.log), log)
                write(f"adjustment log: {len(log)} rows to {args.log}")
        else:
            for line in pending_lines(session, store, args.symbols):
                write(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
