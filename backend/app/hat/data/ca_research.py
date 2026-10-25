"""The corporate-action research import (CA2, D241/D245): demerger factors and rights terms
that web research resolved for rows the CA1 file import (`ca_import`) left in review.

Inputs: CA1's NSE file (the base: a research row only overrides an event NSE recorded) and the
research CSVs `a`..`d` (`ca_research_rows`), all outside the repo and untrusted.

A research row matches its file row by symbol and ex-date (`match_rows`); an ISIN on both sides
must agree unless the file's is a placeholder.

Demergers (`decide_demerger`, owner rule): the stored factor is the MARKET factor, the ex-date
open over the previous close (NSE's special pre-open discovers it), never a cost-of-acquisition
% (a book-value tax allocation: TMPV 2025-10-14 CoA 68.85% against a market 0.605) nor a ratio
priced at a resulting company's close. Those two are cross-checks (`cross_checks`): the parent's
CoA % / 100 (`coa_factor`) and 1 - sum(r_i * C_i) / P (`ratio_factor`, each resulting company
priced through a known instrument: its first close, or its close at the ex-date when it already
traded). The gap is stored (`market_factor`) when a cross-check agrees with it by CA1's gate (within
20%, and twice as close to it as to 1), or, with no cross-check or none agreeing (a CoA may well
differ from the market: VEDL's five-way 2026 demerger), when a source confirms the date, the drop
is beyond `DEMERGER_MIN_DROP` (opens 10% or more below the previous close are 0.28% of the 6.8
million sessions in the raw bars, actions included, so on a date a source names such a drop is
the event) and the price holds it five sessions on. Anything else goes to review. The stored text
carries the method (`market_gap`) and the cross-check values, each marked `disagrees` when it
does. Amalgamation legs are not
cross-checks for the parent. An event that distributes no shares (not a demerger, preference shares
only, no shares to holders) is reported, never stored; a low-confidence event, or one whose date
the research disputes, goes to review. The resulting company is never linked into the parent's
lineage.

Rights (`rights_override`): the TERP factor of CA1 (`rights_factor`) with the terms the research
confirms, or corrects when it says ours are wrong and gives the right ones. A partly-paid issue
uses the full issue price (the TERP convention). The gate's `ambiguous` still stores, the text
tagged `price_unconfirmed`; `off_factor`, low confidence, warrants or convertibles alongside, and
terms the research flags without correcting go to review.

Rows are stored with source `ca_research` and journalled `corporate_action.imported` with the
digests of every input file and each row's research file, line, confidence and source URL. A
stored key is never rewritten (a conflict is reported; `correct-action` replaces it).

Point in time (CA5, `ca_known`): a research row may carry a `source_date` column, the date its
source was published. A demerger is known on the first route that accepts its market gap: an
agreeing ratio check at the latest of the resulting company's priced close and the legs' source
dates, an agreeing CoA check at the legs' source dates, or the close `SETTLE_SESSIONS` on. A rights
factor resting on terms only research gives (`research_supplied`) is known at its source date (not
known without one); one whose terms NSE's text already gave, at the ex-date open.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from fractions import Fraction
from typing import Any

from sqlalchemy.orm import Session

from ..money import exact
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
    STORED,
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
from .ca_import import (
    CHILD_DAYS,
    DEMERGER_MIN_DROP,
    T_DEMERGERS,
    T_RIGHTS,
    decide,
    parse_ratio,
    read_file,
    rights_terms,
)
from .ca_known import (
    RESEARCH_SOURCE_DATE,
    RESULTING_FIRST_CLOSE,
    SETTLE_SESSIONS,
    Known,
    earliest,
    from_sources,
    settle_known,
)
from .ca_research_rows import (
    ALONGSIDE,
    CONFIDENCE,
    FILE_A,
    FILE_B,
    FILE_C,
    FILE_D,
    REQUIRED,
    Key,
    Research,
    ResultingLeg,
    RightsResearch,
    date_check,
    read_csv,
    read_research,
    same_company,
)
from .corporate_actions import TYPE_OTHER
from .isin import voting_equity
from .store import DataStore

SOURCE_RESEARCH = "ca_research"
PRICE_UNCONFIRMED = "price_unconfirmed"
MARKET_GAP = "market_gap"
COA_SUM_TOL = Fraction(1, 2)  # the parent's and the resulting companies' % add up to 100 +- this

TABLES = (
    f"{FILE_A}:demergers",
    f"{FILE_B}:demergers",
    f"{FILE_D}:demergers",
    f"{FILE_B}:rights",
    f"{FILE_C}:rights",
    "unmatched",
)


def coa_factor(legs: Sequence[ResultingLeg]) -> Fraction | str | None:
    """The parent's cost-of-acquisition share / 100 over the share-distributing legs: None when
    no leg gives it; a reason when the legs disagree on it, it is outside (0, 100), or, every leg
    giving its own %, the parent's and theirs do not add up to 100."""
    parent = {leg.coa_parent for leg in legs if leg.coa_parent is not None}
    if not parent:
        return None
    if len(parent) > 1:
        return "coa_disagrees"
    pct = parent.pop()
    if not 0 < pct < 100:
        return "coa_out_of_range"
    shares = [leg.coa_resulting for leg in legs]
    if all(s is not None for s in shares) and abs(pct + sum(shares) - 100) > COA_SUM_TOL:  # type: ignore[arg-type]
        return "coa_does_not_sum"
    return pct / 100


def ratio_factor(
    priced: Sequence[tuple[tuple[Fraction, Fraction], Fraction]], parent_close: Fraction
) -> Fraction | None:
    """1 - sum(r_i * C_i) / P over the resulting companies (r_i = a/b shares per parent share,
    C_i its price): the share of the parent's value that stays with it. None outside (0, 1)."""
    if not priced or parent_close <= 0 or any(close <= 0 for _, close in priced):
        return None
    factor = 1 - sum(a / b * close for (a, b), close in priced) / parent_close
    return factor if 0 < factor < 1 else None


def agrees(gap: Fraction, check: Fraction) -> bool:
    """Does a cross-check explain the market gap? CA1's gate with the check as the expected
    factor: within `GATE_TOL` (20%) of it, and at least twice as close to it as to 1 in log
    terms, so a plain session move does not count as agreement."""
    return gate(check, Fraction(1), gap).confirmed


def market_factor(
    gap: Fraction, checks: Mapping[str, Fraction], when: str, settled: Fraction | None
) -> tuple[Fraction | None, str]:
    """A demerger's stored factor is the market gap (ex-date open / previous close, which NSE's
    special pre-open discovers), never a cross-check. (gap, which checks agree) when a cross-check
    explains it. Otherwise (no cross-check, or every one disagrees: a CoA is a book-value tax
    split that may well differ) the gap is still the factor when a source confirms the date, the
    drop is beyond `DEMERGER_MIN_DROP` and the price holds it: `settled` (the close
    `SETTLE_SESSIONS` sessions on, over the previous close) agrees with the gap as a cross-check
    would, so a pre-open discovery the market reverses (TATACOMM 2019-09-17: open 0.62, upper
    circuits to 0.83 in five sessions) is not stored; else (None, why it goes to review)."""
    if not 0 < gap.numerator < gap.denominator:  # 0 < gap < 1, exact
        return None, "no_drop"
    agreeing = [k for k, v in checks.items() if agrees(gap, v)]
    if agreeing:
        return gap, "agrees with " + "+".join(agreeing)
    unchecked = "gap_disagrees" if checks else "no_cross_check"
    if when != "confirmed":
        return None, f"{unchecked}_date_unconfirmed"
    if gap > 1 - DEMERGER_MIN_DROP:
        return None, f"{unchecked}_gap_not_material"
    if settled is None or not agrees(settled, gap):
        return None, f"{unchecked}_gap_not_held"
    if checks:
        return gap, "disagrees with " + "+".join(checks) + ", material drop on a confirmed date"
    return gap, "no cross-check, material drop on a confirmed date"


# --- matching -----------------------------------------------------------------------------------


def match_rows(
    file_rows: Sequence[Mapping[str, str]], keys: Iterable[Key]
) -> tuple[dict[Key, Mapping[str, str]], list[Key]]:
    """Each research key's file row (same symbol and ex-date; NSE keeps one row per event in
    these tables), and the keys with none or with several."""
    index: dict[Key, list[Mapping[str, str]]] = defaultdict(list)
    for row in file_rows:
        ex = nse_date(row["exDate"])
        if ex is not None:
            index[(row["symbol"], ex)].append(row)
    matched, unmatched = {}, []
    for key in keys:
        found = index.get(key, [])
        if len(found) == 1:
            matched[key] = found[0]
        else:
            unmatched.append(key)
    return matched, unmatched


def isin_for(file_isin: str, research_isin: str) -> str | None:
    """The ISIN to match instruments by: the file's, or the research's when the file holds a
    placeholder; None when both are real and differ."""
    if not voting_equity(file_isin):
        return research_isin
    if research_isin and research_isin != file_isin:
        return None
    return file_isin


# --- deciding -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Priced:
    """A resulting company priced through a known instrument."""

    instrument: Instrument
    day: date
    close: Fraction
    listed: bool  # a fresh listing (first close) rather than a company already trading


def resolve_resulting(
    ctx: Context, leg: ResultingLeg, ex_date: date, parent_ids: set[str]
) -> Instrument | None:
    """The instrument of a resulting company: by its ISIN; else by its NSE symbol (now or in
    the symbol history) when that instrument's name is the company's or it first listed in the
    year after the ex-date; else the one fresh equity listing of that year with the company's
    name. Never the parent's own chain."""
    ref = ctx.reference

    def fresh(iid: str) -> bool:
        first = ctx.first_closes.get(iid)
        return first is not None and ex_date <= first[0] <= ex_date + timedelta(days=CHILD_DAYS)

    if leg.isin and leg.isin in ref.by_isin:
        iid = ref.by_isin[leg.isin]
        return None if iid in parent_ids else ref.instruments.get(iid)
    if leg.symbol:
        ids = set(ref.by_symbol.get(leg.symbol, ())) | {
            iid for iid, _, _ in ref.history.get(leg.symbol, ())
        }
        found = [
            ref.instruments[i]
            for i in sorted(ids - parent_ids)
            if i in ref.instruments
            and voting_equity(ref.instruments[i].isin)
            and (same_company(ref.instruments[i].name, leg.name) or fresh(i))
        ]
        if len(found) == 1:
            return found[0]
    named = [
        i
        for iid, i in ref.instruments.items()
        if iid not in parent_ids
        and iid not in ref.successors
        and voting_equity(i.isin)
        and fresh(iid)
        and same_company(i.name, leg.name)
    ]
    return named[0] if len(named) == 1 else None


def price_resulting(
    ctx: Context, instrument: Instrument, ex_date: date, bars: Sequence[Bar]
) -> Priced | None:
    """The resulting company's first close when it listed in the year after the ex-date, or its
    first close on or after the ex-date when it already traded (`bars`: its window)."""
    first = ctx.first_closes.get(instrument.id)
    if first is None:
        return None
    if ex_date <= first[0] <= ex_date + timedelta(days=CHILD_DAYS):
        return Priced(instrument, first[0], Fraction(first[1]), True)
    if first[0] < ex_date:
        on = [b for b in bars if b.instrument_id == instrument.id and b.day >= ex_date]
        if on:
            bar = min(on, key=lambda b: b.day)
            return Priced(instrument, bar.day, Fraction(bar.close), False)
    return None


def _event_text(
    legs: Sequence[ResultingLeg], checks: Mapping[str, Fraction], gap: Fraction, note: str
) -> str:
    """The stored text: the event, the method and each cross-check, `disagrees` when the gap
    does not agree with it (`note`: a CoA the research gives that contradicts itself)."""
    parts = [
        f"{leg.ratio[0]}:{leg.ratio[1]} {leg.name[:60]}" if leg.ratio else leg.name[:60]
        for leg in legs
    ]
    marks = [f"{k} {fmt4(v)}{'' if agrees(gap, v) else ' disagrees'}" for k, v in checks.items()]
    tag = "; ".join([MARKET_GAP, *marks, *([note] if note else [])])
    return f"Demerger {'; '.join(parts)} [research {tag}]"[:TEXT_MAX]


def _sources(legs: Sequence[ResultingLeg]) -> list[dict[str, Any]]:
    return [
        {
            "file": leg.source.file,
            "line": leg.source.line,
            "confidence": next(k for k, v in CONFIDENCE.items() if v == leg.source.confidence),
            "source_url": leg.source.url[:500],
            "quote": leg.source.quote[:300],
        }
        for leg in legs
    ]


def cross_checks(
    distributing: Sequence[ResultingLeg],
    priced: Sequence[Priced | None],
    prev_close: Fraction,
    *,
    ratio_basis_known: bool,
) -> tuple[dict[str, Fraction], str]:
    """The cross-checks a demerger's market gap is compared with: `coa` (the parent's
    cost-of-acquisition %, a book-value tax allocation) and `ratio` (1 - sum(r_i*C_i)/P with
    each resulting company's price), each where the research and the bars give it; and a note when
    the CoA figures contradict themselves (then there is no `coa` check)."""
    checks: dict[str, Fraction] = {}
    coa = coa_factor(distributing)
    note = coa if isinstance(coa, str) else ""
    if isinstance(coa, Fraction):
        checks["coa"] = coa
    pairs = [
        (leg.ratio, c.close) for leg, c in zip(distributing, priced, strict=True) if leg.ratio and c
    ]
    if ratio_basis_known and len(pairs) == len(distributing):
        found = ratio_factor(pairs, prev_close)
        if found is not None:
            checks["ratio"] = found
    return checks, note


def decide_demerger(
    p: Proposal,
    legs: Sequence[ResultingLeg],
    ctx: Context,
    bars: Sequence[Bar],
    resulting: Mapping[int, Priced | None],
) -> Outcome:
    """The parent's factor for one researched demerger event: the market gap
    (`market_factor`), with the CoA and priced-ratio cross-checks beside it (`resulting`: each
    leg's price by its index, None when no known instrument prices it)."""
    if p.status or p.ex_date is None:
        return Outcome(p, p.status or REVIEW, p.reason or "bad_ex_date")
    distributing = [leg for leg in legs if leg.distributing]
    if not distributing:
        excluded = next((leg.excluded for leg in legs if leg.excluded), "amalgamation_only")
        status = REVIEW if excluded == "event_unknown" else SKIPPED
        return Outcome(p, status, excluded)
    if min(leg.source.confidence for leg in distributing) < CONFIDENCE["medium"]:
        return Outcome(p, REVIEW, "low_confidence")
    when = date_check(p.ex_date, (t for leg in legs for t in leg.dates))
    if when == "disputed":
        return Outcome(p, REVIEW, "date_disputed")
    ids = ctx.reference.ids(p.symbol, p.isin, p.ex_date)
    if not ids:
        return Outcome(p, SKIPPED if p.sme else REVIEW, "no_instrument")
    pair = around(bars, p.ex_date, p.isin)
    if isinstance(pair, str):
        return Outcome(p, REVIEW, pair)
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
        "research": _sources(legs),
    }
    near = near_actions(ctx, ids | {before.instrument_id, on.instrument_id}, p.ex_date)
    same_day = math.prod(
        (
            Fraction(s.factor)
            for s in near
            if before.day < s.ex_date <= on.day and s.kind != KIND_DEMERGER
        ),
        start=Fraction(1),
    )
    priced = [resulting.get(i) for i, leg in enumerate(legs) if leg.distributing]
    detail += "".join(
        f", resulting {c.instrument.symbol} {'first ' if c.listed else ''}close {c.close:.2f}"
        f" on {c.day}"
        for c in priced
        if c
    )
    # with another action between the sessions, the ratio's basis (before or after it) is unknown
    checks, note = cross_checks(distributing, priced, prev_close, ratio_basis_known=same_day == 1)
    if note:
        detail += f", {note}"
    gap = ex_open / prev_close / same_day
    detail += f", market gap {fmt4(gap)}" + "".join(f", {k} {fmt4(v)}" for k, v in checks.items())
    evidence["cross_checks"] = {k: fmt4(v) for k, v in checks.items()}
    after = sorted(
        (b for b in bars if b.instrument_id == on.instrument_id and b.day >= on.day),
        key=lambda b: b.day,
    )
    held = None
    if len(after) >= SETTLE_SESSIONS and after[SETTLE_SESSIONS - 1].close > 0:
        last = after[SETTLE_SESSIONS - 1]
        # the raw close is unadjusted: an action stored after the ex session moves it too
        later = math.prod(
            (
                Fraction(s.factor)
                for s in ctx.stored.get(on.instrument_id, ())
                if on.day < s.ex_date <= last.day
            ),
            start=Fraction(1),
        )
        held = Fraction(last.close) / prev_close / same_day / later
    if held is not None:
        detail += f", close {SETTLE_SESSIONS} sessions on {fmt4(held)}"
    factor, reason = market_factor(gap, checks, when, held)
    if factor is None:
        return Outcome(p, REVIEW, reason, detail)
    # the fifth-session close dates the gap only when it held it; else a cross-check's date does
    settle_days = [b.day for b in after] if held is not None and agrees(held, gap) else []
    known = demerger_known(p.ex_date, gap, checks, distributing, priced, settle_days)
    evidence["derived"] = f"{MARKET_GAP} {reason}"
    evidence["gap"] = fmt4(gap)
    p = replace(p, text=_event_text(legs, checks, gap, note))
    # the market factor passes the gate by construction; the cross-checks were the test
    verdict = gate(factor * same_day, prev_close, ex_open)
    near_kind = [s for s in near if s.kind == KIND_DEMERGER]
    outcome = settle(
        p,
        before,
        [Leg(TYPE_OTHER, factor, True)],
        near_kind,
        verdict,
        detail,
        evidence,
        source=SOURCE_RESEARCH,
        known=known,
    )
    if outcome.status == STORED:
        outcome.reason = f"{MARKET_GAP} ({reason})"
    return outcome


def demerger_known(
    ex_date: date,
    gap: Fraction,
    checks: Mapping[str, Fraction],
    distributing: Sequence[ResultingLeg],
    priced: Sequence[Priced | None],
    days: Sequence[date],
) -> Known:
    """When a demerger's market gap was knowable (CA5): the first route that accepts it, an
    agreeing `ratio` check (at the latest of its priced closes and the legs' source dates), an
    agreeing `coa` check (at the legs' source dates), or the gap held to the close
    `SETTLE_SESSIONS` on (`days`: the ex instrument's sessions, empty when the close did not
    hold the gap)."""
    sources = [leg.source.day for leg in distributing]
    routes = [settle_known(days, ex_date)]
    if "ratio" in checks and agrees(gap, checks["ratio"]):
        closes = [c.day for c in priced if c is not None]
        routes.append(from_sources(ex_date, [*sources, *closes], RESULTING_FIRST_CLOSE))
    if "coa" in checks and agrees(gap, checks["coa"]):
        routes.append(from_sources(ex_date, sources, RESEARCH_SOURCE_DATE))
    return earliest(routes)


def rights_override(
    p: Proposal, row: Mapping[str, str], research: RightsResearch
) -> tuple[Proposal, str]:
    """The proposal with the research's terms, or why it stays in review; the second value is
    the note the stored text carries (corrected terms, partly paid)."""
    if research.source.confidence < CONFIDENCE["medium"]:
        return p.ends(REVIEW, "low_confidence"), ""
    if research.alongside or ALONGSIDE.search(p.text):
        return p.ends(REVIEW, "warrants_or_convertibles_alongside"), ""
    if research.terms is None:
        return p.ends(REVIEW, "research_no_terms"), ""
    plain = re.sub(r"partly[\s-]*paid(?:[\s-]*up)?", " ", p.text, flags=re.IGNORECASE)
    ours = rights_terms(plain, row["faceVal"])
    ours_ratio = parse_ratio(row["entitlement_ratio_parsed"])
    t = research.terms
    notes = []
    if isinstance(ours, RightsTerms):
        # our file's face value is today's: after a split it is not the one at the issue
        premium = ours.price - Fraction(exact(row["faceVal"], positive=True) or 0)
        same = (ours.new, ours.held) == (t.new, t.held) and (
            ours.price == t.price or premium == research.premium
        )
        if same and ours.price != t.price:
            notes.append("face value at the issue per research")
    else:
        same = ours_ratio == (t.new, t.held)
    corrects_face_value = isinstance(ours, RightsTerms) and ours.price != t.price
    if same and research.terms_wrong and not corrects_face_value:
        return p.ends(REVIEW, "research_flags_row"), ""  # wrong, but not in its terms
    if not same:
        if not research.terms_wrong:
            return p.ends(REVIEW, "research_terms_differ"), ""
        notes.append("terms corrected by research")
    partly = research.partly_paid or "partly" in p.text.lower()
    if partly:
        notes.append("partly paid: full issue price")
    note = f"{t.new}:{t.held} @ {fmt4(t.price)}" + "".join(f"; {n}" for n in notes)
    return replace(p, rights=t, status="", reason=""), note


def _rights_proposal(
    label: str, row: Mapping[str, str], research: RightsResearch
) -> tuple[Proposal, str]:
    p = Proposal(
        label,
        str(research.source.line),
        row["symbol"],
        row["isin"],
        nse_date(row["exDate"]),
        row["subject_raw"],
        hint=f"{row['market']} FV {row['faceVal']}",
        sme="sme" in row["market"],
    )
    isin = isin_for(row["isin"], research.isin)
    if isin is None:
        return p.ends(REVIEW, "isin_disagrees"), ""
    p = replace(p, isin=isin)
    if p.ex_date is None:
        return p.ends(REVIEW, "bad_ex_date"), ""
    return rights_override(p, row, research)


def decide_rights(
    p: Proposal, note: str, research: RightsResearch, ctx: Context, bars: Sequence[Bar]
) -> Outcome:
    """CA1's decision with the research's terms; the gate's `ambiguous` still stores, tagged
    `price_unconfirmed`."""
    full = f"{p.text} [research {research.source.file}: {note}]"
    p = replace(p, text=full[:TEXT_MAX])
    known = None
    if p.ex_date is not None and research_supplied(full):  # before a cut drops the tag's markers
        known = from_sources(p.ex_date, [research.source.day], RESEARCH_SOURCE_DATE)
    outcome = decide(
        p, ctx, bars, source=SOURCE_RESEARCH, accept=frozenset({"ambiguous"}), known=known
    )
    outcome.evidence["research"] = _sources_rights(research)
    if outcome.status == STORED and outcome.reason == "ambiguous":
        tag = f"{p.text[:-1]}; {PRICE_UNCONFIRMED}]"
        outcome.records = [replace(r, raw_text=tag) for r in outcome.records]
        outcome.reason = PRICE_UNCONFIRMED
    return outcome


_SUPPLIED = (
    "terms corrected by research",
    "face value at the issue per research",
)
_PARTLY_PAID = re.compile(r"partly[\s-]*paid(?:[\s-]*up)?", re.IGNORECASE)


def research_supplied(text: str) -> bool:
    """Does a stored rights text (`decide_rights`: NSE's purpose, then `[research ...]`) rest on
    terms only research gives: terms or a face value research corrected, or a purpose whose own
    terms do not price the issue (no issue price, non-simple terms)? Then the factor is known at
    the research's source date, not at the ex-date open (CA5)."""
    head, _, tag = text.partition(" [research ")
    if any(marker in tag for marker in _SUPPLIED) or (tag and not tag.endswith("]")):
        return True  # a stored text cut at `TEXT_MAX` may have lost its markers: never ex_open
    return not isinstance(rights_terms(_PARTLY_PAID.sub(" ", head), "1"), RightsTerms)


def _sources_rights(research: RightsResearch) -> list[dict[str, Any]]:
    s = research.source
    level = next(k for k, v in CONFIDENCE.items() if v == s.confidence)
    return [
        {
            "file": s.file,
            "line": s.line,
            "confidence": level,
            "source_url": s.url[:500],
            "quote": s.quote[:300],
        }
    ]


def _demerger_proposal(label: str, key: Key, row: Mapping[str, str], parent_isin: str) -> Proposal:
    return Proposal(
        label,
        row["rowid"],
        key[0],
        parent_isin,
        key[1],
        f"Demerger {row['ratio']}",
        hint=row["ratio_note"][:300],
        sme=row["market"] == "sme",
    )


def _label(legs: Sequence[ResultingLeg]) -> str:
    files = {leg.source.file for leg in legs}
    return f"{FILE_D if FILE_D in files else min(files)}:demergers"


def evaluate(
    session: Session,
    store: DataStore,
    tables: Mapping[str, Sequence[Mapping[str, str]]],
    research: Research,
) -> list[Outcome]:
    """Every research row's outcome against CA1's file rows (`tables`); nothing is written."""
    reference = load_reference(session)
    ctx = Context(reference, stored_price_actions(session), read_first_closes(store))
    outcomes: list[Outcome] = [
        Outcome(
            Proposal("unmatched", str(line), symbol, "", None, f"{file} line {line}"),
            REVIEW,
            reason,
        )
        for file, line, symbol, reason in research.unparsed
    ]
    dem_rows, dem_missing = match_rows(tables.get(T_DEMERGERS, ()), research.events)
    rig_rows, rig_missing = match_rows(tables.get(T_RIGHTS, ()), research.rights)
    for key in [*dem_missing, *rig_missing]:
        text = f"no single NSE file row for {key[0]} {key[1]}"
        p = Proposal("unmatched", "", key[0], "", key[1], text)
        outcomes.append(Outcome(p, REVIEW, "no_file_row"))

    demergers = []
    for key, row in dem_rows.items():
        legs = research.events[key]
        p = _demerger_proposal(_label(legs), key, row, research.parent_isin.get(key, ""))
        demergers.append((p, legs))
    rights = []
    for key, row in rig_rows.items():
        found = research.rights[key]
        p, note = _rights_proposal(f"{found.source.file}:rights", row, found)
        rights.append((p, note, found))

    wanted: dict[int, tuple[date, set[str]]] = {}
    for k, (p, _) in enumerate(demergers):
        if not p.status and p.ex_date is not None:
            wanted[k] = (p.ex_date, reference.ids(p.symbol, p.isin, p.ex_date))
    offset = len(demergers)
    for k, (p, _, _) in enumerate(rights):
        if not p.status and p.ex_date is not None:
            wanted[offset + k] = (p.ex_date, reference.ids(p.symbol, p.isin, p.ex_date))
    # the resulting companies that already traded are priced at the ex-date
    resolved: dict[tuple[int, int], Instrument] = {}
    child_wanted: dict[int, tuple[date, set[str]]] = {}
    for k, (p, legs) in enumerate(demergers):
        if k not in wanted or p.ex_date is None:
            continue
        for i, leg in enumerate(legs):
            if leg.distributing:
                instrument = resolve_resulting(ctx, leg, p.ex_date, wanted[k][1])
                if instrument is not None:
                    resolved[(k, i)] = instrument
                    child_wanted[len(child_wanted) + 10 * (offset + len(rights))] = (
                        p.ex_date,
                        {instrument.id},
                    )
    windows = read_windows(store, {**wanted, **child_wanted})
    child_bars = [b for k in child_wanted for b in windows.get(k, ())]

    for k, (p, legs) in enumerate(demergers):
        prices: dict[int, Priced | None] = {}
        for i in range(len(legs)):
            instrument = resolved.get((k, i))
            prices[i] = (
                None
                if instrument is None or p.ex_date is None
                else price_resulting(ctx, instrument, p.ex_date, child_bars)
            )
        outcomes.append(decide_demerger(p, legs, ctx, windows.get(k, ()), prices))
    for k, (p, note, found) in enumerate(rights):
        if p.status:
            o = Outcome(p, p.status, p.reason)
            o.evidence["research"] = _sources_rights(found)
            outcomes.append(o)
        else:
            outcomes.append(decide_rights(p, note, found, ctx, windows.get(offset + k, ())))
    dedupe(outcomes)
    drop_vacated(session, outcomes)
    return outcomes


# --- command line -------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.ca_research --db FILE [--a A.csv] [--b B.csv] [--c C.csv]
    [--d D.csv] [--apply] [--all]

    Reads CA1's corporate-action file FILE and the research CSVs (all under the working
    directory, opened read-only) and the raw bars under `HAT_DATA_ROOT`, and prints what it
    would store (a dry run) or, with `--apply`, stores it in one transaction. The report goes to
    stdout; `--all` lists every row. After an apply, rebuild the lineage
    (`python -m app.hat.data.lineage build`) and run `features`."""
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.ca_research")
    parser.add_argument("--db", required=True)
    for file in REQUIRED:
        parser.add_argument(f"--{file}", help=f"research file {file} (.csv)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    try:
        path = input_path(args.db, ".db")
        tables = read_file(path)
        files = {f: input_path(getattr(args, f), ".csv") for f in REQUIRED if getattr(args, f)}
        research = read_research({f: read_csv(p, f) for f, p in files.items()})
    except CaFileError as exc:
        sys.stderr.write(f"refused: {exc.code}\n")
        return 2
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatSessionLocal

    from ..time import utc_now

    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    with HatSessionLocal() as session:
        outcomes = evaluate(session, store, tables, research)
        if args.apply:
            journal = {
                "source": SOURCE_RESEARCH,
                "file_sha256": digest(path),
                "research_sha256": {f: digest(p) for f, p in files.items()},
            }
            created = apply(session, outcomes, journal=journal, now=utc_now())
            session.commit()
            sys.stdout.write(f"applied: {created} rows\n")
        else:
            session.rollback()
    lines = report_lines(outcomes, applied=args.apply, everything=args.all, tables=TABLES)
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
