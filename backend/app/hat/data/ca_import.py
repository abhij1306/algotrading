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
(`split`, `bonus`, or `other` for rights and demergers), source `nse_ca_file`, by the shared
apply step (`ca_apply`). Repeating an import stores nothing.

Each row records when its factor became knowable (CA5, `ca_known`): a split, bonus, ETF split or
rights factor at the ex-date open, a demerger priced from its resulting company at that
company's first close, and a gap-only demerger at the close `SETTLE_SESSIONS` on.
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..money import exact
from ..time import IST
from .ca_apply import (
    KIND_DEMERGER,
    Context,
    Leg,
    Outcome,
    Proposal,
    RightsTerms,
    apply,
    dedupe,
    digest,
    drop_vacated,
    input_path,
    near_actions,
    report_lines,
    settle,
    stored_price_actions,
)
from .ca_gate import (
    REVIEW,
    SKIPPED,
    TEXT_MAX,
    Bar,
    CaFileError,
    Instrument,
    around,
    fmt4,
    gate,
    load_reference,
    nse_date,
    read_first_closes,
    read_windows,
)
from .ca_known import RESULTING_FIRST_CLOSE, UNKNOWN, Known, at_ex_open, settle_known
from .corporate_actions import (
    TYPE_BONUS,
    TYPE_OTHER,
    TYPE_SPLIT,
    non_equity_bonus,
    parse_action_text,
)
from .isin import voting_equity
from .ratios import split_pair_numbers
from .store import DataStore

SOURCE_FILE = "nse_ca_file"
CHILD_DAYS = 366  # a resulting company lists within a year of the ex-date (a leap year too)
DEMERGER_MIN_DROP = Fraction(1, 10)  # a gap-only demerger factor needs a drop beyond 10%
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


_RIGHTS_RATIO = re.compile(rf"\brights?\b(?:\s*issue)?[\s:@-]*{_NUMBER}\s*:\s*{_NUMBER}")
# an amount with digit groups ("Rs 1,000", "Rs 1,00,000") or without
_AMOUNT = r"(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_PREMIUM = re.compile(rf"\b(?:premium|prem|prm)\b\s*(?:of\s*)?(?:(?:rs|re)\b\.?)?\s*{_AMOUNT}")
_AT_PAR = re.compile(r"(?:\bat|@)\s*par\b")
_NON_SIMPLE = re.compile(r"warrant|ccps|ccd|convertible|debenture|partly|preference|\bncd")


FaceValueChange = tuple[date, Fraction]  # a face-value change's ex-date and new/old


def face_value_changes(text: str) -> Fraction | None:
    """The new/old face value of a split or consolidation purpose ("Face Value Split From Rs 10/-
    To Rs 2/-", "Consolidation From Rs 1/- To Rs 10/-"), from the words that name it on; None
    when the text names no face-value change or its two numbers do not read."""
    lowered = re.sub(r"\bfrm\b", "from", text.lower())
    cut = _SPLIT_START.search(lowered) or re.search(r"consolidat", lowered)
    pair = None if cut is None else split_pair_numbers(lowered[cut.start() :])
    return None if pair is None else pair[1] / pair[0]


def face_value_on(face_value: Fraction, day: date, changes: Sequence[FaceValueChange]) -> Fraction:
    """The face value on `day` from today's (the file's `faceVal`) and the company's face-value
    changes: today's divided by the new/old of each change after `day`."""
    for ex_date, ratio in changes:
        if ex_date > day:
            face_value /= ratio
    return face_value


def rights_terms(text: str, face_value: str | Fraction) -> RightsTerms | str:
    """The terms of an NSE rights purpose ("Rights 1:10 @ Premium Rs 95/-") with the face value
    on its ex-date, or the reason it cannot be priced: `non_simple` (partly paid shares,
    warrants, convertibles, debentures), `unparsed` or `no_issue_price`."""
    lowered = text.lower()
    if _NON_SIMPLE.search(lowered):
        return "non_simple"
    ratios = _RIGHTS_RATIO.findall(lowered)
    fv = (
        face_value if isinstance(face_value, Fraction) else exact(face_value.strip(), positive=True)
    )
    if len(ratios) != 1 or fv is None or fv <= 0:
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


def _rights(row: Mapping[str, str], changes: Sequence[FaceValueChange] = ()) -> Proposal:
    """A rights row, priced at the face value on its ex-date: the file's `faceVal` is today's,
    so a later split (`changes`, the company's face-value changes) would understate the issue
    price."""
    text = row["subject_raw"]
    p = Proposal(T_RIGHTS, row["rowid"], row["symbol"], row["isin"], nse_date(row["exDate"]), text)
    p = replace(p, hint=f"{row['market']} FV {row['faceVal']}", sme="sme" in row["market"])
    if "invit" in row["market"].lower():
        return p.ends(SKIPPED, "invit_units")
    if p.ex_date is None:
        return p.ends(REVIEW, "bad_ex_date")
    today = exact(row["faceVal"], positive=True)
    face_value = None if today is None else face_value_on(Fraction(today), p.ex_date, changes)
    terms = rights_terms(text, face_value if face_value is not None else row["faceVal"])
    if isinstance(terms, str):
        return p.ends(REVIEW, terms)
    parsed = parse_ratio(row["entitlement_ratio_parsed"])
    premium = exact(row["premium_rs_parsed"]) if row["premium_rs_parsed"] else None
    if parsed != (terms.new, terms.held) or (
        premium is not None
        and (face_value is None or Fraction(premium) != terms.price - face_value)
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
    if old is None or new is None or new >= old:
        return p.ends(REVIEW, "unparsed")
    return replace(p, legs=(Leg(TYPE_SPLIT, Fraction(new) / Fraction(old)),))


def proposals(
    tables: Mapping[str, Sequence[Mapping[str, str]]], asof: date | None = None
) -> list[Proposal]:
    """What each row of the file proposes, as far as its text goes; a rights row is priced at
    the face value of its ex-date, from the file's own face-value splits of the same company
    (same ISIN or symbol) that took effect by `asof` (the day the file was pulled from NSE: a
    split after it had not changed the file's `faceVal`)."""
    changes: dict[str, list[FaceValueChange]] = {}
    for row in tables.get(T_SPLITS, ()):
        day, ratio = nse_date(row["exDate"]), face_value_changes(row["subject_raw"])
        if day is not None and ratio is not None and (asof is None or day <= asof):
            for key in {row["isin"], row["symbol"]} - {""}:
                changes.setdefault(key, []).append((day, ratio))

    def rights(row: Mapping[str, str]) -> Proposal:
        keys = {row["isin"], row["symbol"]} - {""}
        return _rights(row, sorted({c for k in keys for c in changes.get(k, ())}))

    readers: dict[str, Callable[[Mapping[str, str]], Proposal]] = {
        T_SPLITS: _split_bonus,
        T_RIGHTS: rights,
        T_DEMERGERS: _demerger,
        T_ETF: _etf,
    }
    return [readers[table](row) for table in COLUMNS for row in tables.get(table, ())]


# --- deciding -----------------------------------------------------------------------------------

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
            or not voting_equity(ctx.reference.instruments[iid].isin)
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


def evaluate(
    session: Session,
    store: DataStore,
    tables: Mapping[str, Sequence[Mapping[str, str]]],
    pulled: date | None = None,
) -> list[Outcome]:
    """Every row's outcome, the file pulled from NSE on `pulled`; nothing is written."""
    props = proposals(tables, pulled)
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
    drop_vacated(session, outcomes)
    return outcomes


# --- command line -------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.ca_import --db FILE [--apply] [--all]

    Reads the corporate-action file FILE (a `.db` under the working directory, opened
    read-only) and the raw bars under `HAT_DATA_ROOT`, and prints what it would store (a dry
    run) or, with `--apply`, stores it in one transaction. The report goes to stdout (redirect
    it to a file outside the repo); `--all` lists every row, not only the stored, conflict and
    review ones. After an apply, rebuild the lineage (`python -m app.hat.data.lineage build`)
    and run `features`, which recomputes the chains whose factors changed."""
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.ca_import")
    parser.add_argument("--db", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    try:
        path = input_path(args.db, ".db")
        tables = read_file(path)
    except CaFileError as exc:
        sys.stderr.write(f"refused: {exc.code}\n")
        return 2
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatDataSessionLocal

    from ..time import utc_now

    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    with HatDataSessionLocal() as session:
        pulled = datetime.fromtimestamp(path.stat().st_mtime, IST).date()  # the file's pull
        outcomes = evaluate(session, store, tables, pulled)
        if args.apply:
            journal = {"source": SOURCE_FILE, "file_sha256": digest(path)}
            created = apply(session, outcomes, journal=journal, now=utc_now())
            session.commit()
            sys.stdout.write(f"applied: {created} rows\n")
        else:
            session.rollback()
    lines = report_lines(outcomes, applied=args.apply, everything=args.all, tables=tuple(COLUMNS))
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
