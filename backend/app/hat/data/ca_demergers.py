"""Demerger estimates (CA6): every stored demerger's other values, as versions beside the applied
one, its band and the owner's review list. Rerunnable: a version already stored is not appended
again, so a demerger CA7 adds is picked up by the next run.

For each stored demerger (a price-changing row whose text names one, `point_in_time.demerger_text`):

- the raw gap: the ex open over the previous close, divided by any other factor ex between the two
  sessions (`point_in_time.ex_gaps`);
- the index-neutralised gap: the raw gap over `NEUTRAL_INDEX`'s move across the same night (its
  ex-session open over its previous-session close, `index_daily`), so the market's move that
  morning is not read as the action; without the index's two sessions there is none and the raw
  gap stands alone. Both gaps are available at the close `SETTLE_SESSIONS` on (the rule that
  accepts a gap, `ca_known.settle_known`);
- the ratio-priced value, 1 - sum(r_i * C_i) / P over the resulting companies' first closes,
  available from that close: CA1's journalled resulting company (`check.derived`), or the ratio
  cross-check a research import journalled (`check.cross_checks.ratio`), dated by the one listing
  in the year after the ex-date whose first close gives that value; undated when none or several do.

Each estimate other than the applied value is a `cross_check` version (the raw gap only where the
applied value is not the gap). A ratio value more than `DISPUTE_TOL` (log) from the applied value
is `disputed` and listed for review. Every appended version carries the band: min..max of the
estimates and the applied value. A ratio value never takes precedence (it is a cross-check only).

The applied value stays the raw market gap. Only when the owner approved the neutralised gap
(`--neutral`, with `--apply`) does each demerger whose applied value is the raw gap take the
neutralised gap as a new accepted version (its date and basis kept), `hat_corporate_action`
following it; the features then need `features --full`.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatCorporateAction, HatEvent, HatInstrument
from .ca_import import CHILD_DAYS, EVENT_IMPORTED, read_first_closes, same_factor
from .ca_known import settle_known
from .ca_research import ratio_factor
from .ca_versions import (
    CROSS_CHECK,
    DISPUTED,
    INDEX_NEUTRAL_GAP,
    RAW_GAP,
    RESULTING_RATIO,
    Ledger,
    band,
    log_apart,
)
from .corporate_actions import PRICE_NEUTRAL_TYPES, factor_to_decimal, lock_reference_data
from .point_in_time import ExGap, Row, demerger_text, ex_gaps
from .store import DataStore

NEUTRAL_INDEX = "NIFTY 500"
DISPUTE_TOL = 0.10  # a ratio value further from the applied one than this (log) is disputed
SOURCE = "ca_demergers"

_CA1 = re.compile(r"resulting \S+ \S+ first close [\d.]+ on (\d{4}-\d{2}-\d{2}) -> factor")
_LEG = re.compile(r"^Demerger (\d+):(\d+) [^;\[]+\[research")


def neutral_gap(
    gap: Fraction, index: Mapping[date, tuple[Fraction, Fraction]], previous: date, ex: date
) -> Fraction | None:
    """The gap over the index's move from `previous`'s close to `ex`'s open ({day: (open,
    close)}); None without both sessions."""
    before, on = index.get(previous), index.get(ex)
    if before is None or on is None or before[1] <= 0 or on[0] <= 0:
        return None
    return gap / (on[0] / before[1])


def disputed(applied: Fraction, value: Fraction) -> bool:
    return log_apart(applied, value) > math.log1p(DISPUTE_TOL)


@dataclass(frozen=True)
class Ratio:
    value: Fraction
    available: date | None


def ratio_from_journal(
    ex_date: date,
    imported: Mapping[str, Any],
    text: str,
    first_closes: Mapping[str, tuple[date, Decimal]],
) -> Ratio | None:
    """The ratio-priced value an import journalled for the row (`imported`: the
    `corporate_action.imported` payload, `text` the stored text): CA1's priced resulting company
    (the factor it stored, from that company's first close; an owner's correction may have
    replaced it since), or a research ratio cross-check dated by the one fresh listing whose first
    close gives it (one leg only)."""
    check = imported.get("check") or {}
    found = _CA1.search(str(check.get("derived", "")))
    if found and imported.get("factor"):
        value = Fraction(str(imported["factor"]))
        return Ratio(value, max(ex_date, date.fromisoformat(found[1])))
    raw = (check.get("cross_checks") or {}).get("ratio")
    if raw is None:
        return None
    value = Fraction(str(raw))
    leg = _LEG.search(text)
    prev = check.get("previous_close")
    if leg is None or prev is None:
        return Ratio(value, None)
    shares = (Fraction(int(leg[1])), Fraction(int(leg[2])))
    last = ex_date + timedelta(days=CHILD_DAYS)
    days = []
    for first, close in first_closes.values():
        if ex_date <= first <= last:
            priced = ratio_factor([(shares, Fraction(close))], Fraction(str(prev)))
            if priced is not None and abs(priced - value) <= Fraction(1, 20000):
                days.append((first, priced))
    if len(days) != 1:
        return Ratio(value, None)
    return Ratio(days[0][1], days[0][0])


@dataclass(frozen=True)
class Demerger:
    """One stored demerger and its estimates."""

    symbol: str
    row: Row
    known_on: date | None
    applied: Fraction
    raw: Fraction | None
    neutral: Fraction | None
    ratio: Ratio | None
    gap_available: date | None

    @property
    def band(self) -> tuple[Fraction, Fraction]:
        values = [self.applied, self.raw, self.neutral, self.ratio and self.ratio.value]
        return band(v for v in values if v is not None)

    @property
    def disputed(self) -> bool:
        return self.ratio is not None and disputed(self.applied, self.ratio.value)

    @property
    def applies_gap(self) -> bool:
        return self.raw is not None and same_factor(Decimal(self.row.factor), self.raw)


def _rows(session: Session) -> list[tuple[str, Row, date | None]]:
    query = (
        select(HatCorporateAction, HatInstrument.symbol, HatInstrument.isin)
        .join(HatInstrument, HatInstrument.id == HatCorporateAction.instrument_id)
        .where(HatCorporateAction.action_type.not_in(PRICE_NEUTRAL_TYPES))
        .order_by(HatCorporateAction.ex_date, HatInstrument.symbol)
    )
    return [
        (
            symbol,
            Row(
                str(a.id),
                str(a.instrument_id),
                isin,
                a.ex_date,
                a.action_type,
                Decimal(a.adjustment_factor),
                a.raw_text,
                a.source,
                None,
            ),
            a.known_on,
        )
        for a, symbol, isin in session.execute(query)
        if demerger_text(a.raw_text)
    ]


def _imports(session: Session) -> dict[tuple[str, date, str], Mapping[str, Any]]:
    """(instrument, ex-date, type) -> its `corporate_action.imported` payload."""
    found = {}
    for e in session.scalars(select(HatEvent).where(HatEvent.event_type == EVENT_IMPORTED)):
        if e.payload.get("check") and "type" in e.payload:
            key = (str(e.aggregate_id), date.fromisoformat(e.payload["ex_date"]), e.payload["type"])
            found[key] = e.payload
    return found


_INDEX_SQL = """
SELECT date, CAST(open AS VARCHAR), CAST(close AS VARCHAR) FROM index_daily
WHERE index_name = ? ORDER BY date
"""


def read_index(store: DataStore) -> dict[date, tuple[Fraction, Fraction]]:
    with closing(store.connect()) as con:
        rows = con.execute(_INDEX_SQL, [NEUTRAL_INDEX]).fetchall()
    return {d: (Fraction(o), Fraction(c)) for d, o, c in rows}


def plan(session: Session, store: DataStore) -> list[Demerger]:
    """Every stored demerger with its estimates; nothing is written."""
    stored = _rows(session)
    rows = [r for _, r, _ in stored]
    gaps: dict[str, ExGap] = ex_gaps(store, rows, session)
    index = read_index(store)
    imports = _imports(session)
    first_closes = read_first_closes(store)
    out = []
    for symbol, r, known_on in stored:
        g = gaps.get(r.id)
        applied = Fraction(r.factor)
        imported = imports.get((r.instrument_id, r.ex_date, r.action_type), {})
        out.append(
            Demerger(
                symbol,
                r,
                known_on,
                applied,
                None if g is None else g.gap,
                None if g is None else neutral_gap(g.gap, index, g.previous, g.ex),
                ratio_from_journal(r.ex_date, imported, r.text, first_closes),
                None if g is None else settle_known(g.days, r.ex_date).on,
            )
        )
    return out


def apply(session: Session, demergers: Sequence[Demerger], *, neutral: bool) -> tuple[int, int]:
    """Append each demerger's estimates not stored yet (the caller holds the reference-data
    lock); with `neutral`, the owner-approved neutralised gap becomes the accepted value of each
    demerger applying the raw gap. Returns (versions appended, rows changed)."""
    ledger = Ledger.open(session, {d.row.instrument_id for d in demergers})
    appended = changed = 0
    for d in demergers:
        key = (d.row.instrument_id, d.row.ex_date, d.row.action_type)
        span = d.band
        wanted: list[tuple[str, Fraction, date | None, str]] = []
        if d.neutral is not None:
            wanted.append((CROSS_CHECK, d.neutral, d.gap_available, INDEX_NEUTRAL_GAP))
        if d.raw is not None and not d.applies_gap:
            wanted.append((CROSS_CHECK, d.raw, d.gap_available, RAW_GAP))
        if d.ratio is not None and not same_factor(Decimal(d.row.factor), d.ratio.value):
            status = DISPUTED if d.disputed else CROSS_CHECK
            wanted.append((status, d.ratio.value, d.ratio.available, RESULTING_RATIO))
        for status, value, available, basis in wanted:
            factor = factor_to_decimal(value)
            if ledger.has(key, status, factor, None, basis, available):
                continue
            ledger.add(
                key,
                status=status,
                factor=factor,
                cash=None,
                available=available,
                basis=basis,
                source=SOURCE,
                text=f"{basis} of {d.row.text}",
                band_range=span,
            )
            appended += 1
        if neutral and d.neutral is not None and d.applies_gap:
            row = session.get(HatCorporateAction, uuid.UUID(d.row.id))
            factor = factor_to_decimal(d.neutral)
            if row is not None and Decimal(row.adjustment_factor) != factor:
                row.adjustment_factor = factor
                ledger.add_row(row, source=SOURCE, text=f"{INDEX_NEUTRAL_GAP} (owner approved)")
                changed += 1
    session.flush()
    return appended, changed


def _f(value: Fraction | None) -> str:
    return "-" if value is None else f"{float(value):.4f}"


def table_lines(demergers: Sequence[Demerger]) -> list[str]:
    """The demerger table, then the review list (disputed ratio values)."""
    neutral = sum(d.neutral is not None for d in demergers)
    dated = sum(d.ratio is not None and d.ratio.available is not None for d in demergers)
    lines = [
        f"demergers {len(demergers)}: index-neutralised {neutral}"
        f", ratio value {sum(d.ratio is not None for d in demergers)} (dated {dated})"
        f", applied is the raw gap {sum(d.applies_gap for d in demergers)}"
        f", disputed {sum(d.disputed for d in demergers)}",
        "symbol\tex_date\tsource\tapplied\traw_gap\tneutral\tratio\tratio_on\tband\tknown_on\tstatus",
    ]
    for d in demergers:
        low, high = d.band
        lines.append(
            "\t".join(
                [
                    d.symbol,
                    d.row.ex_date.isoformat(),
                    d.row.source,
                    _f(d.applied),
                    _f(d.raw),
                    _f(d.neutral),
                    _f(d.ratio and d.ratio.value),
                    str(d.ratio.available) if d.ratio and d.ratio.available else "-",
                    f"{_f(low)}..{_f(high)}",
                    str(d.known_on or "-"),
                    "disputed" if d.disputed else "ok",
                ]
            )
        )
    review = [d for d in demergers if d.disputed]
    lines.append(
        f"review (ratio more than {DISPUTE_TOL:.0%} log from the applied value): {len(review)}"
    )
    lines += [
        f"  {d.symbol} {d.row.ex_date} applied {_f(d.applied)}"
        f" ratio {_f(d.ratio and d.ratio.value)} {d.row.text[:80]}"
        for d in review
    ]
    return lines


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.ca_demergers [--apply] [--neutral]

    Lists every stored demerger's estimates, band and review list (a dry run); `--apply` appends
    the versions not stored yet in one transaction under the reference-data lock; `--neutral`
    (with `--apply`, only once the owner approved it) makes the neutralised gap the applied value,
    after which `python -m app.hat.data.features --full` must run. Bars and index under
    `HAT_DATA_ROOT`."""
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.ca_demergers")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--neutral", action="store_true")
    args = parser.parse_args(argv)
    if args.neutral and not args.apply:
        parser.error("--neutral needs --apply")
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatSessionLocal

    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    write: Callable[[str], Any] = lambda s: sys.stdout.write(s + "\n")  # noqa: E731
    with HatSessionLocal() as session:
        if args.apply:
            lock_reference_data(session)
        demergers = plan(session, store)
        for line in table_lines(demergers):
            write(line)
        if args.apply:
            appended, changed = apply(session, demergers, neutral=args.neutral)
            session.commit()
            write(f"applied: {appended} versions appended, {changed} factors changed")
            if changed:
                write("the factors changed: run `python -m app.hat.data.features --full`")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
