"""Point-in-time industry history from NSE's NIFTY 500 list snapshots (docs/decisions.md,
"Industry history").

One taxonomy: the `Industry` column of NSE's current NIFTY 500 list. A snapshot is in it when
most of its labels are labels of the newest snapshot (the lists from 2022 are; the upper-case
CNX 500 lists of 2010-2020 are not). Each instrument chain (an ISIN and its linked successors,
`chains`) takes its classification from the snapshots in that taxonomy that list one of its
ISINs: the first is in force from the chain's first bar, and a later snapshot that lists it
under another industry appends a reclassification from that snapshot's day. A chain only the
old lists name takes the current label its old label maps to, from its first bar, when the
mapping is unambiguous (`old_mapping`); otherwise it stays unknown (no row).

The snapshots are `HAT_DATA_ROOT/admin/<dir>/YYYY-MM-DD.csv` (data, never committed), dated by
the day they were captured. `load_industry_history` writes `hat_industry` from them, under the
reference-data lock, on each chain's current ISIN; a chain's rows dated on or before the newest
snapshot are replaced by what the snapshots say, and later rows (the nightly reference job's,
`instruments.load_industries`) are kept. Without `apply` a dry run that writes nothing; the
same files applied twice change nothing.
"""

from __future__ import annotations

import re
import uuid
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..models import HatIndexMember, HatIndustry, HatInstrument, HatTradingCalendar
from .chains import chains, stored_links
from .corporate_actions import lock_reference_data
from .index_membership import NIFTY_500
from .reference import parse_industry_csv
from .store import DataStore

__all__ = [
    "COUNT_FROM",
    "IndustryHistoryError",
    "Snapshot",
    "chain_history",
    "load_industry_history",
    "old_mapping",
    "parse_snapshot",
    "plan_rows",
    "snapshot_day",
    "unknown_member_days",
]

COUNT_FROM = date(2016, 1, 1)  # the dry run's coverage counts start with the backtest history
# An old label maps to the current label that at least this share of the chains listed in both
# taxonomies took, over at least `MAP_MIN_CHAINS` of them; anything looser stays unknown.
MAP_SHARE = 0.9
MAP_MIN_CHAINS = 3
CURRENT_SHARE = 0.5  # a snapshot whose labels are mostly the newest one's is in its taxonomy
_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.csv$")


class IndustryHistoryError(HatError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Snapshot:
    """One NIFTY 500 list as captured on `day`: (ISIN, industry) per listed stock."""

    day: date
    rows: tuple[tuple[str, str], ...]

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(industry for _, industry in self.rows)


def snapshot_day(name: str) -> date | None:
    """The capture day of a snapshot file named `YYYY-MM-DD.csv`; None for any other name."""
    found = _NAME.match(name)
    return None if found is None else date.fromisoformat(found.group(1))


def parse_snapshot(text: str) -> list[tuple[str, str]]:
    """(ISIN, industry) of an NSE index-constituent CSV; the old lists' title lines above the
    `Company Name` header are skipped, as are rows without an ISIN or an industry."""
    lines = text.lstrip("﻿").splitlines()
    start = next((n for n, line in enumerate(lines) if line.startswith("Company Name")), None)
    if start is None:
        return []
    rows = parse_industry_csv("\n".join(lines[start:]))
    return sorted({(r.isin, r.industry) for r in rows if r.isin and r.industry})


def _current(snapshots: Sequence[Snapshot]) -> list[Snapshot]:
    """The snapshots in the newest one's taxonomy, oldest first."""
    ordered = sorted(snapshots, key=lambda s: s.day)
    vocabulary = ordered[-1].labels
    return [
        s
        for s in ordered
        if s.labels and len(s.labels & vocabulary) >= CURRENT_SHARE * len(s.labels)
    ]


def _by_chain(snapshot: Snapshot, chain_of: Mapping[str, str]) -> dict[str, str]:
    """The snapshot's industry per chain (an ISIN no instrument has is skipped)."""
    return {chain_of[i]: industry for i, industry in snapshot.rows if i in chain_of}


def old_mapping(old: Iterable[Mapping[str, str]], current: Mapping[str, str]) -> dict[str, str]:
    """Each old label -> the current label it maps to: the chains an old list (`old`, chain ->
    label) and the current taxonomy (`current`, chain -> its newest label) both name vote, and
    a label maps when at least `MAP_SHARE` of `MAP_MIN_CHAINS` or more agree."""
    votes: dict[str, dict[str, str]] = defaultdict(dict)
    for listing in old:
        for chain, label in listing.items():
            if chain in current:
                votes[label][chain] = current[chain]
    out = {}
    for label, by_chain in votes.items():
        (best, n), *_ = Counter(by_chain.values()).most_common(1)
        if len(by_chain) >= MAP_MIN_CHAINS and n >= MAP_SHARE * len(by_chain):
            out[label] = best
    return out


def chain_history(
    snapshots: Sequence[Snapshot], chain_of: Mapping[str, str], first_bar: Mapping[str, date]
) -> tuple[dict[str, list[tuple[date, str]]], dict[str, str]]:
    """Each classified chain's (effective_from, industry) rows, oldest first, and the old-label
    mapping used. The first row is in force from the chain's first bar (the snapshot's day when
    the chain has no bar before it); a later current-taxonomy snapshot under another industry
    appends a row from its day."""
    current = _current(snapshots)
    newer = {s.day for s in current}
    history: dict[str, list[tuple[date, str]]] = {}
    for snapshot in current:
        for chain, industry in sorted(_by_chain(snapshot, chain_of).items()):
            rows = history.setdefault(chain, [])
            if not rows:
                rows.append((min(first_bar.get(chain, snapshot.day), snapshot.day), industry))
            elif rows[-1][1] != industry:
                rows.append((snapshot.day, industry))
    newest = {chain: rows[-1][1] for chain, rows in history.items()}
    old = [
        (s.day, _by_chain(s, chain_of))
        for s in sorted(snapshots, key=lambda s: s.day)
        if s.day not in newer
    ]
    mapping = old_mapping((listing for _, listing in old), newest)
    for day, listing in old:  # oldest first, so the newest old list's label wins
        for chain, label in listing.items():
            if chain not in newest:
                found = mapping.get(label)
                if found is None:
                    history.pop(chain, None)  # its newest old label maps to nothing: unknown
                else:
                    history[chain] = [(min(first_bar.get(chain, day), day), found)]
    return history, mapping


def plan_rows(
    target: Mapping[str, Sequence[tuple[date, str]]],
    stored: Iterable[tuple[str, str, date, str]],
    managed_until: date,
) -> tuple[list[tuple[str, date, str]], list[tuple[str, date]], int]:
    """(rows to insert as (head, effective_from, industry), stored rows to delete as
    (instrument, effective_from), stored rows kept) for the classified chains: `target` per
    chain head; `stored` as (instrument, its chain head, effective_from, industry). A classified
    chain's stored rows dated on or before `managed_until` that are not its target go; later
    ones, and every row of a chain the snapshots do not classify, stay."""
    held: set[tuple[str, date, str]] = set()
    deletes: list[tuple[str, date]] = []
    kept = 0
    for instrument, head, day, industry in stored:
        if head not in target or day > managed_until:
            kept += 1
        elif instrument == head and (day, industry) in set(target[head]):
            held.add((head, day, industry))
            kept += 1
        else:
            deletes.append((instrument, day))
    inserts = [
        (head, day, industry)
        for head, rows in sorted(target.items())
        for day, industry in rows
        if (head, day, industry) not in held
    ]
    return inserts, sorted(deletes), kept


def unknown_member_days(
    sessions: Sequence[date],
    spans: Mapping[str, Sequence[tuple[date, date | None]]],
    history: Mapping[str, Sequence[tuple[date, str]]],
) -> tuple[int, int, dict[str, int]]:
    """(member-days, member-days without a classification in force, those per chain) over
    `sessions` (sorted): a chain is a member on the sessions inside its [from, to) `spans`, and
    unknown before its first `history` row."""
    total = unknown = 0
    per_chain: dict[str, int] = {}
    for chain, member_spans in spans.items():
        rows = history.get(chain, ())
        first = min((day for day, _ in rows), default=None)
        missing = 0
        for lo, hi in member_spans:
            a = bisect_left(sessions, lo)
            b = len(sessions) if hi is None else bisect_left(sessions, hi)
            if b <= a:
                continue
            total += b - a
            cut = b if first is None else min(b, max(a, bisect_left(sessions, first)))
            missing += cut - a
        if missing:
            per_chain[chain] = missing
            unknown += missing
    return total, unknown, per_chain


# --- the database ------------------------------------------------------------------------------

_FIRST_BARS_SQL = "SELECT CAST(instrument_id AS VARCHAR), min(date) FROM bars_daily_raw GROUP BY 1"


def _sessions(session: Session, first: date, last: date) -> list[date]:
    return sorted(
        session.scalars(
            select(HatTradingCalendar.day).where(
                HatTradingCalendar.is_trading.is_(True), HatTradingCalendar.day.between(first, last)
            )
        ).unique()
    )


def load_industry_history(
    session: Session,
    store: DataStore,
    snapshots: Sequence[Snapshot],
    *,
    count_from: date = COUNT_FROM,
    apply: bool,
) -> dict[str, Any]:
    """Write `hat_industry` from `snapshots` (module docstring) and report what changes and the
    NIFTY 500 coverage from `count_from` to the newest snapshot: the member chains classified
    and the member-days left unknown, before and after. Without `apply` nothing is written.
    Refused without a snapshot (`industry_snapshots_missing`)."""
    if not snapshots:
        raise IndustryHistoryError("industry_snapshots_missing")
    lock_reference_data(session)
    newest = max(s.day for s in snapshots)
    ids = {
        isin: str(i) for isin, i in session.execute(select(HatInstrument.isin, HatInstrument.id))
    }
    heads = chains(stored_links(session))

    def head(instrument: str) -> str:
        return heads.get(instrument, (instrument, 0))[0]

    chain_of = {isin: head(i) for isin, i in ids.items()}
    with store.connect() as con:
        first_bar: dict[str, date] = {}
        for instrument, day in con.execute(_FIRST_BARS_SQL).fetchall():
            chain = head(instrument)
            first_bar[chain] = min(day, first_bar.get(chain, day))
    target, mapping = chain_history(snapshots, chain_of, first_bar)
    stored = [
        (str(r.instrument_id), head(str(r.instrument_id)), r.effective_from, r.industry)
        for r in session.scalars(select(HatIndustry))
    ]
    inserts, deletes, kept = plan_rows(target, stored, newest)
    spans: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
    for r in session.scalars(select(HatIndexMember).where(HatIndexMember.index_name == NIFTY_500)):
        if r.effective_to is None or r.effective_to > count_from:
            spans[head(str(r.instrument_id))].append((r.effective_from, r.effective_to))
    sessions = _sessions(session, count_from, newest)
    before: dict[str, list[tuple[date, str]]] = defaultdict(list)
    for _, chain, day, industry in stored:
        before[chain].append((day, industry))
    total, unknown_before, _ = unknown_member_days(sessions, spans, before)
    _, unknown_after, left = unknown_member_days(sessions, spans, target)
    if apply:
        for instrument, day in deletes:
            session.execute(
                delete(HatIndustry).where(
                    HatIndustry.instrument_id == uuid.UUID(instrument),
                    HatIndustry.effective_from == day,
                )
            )
        session.flush()
        session.add_all(
            HatIndustry(
                instrument_id=uuid.UUID(chain),
                sector=industry,
                industry=industry,
                effective_from=day,
            )
            for chain, day, industry in inserts
        )
        session.flush()
    names = {str(i): s for i, s in session.execute(select(HatInstrument.id, HatInstrument.symbol))}
    return {
        "applied": apply,
        "snapshots": [s.day.isoformat() for s in sorted(snapshots, key=lambda s: s.day)],
        "current_taxonomy_from": _current(snapshots)[0].day.isoformat(),
        "old_label_mapping": dict(sorted(mapping.items())),
        "chains_classified": len(target),
        "chains_reclassified": sum(len(rows) > 1 for rows in target.values()),
        "inserted": len(inserts),
        "deleted": len(deletes),
        "kept": kept,
        "coverage": {
            "index": NIFTY_500,
            "from": count_from.isoformat(),
            "to": newest.isoformat(),
            "member_chains": len(spans),
            "member_chains_classified": sum(1 for c in spans if c in target),
            "member_days": total,
            "unknown_member_days_before": unknown_before,
            "unknown_member_days": unknown_after,
            "unknown_pct_before": _pct(unknown_before, total),
            "unknown_pct": _pct(unknown_after, total),
            "unknown_members": sorted(names.get(c, c) for c in left),
        },
    }


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 3) if whole else 0.0


def read_snapshots(folder: Path) -> list[Snapshot]:
    """The `YYYY-MM-DD.csv` snapshots in `folder`; other files are ignored."""
    out = []
    for path in sorted(folder.iterdir()):
        day = snapshot_day(path.name)
        if day is not None:
            try:
                text = path.read_text("utf-8", errors="replace")
            except OSError:
                raise IndustryHistoryError("industry_snapshot_unreadable") from None
            out.append(Snapshot(day, tuple(parse_snapshot(text))))
    return out
