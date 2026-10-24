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
  into the parent's lineage (owner rule); the stored row is flagged a demerger
  (`hat_corporate_action.demerger`), so the lineage build reviews any ISIN change near it.
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

Each row records when its factor became knowable (CA5, `ca_known`): a split, bonus, ETF split or
rights factor at the ex-date open, a demerger priced from its resulting company at that
company's first close, and a gap-only demerger at the close `SETTLE_SESSIONS` on.
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
from ..models import HatCorporateAction
from ..money import exact
from .ca_gate import (
    ALREADY,
    CONFLICT,
    EVENT_IMPORTED,
    REVIEW,
    SKIPPED,
    STATUSES,
    STORED,
    TEXT_MAX,
    Bar,
    CaFileError,
    Instrument,
    Reference,
    Verdict,
    around,
    fmt4,
    gate,
    load_reference,
    nse_date,
    read_first_closes,
    read_windows,
    same_factor,
)
from .ca_known import RESULTING_FIRST_CLOSE, UNKNOWN, Known, at_ex_open, settle_known
from .corporate_actions import (
    PRICE_NEUTRAL_TYPES,
    TYPE_BONUS,
    TYPE_OTHER,
    TYPE_SPLIT,
    ActionRecord,
    factor_to_decimal,
    load_actions,
    lock_reference_data,
    non_equity_bonus,
    owner_vacated,
    parse_action_text,
)
from .store import FACTOR_MIN, DataStore

SOURCE_FILE = "nse_ca_file"
NEAR_DAYS = 5  # a stored action this close to the ex-date is the same event (sources differ)
CHILD_DAYS = 366  # a resulting company lists within a year of the ex-date (a leap year too)
DEMERGER_MIN_DROP = Fraction(1, 10)  # a gap-only demerger factor needs a drop beyond 10%
KIND_DEMERGER = "demerger"  # a stored `other` row that is a demerger (`Stored.kind`)
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


# --- parsing (pure) -----------------------------------------------------------------------------

_NUMBER = r"(\d+(?:\.\d+)?)"
_RATIO = re.compile(rf"^\s*{_NUMBER}\s*:\s*{_NUMBER}\s*$")


def parse_ratio(raw: str) -> tuple[Fraction, Fraction] | None:
    """`a:b` (decimals allowed: `1:48.18`) as two positive numbers, else None."""
    found = _RATIO.match(raw)
    if found is None:
        return None
    a, b = Fraction(found.group(1)), Fraction(found.group(2))
    return (a, b) if a > 0 and b > 0 else None


@dataclass(frozen=True)
class Leg:
    """One factor a row proposes, of a stored type (an `other` that is a demerger flagged)."""

    action_type: str
    factor: Fraction
    demerger: bool = False


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
# an amount with digit groups ("Rs 1,000", "Rs 1,00,000") or without
_AMOUNT = r"(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_PREMIUM = re.compile(rf"\b(?:premium|prem|prm)\b\s*(?:of\s*)?(?:(?:rs|re)\b\.?)?\s*{_AMOUNT}")
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
        premium = Fraction(premiums[0].replace(",", ""))
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
        # the bonus is price-neutral (D1); a split named in the same purpose still counts
        cut = _SPLIT_START.search(text.lower())
        split = split_bonus_legs(text[cut.start() :]) if cut is not None else None
        if split is None or "split" not in row["action_type"]:
            return p.ends(SKIPPED, "non_equity_text")
        return replace(p, legs=split, hint=f"{p.hint}; non-equity bonus leg not a factor")
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


# --- deciding -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stored:
    instrument_id: str
    ex_date: date
    action_type: str
    factor: Decimal
    demerger: bool = False

    @property
    def kind(self) -> str:
        """The type, with a demerger told apart from the other `other` rows (rights)."""
        return KIND_DEMERGER if self.demerger else self.action_type


@dataclass
class Outcome:
    proposal: Proposal
    status: str
    reason: str = ""
    detail: str = ""
    records: list[ActionRecord] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


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


def near_actions(ctx: Context, ids: Iterable[str], ex_date: date) -> list[Stored]:
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


def decide(
    p: Proposal,
    ctx: Context,
    bars: Sequence[Bar],
    *,
    source: str = SOURCE_FILE,
    accept: frozenset[str] = frozenset(),
    known: Known | None = None,
) -> Outcome:
    """The outcome of one proposal: its factor from the bars where it needs them, the gate, and
    the plan against what is stored (`settle`: `source` and `accept`). A split, bonus or rights
    factor is known at the ex-date open unless `known` says otherwise (research terms); a
    demerger's is `_demerger_leg`'s (CA5)."""
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
    near = near_actions(ctx, ids | {before.instrument_id, on.instrument_id}, p.ex_date)
    legs = list(p.legs)
    if p.rights is not None:
        factor = rights_factor(p.rights, prev_close)
        detail += f", TERP factor {fmt4(factor)}"
        if factor == 1:
            return Outcome(p, SKIPPED, "rights_at_or_above_price", detail)
        legs = [Leg(TYPE_OTHER, factor)]
    kinds = {KIND_DEMERGER} if p.demerger else {leg.action_type for leg in legs}
    # a stored factor of another kind ex between the two sessions moves the open too (a rights
    # factor is not the demerger's, nor a demerger the rights issue's)
    same_day = math.prod(
        (
            Fraction(s.factor)
            for s in near
            if before.day < s.ex_date <= on.day and s.kind not in kinds
        ),
        start=Fraction(1),
    )
    if p.demerger is not None and same_day != 1:
        # the ratio's basis (before or after the other action) is not knowable from the file
        return Outcome(p, REVIEW, "same_day_action", detail)
    known = known or at_ex_open(p.ex_date)
    if p.demerger is not None:
        days = sorted(b.day for b in bars if b.instrument_id == on.instrument_id)
        leg, reason, how, known = _demerger_leg(
            p, ctx, before, prev_close, (ex_open / prev_close / same_day, days)
        )
        detail += how
        if leg is None:
            return Outcome(p, REVIEW, reason, detail)
        legs = [leg]
        evidence["derived"] = how.lstrip(", ")
    expected = math.prod((leg.factor for leg in legs), start=Fraction(1)) * same_day
    verdict = gate(expected, prev_close, ex_open)
    detail += f", gap {fmt4(verdict.gap)} vs factor {fmt4(expected)}"
    evidence["gap"] = fmt4(verdict.gap)
    near = [s for s in near if s.kind in kinds]
    return settle(
        p, before, legs, near, verdict, detail, evidence, source=source, accept=accept, known=known
    )


def settle(
    p: Proposal,
    before: Bar,
    legs: Sequence[Leg],
    near: Sequence[Stored],
    verdict: Verdict,
    detail: str,
    evidence: dict[str, Any],
    *,
    source: str = SOURCE_FILE,
    accept: frozenset[str] = frozenset(),
    known: Known = UNKNOWN,
) -> Outcome:
    """The plan of `legs` against the same-kind actions stored `near` the ex-date, given the
    gate's verdict: store, already, conflict or review. A verdict whose reason is in `accept`
    still stores (the research import's rights, `price_unconfirmed`); the row goes on the
    instrument of the bar `before` the ex-date, known on `known` (CA5)."""
    new, already, conflicts = plan_legs(legs, near)
    if conflicts:
        text = "; ".join(
            f"{leg.action_type} file {fmt4(leg.factor)} vs stored {s.factor} on {s.ex_date}"
            for leg, s in conflicts
        )
        bars_say = "the bars confirm the file" if verdict.confirmed else f"file {verdict.reason}"
        return Outcome(p, CONFLICT, "stored_factor_differs", f"{detail}; {text}; {bars_say}")
    if not new:
        return Outcome(p, ALREADY, "", detail)
    if not verdict.confirmed and verdict.reason not in accept:
        return Outcome(p, REVIEW, verdict.reason, detail)
    if p.ex_date is None or any(not FACTOR_MIN <= factor_to_decimal(leg.factor) < 1 for leg in new):
        return Outcome(p, REVIEW, "factor_out_of_range", detail)
    records = [
        ActionRecord(
            uuid.UUID(before.instrument_id),
            p.ex_date,
            leg.action_type,
            factor_to_decimal(leg.factor),
            p.text,
            source,
            known=known,
            demerger=leg.demerger,
        )
        for leg in new
    ]
    reason = "other legs stored" if already else verdict.reason
    return Outcome(p, STORED, reason, detail, records, evidence)


def _demerger_leg(
    p: Proposal,
    ctx: Context,
    before: Bar,
    prev_close: Fraction,
    market: tuple[Fraction, Sequence[date]],
) -> tuple[Leg | None, str, str, Known]:
    """(leg, review reason, detail, known): the parent's factor from the resulting company's
    first close (known at that close), else from the gap when it is a clear drop (`market`: the
    gap and the ex instrument's sessions; known at the close `SETTLE_SESSIONS` on, as a market
    gap with no cross-check)."""
    if p.demerger is None or p.ex_date is None:
        return None, "unparsed", "", UNKNOWN
    parent = ctx.reference.instruments.get(before.instrument_id)
    if parent is None:
        return None, "no_instrument", "", UNKNOWN
    children = resulting_company(ctx, parent, p.ex_date, p.note)
    if len(children) > 1:
        names = ", ".join(sorted(c.symbol for c in children))
        return None, "several_resulting_candidates", f", candidates {names}", UNKNOWN
    if children:
        child = children[0]
        first, close = ctx.first_closes[child.id]
        how = f", resulting {child.symbol} {child.isin} first close {close} on {first}"
        factor = demerger_factor(p.demerger, Fraction(close), prev_close)
        if factor is None:
            return None, "resulting_price_too_high", how, UNKNOWN
        known = Known(max(first, p.ex_date), RESULTING_FIRST_CLOSE)
        return Leg(TYPE_OTHER, factor, True), "", f"{how} -> factor {fmt4(factor)}", known
    gap, days = market
    if gap <= 1 - DEMERGER_MIN_DROP:
        return (
            Leg(TYPE_OTHER, gap, True),
            "",
            ", no resulting listing found: the ex-date gap is the factor",
            settle_known(days, p.ex_date),
        )
    how = f", gap {fmt4(gap)}, no resulting listing found"
    return None, "gap_too_small_no_resulting", how, UNKNOWN


def dedupe(outcomes: list[Outcome]) -> None:
    """Two rows of the file (NSE keeps revisions as separate rows) that store the same key: the
    same factor is stored once, by the first row (a later row keeps its other legs); different
    factors for the key put every row naming it in review, since the file contradicts itself."""
    by_key: dict[tuple, list[tuple[Outcome, ActionRecord]]] = defaultdict(list)
    for o in outcomes:
        for r in o.records:
            by_key[r.key].append((o, r))
    disagree: set[int] = set()
    for group in by_key.values():
        if len({r.factor for _, r in group}) > 1:
            disagree.update(id(o) for o, _ in group)
        else:
            for o, r in group[1:]:
                o.records = [kept for kept in o.records if kept is not r]
    for o in outcomes:
        if id(o) in disagree:
            o.status, o.reason, o.records = REVIEW, "file_disagrees_with_itself", []
        elif o.status == STORED and not o.records:
            o.status, o.reason = ALREADY, "duplicate_in_file"


def stored_price_actions(session: Session) -> dict[str, list[Stored]]:
    out: dict[str, list[Stored]] = defaultdict(list)
    for row in session.scalars(
        select(HatCorporateAction).where(HatCorporateAction.action_type.not_in(PRICE_NEUTRAL_TYPES))
    ):
        iid = str(row.instrument_id)
        out[iid].append(
            Stored(iid, row.ex_date, row.action_type, Decimal(row.adjustment_factor), row.demerger)
        )
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
    dedupe(outcomes)
    held = owner_vacated(session, {r.instrument_id for o in outcomes for r in o.records})
    for o in outcomes:
        if any(r.key in held for r in o.records):
            o.status, o.reason, o.records = SKIPPED, "owner_vacated", []
    return outcomes


def apply(
    session: Session, outcomes: Sequence[Outcome], *, journal: Mapping[str, Any], now: datetime
) -> int:
    """Store every confirmed record (`load_actions`) and journal each new row as
    `corporate_action.imported` in the same transaction, `journal` (the source and the input
    files' digests) in every payload; returns the rows created. Raises when the database
    disagrees with the plan (a row appeared meanwhile): nothing is then kept."""
    lock_reference_data(session)
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
                    **journal,
                    "run_id": run_id,
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


def report_lines(
    outcomes: Sequence[Outcome],
    *,
    applied: bool,
    everything: bool,
    tables: Sequence[str] = tuple(COLUMNS),
) -> list[str]:
    """Counts per table and status, then the stored, conflict and review rows with their
    reasons (every row when `everything`)."""
    counts: Counter[tuple[str, str]] = Counter((o.proposal.table, o.status) for o in outcomes)
    rows = Counter(r.action_type for o in outcomes for r in o.records)
    label = {STORED: "stored" if applied else "to_store"}
    lines = [
        "table               " + " ".join(f"{label.get(s, s):>9}" for s in STATUSES) + "    total",
    ]
    for table in tables:
        cells = [counts[(table, s)] for s in STATUSES]
        lines.append(f"{table:<20}" + " ".join(f"{c:>9}" for c in cells) + f" {sum(cells):>8}")
    lines.append(
        "rows "
        + ("written" if applied else "to write")
        + ": "
        + ", ".join(f"{k} {v}" for k, v in sorted(rows.items()))
    )
    for table in tables:
        reasons = Counter(
            (o.status, o.reason)
            for o in outcomes
            if o.proposal.table == table and o.status in (REVIEW, SKIPPED)
        )
        lines.append(
            f"reasons {table}: "
            + ", ".join(f"{s}/{r} {n}" for (s, r), n in sorted(reasons.items()))
        )
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
    from app.database import HatSessionLocal

    from ..time import utc_now

    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    with HatSessionLocal() as session:
        outcomes = evaluate(session, store, tables)
        if args.apply:
            journal = {"source": SOURCE_FILE, "file_sha256": file_digest(path)}
            created = apply(session, outcomes, journal=journal, now=utc_now())
            session.commit()
            sys.stdout.write(f"applied: {created} rows\n")
        else:
            session.rollback()
    sys.stdout.write("\n".join(report_lines(outcomes, applied=args.apply, everything=args.all)))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
