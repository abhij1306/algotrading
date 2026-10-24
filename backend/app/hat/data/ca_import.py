"""The corporate-action file import (CA1, D232): the splits and bonuses HAT missed, rights
issues, demergers and ETF unit splits, read from a collected NSE file and stored in
`hat_corporate_action` only when the raw bars confirm them.

The file is a SQLite database built from NSE's corporate-action APIs (tables `splits_bonus`,
`rights_issues`, `demergers`, `etf_unit_actions`); it lives outside the repo and is untrusted
input: it is opened read-only, every cell is read as text, and a row that does not parse goes to
the review list, never into the database.

What each row proposes (`proposals`):
- a split or a bonus: the factor of its NSE purpose text (`parse_action_text`); a bonus with a
  split is both legs. The file's `instrument_class` is only a hint in the report: whether a bonus
  was of equity is what the bars say (a text naming preference shares or debentures is
  price-neutral, as in D1, and skipped).
- a rights issue of M new shares per N held at S (face value plus premium): the cum price P is
  the previous close, and the factor is TERP / P = (N*P + M*S) / ((N+M)*P), 1 (nothing stored)
  when S >= P (`rights_factor`). Partly paid shares, warrants, CCPS and debentures go to review;
  InvIT units are skipped.
- a demerger (status `found`; any other status is reported, never stored) of r resulting shares
  per parent share: the parent's factor is 1 - r*C/P, C the resulting company's first close when
  it lists as a new instrument named like the parent or its evidence (`demerger_factor`). With
  no such listing, the ex-date gap itself is the factor when it is a clear drop
  (`DEMERGER_MIN_DROP`); a composite scheme goes to review. The resulting company is never linked
  into the parent's lineage (owner rule); the stored row's text names the demerger, so the
  lineage build reviews any ISIN change near it (`lineage.RESTRUCTURING`).
- an ETF unit split: new face value over old.

Every factor passes the price gate (`gate`) against the raw bars: the ex-date open over the
previous session's close (NSE's PREVCLOSE in these bars is not adjusted, so the open is the
only witness) must be within `GATE_TOL` of the factor (times any factor already stored on that
day) and at least twice as close to it as to 1, so a gap that could be a plain session move is
ambiguous, not a confirmation. A confirmed factor is stored as a new row of its own type
(`split`, `bonus`, or `other` for rights and demergers) on the instrument that holds the
history before the ex-date, source `nse_ca_file`, through `load_actions` (a stored key is never
rewritten: a different factor is a conflict, fixed with `correct_action`; a key the owner
vacated is never re-created) and journalled `corporate_action.imported`. An action already
stored within `NEAR_DAYS` under the same company's other ISIN, symbol or lineage chain is not
stored again. Repeating an import stores nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import events
from ..errors import HatError
from ..models import HatCorporateAction, HatInstrument, HatInstrumentSymbolHistory, HatIsinLineage
from ..money import exact
from .corporate_actions import (
    PRICE_NEUTRAL_TYPES,
    TYPE_BONUS,
    TYPE_OTHER,
    TYPE_SPLIT,
    ActionRecord,
    factor_to_decimal,
    load_actions,
    non_equity_bonus,
    owner_vacated,
    parse_action_text,
)
from .store import FACTOR_MIN, DataStore

SOURCE_FILE = "nse_ca_file"
EVENT_IMPORTED = "corporate_action.imported"

# The gate: NSE's widest daily price band is 20%, so the ex-date open of a real action is within
# 20% of the previous close times its factor. Measured on the 707 split and bonus factors HAT
# had stored with bars on both days (2026-10-24): the median miss is 2.6%, 90% are within 10%
# and 95% within 16%.
GATE_TOL = 0.20
NEAR_DAYS = 5  # a stored action this close to the ex-date is the same event (sources differ)
BAR_DAYS = 14  # the previous session and the ex session must fall this close to the ex-date
CHILD_DAYS = 365  # a resulting company lists within a year of the demerger's ex-date
DEMERGER_MIN_DROP = Fraction(1, 10)  # a gap-only demerger factor needs a drop beyond 10%
SAME_FACTOR_TOL = Fraction(1, 20000)  # half a unit in the 4th place
TEXT_MAX = 2000  # cells are cut to this length

# --- outcomes -----------------------------------------------------------------------------------

STORED = "stored"  # a new row (with --apply; "to store" in a dry run)
ALREADY = "already"  # confirmed, and the same factor is stored
CONFLICT = "conflict"  # confirmed or not, a different factor is stored for the same event
REVIEW = "review"  # not confirmed, ambiguous, unparsed or without bars: the owner decides
SKIPPED = "skipped"  # nothing to store: price-neutral, InvIT units, an SME row with no instrument
STATUSES = (STORED, ALREADY, CONFLICT, REVIEW, SKIPPED)

T_SPLITS = "splits_bonus"
T_RIGHTS = "rights_issues"
T_DEMERGERS = "demergers"
T_ETF = "etf_unit_actions"
COLUMNS: dict[str, tuple[str, ...]] = {
    T_SPLITS: ("symbol", "isin", "action_type", "subject_raw", "instrument_class", "exDate"),
    T_RIGHTS: (
        "symbol",
        "market",
        "isin",
        "subject_raw",
        "entitlement_ratio_parsed",
        "premium_rs_parsed",
        "faceVal",
        "exDate",
    ),
    T_DEMERGERS: ("symbol", "market", "exDate", "ratio", "ratio_note", "status", "evidence"),
    T_ETF: ("symbol", "old_face_value_rs", "new_face_value_rs", "ex_effective_date"),
}


class CaFileError(HatError):
    """The file cannot be read as the expected tables; `code` is stable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- parsing (pure) -----------------------------------------------------------------------------

_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_NSE_DATE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")
_NUMBER = r"(\d+(?:\.\d+)?)"
_RATIO = re.compile(rf"^\s*{_NUMBER}\s*:\s*{_NUMBER}\s*$")


def nse_date(raw: str) -> date | None:
    """`04-Sep-2014` as a date; anything else (`-`, empty, a bad day) is None."""
    found = _NSE_DATE.fullmatch(raw.strip())
    if found is None or found.group(2).lower() not in _MONTHS:
        return None
    try:
        return date(int(found.group(3)), _MONTHS.index(found.group(2).lower()) + 1, int(found[1]))
    except ValueError:
        return None


def parse_ratio(raw: str) -> tuple[Fraction, Fraction] | None:
    """`a:b` (decimals allowed: `1:48.18`) as two positive numbers, else None."""
    found = _RATIO.match(raw)
    if found is None:
        return None
    a, b = Fraction(found.group(1)), Fraction(found.group(2))
    return (a, b) if a > 0 and b > 0 else None


@dataclass(frozen=True)
class Leg:
    """One factor a row proposes, of a stored type."""

    action_type: str
    factor: Fraction


_SPLIT_START = re.compile(r"face\s*val\w*\s*split|split|sub-?\s*division")


def split_bonus_legs(text: str) -> tuple[Leg, ...] | None:
    """The legs of an NSE split or bonus purpose: one, or a bonus and a split when the text
    names both ("Bonus 1:1/Face Value Split ... From Rs 10/- ... To Rs 2/-"). None when a leg
    does not parse."""
    lowered = re.sub(r"\bsplt\b", "split", text.lower())
    lowered = re.sub(r"\bfrm\b", "from", lowered)
    cut = _SPLIT_START.search(lowered)
    if cut is None:  # a bonus, perhaps with a dividend or an AGM in the same purpose
        parsed = parse_action_text(lowered)
        return None if parsed is None else (Leg(parsed.action_type, parsed.factor),)
    # the split's numbers are the only ones from its first word on ("Dividend - Rs 1.50/- Per
    # Share / Face Value Split - From Rs 10/- ... To Rs 5/-")
    split = parse_action_text(lowered[cut.start() :])
    if split is None or split.action_type != TYPE_SPLIT:
        return None
    if "bonus" not in lowered[: cut.start()]:
        return (Leg(TYPE_SPLIT, split.factor),)
    bonus = parse_action_text(lowered[: cut.start()])
    if bonus is None or bonus.action_type != TYPE_BONUS:
        return None
    return (Leg(TYPE_BONUS, bonus.factor), Leg(TYPE_SPLIT, split.factor))


@dataclass(frozen=True)
class RightsTerms:
    """`new` shares for every `held`, issued at `price` (face value plus premium)."""

    new: Fraction
    held: Fraction
    price: Fraction


_RIGHTS_RATIO = re.compile(rf"\brights?\b(?:\s*issue)?[\s:@-]*{_NUMBER}\s*:\s*{_NUMBER}")
_PREMIUM = re.compile(rf"\b(?:premium|prem|prm)\b\s*(?:of\s*)?(?:(?:rs|re)\b\.?)?\s*{_NUMBER}")
_AT_PAR = re.compile(r"(?:\bat|@)\s*par\b")
_NON_SIMPLE = re.compile(r"warrant|ccps|ccd|convertible|debenture|partly|preference|\bncd")


def rights_terms(text: str, face_value: str) -> RightsTerms | str:
    """The terms of an NSE rights purpose ("Rights 1:10 @ Premium Rs 95/-") with the face value
    of the row, or the reason it cannot be priced: `non_simple` (partly paid shares, warrants,
    convertibles, debentures), `unparsed` or `no_issue_price`."""
    lowered = text.lower()
    if _NON_SIMPLE.search(lowered):
        return "non_simple"
    ratios = _RIGHTS_RATIO.findall(lowered)
    fv = exact(face_value.strip(), positive=True)
    if len(ratios) != 1 or fv is None:
        return "unparsed"
    new, held = Fraction(ratios[0][0]), Fraction(ratios[0][1])
    if new <= 0 or held <= 0:
        return "unparsed"
    premiums = _PREMIUM.findall(lowered)
    if len(premiums) == 1:
        premium = Fraction(premiums[0])
    elif not premiums and _AT_PAR.search(lowered):
        premium = Fraction(0)
    else:
        return "no_issue_price"
    return RightsTerms(new, held, Fraction(fv) + premium)


def rights_factor(terms: RightsTerms, cum_price: Fraction) -> Fraction:
    """TERP over the cum price: (N*P + M*S) / ((N+M)*P) for M new per N held at S; 1 when the
    issue price is at or above the cum price (no value passes to the holder)."""
    if cum_price <= 0:
        raise ValueError("cum_price")
    if terms.price >= cum_price:
        return Fraction(1)
    return (terms.held * cum_price + terms.new * terms.price) / (
        (terms.held + terms.new) * cum_price
    )


def demerger_factor(
    ratio: tuple[Fraction, Fraction], child_close: Fraction, parent_close: Fraction
) -> Fraction | None:
    """1 - r*C/P for r = a/b resulting shares per parent share: the share of the parent's value
    that stays with it. None outside (0, 1)."""
    if child_close <= 0 or parent_close <= 0:
        return None
    factor = 1 - ratio[0] / ratio[1] * child_close / parent_close
    return factor if 0 < factor < 1 else None


@dataclass(frozen=True)
class Verdict:
    """The gate's answer: `reason` is "" when the bars confirm, else `off_factor` or
    `ambiguous`; `gap` is the ex-date open over the previous close."""

    reason: str
    gap: Fraction

    @property
    def confirmed(self) -> bool:
        return not self.reason


def gate(expected: Fraction, previous_close: Fraction, ex_open: Fraction) -> Verdict:
    """Do the bars confirm `expected`? The gap (ex open / previous close) must be within
    `GATE_TOL` of it, and the gap's distance from it (in log terms) at most half its distance
    from 1: otherwise a plain session move could explain the gap as well (`ambiguous`)."""
    if expected <= 0 or previous_close <= 0 or ex_open <= 0:
        raise ValueError("gate needs positive prices and factor")
    gap = ex_open / previous_close
    off = abs(math.log(gap / expected))
    move = abs(math.log(gap))
    if off > math.log1p(GATE_TOL):
        return Verdict("off_factor", gap)
    if 2 * off > move:
        return Verdict("ambiguous", gap)
    return Verdict("", gap)


_COMPOSITE = re.compile(r"composite|two resulting|multi|amalgamation|\blegs?\b|merger")


def name_key(name: str) -> str:
    """A company's first distinctive word, to match a resulting company to its parent."""
    words = re.findall(r"[A-Z0-9&]+", name.upper())
    skip = {"THE", "SHREE", "SHRI", "SRI", "NEW"}
    return next((w for w in words if w not in skip), "")


# --- the file -----------------------------------------------------------------------------------


def _cell(value: object) -> str:
    return ("" if value is None else str(value)).strip()[:TEXT_MAX]


def read_file(path: Path) -> dict[str, list[dict[str, str]]]:
    """Each table's rows as text (`rowid` too), the file opened read-only. Raises
    `CaFileError("ca_file_unreadable")` when a table or column is missing."""
    uri = path.resolve().as_uri() + "?mode=ro"
    out: dict[str, list[dict[str, str]]] = {}
    try:
        with closing(sqlite3.connect(uri, uri=True)) as con:
            for table, columns in COLUMNS.items():
                listed = ", ".join(f'"{c}"' for c in columns)  # constant names, never file text
                cursor = con.execute(f'SELECT rowid, {listed} FROM "{table}"')  # noqa: S608
                out[table] = [
                    {k: _cell(v) for k, v in zip(("rowid", *columns), row, strict=True)}
                    for row in cursor
                ]
    except sqlite3.Error as exc:
        raise CaFileError("ca_file_unreadable") from exc
    return out


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Proposal:
    """One file row, as far as the text alone decides it. `legs` is empty for a rights issue or a
    demerger, whose factor needs the bars; `status`/`reason` are set when the row ends here."""

    table: str
    rowid: str
    symbol: str
    isin: str
    ex_date: date | None
    text: str
    hint: str = ""
    legs: tuple[Leg, ...] = ()
    note: str = ""  # a demerger's curated ratio note: names the resulting company
    rights: RightsTerms | None = None
    demerger: tuple[Fraction, Fraction] | None = None
    sme: bool = False
    status: str = ""
    reason: str = ""

    def ends(self, status: str, reason: str) -> Proposal:
        return replace(self, status=status, reason=reason)


# "Bonus - 1 Debenture For 1 Equity Share Held": `non_equity_bonus` wants the word next to "bonus"
_BONUS_DEBENTURE = re.compile(r"bonus\b.*\b(?:debentures?|ncrps|preference)\b")
_TYPE_LEGS = {
    "split": (TYPE_SPLIT,),
    "bonus": (TYPE_BONUS,),
    "bonus+split": (TYPE_BONUS, TYPE_SPLIT),
}


def _split_bonus(row: Mapping[str, str]) -> Proposal:
    text = row["subject_raw"]
    hint = row["instrument_class"]
    p = Proposal(T_SPLITS, row["rowid"], row["symbol"], row["isin"], nse_date(row["exDate"]), text)
    p = replace(p, hint=f"class {hint}" if hint else "")
    if p.ex_date is None:
        return p.ends(REVIEW, "bad_ex_date")
    if non_equity_bonus(text) or _BONUS_DEBENTURE.search(text.lower()):
        return p.ends(SKIPPED, "non_equity_text")
    legs = split_bonus_legs(text)
    if legs is None:
        return p.ends(REVIEW, "unparsed")
    if tuple(leg.action_type for leg in legs) != _TYPE_LEGS.get(row["action_type"]):
        return p.ends(REVIEW, "type_mismatch")
    return replace(p, legs=legs)


def _rights(row: Mapping[str, str]) -> Proposal:
    text = row["subject_raw"]
    p = Proposal(T_RIGHTS, row["rowid"], row["symbol"], row["isin"], nse_date(row["exDate"]), text)
    p = replace(p, hint=f"{row['market']} FV {row['faceVal']}", sme="sme" in row["market"])
    if "invit" in row["market"].lower():
        return p.ends(SKIPPED, "invit_units")
    if p.ex_date is None:
        return p.ends(REVIEW, "bad_ex_date")
    terms = rights_terms(text, row["faceVal"])
    if isinstance(terms, str):
        return p.ends(REVIEW, terms)
    parsed = parse_ratio(row["entitlement_ratio_parsed"])
    premium = exact(row["premium_rs_parsed"]) if row["premium_rs_parsed"] else None
    face_value = exact(row["faceVal"], positive=True)
    if parsed != (terms.new, terms.held) or (
        premium is not None
        and (face_value is None or Fraction(premium) != terms.price - Fraction(face_value))
    ):
        return p.ends(REVIEW, "parse_disagrees")  # the file's own parse reads the text otherwise
    return replace(p, rights=terms)


def _demerger(row: Mapping[str, str]) -> Proposal:
    note = f"{row['ratio_note']} {row['evidence']}".strip()
    text = f"Demerger {row['ratio']}" + (
        f" ({row['ratio_note'][:200]})" if row["ratio_note"] else ""
    )
    p = Proposal(T_DEMERGERS, row["rowid"], row["symbol"], "", nse_date(row["exDate"]), text)
    p = replace(p, hint=note[:300], note=row["ratio_note"], sme=row["market"] == "sme")
    if row["status"] != "found":
        return p.ends(REVIEW, f"ratio_{row['status'] or 'missing'}")
    if p.ex_date is None:
        return p.ends(REVIEW, "bad_ex_date")
    ratio = parse_ratio(row["ratio"])
    if ratio is None:
        return p.ends(REVIEW, "unparsed")
    if _COMPOSITE.search(row["ratio_note"].lower()):
        return p.ends(REVIEW, "composite_scheme")
    return replace(p, demerger=ratio)


def _etf(row: Mapping[str, str]) -> Proposal:
    old = exact(row["old_face_value_rs"], positive=True)
    new = exact(row["new_face_value_rs"], positive=True)
    text = f"ETF unit split FV {row['old_face_value_rs']} to {row['new_face_value_rs']}"
    p = Proposal(T_ETF, row["rowid"], row["symbol"], "", nse_date(row["ex_effective_date"]), text)
    if p.ex_date is None:
        return p.ends(REVIEW, "bad_ex_date")
    if old is None or new is None or not new < old:
        return p.ends(REVIEW, "unparsed")
    return replace(p, legs=(Leg(TYPE_SPLIT, Fraction(new) / Fraction(old)),))


def proposals(tables: Mapping[str, Sequence[Mapping[str, str]]]) -> list[Proposal]:
    """What each row of the file proposes, as far as its text goes."""
    readers: dict[str, Callable[[Mapping[str, str]], Proposal]] = {
        T_SPLITS: _split_bonus,
        T_RIGHTS: _rights,
        T_DEMERGERS: _demerger,
        T_ETF: _etf,
    }
    return [readers[table](row) for table in COLUMNS for row in tables.get(table, ())]


# --- instruments and bars -----------------------------------------------------------------------


@dataclass(frozen=True)
class Instrument:
    id: str
    isin: str
    symbol: str
    name: str


@dataclass
class Reference:
    """HAT's instruments as the import matches them."""

    instruments: dict[str, Instrument]
    by_isin: dict[str, str]
    by_symbol: dict[str, set[str]]  # current symbols
    history: dict[str, list[tuple[str, date, date | None]]]  # symbol -> (id, from, to)
    chain: dict[str, set[str]]  # a linked instrument -> every member of its chain
    successors: set[str]  # the newer side of a linked pair: not a fresh listing

    def ids(self, symbol: str, isin: str, day: date) -> set[str]:
        """The instruments the row can mean: its ISIN, its symbol now or around `day`, and the
        linked chains of those."""
        found = set(self.by_symbol.get(symbol, ()))
        if isin in self.by_isin:
            found.add(self.by_isin[isin])
        slack = timedelta(days=BAR_DAYS)
        for iid, start, end in self.history.get(symbol, ()):
            if start <= day + slack and (end is None or end >= day - slack):
                found.add(iid)
        for iid in list(found):
            found |= self.chain.get(iid, set())
        return found


def load_reference(session: Session) -> Reference:
    instruments = {
        str(i.id): Instrument(str(i.id), i.isin, i.symbol, i.name)
        for i in session.scalars(select(HatInstrument))
    }
    by_symbol: dict[str, set[str]] = defaultdict(set)
    for i in instruments.values():
        by_symbol[i.symbol].add(i.id)
    history: dict[str, list[tuple[str, date, date | None]]] = defaultdict(list)
    for h in session.scalars(select(HatInstrumentSymbolHistory)):
        history[h.symbol].append((str(h.instrument_id), h.valid_from, h.valid_to))
    links = session.execute(
        select(HatIsinLineage.old_instrument_id, HatIsinLineage.new_instrument_id).where(
            HatIsinLineage.status == "linked"
        )
    ).all()
    members: dict[str, set[str]] = {}
    for old, new in links:
        group = members.get(str(old), {str(old)}) | members.get(str(new), {str(new)})
        for iid in group:
            members[iid] = group
    return Reference(
        instruments,
        {i.isin: i.id for i in instruments.values()},
        dict(by_symbol),
        dict(history),
        members,
        {str(new) for _, new in links},
    )


@dataclass(frozen=True)
class Bar:
    instrument_id: str
    day: date
    isin: str
    open: Decimal
    close: Decimal


# The latest ingest run's row of each instrument and day, for the instruments each proposal
# can mean, within BAR_DAYS of its ex-date (an equi-join on the instrument first, so DuckDB
# never compares every bar with every proposal).
_WINDOW_SQL = """
SELECT w.k, r.instrument_id, r.date, arg_max(r.isin, r.ingest_run_id),
       arg_max(r.open, r.ingest_run_id), arg_max(r.close, r.ingest_run_id)
FROM want w JOIN bars_daily_raw r ON r.instrument_id = w.instrument_id
WHERE r.date BETWEEN w.ex - ? AND w.ex + ?
GROUP BY w.k, r.instrument_id, r.date
"""
_FIRST_SQL = """
WITH span AS (SELECT instrument_id, min(date) AS first_day FROM bars_daily_raw GROUP BY 1)
SELECT r.instrument_id, r.date, arg_max(r.close, r.ingest_run_id)
FROM bars_daily_raw r JOIN span s ON r.instrument_id = s.instrument_id AND r.date = s.first_day
GROUP BY r.instrument_id, r.date
"""


def read_windows(
    store: DataStore, wanted: Mapping[int, tuple[date, set[str]]]
) -> dict[int, list[Bar]]:
    """The bars around each wanted (ex-date, instruments), by key."""
    out: dict[int, list[Bar]] = defaultdict(list)
    rows = [(k, iid, ex) for k, (ex, ids) in wanted.items() for iid in sorted(ids)]
    if not rows:
        return out
    with closing(store.connect()) as con:
        con.execute("CREATE TABLE want (k INTEGER, instrument_id VARCHAR, ex DATE)")
        con.executemany("INSERT INTO want VALUES (?, ?, ?)", rows)
        for k, iid, day, isin, open_, close in con.execute(
            _WINDOW_SQL, [BAR_DAYS, BAR_DAYS]
        ).fetchall():
            out[k].append(Bar(str(iid), day, isin, open_, close))
    return out


def read_first_closes(store: DataStore) -> dict[str, tuple[date, Decimal]]:
    """Each instrument's first session and close in the bars."""
    with closing(store.connect()) as con:
        return {str(i): (d, c) for i, d, c in con.execute(_FIRST_SQL).fetchall()}


def around(bars: Iterable[Bar], ex_date: date, isin: str) -> tuple[Bar, Bar] | str:
    """The last bar before the ex-date and the first on or after it, or why there is no pair:
    `no_bars`, or `ambiguous_bars` when two instruments trade on that day and the row's ISIN
    names neither. A suspension may put the pair a few sessions apart (within `BAR_DAYS`)."""
    before = [b for b in bars if b.day < ex_date]
    after = [b for b in bars if b.day >= ex_date]
    if not before or not after:
        return "no_bars"
    picked = []
    for side, day in ((before, max(b.day for b in before)), (after, min(b.day for b in after))):
        found = [b for b in side if b.day == day]
        if len(found) > 1:
            found = [b for b in found if b.isin == isin]
        if len(found) != 1:
            return "ambiguous_bars"
        picked.append(found[0])
    return picked[0], picked[1]


# --- deciding -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stored:
    instrument_id: str
    ex_date: date
    action_type: str
    factor: Decimal


@dataclass
class Outcome:
    proposal: Proposal
    status: str
    reason: str = ""
    detail: str = ""
    records: list[ActionRecord] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def _f(value: Fraction | Decimal) -> str:
    return f"{float(value):.4f}"


def same_factor(stored: Decimal, factor: Fraction) -> bool:
    """A stored factor is the same as `factor` up to the rounding of its source (the MCP's are
    rounded to 4 places: 0.6667 is a 1:2 bonus)."""
    return abs(Fraction(stored) - factor) <= SAME_FACTOR_TOL


def plan_legs(
    legs: Sequence[Leg], near: Sequence[Stored]
) -> tuple[list[Leg], list[Leg], list[tuple[Leg, Stored]]]:
    """Each leg against the price-changing actions stored near its ex-date for the same company:
    to store (none of its type), already stored (the same factor), or a conflict (another)."""
    new, already, conflicts = [], [], []
    for leg in legs:
        same = [s for s in near if s.action_type == leg.action_type]
        if not same:
            new.append(leg)
        elif any(same_factor(s.factor, leg.factor) for s in same):
            already.append(leg)
        else:
            conflicts.append((leg, same[0]))
    return new, already, conflicts


@dataclass
class Context:
    reference: Reference
    stored: dict[str, list[Stored]]  # instrument -> its price-changing actions
    first_closes: dict[str, tuple[date, Decimal]]


def _near(ctx: Context, ids: Iterable[str], ex_date: date) -> list[Stored]:
    seen: dict[tuple[date, str, Decimal], Stored] = {}
    for iid in ids:
        for s in ctx.stored.get(iid, ()):
            if abs((s.ex_date - ex_date).days) <= NEAR_DAYS:
                seen.setdefault((s.ex_date, s.action_type, s.factor), s)  # chain copies once
    return sorted(seen.values(), key=lambda s: (s.ex_date, s.action_type, s.instrument_id))


def equity_share_isin(isin: str) -> bool:
    """An Indian company's equity share ISIN (INE, issuer, security type 01): not an ETF unit
    (INF), a rights entitlement (type 20) or a debt security."""
    return len(isin) == 12 and isin.startswith("INE") and isin[7:9] == "01"


_NOTE_SKIP = {"SHARE", "SHARES", "RESULTING", "COMPANY", "LIMITED", "EQUITY", "DEMERGER"}


def resulting_company(
    ctx: Context, parent: Instrument, ex_date: date, note: str
) -> list[Instrument]:
    """The fresh equity listings (not a newer ISIN of a linked chain) in the year after a
    demerger whose name begins like the parent's or whose first word the file's curated note
    names ("1 Trejhara Solutions share per 2 Aurionpro shares"): the resulting company, when
    there is exactly one. The raw filing quote is not read: its generic words match ETFs."""
    words = set(re.findall(r"[A-Z0-9&]+", note.upper())) - _NOTE_SKIP
    parent_key = name_key(parent.name)
    found = []
    for iid, (first, _) in ctx.first_closes.items():
        if not ex_date <= first <= ex_date + timedelta(days=CHILD_DAYS):
            continue
        if (
            iid == parent.id
            or iid in ctx.reference.successors
            or iid not in ctx.reference.instruments
            or not equity_share_isin(ctx.reference.instruments[iid].isin)
        ):
            continue
        child = ctx.reference.instruments[iid]
        key = name_key(child.name)
        if key and (key == parent_key or (len(key) >= 4 and key in words)):
            found.append(child)
    if len(found) > 1:  # the evidence names one of them
        named = [c for c in found if len(name_key(c.name)) >= 4 and name_key(c.name) in words]
        found = named if len(named) == 1 else found
    return found


def decide(p: Proposal, ctx: Context, bars: Sequence[Bar]) -> Outcome:
    """The outcome of one proposal: its factor from the bars where it needs them, the gate, and
    the plan against what is stored."""
    if p.status or p.ex_date is None:
        return Outcome(p, p.status or REVIEW, p.reason or "bad_ex_date")
    ids = ctx.reference.ids(p.symbol, p.isin, p.ex_date)
    if not ids:
        return Outcome(p, SKIPPED if p.sme else REVIEW, "no_instrument")
    pair = around(bars, p.ex_date, p.isin)
    if isinstance(pair, str):
        return Outcome(p, SKIPPED if p.sme else REVIEW, f"sme_{pair}" if p.sme else pair)
    before, on = pair
    prev_close, ex_open = Fraction(before.close), Fraction(on.open)
    detail = f"prev {before.day} close {before.close} -> {on.day} open {on.open}"
    if prev_close <= 0 or ex_open <= 0:
        return Outcome(p, REVIEW, "no_bars", detail)
    evidence: dict[str, Any] = {
        "previous_day": before.day.isoformat(),
        "previous_close": str(before.close),
        "ex_day": on.day.isoformat(),
        "ex_open": str(on.open),
    }
    near = _near(ctx, ids | {before.instrument_id, on.instrument_id}, p.ex_date)
    legs = list(p.legs)
    if p.rights is not None:
        factor = rights_factor(p.rights, prev_close)
        detail += f", TERP factor {_f(factor)}"
        if factor == 1:
            return Outcome(p, SKIPPED, "rights_at_or_above_price", detail)
        legs = [Leg(TYPE_OTHER, factor)]
    kinds = {leg.action_type for leg in legs} | ({TYPE_OTHER} if p.demerger else set())
    # a stored factor of another type ex between the two sessions moves the open too
    same_day = math.prod(
        (
            Fraction(s.factor)
            for s in near
            if before.day < s.ex_date <= on.day and s.action_type not in kinds
        ),
        start=Fraction(1),
    )
    if p.demerger is not None and same_day != 1:
        # the ratio's basis (before or after the other action) is not knowable from the file
        return Outcome(p, REVIEW, "same_day_action", detail)
    if p.demerger is not None:
        leg, reason, how = _demerger_leg(
            p, ctx, before, prev_close, ex_open / prev_close / same_day
        )
        detail += how
        if leg is None:
            return Outcome(p, REVIEW, reason, detail)
        legs = [leg]
        evidence["derived"] = how.lstrip(", ")
    expected = math.prod((leg.factor for leg in legs), start=Fraction(1)) * same_day
    verdict = gate(expected, prev_close, ex_open)
    detail += f", gap {_f(verdict.gap)} vs factor {_f(expected)}"
    evidence["gap"] = _f(verdict.gap)
    new, already, conflicts = plan_legs(legs, near)
    if conflicts:
        text = "; ".join(
            f"{leg.action_type} file {_f(leg.factor)} vs stored {s.factor} on {s.ex_date}"
            for leg, s in conflicts
        )
        bars_say = "the bars confirm the file" if verdict.confirmed else f"file {verdict.reason}"
        return Outcome(p, CONFLICT, "stored_factor_differs", f"{detail}; {text}; {bars_say}")
    if not new:
        return Outcome(p, ALREADY, "", detail)
    if not verdict.confirmed:
        return Outcome(p, REVIEW, verdict.reason, detail)
    if any(not FACTOR_MIN <= factor_to_decimal(leg.factor) < 1 for leg in new):
        return Outcome(p, REVIEW, "factor_out_of_range", detail)
    records = [
        ActionRecord(
            uuid.UUID(before.instrument_id),
            p.ex_date,
            leg.action_type,
            factor_to_decimal(leg.factor),
            p.text,
            SOURCE_FILE,
        )
        for leg in new
    ]
    reason = "other legs stored" if already else ""
    return Outcome(p, STORED, reason, detail, records, evidence)


def _demerger_leg(
    p: Proposal, ctx: Context, before: Bar, prev_close: Fraction, gap: Fraction
) -> tuple[Leg | None, str, str]:
    """(leg, review reason, detail): the parent's factor from the resulting company's first
    close, else from the gap when it is a clear drop."""
    if p.demerger is None or p.ex_date is None:
        return None, "unparsed", ""
    parent = ctx.reference.instruments.get(before.instrument_id)
    if parent is None:
        return None, "no_instrument", ""
    children = resulting_company(ctx, parent, p.ex_date, p.note)
    if len(children) > 1:
        names = ", ".join(sorted(c.symbol for c in children))
        return None, "several_resulting_candidates", f", candidates {names}"
    if children:
        child = children[0]
        first, close = ctx.first_closes[child.id]
        how = f", resulting {child.symbol} {child.isin} first close {close} on {first}"
        factor = demerger_factor(p.demerger, Fraction(close), prev_close)
        if factor is None:
            return None, "resulting_price_too_high", how
        return Leg(TYPE_OTHER, factor), "", f"{how} -> factor {_f(factor)}"
    if gap <= 1 - DEMERGER_MIN_DROP:
        return (
            Leg(TYPE_OTHER, gap),
            "",
            ", no resulting listing found: the ex-date gap is the factor",
        )
    return None, "gap_too_small_no_resulting", f", gap {_f(gap)}, no resulting listing found"


def _dedupe(outcomes: list[Outcome]) -> None:
    """Two rows of the file (NSE keeps revisions as separate rows) that store the same key: the
    same factor is stored once; different factors are both reviewed."""
    by_key: dict[tuple, list[Outcome]] = defaultdict(list)
    for o in outcomes:
        for r in o.records:
            by_key[r.key].append(o)
    for group in by_key.values():
        if len(group) < 2:
            continue
        factors = {r.factor for o in group for r in o.records}
        rest = group[1:] if len(factors) == 1 else group
        for o in rest:
            o.status, o.reason, o.records = (
                (ALREADY, "duplicate_in_file", [])
                if len(factors) == 1
                else (REVIEW, "file_disagrees_with_itself", [])
            )


def stored_price_actions(session: Session) -> dict[str, list[Stored]]:
    out: dict[str, list[Stored]] = defaultdict(list)
    for row in session.scalars(
        select(HatCorporateAction).where(HatCorporateAction.action_type.not_in(PRICE_NEUTRAL_TYPES))
    ):
        iid = str(row.instrument_id)
        out[iid].append(Stored(iid, row.ex_date, row.action_type, Decimal(row.adjustment_factor)))
    return out


def evaluate(
    session: Session, store: DataStore, tables: Mapping[str, Sequence[Mapping[str, str]]]
) -> list[Outcome]:
    """Every row's outcome; nothing is written."""
    props = proposals(tables)
    reference = load_reference(session)
    needs_children = any(p.demerger is not None and not p.status for p in props)
    ctx = Context(
        reference,
        stored_price_actions(session),
        read_first_closes(store) if needs_children else {},
    )
    wanted = {
        k: (p.ex_date, reference.ids(p.symbol, p.isin, p.ex_date))
        for k, p in enumerate(props)
        if not p.status and p.ex_date is not None
    }
    windows = read_windows(store, wanted)
    outcomes = [decide(p, ctx, windows.get(k, ())) for k, p in enumerate(props)]
    _dedupe(outcomes)
    held = owner_vacated(session, {r.instrument_id for o in outcomes for r in o.records})
    for o in outcomes:
        if any(r.key in held for r in o.records):
            o.status, o.reason, o.records = SKIPPED, "owner_vacated", []
    return outcomes


def apply(session: Session, outcomes: Sequence[Outcome], *, digest: str, now: datetime) -> int:
    """Store every confirmed record (`load_actions`) and journal each new row as
    `corporate_action.imported` in the same transaction; returns the rows created. Raises when
    the database disagrees with the plan (a row appeared meanwhile): nothing is then kept."""
    run_id = str(uuid.uuid4())
    records = [r for o in outcomes for r in o.records]
    report = load_actions(session, records)
    if report.created != len(records) or report.conflicts or report.held:
        raise CaFileError("ca_import_raced")
    for o in outcomes:
        for r in o.records:
            events.record(
                session,
                aggregate_type="instrument",
                aggregate_id=r.instrument_id,  # type: ignore[arg-type]
                event_type=EVENT_IMPORTED,
                actor="engine",
                payload={
                    "source": SOURCE_FILE,
                    "run_id": run_id,
                    "file_sha256": digest,
                    "table": o.proposal.table,
                    "row": o.proposal.rowid,
                    "symbol": o.proposal.symbol,
                    "isin": o.proposal.isin,
                    "ex_date": r.ex_date.isoformat(),
                    "type": r.action_type,
                    "factor": str(r.factor),
                    "text": r.raw_text[:500],
                    "check": o.evidence,
                },
                occurred_at=now,
            )
    session.flush()
    return report.created


def report_lines(outcomes: Sequence[Outcome], *, applied: bool, everything: bool) -> list[str]:
    """Counts per table and status, then the stored, conflict and review rows with their
    reasons (every row when `everything`)."""
    counts: Counter[tuple[str, str]] = Counter((o.proposal.table, o.status) for o in outcomes)
    rows = Counter(r.action_type for o in outcomes for r in o.records)
    label = {STORED: "stored" if applied else "to_store"}
    lines = [
        "table               " + " ".join(f"{label.get(s, s):>9}" for s in STATUSES) + "    total",
    ]
    for table in COLUMNS:
        cells = [counts[(table, s)] for s in STATUSES]
        lines.append(f"{table:<20}" + " ".join(f"{c:>9}" for c in cells) + f" {sum(cells):>8}")
    lines.append(
        "rows "
        + ("written" if applied else "to write")
        + ": "
        + ", ".join(f"{k} {v}" for k, v in sorted(rows.items()))
    )
    reasons = Counter((o.status, o.reason) for o in outcomes if o.status in (REVIEW, SKIPPED))
    lines.append("reasons: " + ", ".join(f"{s}/{r} {n}" for (s, r), n in sorted(reasons.items())))
    shown = STATUSES if everything else (STORED, CONFLICT, REVIEW)
    for status in shown:
        for o in outcomes:
            if o.status != status:
                continue
            p = o.proposal
            factors = " ".join(f"{r.action_type}={r.factor.normalize()}" for r in o.records)
            lines.append(
                "\t".join(
                    [
                        label.get(o.status, o.status).upper(),
                        o.reason,
                        p.table,
                        p.symbol,
                        p.isin,
                        p.ex_date.isoformat() if p.ex_date else "-",
                        factors,
                        p.text[:120],
                        o.detail,
                        p.hint[:160],
                    ]
                )
            )
    return lines


# --- command line -------------------------------------------------------------------------------


def input_file(name: str) -> Path:
    """The file named on the command line, under the working directory with a `.db` suffix:
    anything resolving outside it, or with another suffix, is refused before it is opened."""
    base = Path.cwd().resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or target.suffix.lower() != ".db":
        raise CaFileError("ca_file_outside_cwd")
    if not target.is_file():
        raise CaFileError("ca_file_missing")
    return target


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.ca_import --db FILE [--apply] [--all]

    Reads the corporate-action file FILE (a `.db` under the working directory, opened
    read-only) and the raw bars under `HAT_DATA_ROOT`, and prints what it would store (a dry
    run) or, with `--apply`, stores it in one transaction. The report goes to stdout (redirect
    it to a file outside the repo); `--all` lists every row, not only the stored, conflict and
    review ones. After an apply, rebuild the lineage (`python -m app.hat.data.lineage build`)
    and the features (`features --full`)."""
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.ca_import")
    parser.add_argument("--db", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    try:
        path = input_file(args.db)
        tables = read_file(path)
    except CaFileError as exc:
        sys.stderr.write(f"refused: {exc.code}\n")
        return 2
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import SessionLocal

    from ..time import utc_now

    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    with SessionLocal() as session:
        outcomes = evaluate(session, store, tables)
        if args.apply:
            created = apply(session, outcomes, digest=file_digest(path), now=utc_now())
            session.commit()
            sys.stdout.write(f"applied: {created} rows\n")
        else:
            session.rollback()
    sys.stdout.write("\n".join(report_lines(outcomes, applied=args.apply, everything=args.all)))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
