"""ISIN lineage: one price history per company across ISIN changes, never across two companies
(PR 74).

HAT keeps one instrument per ISIN (`identity.py`). NSE gives the same shares a new ISIN at a
face-value split or consolidation, at a reissue, sometimes with a symbol or name change, and each
ISIN then holds a piece of one history: a fresh listing on one side, a dead end on the other.
`hat_isin_lineage` links the old ISIN's instrument to the new one; the adjusted view reads every
linked instrument under its chain's current id (`Adjustment.lineage`) and applies each corporate
action once per chain (`chains`: the copies the MCP lookup stored under each ISIN of the chain
collapse into one). The instruments, their symbol histories, signals and positions are not
touched: each ISIN keeps its own id and rows.

A succession is linked only when all of these hold (`decide`), else it goes to the review list
(`review` rows, `python -m app.hat.data.lineage report`, `lineage_review`): a false link is
worse than a missing one.
- Succession: the old ISIN's last session is followed by the new ISIN's first within
  `MAX_GAP_SESSIONS` (NSE suspends a few sessions around the change), the two never trade on
  the same day, and neither side has another candidate.
- The same security: the same symbol across the change, or an NSE symbol-change record from the
  old symbol to the new one near it (`SYMBOL_CHANGE_SLACK`).
- NSE carried the old close: the new ISIN's first PREVCLOSE is the old last close, raw or times
  the factor of the actions between the two days, within `PREV_CLOSE_TOL` (1%).
- Price continuity: the new first open is within `OPEN_TOL` (10%) of the old last close times
  that factor. 10% is half NSE's widest price band and well under the error a missing
  ISIN-changing factor leaves (a 2:1 split is 50%, an ETF unit split 90%).
- No unexplained move at the edges: every session move in the last and first `EDGE_DAYS` of the
  two ISINs (open over the previous close times the factors between) is within `JUMP_TOL`
  (25%, beyond NSE's widest band). The action that brings the new ISIN is often ex on the old
  ISIN's last session; when its factor is not stored the chain would carry a crash there.
- No restructuring: no stored action of either side near the change is a demerger or names a
  demerger, amalgamation, merger, scheme of arrangement or capital reduction.

The owner can overrule the rules on a found succession (`link` / `unlink OLD NEW`, D1,
`lineage_override`): the verdict is kept in `hat_lineage_override` and every build applies it
over the rules' (`apply_overrides`), so it holds across nightly rebuilds.

Each row keeps `known_on`, the last bar the rules read (the new ISIN's head, its first
`EDGE_DAYS` days), which the point-in-time report lists (CA5, `point_in_time`).

A demerger is never a succession: the parent keeps its ISIN (TATAMOTORS became TMPV on
INE155A01022) and the resulting company is a new listing with its own history (TMCV,
INE1TAE01010). A merged-away company's history just ends (HDFC Ltd, July 2023). The parent's
price drop on a demerger ex-date is adjusted only by a factor the corporate-action file import
stored (`ca_import.py`, type `other` flagged a demerger, so a succession near it is reviewed);
without one the drop shows in the report's unexplained jumps (`show TMPV` lists its chain's).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import HatConfigError
from ..models import HatInstrument, HatIsinLineage, HatLineageOverride
from .calendar import recorded_history, session_calendar
from .chains import (
    LINKED,
    REVIEW,
    Action,
    LineageError,
    chain_members,
    chains,
    stored_actions,
    stored_links,
)
from .corporate_actions import TYPE_SPLIT, lock_reference_data
from .lineage_review import JUMP_KINDS, JUMP_TOL, review_report, show_chains
from .nse_changes import (
    NAME_CHANGES_URL,
    SYMBOL_CHANGES_URL,
    NameChange,
    SymbolChange,
    parse_name_changes,
    parse_symbol_changes,
    read_list,
)
from .store import DATASET_BARS, DataStore, newest

__all__ = [
    "Bar",
    "Candidate",
    "Decision",
    "Span",
    "apply_overrides",
    "build_lineage",
    "candidates",
    "decide",
    "decide_all",
]

MAX_GAP_SESSIONS = 5  # sessions between the old ISIN's last day and the new one's first
REVIEW_GAP_SESSIONS = 30  # a longer gap is not a succession at all; up to it, a review case
PREV_CLOSE_TOL = Decimal("0.01")
OPEN_TOL = Decimal("0.10")
SYMBOL_CHANGE_SLACK = timedelta(days=7)
RESTRUCTURING_WINDOW = timedelta(days=30)
RESTRUCTURING = re.compile(
    r"demerg|amalgam|merger|arrangement|capital reduction|reduction (?:of|in) (?:share )?capital",
    re.IGNORECASE,
)
# A session move (open over the previous close) this far from 1 needs a factor: NSE's widest
# price band is 20%, and the margin keeps band moves rounded to the tick off the list.
EDGE_DAYS = 10  # the days each side of a change whose every session move is checked

VIA_SYMBOL, VIA_SYMBOL_CHANGE = "same_symbol", "symbol_change"
# Why a succession is linked, most specific first.
FACE_VALUE_CHANGE, SYMBOL_CHANGE, NAME_CHANGE, ISIN_REISSUE = (
    "face_value_change",
    "symbol_change",
    "name_change",
    "isin_reissue",
)
# Why one is not.
AMBIGUOUS, LONG_GAP, RESTRUCTURED = "ambiguous", "long_gap", "restructuring"
FACTOR_CONFLICT, NO_PREV_CLOSE = "factor_conflict", "no_prev_close"
PREV_CLOSE_MISMATCH, PRICE_JUMP = "prev_close_mismatch", "price_jump"
# The owner's verdicts (`hat_lineage_override`) and the reasons they leave.
LINK, UNLINK = "link", "unlink"
OWNER_LINK, OWNER_UNLINK = "owner_link", "owner_unlink"
OWNER_DISPLACED = "owner_displaced"  # a rules link that shares a side with an owner link
# --- the pure rules -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    day: date
    open: Decimal
    close: Decimal


@dataclass(frozen=True)
class Span:
    """One ISIN's run of daily bars: its first and last session and symbol, NSE's PREVCLOSE on
    the first, and its bars of the first and of the last `EDGE_DAYS` (oldest first)."""

    instrument_id: str
    isin: str
    first_day: date
    last_day: date
    first_symbol: str
    last_symbol: str
    first_prev_close: Decimal | None
    head: tuple[Bar, ...]
    tail: tuple[Bar, ...]

    @property
    def first_open(self) -> Decimal:
        return self.head[0].open

    @property
    def last_close(self) -> Decimal:
        return self.tail[-1].close


@dataclass(frozen=True)
class Candidate:
    """A possible succession old -> new; `change` is the symbol-change record it came by."""

    old: Span
    new: Span
    gap_sessions: int
    via: str
    change: SymbolChange | None = None


@dataclass(frozen=True)
class Decision:
    old_id: str
    new_id: str
    effective: date
    status: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    known_on: date | None = None  # the last bar the rules read: the new ISIN's head (CA5)


def sessions_between(sessions: Sequence[date], first: date, last: date) -> int:
    """The sessions strictly between two days, from the sorted exchange sessions."""
    return max(0, bisect_left(sessions, last) - bisect_right(sessions, first))


def exchange_sessions(
    stored: Sequence[date], closed: Collection[date], special: Collection[date]
) -> list[date]:
    """The exchange sessions from the first stored day to the last: every weekday not a known
    holiday, every special session and every stored day. A session whose bar partition is
    missing still counts, so a gap is never shorter than the exchange's; with no holiday known
    a holiday counts as a session, which only lengthens a gap (never a link it should not be)."""
    if not stored:
        return []
    first, last = min(stored), max(stored)
    out = set(stored) | {d for d in special if first <= d <= last}
    day = first
    while day <= last:
        if day.weekday() < 5 and day not in closed:
            out.add(day)
        day += timedelta(days=1)
    return sorted(out)


def calendar_days(session: Session) -> tuple[set[date], set[date]]:
    """(closed days, special sessions) from the recorded holiday history and the stored
    calendar; a stored row wins over the recorded file."""
    days = {d.day: d.is_trading for d in recorded_history()}
    calendar = session_calendar(session)
    if calendar is not None:
        days.update((day, row.is_trading) for day, row in calendar.days.items())
    closed = {d for d, trading in days.items() if not trading}
    special = {d for d, trading in days.items() if trading and d.weekday() >= 5}
    return closed, special


def candidates(
    spans: Iterable[Span], sessions: Sequence[date], changes: Iterable[SymbolChange]
) -> list[Candidate]:
    """Every pair (old, new) where the new ISIN starts after the old one's last session, within
    `REVIEW_GAP_SESSIONS`, under the old one's last symbol or the one a symbol-change record
    gave it near the change. One candidate per pair; the same symbol wins over a record."""
    spans = list(spans)
    by_first: dict[str, list[Span]] = defaultdict(list)
    for span in spans:
        by_first[span.first_symbol].append(span)
    renamed: dict[str, list[SymbolChange]] = defaultdict(list)
    for change in changes:
        renamed[change.old].append(change)
    out: dict[tuple[str, str], Candidate] = {}
    for old in spans:
        targets: list[tuple[Span, SymbolChange | None]] = [
            (new, None) for new in by_first.get(old.last_symbol, ())
        ]
        for change in renamed.get(old.last_symbol, ()):
            targets += [
                (new, change)
                for new in by_first.get(change.new, ())
                if old.last_day - SYMBOL_CHANGE_SLACK
                <= change.effective
                <= new.first_day + SYMBOL_CHANGE_SLACK
            ]
        for new, change in targets:
            key = (old.instrument_id, new.instrument_id)
            if old.instrument_id == new.instrument_id or new.first_day <= old.last_day:
                continue
            if key in out:
                continue
            gap = sessions_between(sessions, old.last_day, new.first_day)
            if gap <= REVIEW_GAP_SESSIONS:
                via = VIA_SYMBOL if change is None else VIA_SYMBOL_CHANGE
                out[key] = Candidate(old, new, gap, via, change)
    return sorted(out.values(), key=lambda c: (c.new.first_day, c.old.isin, c.new.isin))


def _ratio(value: Decimal, base: Decimal) -> Decimal:
    return value / base - 1


def _one_per_action(actions: Iterable[Action]) -> tuple[dict[tuple[date, str], Action], bool]:
    """The price-changing actions, one per (ex-date, type), and True when two copies of one
    disagree."""
    chosen: dict[tuple[date, str], Action] = {}
    conflict = False
    for action in actions:
        if action.changes_price:
            held = chosen.setdefault((action.ex_date, action.action_type), action)
            conflict = conflict or held.factor != action.factor
    return chosen, conflict


def _factor(chosen: Mapping[tuple[date, str], Action], after: date, through: date) -> Decimal:
    """The product of the factors with ex-date in (after, through]."""
    factor = Decimal(1)
    for (ex_date, _), action in chosen.items():
        if after < ex_date <= through:
            factor *= action.factor
    return factor


def _worst_move(
    bars: Sequence[Bar], chosen: Mapping[tuple[date, str], Action]
) -> tuple[Bar, Bar, Decimal] | None:
    """The session move (open over the previous close times the factors between) furthest
    from 1 in `bars`, or None with fewer than two bars or a non-positive close."""
    worst = None
    for before, after in zip(bars, bars[1:], strict=False):
        base = before.close * _factor(chosen, before.day, after.day)
        if base <= 0:
            return None
        move = after.open / base
        if worst is None or abs(move - 1) > abs(worst[2] - 1):
            worst = (before, after, move)
    return worst


def decide(
    candidate: Candidate,
    actions: Sequence[Action],
    names: Iterable[NameChange] = (),
    *,
    ambiguous: bool = False,
) -> Decision:
    """The verdict on one candidate from the stored actions of both sides (`actions`) and the
    NSE name changes. See the module docstring for the rules."""
    old, new = candidate.old, candidate.new
    chosen, conflict = _one_per_action(actions)
    factor = _factor(chosen, old.last_day, new.first_day)
    gap_actions = sorted(
        (a for (ex, _), a in chosen.items() if old.last_day < ex <= new.first_day),
        key=lambda a: (a.ex_date, a.action_type),
    )
    expected = old.last_close * factor
    evidence: dict[str, Any] = {
        "old_isin": old.isin,
        "new_isin": new.isin,
        "old_symbol": old.last_symbol,
        "new_symbol": new.first_symbol,
        "old_last_day": old.last_day.isoformat(),
        "new_first_day": new.first_day.isoformat(),
        "gap_sessions": candidate.gap_sessions,
        "via": candidate.via,
        "old_last_close": str(old.last_close),
        "new_prev_close": None if new.first_prev_close is None else str(new.first_prev_close),
        "new_first_open": str(new.first_open),
        "gap_factor": str(factor),
        "gap_actions": [f"{a.ex_date} {a.action_type} {a.factor}" for a in gap_actions],
    }
    if candidate.change is not None:
        evidence["symbol_change"] = candidate.change.effective.isoformat()
    window = (old.last_day - RESTRUCTURING_WINDOW, new.first_day + RESTRUCTURING_WINDOW)
    restructuring = sorted(
        {
            a.text
            for a in actions
            if window[0] <= a.ex_date <= window[1] and (a.demerger or RESTRUCTURING.search(a.text))
        }
    )
    renamed = sorted(
        n.effective.isoformat()
        for n in names
        if n.symbol in (old.last_symbol, new.first_symbol) and window[0] <= n.effective <= window[1]
    )
    if renamed:
        evidence["name_change"] = renamed

    def verdict(status: str, reason: str) -> Decision:
        return Decision(
            old.instrument_id,
            new.instrument_id,
            new.first_day,
            status,
            reason,
            evidence,
            new.head[-1].day if new.head else None,
        )

    if ambiguous:
        return verdict(REVIEW, AMBIGUOUS)
    if candidate.gap_sessions > MAX_GAP_SESSIONS:
        return verdict(REVIEW, LONG_GAP)
    if restructuring:
        evidence["restructuring"] = restructuring
        return verdict(REVIEW, RESTRUCTURED)
    if conflict:
        return verdict(REVIEW, FACTOR_CONFLICT)
    if old.last_close <= 0 or expected <= 0:
        return verdict(REVIEW, PRICE_JUMP)
    if new.first_prev_close is None:
        return verdict(REVIEW, NO_PREV_CLOSE)
    raw_gap = _ratio(new.first_prev_close, old.last_close)
    adjusted_gap = _ratio(new.first_prev_close, expected)
    evidence["prev_close_ratio"] = str(round(min(raw_gap, adjusted_gap, key=abs) + 1, 6))
    if min(abs(raw_gap), abs(adjusted_gap)) > PREV_CLOSE_TOL:
        return verdict(REVIEW, PREV_CLOSE_MISMATCH)
    open_gap = _ratio(new.first_open, expected)
    evidence["open_ratio"] = str(round(open_gap + 1, 6))
    if abs(open_gap) > OPEN_TOL:
        return verdict(REVIEW, PRICE_JUMP)
    # The action that came with the new ISIN is often ex on the old ISIN's last sessions
    # (HDFCBANK 2019); without its factor the chain would carry a crash there (GOLDBEES 2019).
    worst = _worst_move([*old.tail, *new.head], chosen)
    if worst is not None:
        before, after, move = worst
        evidence["edge_move"] = f"{before.day} -> {after.day} x{round(move, 6)}"
        if abs(move - 1) > JUMP_TOL:
            return verdict(REVIEW, PRICE_JUMP)
    if any(a.action_type == TYPE_SPLIT for a in _near(actions, old.last_day, new.first_day)):
        return verdict(LINKED, FACE_VALUE_CHANGE)
    if old.last_symbol != new.first_symbol:
        return verdict(LINKED, SYMBOL_CHANGE)
    if renamed:
        return verdict(LINKED, NAME_CHANGE)
    return verdict(LINKED, ISIN_REISSUE)


def _near(actions: Iterable[Action], last: date, first: date) -> list[Action]:
    """The actions with an ex-date from a week before the old ISIN's last day through the new
    one's first: the old ISIN often trades its last session ex the split (HDFCBANK, 2019)."""
    return [a for a in actions if last - SYMBOL_CHANGE_SLACK <= a.ex_date <= first]


def decide_all(
    found: Sequence[Candidate],
    actions: Mapping[str, Sequence[Action]],
    names: Sequence[NameChange] = (),
    members: Mapping[str, frozenset[str]] | None = None,
) -> list[Decision]:
    """A verdict per candidate, from the stored actions of every instrument of both sides'
    chains (`members`, from the stored links: an action the MCP stored under an older ISIN of
    the chain counts too). An instrument in more than one candidate on the same side makes each
    of them ambiguous, so a chain never forks."""
    members = members or {}
    olds = defaultdict(int)
    news = defaultdict(int)
    for c in found:
        olds[c.old.instrument_id] += 1
        news[c.new.instrument_id] += 1

    def chain(c: Candidate) -> list[str]:
        ids = {c.old.instrument_id, c.new.instrument_id}
        for iid in (c.old.instrument_id, c.new.instrument_id):
            ids |= members.get(iid, frozenset())
        return sorted(ids)

    return [
        decide(
            c,
            [a for iid in chain(c) for a in actions.get(iid, ())],
            names,
            ambiguous=olds[c.old.instrument_id] > 1 or news[c.new.instrument_id] > 1,
        )
        for c in found
    ]


def apply_overrides(
    decisions: Iterable[Decision], overrides: Mapping[tuple[str, str], str]
) -> list[Decision]:
    """The decisions with the owner's verdicts, keyed (old id, new id), over the rules': a pair
    the owner linked is linked (`owner_link`), one unlinked is a review case (`owner_unlink`),
    and a rules link sharing a side with an owner link becomes a review case
    (`owner_displaced`), so a chain never forks. The rules' own verdict stays in the evidence
    (`rules`). A verdict on a pair no decision holds changes nothing."""
    owned = [pair for pair, verdict in overrides.items() if verdict == LINK]
    olds, news = {old for old, _ in owned}, {new for _, new in owned}
    out = []
    for d in decisions:
        verdict = overrides.get((d.old_id, d.new_id))
        if verdict == LINK:
            status, reason = LINKED, OWNER_LINK
        elif verdict == UNLINK:
            status, reason = REVIEW, OWNER_UNLINK
        elif d.status == LINKED and (d.old_id in olds or d.new_id in news):
            status, reason = REVIEW, OWNER_DISPLACED
        else:
            out.append(d)
            continue
        rules = d.evidence.get("rules", f"{d.status}:{d.reason}")
        out.append(
            replace(d, status=status, reason=reason, evidence={**d.evidence, "rules": rules})
        )
    return out


# --- the database -------------------------------------------------------------------------------


def stored_overrides(session: Session) -> dict[tuple[str, str], str]:
    return {
        (str(r.old_instrument_id), str(r.new_instrument_id)): r.verdict
        for r in session.scalars(select(HatLineageOverride))
    }


# Aggregates only (a window over every bar would hold the whole history in memory); on a day
# written by more than one ingest run the latest run's row wins, as in `bars_daily`.
_SPANS_SQL = f"""
WITH span AS (
    SELECT instrument_id, min(date) AS first_day, max(date) AS last_day
    FROM bars_daily_raw GROUP BY instrument_id
),
f AS (
    SELECT r.instrument_id, {newest("isin")} AS isin, {newest("symbol")} AS symbol,
           {newest("prev_close")} AS prev_close
    FROM bars_daily_raw r JOIN span s ON r.instrument_id = s.instrument_id AND r.date = s.first_day
    GROUP BY r.instrument_id
),
l AS (
    SELECT r.instrument_id, {newest("symbol")} AS symbol
    FROM bars_daily_raw r JOIN span s ON r.instrument_id = s.instrument_id AND r.date = s.last_day
    GROUP BY r.instrument_id
)
SELECT s.instrument_id, f.isin, s.first_day, s.last_day, f.symbol, l.symbol, f.prev_close
FROM span s
JOIN f ON f.instrument_id = s.instrument_id
JOIN l ON l.instrument_id = s.instrument_id
ORDER BY s.instrument_id
"""  # noqa: S608
_EDGES_SQL = f"""
WITH span AS (
    SELECT instrument_id, min(date) AS first_day, max(date) AS last_day
    FROM bars_daily_raw GROUP BY instrument_id
)
SELECT r.instrument_id, r.date, {newest("open")}, {newest("close")}
FROM bars_daily_raw r JOIN span s ON r.instrument_id = s.instrument_id
     AND (r.date - s.first_day <= ? OR s.last_day - r.date <= ?)
GROUP BY r.instrument_id, r.date
ORDER BY r.instrument_id, r.date
"""  # noqa: S608


def read_spans(store: DataStore) -> list[Span]:
    """Each instrument's span of raw daily bars, with the bars of its first and last `EDGE_DAYS`
    (DuckDB returns the decimal prices as Decimals)."""
    with closing(store.connect()) as con:
        rows = con.execute(_SPANS_SQL).fetchall()
        edges = con.execute(_EDGES_SQL, [EDGE_DAYS, EDGE_DAYS]).fetchall()
    bars: dict[str, list[Bar]] = defaultdict(list)
    for instrument_id, day, open_, close in edges:
        bars[str(instrument_id)].append(Bar(day, open_, close))
    out = []
    for iid, isin, first, last, first_symbol, last_symbol, prev_close in rows:
        own = bars[str(iid)]
        head = tuple(b for b in own if (b.day - first).days <= EDGE_DAYS)
        tail = tuple(b for b in own if (last - b.day).days <= EDGE_DAYS)
        out.append(
            Span(str(iid), isin, first, last, first_symbol, last_symbol, prev_close, head, tail)
        )
    return out


@dataclass
class BuildReport:
    linked: int = 0
    review: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    unknown: int = 0  # candidates naming an instrument id the database does not have
    reasons: dict[str, int] = field(default_factory=dict)
    new_review: list[str] = field(default_factory=list)  # "OLDISIN>NEWISIN", new on the list


def save_decisions(session: Session, decisions: Sequence[Decision], report: BuildReport) -> None:
    """Make `hat_isin_lineage` hold exactly `decisions`: a pair's row is created, updated when
    its verdict or evidence changed, left alone when not, and a stored pair no longer found is
    deleted. Deletes run first, so a pair that changes sides never trips the one-link index."""
    stored = {
        (str(r.old_instrument_id), str(r.new_instrument_id)): r
        for r in session.scalars(select(HatIsinLineage))
    }
    wanted = {(d.old_id, d.new_id): d for d in decisions}
    for key, row in stored.items():
        if key not in wanted:
            session.delete(row)
            report.removed += 1
    session.flush()
    # a link demoted first, then the rest: the partial unique indexes see no transient fork
    ordered = sorted(wanted.values(), key=lambda d: d.status == LINKED)
    for decision in ordered:
        row = stored.get((decision.old_id, decision.new_id))
        values = {
            "effective_date": decision.effective,
            "status": decision.status,
            "reason": decision.reason,
            "evidence": decision.evidence,
            "known_on": decision.known_on,
        }
        if decision.status == REVIEW and (row is None or row.status != REVIEW):
            evidence = decision.evidence
            report.new_review.append(f"{evidence['old_isin']}>{evidence['new_isin']}")
        if row is None:
            session.add(
                HatIsinLineage(
                    old_instrument_id=uuid.UUID(decision.old_id),
                    new_instrument_id=uuid.UUID(decision.new_id),
                    **values,
                )
            )
            report.created += 1
        elif all(getattr(row, k) == v for k, v in values.items()):
            report.unchanged += 1
        else:
            for k, v in values.items():
                setattr(row, k, v)
            report.updated += 1
        session.flush()


def build_lineage(
    session: Session,
    store: DataStore,
    changes: Sequence[SymbolChange],
    names: Sequence[NameChange] = (),
) -> BuildReport:
    """Find every succession in the stored bars, decide each and store the verdicts in the
    session (the caller commits). Idempotent: the same bars, actions and lists change nothing.
    Raises `lineage_no_bars` on an empty store rather than delete every stored row.

    No Postgres transaction sits idle across the long work (a role's
    `idle_in_transaction_session_timeout` would end it): the DuckDB scan runs before the
    database is read, and the read transaction is rolled back before the candidates are found,
    so the session must carry no pending change. The decisions and the writes then run in one
    transaction under the reference-data lock (audit M4), which reads the actions and the
    owner's overrides: a `lineage link`, `correct-action` or import that commits during the
    build is waited for and decided on, never overwritten."""
    stored = store.days(DATASET_BARS)
    if not stored:
        raise LineageError("lineage_no_bars")
    spans = read_spans(store)
    closed, special = calendar_days(session)
    known = {str(i) for i in session.scalars(select(HatInstrument.id))}
    session.rollback()  # end the read transaction: only plain values are kept
    sessions = exchange_sessions(stored, closed, special)
    report = BuildReport()
    found = []
    for c in candidates(spans, sessions, changes):
        if c.old.instrument_id in known and c.new.instrument_id in known:
            found.append(c)
        else:
            report.unknown += 1
    lock_reference_data(session)
    actions = stored_actions(session)
    overrides = stored_overrides(session)
    members = chain_members(stored_links(session))
    by_instrument: dict[str, list[Action]] = defaultdict(list)
    for action in actions:
        by_instrument[action.instrument_id].append(action)
    decisions = apply_overrides(decide_all(found, by_instrument, names, members), overrides)
    for d in decisions:
        report.reasons[f"{d.status}:{d.reason}"] = (
            report.reasons.get(f"{d.status}:{d.reason}", 0) + 1
        )
    report.linked = sum(d.status == LINKED for d in decisions)
    report.review = len(decisions) - report.linked
    chains((d.old_id, d.new_id) for d in decisions if d.status == LINKED)  # never a fork
    save_decisions(session, decisions, report)
    return report


TextSource = Callable[[], str]


def refresh_lineage(
    session_factory: Callable[[], Session],
    store: DataStore,
    symbol_changes: TextSource,
    name_changes: TextSource | None = None,
) -> BuildReport:
    """The nightly refresh (`ca.adjust`): the NSE lists, then `build_lineage` in one
    transaction. A list that cannot be read raises before anything changes: built without
    it, a link that rests on a symbol change would be dropped."""
    changes = parse_symbol_changes(symbol_changes())
    if not changes:
        raise LineageError("lineage_no_symbol_changes")
    names = parse_name_changes(name_changes()) if name_changes is not None else []
    with session_factory() as session:
        report = build_lineage(session, store, changes, names)
        session.commit()
    return report


# --- command line -------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.lineage build [--symbol-changes CSV] [--name-changes CSV]
    python -m app.hat.data.lineage report [--since YYYY-MM-DD] [--limit N] [--kind K ...]
    python -m app.hat.data.lineage show SYMBOL [SYMBOL ...]
    python -m app.hat.data.lineage link|unlink OLD_ISIN NEW_ISIN --note TEXT

    `build` finds and decides every ISIN succession in the bars under `HAT_DATA_ROOT` and stores
    the verdicts (one transaction; run again, it changes nothing). Without a CSV it downloads
    NSE's list. `report` prints the review list, `show` the chains of some symbols. `link` and
    `unlink` are the owner's verdict on a succession the build found (`override_lineage`; it
    needs `HAT_TELEGRAM_CHAT_ID`, the owner's chat id its event carries). After a
    build that changed a link, the next features run recomputes the chains it touched (the
    features state keeps the lineage); `python -m app.hat.data.features` does it at once."""
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.lineage")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--symbol-changes")
    build.add_argument("--name-changes")
    report = sub.add_parser("report")
    report.add_argument("--since", type=date.fromisoformat, default=date(1990, 1, 1))
    report.add_argument("--limit", type=int, default=50)
    report.add_argument("--kind", action="append", choices=JUMP_KINDS, help="list only these jumps")
    show = sub.add_parser("show")
    show.add_argument("symbols", nargs="+")
    for verdict in (LINK, UNLINK):
        override = sub.add_parser(verdict)
        override.add_argument("old_isin")
        override.add_argument("new_isin")
        override.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatSessionLocal

    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    write = sys.stdout.write
    with HatSessionLocal() as session:
        if args.command == "build":
            changes = parse_symbol_changes(read_list(args.symbol_changes, SYMBOL_CHANGES_URL))
            names = parse_name_changes(read_list(args.name_changes, NAME_CHANGES_URL))
            result = build_lineage(session, store, changes, names)
            session.commit()
            write(json.dumps(result.__dict__, sort_keys=True) + "\n")
        elif args.command in (LINK, UNLINK):
            from ..telegram.config import owner_chat_id
            from ..time import utc_now
            from .lineage_override import override_lineage

            try:
                owner = (owner_chat_id(), settings.user_id, utc_now())
                result = override_lineage(
                    session, args.old_isin, args.new_isin, args.command, note=args.note, owner=owner
                )
            except HatConfigError:
                sys.stderr.write("refused: no_owner_chat\n")
                return 2
            except LineageError as exc:
                sys.stderr.write(f"refused: {exc.code}\n")
                return 2
            session.commit()
            write(json.dumps(result, sort_keys=True) + "\n")
        elif args.command == "report":
            review_report(
                session,
                store,
                args.since,
                args.limit,
                lambda s: write(s + "\n"),
                kinds=args.kind or JUMP_KINDS,
            )
        else:
            show_chains(session, store, args.symbols, lambda s: write(s + "\n"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
